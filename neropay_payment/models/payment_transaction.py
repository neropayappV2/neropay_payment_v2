import logging
import time
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.client import (
    NeroPayError, checkout_url, identifier, minor, validate_link, validate_transaction,
)

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    neropay_identifier = fields.Char(readonly=True, copy=False, index=True)
    neropay_link_id = fields.Char(string='NeroPay Link ID', readonly=True, copy=False)
    neropay_checkout_url = fields.Char(readonly=True, copy=False, groups='base.group_system')
    neropay_callback_token = fields.Char(readonly=True, copy=False, index=True, groups='base.group_system')
    neropay_last_check = fields.Datetime(string='Last NeroPay check', readonly=True, copy=False)
    neropay_needs_review = fields.Boolean(string='NeroPay review required', readonly=True, copy=False)
    neropay_review_note = fields.Char(string='NeroPay review note', readonly=True, copy=False)

    _neropay_callback_unique = models.Constraint('UNIQUE(neropay_callback_token)', 'Callback token must be unique.')
    _neropay_identifier_unique = models.Constraint('UNIQUE(neropay_identifier)', 'NeroPay order reference must be unique.')

    def _neropay_lock(self):
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute('SELECT id FROM payment_transaction WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset()

    def _neropay_guard_related_orders(self):
        # Serialise separate Odoo transactions for the same invoice/order before creating links.
        # Identifiers protect one payment transaction; these locks protect concurrent checkout clicks.
        for field_name, table in (('invoice_ids', 'account_move'), ('sale_order_ids', 'sale_order')):
            if field_name not in self._fields:
                continue
            records = self[field_name]
            if not records:
                continue
            records.flush_recordset()
            self.env.cr.execute('SELECT id FROM ' + table + ' WHERE id IN %s ORDER BY id FOR UPDATE',
                                [tuple(sorted(records.ids))])
            # Odoo uses REPEATABLE READ. A lock alone would leave a waiting request with an old
            # snapshot. Create a row version without changing business values so the concurrent
            # request is retried by Odoo with a fresh snapshot and can see the pending payment.
            self.env.cr.execute('UPDATE ' + table + ' SET write_date = write_date WHERE id IN %s',
                                [tuple(sorted(records.ids))])
            existing = self.env['payment.transaction'].sudo().search([
                ('id', '!=', self.id), ('company_id', '=', self.company_id.id),
                ('provider_code', '=', 'neropay'), ('neropay_identifier', '!=', False),
                ('state', 'in', ['draft', 'pending']), (field_name, 'in', records.ids),
            ], limit=1)
            if existing:
                raise ValidationError(_('A NeroPay payment is already awaiting confirmation for this order or invoice. Check the original payment before starting another.'))

    def _neropay_review(self, message):
        self.write({'neropay_needs_review': True, 'neropay_review_note': message})
        _logger.warning('NeroPay transaction %s requires review: %s', self.id, message)

    def _neropay_validate_link(self, data):
        return validate_link(data, self.provider_id.neropay_account_id, self.neropay_identifier,
                             self.amount, self.currency_id.name, self.neropay_link_id,
                             self.provider_reference)

    def _neropay_attach_link(self, data):
        self._neropay_validate_link(data)
        self.write({'neropay_link_id': str(data['id']), 'provider_reference': data['reference'],
                    'neropay_checkout_url': data['payment_link'],
                    'neropay_needs_review': False, 'neropay_review_note': False})

    def _get_specific_rendering_values(self, processing_values):
        if self.provider_code != 'neropay':
            return super()._get_specific_rendering_values(processing_values)
        self.ensure_one()
        provider = self.provider_id
        if provider.state != 'enabled' or not provider.neropay_live_confirmed:
            raise ValidationError(_('NeroPay live payments are not enabled.'))
        if self.operation != 'online_redirect' or self.currency_id.name not in {'GBP', 'EUR', 'USD'}:
            raise ValidationError(_('This NeroPay release supports hosted GBP, EUR and USD payments only.'))
        if minor(self.amount) < 75:
            raise ValidationError(_('The minimum NeroPay payment is 0.75 in the selected currency.'))
        base_url = provider.get_base_url().rstrip('/')
        parsed = urlsplit(base_url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValidationError(_('Configure an HTTPS Odoo base URL before using NeroPay.'))
        self._neropay_guard_related_orders()
        self._neropay_lock()
        if not self.neropay_identifier:
            database_uuid = self.env['ir.config_parameter'].sudo().get_param('database.uuid')
            if not database_uuid:
                raise ValidationError(_('The Odoo database UUID is missing.'))
            self.write({
                'neropay_identifier': identifier(database_uuid, self.company_id.id, provider.id, self.reference),
                'neropay_callback_token': uuid.uuid4().hex + uuid.uuid4().hex,
            })
        if self.state not in ('draft', 'pending'):
            return {'api_url': '/payment/neropay/redirect', 'token': self.neropay_callback_token}
        # No automatic second write after an uncertain remote response.
        if not self.neropay_link_id and not self.neropay_needs_review:
            try:
                client = provider._neropay_client()
                existing = client.find_link(self.neropay_identifier)
                if existing:
                    self._neropay_attach_link(existing)
                else:
                    token = self.neropay_callback_token
                    data = client.create_link({
                        'identifier': self.neropay_identifier,
                        'amount': minor(self.amount) / 100,
                        'currency': self.currency_id.name,
                        'details': ('Odoo ' + self.reference)[:250],
                        'customer_name': (self.partner_name or 'Customer')[:150],
                        'customer_email': (self.partner_email or '')[:150],
                        'success_url': base_url + '/payment/neropay/return/' + token,
                        'cancel_url': base_url + '/payment/neropay/return/' + token,
                        'ipn_url': base_url + '/payment/neropay/ipn/' + token,
                    })
                    self._neropay_attach_link(data)
            except NeroPayError:
                # Keep the Odoo transaction and identifier committed, not rolled back by a UI error.
                self._neropay_review(_('Payment creation could not be confirmed. Use Check NeroPay status; do not start a replacement payment until the original has been checked.'))
        self._set_pending(state_message=_('Awaiting a verified NeroPay payment.'))
        return {'api_url': '/payment/neropay/redirect', 'token': self.neropay_callback_token}

    def _neropay_refresh(self, force=False):
        """Read-only provider requests, regardless of whether invoked by IPN, cron or an operator."""
        self.ensure_one()
        if (self.provider_code != 'neropay' or not self.neropay_identifier or self.state == 'done'
                or self.provider_id.state != 'enabled' or not self.provider_id.neropay_live_confirmed):
            return
        self._neropay_lock()
        now = fields.Datetime.now()
        if not force and self.neropay_last_check and self.neropay_last_check > now - timedelta(seconds=30):
            return
        self.neropay_last_check = now
        try:
            client = self.provider_id._neropay_client()
            if not self.neropay_link_id:
                recovered = client.find_link(self.neropay_identifier)
                if not recovered:
                    self._neropay_review(_('No matching link found. No replacement was created; review the original API request manually.'))
                    return
                self._neropay_attach_link(recovered)
            link = self._neropay_validate_link(client.link(self.neropay_link_id))
            if link.get('status') != 'paid':
                # A declined attempt or the cancel URL is not necessarily the final order outcome.
                if link.get('status') in {'open', 'failed'}:
                    self.write({'neropay_needs_review': False, 'neropay_review_note': False})
                else:
                    self._neropay_review(_('The checkout is not open or paid. Review its current status in NeroPay.'))
                self._set_pending(state_message=_('Awaiting a verified NeroPay payment.'))
                return
            transaction = validate_transaction(client.transaction(self.provider_reference),
                                               self.provider_id.neropay_account_id, self.provider_reference,
                                               self.amount, self.currency_id.name)
            self._process('neropay', transaction)
        except NeroPayError as exc:
            self._neropay_review(str(exc))

    def _extract_amount_data(self, payment_data):
        if self.provider_code != 'neropay':
            return super()._extract_amount_data(payment_data)
        return {'amount': payment_data.get('amount'), 'currency_code': payment_data.get('currency')}

    def _apply_updates(self, payment_data):
        if self.provider_code != 'neropay':
            return super()._apply_updates(payment_data)
        validate_transaction(payment_data, self.provider_id.neropay_account_id, self.provider_reference,
                             self.amount, self.currency_id.name)
        if self.state == 'done':
            return
        if payment_data.get('status') == 'succeeded':
            if self.state == 'cancel':
                self._neropay_review(_('Payment succeeded after this transaction was cancelled in Odoo. Review the order before recording payment.'))
                return
            self.write({'neropay_needs_review': False, 'neropay_review_note': False})
            self._set_done()
            self.env.ref('payment.cron_post_process_payment_tx')._trigger()
        elif payment_data.get('status') in {'pending', 'initiated'}:
            self._set_pending()
        else:
            self._neropay_review(_('The underlying payment is not succeeded. Review any refunds, disputes or failed attempts before recording payment.'))

    def action_neropay_refresh(self):
        if not self.env.user.has_group('account.group_account_invoice'):
            raise AccessError(_('Only an authorised accounting user can refresh this payment.'))
        self.check_access('write')
        for tx in self:
            if tx.provider_code != 'neropay':
                continue
            tx.sudo()._neropay_refresh(force=True)
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    @api.model
    def _cron_neropay_refresh(self):
        # Fair, bounded polling: pending records are oldest-check first. No charging, ever.
        now = fields.Datetime.now()
        candidates = self.search([
            ('provider_code', '=', 'neropay'), ('state', 'in', ['draft', 'pending']),
            ('neropay_identifier', '!=', False), ('create_date', '>=', now - timedelta(days=30)),
            '|', ('neropay_last_check', '=', False), ('neropay_last_check', '<', now - timedelta(minutes=3)),
        ], order='neropay_last_check asc nulls first, id asc', limit=10)
        started = time.monotonic()
        for tx in candidates:
            if time.monotonic() - started > 40:
                break
            with self.env.cr.savepoint():
                tx._neropay_refresh()
