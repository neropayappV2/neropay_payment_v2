"""Run inside a disposable Odoo 19 database; all NeroPay requests are mocked."""
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.addons.payment.tests.common import PaymentCommon

from ..services.client import NeroPayError


@tagged('-at_install', 'post_install')
class TestNeroPayTransactions(PaymentCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env.ref('neropay_payment.payment_provider_neropay').sudo()
        cls.provider.write({'neropay_secret_key': 'offline-fixture-not-a-secret',
                            'neropay_account_id': 'NP_ODOO_TEST',
                            'neropay_live_confirmed': True, 'state': 'enabled'})
        cls.payment_method = cls.env.ref('payment.payment_method_card')
        cls.payment_method_id = cls.payment_method.id
        cls.env['ir.config_parameter'].sudo().set_param('web.base.url', 'https://odoo.example.test')

    def setUp(self):
        super().setUp()
        self.client_patch = patch('odoo.addons.neropay_payment.models.payment_provider.Client')
        self.client = self.startPatcher(self.client_patch).return_value
        self.client.find_link.return_value = None
        self.client.create_link.side_effect = self._created_link

    def _created_link(self, payload):
        return {'id': 42, 'identifier': payload['identifier'], 'account_id': 'NP_ODOO_TEST',
                'reference': 'NP-TEST-42', 'amount': self.amount, 'currency': self.currency.name,
                'status': 'open', 'payment_link': 'https://eu.neropay.app/initiate/payment/checkout?payment_id=test'}

    def _start(self):
        tx = self._create_transaction('redirect')
        tx._get_specific_rendering_values({})
        self.client.link.return_value = self._created_link({'identifier': tx.neropay_identifier})
        self.client.transaction.return_value = {
            'account_id': 'NP_ODOO_TEST', 'transaction_reference': 'NP-TEST-42',
            'amount': self.amount, 'currency': self.currency.name,
            'remark': 'make_payment', 'status': 'succeeded'}
        return tx

    def test_creation_pending_not_paid(self):
        tx = self._start()
        self.assertEqual(tx.state, 'pending')
        self.assertEqual(tx.neropay_link_id, '42')

    def test_repeat_render_reuses_link(self):
        tx = self._start()
        tx._get_specific_rendering_values({})
        self.client.create_link.assert_called_once()

    def test_success_requires_link_and_ledger(self):
        tx = self._start()
        self.client.link.return_value['status'] = 'paid'
        tx._neropay_refresh(force=True)
        self.assertEqual(tx.state, 'done')

    def test_open_does_not_read_or_trust_ledger(self):
        tx = self._start()
        tx._neropay_refresh(force=True)
        self.assertEqual(tx.state, 'pending')
        self.client.transaction.assert_not_called()

    def test_paid_flag_with_wrong_account_is_not_paid(self):
        tx = self._start()
        self.client.link.return_value.update(status='paid', account_id='NP_OTHER')
        tx._neropay_refresh(force=True)
        self.assertEqual(tx.state, 'pending')
        self.assertTrue(tx.neropay_needs_review)

    def test_paid_flag_with_wrong_amount_is_not_paid(self):
        tx = self._start()
        self.client.link.return_value.update(status='paid', amount=1)
        tx._neropay_refresh(force=True)
        self.assertEqual(tx.state, 'pending')
        self.assertTrue(tx.neropay_needs_review)

    def test_partial_refund_requires_review(self):
        tx = self._start()
        self.client.link.return_value['status'] = 'paid'
        self.client.transaction.return_value['status'] = 'partial_refunded'
        tx._neropay_refresh(force=True)
        self.assertEqual(tx.state, 'pending')
        self.assertTrue(tx.neropay_needs_review)

    def test_timeout_does_not_retry_create(self):
        self.client.create_link.side_effect = NeroPayError('Uncertain')
        tx = self._start()
        tx._get_specific_rendering_values({})
        self.client.create_link.assert_called_once()
        self.assertTrue(tx.neropay_needs_review)

    def test_uncertain_creation_recovers_by_read(self):
        self.client.create_link.side_effect = NeroPayError('Uncertain')
        tx = self._start()
        self.client.find_link.return_value = self._created_link({'identifier': tx.neropay_identifier})
        tx._neropay_refresh(force=True)
        self.assertEqual(tx.neropay_link_id, '42')
        self.client.create_link.assert_called_once()

    def test_duplicate_verification_does_not_regress_done(self):
        tx = self._start()
        self.client.link.return_value['status'] = 'paid'
        tx._neropay_refresh(force=True)
        self.client.link.return_value['status'] = 'failed'
        tx._neropay_refresh(force=True)
        self.assertEqual(tx.state, 'done')
        self.assertEqual(self.client.link.call_count, 1)

    def test_disabled_provider_never_calls_live_api(self):
        tx = self._start()
        self.provider.state = 'disabled'
        tx._neropay_refresh(force=True)
        self.client.link.assert_not_called()

    def test_account_binding_cannot_change(self):
        self._start()
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.provider.neropay_account_id = 'NP_OTHER'

    def test_dry_run_cannot_enable_test_payments(self):
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.provider.state = 'test'

    def test_cron_is_read_only(self):
        tx = self._start()
        self.env['payment.transaction']._cron_neropay_refresh()
        self.client.create_link.assert_called_once()
        self.assertTrue(tx.neropay_last_check)
