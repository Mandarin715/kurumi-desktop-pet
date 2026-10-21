"""No real credentials or account requests; fixed mock OKX responses."""
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from okx_api import (BALANCE, CONFIG, DEFAULT_OKX, GET_ROUTES, POSITIONS, TICKER,
                     Credentials, NoRedirect, OKXClient, OKXError, load_credentials, validate_options)
from okx_risk import EquityTracker
from fx_logic import PriceMoodEngine, validated_policy
from pet_core import atomic_json, load_config, save_options
ROOT = Path(__file__).resolve().parents[1]


class Settings:
    def __init__(self): self.data = {}
    def value(self, key, default=None): return self.data.get(key, default)
    def setValue(self, key, value): self.data[key] = value
    def sync(self): pass


class APITests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.credentials = Credentials('test-key-not-real', 'test-secret-not-real', 'test-pass-not-real')
        self.responses = {
            TICKER: [{'instId': 'BTC-USDT', 'last': '100.5', 'ts': str(int(time.time()*1000))}],
            CONFIG: [{'uid': 'test-account', 'perm': 'read_only'}],
            POSITIONS: [{'pos': '2', 'uplRatio': '-0.04'}, {'pos': '1', 'uplRatio': '.01'},
                        {'pos': '0', 'uplRatio': '-9'}, {'pos': '3', 'uplRatio': ''}],
            BALANCE: [{'totalEq': '1000', 'upl': '-12.5'}],
        }

    def transport(self, path, headers, timeout):
        self.calls.append((path, headers, timeout))
        return {'code': '0', 'data': self.responses[path.split('?')[0]]}

    def client(self, private=True, **options):
        return OKXClient({**DEFAULT_OKX, **options}, self.credentials if private else Credentials(), self.transport)

    def test_public_price_needs_no_key_and_never_sends_auth(self):
        snapshot = self.client(False).poll()
        self.assertEqual(snapshot.price, 100.5)
        self.assertFalse(snapshot.private_enabled)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], TICKER+'?instId=BTC-USDT')
        self.assertFalse(any('ACCESS' in key for key in self.calls[0][1]))

    def test_timeout_default_and_legacy_config_have_five_second_minimum(self):
        self.assertEqual(DEFAULT_OKX['timeout_seconds'], 5.)
        for configured in (3., 5., 7.):
            self.calls.clear()
            self.client(False, timeout_seconds=configured).poll()
            self.assertEqual(self.calls[0][2], max(5., configured))

    def test_private_reads_each_allow_slow_healthy_response(self):
        # Four 3.01s reads must not exhaust the old shared 8s deadline.
        clock = [100.]
        def transport(path, headers, timeout):
            self.assertGreaterEqual(timeout, 5.)
            clock[0] += 3.01
            return self.transport(path, headers, timeout)
        client = self.client(timeout_seconds=3.)
        client.transport = transport
        with patch('okx_api.time.monotonic', side_effect=lambda: clock[0]):
            self.assertTrue(client.poll().private_enabled)
        self.assertEqual(len(self.calls), 4)

    def test_read_only_permissions_and_private_fields(self):
        client = self.client()
        snapshot = client.poll()
        self.assertEqual([p.split('?')[0] for p, _, _ in self.calls], [TICKER, CONFIG, POSITIONS, BALANCE])
        self.assertTrue(snapshot.private_enabled)
        self.assertEqual(snapshot.equity, 1000.)
        self.assertEqual(snapshot.worst_upl_percent, -4.)
        self.assertEqual(snapshot.account_upl_usd, -12.5)
        self.assertNotIn('test-account', snapshot.account_scope)
        self.calls.clear()
        client.poll()
        self.assertEqual([p.split('?')[0] for p, _, _ in self.calls], [TICKER, POSITIONS, BALANCE])

    def test_public_and_account_polling_have_disjoint_routes(self):
        client = self.client()
        self.assertEqual(client.poll_ticker().price, 100.5)
        self.assertEqual([p.split('?')[0] for p, _, _ in self.calls], [TICKER])
        self.assertFalse(any('ACCESS' in key for key in self.calls[0][1]))
        self.calls.clear()
        account = client.poll_account()
        self.assertIsNone(account.price)
        self.assertTrue(account.private_enabled)
        self.assertEqual([p.split('?')[0] for p, _, _ in self.calls], [CONFIG, POSITIONS, BALANCE])

    def test_private_failure_does_not_block_public_price(self):
        client = self.client()
        transport = client.transport
        def private_failure(path, headers, timeout):
            if not path.startswith(TICKER):
                raise TimeoutError('synthetic private failure')
            return transport(path, headers, timeout)
        client.transport = private_failure
        with self.assertRaises(OKXError): client.poll_account()
        self.assertEqual(client.poll_ticker().price, 100.5)

    def test_dangerous_or_unknown_key_never_reads_positions(self):
        for perm in ('read_only,trade', 'read_only,withdraw', 'trade', '', 'read_only,unknown'):
            with self.subTest(perm=perm):
                self.calls.clear()
                self.responses[CONFIG][0]['perm'] = perm
                snapshot = self.client().poll()
                self.assertFalse(snapshot.private_enabled)
                self.assertTrue(snapshot.permission_warning)
                self.assertEqual([p.split('?')[0] for p, _, _ in self.calls], [TICKER, CONFIG])
                if 'trade' in perm or 'withdraw' in perm:
                    self.assertIn('建议改成只读', snapshot.permission_warning)

    def test_signature_and_demo_header(self):
        fixed = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)
        with patch('okx_api.datetime') as clock:
            clock.now.return_value = fixed
            self.client(simulated=True).poll()
        for path, headers, timeout in self.calls:
            self.assertEqual(headers['x-simulated-trading'], '1')
            self.assertGreater(timeout, 0)
            if path.startswith(TICKER):
                self.assertNotIn('OK-ACCESS-KEY', headers)
            else:
                expected = base64.b64encode(hmac.new(b'test-secret-not-real',
                    ('2026-10-21T00:00:00.000ZGET'+path).encode(), hashlib.sha256).digest()).decode()
                self.assertEqual(headers['OK-ACCESS-SIGN'], expected)
                self.assertEqual(headers['OK-ACCESS-KEY'], 'test-key-not-real')

    def test_fixed_route_allowlist_blocks_all_write_routes(self):
        client = self.client()
        self.assertEqual(GET_ROUTES, {TICKER, CONFIG, POSITIONS, BALANCE})
        for route in ('/api/v5/trade/order', '/api/v5/trade/cancel-order', '/api/v5/asset/withdrawal',
                      'https://example.invalid', '/api/v5/account/config?foo=1'):
            with self.assertRaises(OKXError):
                client._get(route, private=True)
        with self.assertRaises(OKXError):
            client._get(TICKER, private=True)
        with self.assertRaises(OKXError):
            client._get(CONFIG, private=True, query={'x': 'y'})
        self.assertEqual(self.calls, [])
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.invalid'))

    def test_errors_and_repr_do_not_expose_secrets(self):
        def failure(*args):
            raise RuntimeError('test-secret-not-real test-pass-not-real')
        client = OKXClient(DEFAULT_OKX, self.credentials, failure)
        with self.assertRaises(OKXError) as caught:
            client.poll()
        self.assertNotIn('test-secret', str(caught.exception))
        self.assertNotIn('test-', repr(self.credentials))
        self.assertNotIn('test-', repr(client))
        client.transport = lambda *args: {'code': '50101', 'msg': 'test-secret-not-real', 'data': []}
        with self.assertRaises(OKXError) as caught:
            client.poll()
        self.assertNotIn('test-secret', str(caught.exception))

    def test_stale_invalid_and_missing_prices_are_rejected(self):
        for quote in [{'instId': 'BTC-USDT', 'last': 'NaN', 'ts': str(int(time.time()*1000))},
                      {'instId': 'BTC-USDT', 'last': '100', 'ts': str(int((time.time()-100)*1000))},
                      {'instId': 'ETH-USDT', 'last': '100', 'ts': str(int(time.time()*1000))}, {}]:
            self.responses[TICKER] = [quote]
            with self.assertRaises(OKXError):
                self.client(False).poll()

    def test_credentials_only_local_and_config_atomic_roundtrip(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as name:
            external = Path(name)
            self.assertFalse(load_credentials(external).present)
            atomic_json(external/'okx.local.json', {'api_key': 'test-key-not-real',
                        'secret_key': 'test-secret-not-real', 'passphrase': 'test-pass-not-real'})
            self.assertTrue(load_credentials(external).complete)
            config = load_config(external, ROOT)
            save_options(config, external, ROOT, 'okx', {**DEFAULT_OKX, 'inst_id': 'eth-usdt', 'simulated': True})
            saved = (external/'kurumi.json').read_text(encoding='utf-8')
            self.assertNotIn('test-secret', saved)
            updated = load_config(external, ROOT)
            self.assertEqual(updated.data_source, 'okx')
            self.assertEqual(updated.okx['inst_id'], 'ETH-USDT')
            self.assertTrue(updated.okx['simulated'])
            self.assertFalse((external/'okx.local.json.tmp').exists())
            (external/'okx.local.json').write_text('bad test-secret-not-real', encoding='utf-8')
            with self.assertRaises(ValueError) as caught:
                load_credentials(external)
            self.assertNotIn('test-secret', str(caught.exception))


class RiskTests(unittest.TestCase):
    def test_observed_equity_day_scope_and_once_per_day(self):
        settings = Settings()
        tracker = EquityTracker(settings)
        self.assertEqual(tracker.observe('one', 1000., 3., '2026-10-21'), (0., False))
        percent, announce = tracker.observe('one', 960., 3., '2026-10-21')
        self.assertAlmostEqual(percent, -4.)
        self.assertTrue(announce)
        self.assertFalse(EquityTracker(settings).observe('one', 950., 3., '2026-10-21')[1])
        self.assertEqual(tracker.observe('two', 960., 3., '2026-10-21'), (0., False))
        self.assertEqual(tracker.observe('one', 900., 3., '2026-10-22'), (0., False))
        self.assertEqual(tracker.observe(None, None, 3.), (None, False))

    def test_account_stop_loss_without_manual_price_and_daily_cry(self):
        engine = PriceMoodEngine(validated_policy())
        engine.set_account_risk('position', '该止损了……真的要止损吗？')
        update = engine.receive(100, 0)
        self.assertEqual(update.text, '该止损了……真的要止损吗？')
        self.assertIn(update.pose, [13, 8, 14])
        engine.set_account_risk('position')
        self.assertIsNone(engine.receive(101, 10).text)
        engine.set_account_risk('daily', '今天先停手，休息一下吧。')
        # A newly breached daily limit bypasses the ordinary pose hold.
        update = engine.receive(101, 20)
        self.assertIn(update.pose or engine.current, [8, 14])
        self.assertIn('停手', update.text)
        engine.set_account_risk('daily')
        self.assertIsNone(engine.receive(101, 40).text)
        engine.set_account_risk(None)
        self.assertIsNone(engine.account_risk)


if __name__ == '__main__':
    unittest.main(verbosity=2)
