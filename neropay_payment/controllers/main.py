import re

from werkzeug.exceptions import NotFound

from odoo import http
from odoo.http import request

from ..services.client import checkout_url


class NeroPayController(http.Controller):
    @staticmethod
    def _transaction(token):
        if not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{64}', token):
            raise NotFound()
        tx = request.env['payment.transaction'].sudo().search([
            ('provider_code', '=', 'neropay'), ('neropay_callback_token', '=', token)
        ], limit=1)
        if not tx:
            raise NotFound()
        return tx

    @http.route('/payment/neropay/redirect', type='http', auth='public', methods=['POST'],
                csrf=False, save_session=False)
    def redirect_to_checkout(self, token=None, **ignored):
        # An unguessable per-transaction capability, not an arbitrary URL or transaction ID.
        tx = self._transaction(token)
        if (tx.state in ('draft', 'pending') and tx.neropay_checkout_url
                and not tx.neropay_needs_review and tx.provider_id.state == 'enabled'):
            response = request.redirect(checkout_url(tx.neropay_checkout_url), code=303, local=False)
        else:
            response = request.render('neropay_payment.payment_waiting', {})
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @http.route('/payment/neropay/return/<string:token>', type='http', auth='public',
                methods=['GET', 'POST'], csrf=False, save_session=False)
    def return_from_checkout(self, token, **ignored):
        # Success/cancel query parameters are deliberately ignored.
        self._transaction(token)._neropay_refresh()
        response = request.redirect('/payment/status', code=303)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @http.route('/payment/neropay/ipn/<string:token>', type='http', auth='public',
                methods=['POST'], csrf=False, save_session=False)
    def ipn(self, token, **ignored):
        # IPN is only a wake-up signal. Do not parse or trust its status, signature or amount.
        # Authentic state comes from the scoped v2 API; cron repairs missing/early callbacks.
        self._transaction(token)._neropay_refresh()
        return request.make_json_response({'received': True}, headers={'Cache-Control': 'no-store'})
