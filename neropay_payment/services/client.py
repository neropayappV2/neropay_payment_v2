"""Small, independently testable NeroPay v2 contract. Never trust a browser's paid flag."""
import hashlib
import hmac
import json
import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlsplit

import requests

API_ROOT = 'https://eu.neropay.app/v2'
CURRENCIES = {'GBP', 'EUR', 'USD'}


class NeroPayError(Exception):
    """A sanitised error safe to display without credentials or provider response bodies."""


def minor(value):
    if isinstance(value, bool) or value is None:
        raise NeroPayError('Invalid payment amount.')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or number > 10000000:
            raise NeroPayError('Invalid payment amount.')
        scaled = number * 100
        if scaled != scaled.to_integral_value():
            raise NeroPayError('This release supports two-decimal amounts only.')
        return int(scaled)
    except (InvalidOperation, ValueError, TypeError):
        raise NeroPayError('Invalid payment amount.') from None


def identifier(database_uuid, company_id, provider_id, reference):
    identity = json.dumps([database_uuid, company_id, provider_id, reference], separators=(',', ':'))
    return 'LinkPay_ODOO_' + hashlib.sha256(identity.encode()).hexdigest()[:48]


def checkout_url(url):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
        raise NeroPayError('Invalid checkout address.')
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == 'https' and parts.hostname == 'eu.neropay.app'
                 and parts.port in (None, 443) and not parts.username and not parts.password
                 and not parts.fragment and parts.path == '/initiate/payment/checkout'
                 and bool(parts.query))
    except ValueError:
        valid = False
    if not valid:
        raise NeroPayError('Untrusted checkout address.')
    return url


def validate_link(data, account, order_identifier, amount, currency, remote_id=None, reference=None):
    if not isinstance(data, dict):
        raise NeroPayError('Invalid payment link response.')
    if data.get('account_id') != account or data.get('identifier') != order_identifier:
        raise NeroPayError('Payment account or order reference does not match.')
    if currency not in CURRENCIES or data.get('currency') != currency or minor(data.get('amount')) != minor(amount):
        raise NeroPayError('Payment amount or currency does not match.')
    link_id = data.get('id')
    if isinstance(link_id, bool) or not re.fullmatch(r'[1-9][0-9]*', str(link_id or '')):
        raise NeroPayError('Missing payment link ID.')
    if remote_id and str(link_id) != str(remote_id):
        raise NeroPayError('Payment link ID changed.')
    if not isinstance(data.get('reference'), str) or not re.fullmatch(r'[A-Za-z0-9_\-]{1,150}', data['reference']):
        raise NeroPayError('Missing payment reference.')
    if reference and data['reference'] != reference:
        raise NeroPayError('Payment reference changed.')
    checkout_url(data.get('payment_link'))
    return data


def validate_transaction(data, account, reference, amount, currency):
    if not isinstance(data, dict) or data.get('account_id') != account:
        raise NeroPayError('Transaction account does not match.')
    if reference not in (data.get('transaction_reference'), data.get('payment_reference')):
        raise NeroPayError('Transaction reference does not match.')
    if data.get('currency') != currency or minor(data.get('amount')) != minor(amount):
        raise NeroPayError('Transaction amount or currency does not match.')
    if data.get('remark') != 'make_payment':
        raise NeroPayError('This is not a hosted payment transaction.')
    return data


def signed_headers(secret, account, payload=None, key=None, timestamp=None):
    headers = {'Authorization': 'Bearer ' + secret, 'Accept': 'application/json',
               'Content-Type': 'application/json', 'NeroPay-Account': account}
    body = None
    if payload is not None:
        if not key:
            raise NeroPayError('Missing idempotency key.')
        body = json.dumps(payload, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()
        stamp = str(int(time.time()) if timestamp is None else timestamp)
        headers.update({'X-NeroPay-Timestamp': stamp, 'Idempotency-Key': key,
                        'X-NeroPay-Signature': hmac.new(secret.encode(), stamp.encode() + b'.' + body, hashlib.sha256).hexdigest()})
    return headers, body


class Client:
    def __init__(self, secret, account):
        if not secret or not re.fullmatch(r'[A-Za-z0-9_\-]{1,150}', account or ''):
            raise NeroPayError('Configure the NeroPay API secret and exact account ID.')
        self.secret, self.account = secret, account

    def request(self, method, path, payload=None, key=None):
        # Callers construct paths, never callback data or a configurable remote origin.
        headers, body = signed_headers(self.secret, self.account, payload, key)
        try:
            with requests.Session() as session:
                session.trust_env = False
                with session.request(method, API_ROOT + path, headers=headers, data=body,
                                     timeout=(5, 15), allow_redirects=False, stream=True) as response:
                    if not 200 <= response.status_code < 300:
                        raise NeroPayError('NeroPay returned HTTP %s. Check the original request before retrying.' % response.status_code)
                    chunks, size = [], 0
                    for chunk in response.iter_content(16384):
                        size += len(chunk)
                        if size > 1000000:
                            raise NeroPayError('NeroPay response is too large.')
                        chunks.append(chunk)
                    result = json.loads(b''.join(chunks))
            if not isinstance(result, dict) or result.get('success') is not True or not isinstance(result.get('data'), (dict, list)):
                raise NeroPayError('Unexpected NeroPay response.')
            return result['data']
        except (requests.RequestException, ValueError, UnicodeError):
            raise NeroPayError('NeroPay could not be reached or returned an invalid response. A submitted payment request may already exist.') from None

    def find_link(self, order_identifier):
        rows = self.request('GET', '/payment-links?' + urlencode({'q': order_identifier, 'limit': 100}))
        if not isinstance(rows, list):
            raise NeroPayError('Unexpected link search response.')
        matches = [row for row in rows if isinstance(row, dict) and row.get('identifier') == order_identifier]
        if len(matches) > 1:
            raise NeroPayError('Multiple links match this order. Manual review is required.')
        if not matches and len(rows) >= 100:
            raise NeroPayError('Link search is incomplete. Manual review is required.')
        return matches[0] if matches else None

    def create_link(self, payload):
        return self.request('POST', '/payment-links', payload, payload['identifier'])

    def link(self, link_id):
        return self.request('GET', '/payment-links/' + str(int(link_id)))

    def transaction(self, reference):
        if not re.fullmatch(r'[A-Za-z0-9_\-]{1,150}', reference):
            raise NeroPayError('Invalid stored transaction reference.')
        return self.request('GET', '/transactions/' + reference)
