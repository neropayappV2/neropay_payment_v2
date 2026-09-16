from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.client import Client, CURRENCIES, NeroPayError


class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(selection_add=[('neropay', 'NeroPay')], ondelete={'neropay': 'set default'})
    neropay_secret_key = fields.Char(string='NeroPay v2 Secret Key', groups='base.group_system', copy=False,
                                    required_if_provider='neropay')
    neropay_account_id = fields.Char(string='NeroPay Account ID', copy=False, required_if_provider='neropay',
                                    help='Exact public account ID for the receiving merchant, not a numeric merchant ID.')
    neropay_live_confirmed = fields.Boolean(string='Enable real NeroPay payment requests', copy=False,
                                           help='This release uses the live API. The v2 dry-run sandbox cannot settle Odoo orders.')

    @api.constrains('code', 'state', 'neropay_live_confirmed')
    def _check_neropay_live_mode(self):
        for provider in self.filtered(lambda p: p.code == 'neropay'):
            if provider.state == 'test':
                raise ValidationError(_('NeroPay v2 sandbox is a dry run, not a payment simulator. Keep this provider Disabled for offline tests. Use Enabled only for an authorised live acceptance test.'))
            if provider.state == 'enabled' and not provider.neropay_live_confirmed:
                raise ValidationError(_('Confirm real NeroPay payment requests before enabling this provider.'))

    def write(self, vals):
        if {'neropay_account_id', 'company_id'} & vals.keys():
            for provider in self.filtered(lambda p: p.code == 'neropay'):
                account_changed = 'neropay_account_id' in vals and vals['neropay_account_id'] != provider.neropay_account_id
                company_changed = 'company_id' in vals and vals['company_id'] != provider.company_id.id
                if (account_changed or company_changed) and self.env['payment.transaction'].sudo().search_count([
                    ('provider_id', '=', provider.id), ('neropay_identifier', '!=', False)
                ], limit=1):
                    raise ValidationError(_('This provider has NeroPay transactions. Create a separate provider for another merchant or company.'))
        return super().write(vals)

    def _get_default_payment_method_codes(self):
        return {'card'} if self.code == 'neropay' else super()._get_default_payment_method_codes()

    def _get_supported_currencies(self):
        currencies = super()._get_supported_currencies()
        return currencies.filtered(lambda c: c.name in CURRENCIES) if self.code == 'neropay' else currencies

    def _neropay_client(self):
        self.ensure_one()
        return Client(self.sudo().neropay_secret_key, self.neropay_account_id)

    def action_neropay_check_connection(self):
        self.ensure_one()
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_('Only a settings administrator can check these credentials.'))
        self.check_access('write')
        if self.code != 'neropay':
            raise ValidationError(_('Select a NeroPay provider.'))
        try:
            # A scoped read confirms credentials without creating a payment or altering funds.
            self._neropay_client().request('GET', '/payment-links?limit=1')
        except NeroPayError as exc:
            raise ValidationError(str(exc)) from None
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('NeroPay'), 'message': _('API read access works. This is not a completed payment test.'),
            'type': 'success', 'sticky': False}}
