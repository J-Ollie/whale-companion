import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from balance import DeepSeekBalance, NoRedirect
from test_guest import GuestHttpTests


class BalanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.balance = DeepSeekBalance(self.root)
        self.balance.path.parent.mkdir(parents=True)
        self.configure()

    def configure(self, base='https://api.deepseek.com', key='TEST_SECRET'):
        self.balance.path.write_text(json.dumps({'provider_sources': [
            {'enable': True, 'api_base': base, 'key': [key]}]}), encoding='utf-8')

    def payload(self):
        return {'is_available': True, 'balance_infos': [{'currency': 'CNY',
            'total_balance': '12.3456', 'granted_balance': '2', 'topped_up_balance': '10.3456',
            'secret': 'MUST_NOT_RETURN'}], 'secret': 'MUST_NOT_RETURN'}

    def opener(self, payload=None):
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(
            return_value=json.dumps(payload or self.payload()).encode())))
        opener.open.return_value.__exit__ = Mock(return_value=False)
        return opener

    def test_fixed_endpoint_projection_and_shared_cache(self):
        opener = self.opener()
        with patch('balance.urllib.request.build_opener', return_value=opener):
            first = self.balance.query()
            second = self.balance.query()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.deepseek.com/user/balance')
        self.assertEqual(request.get_header('Authorization'), 'Bearer TEST_SECRET')
        self.assertEqual(first['status'], 'ok')
        self.assertTrue(second['cached'])
        self.assertEqual(opener.open.call_count, 1)
        self.assertNotIn('SECRET', json.dumps(first))
        self.assertEqual(first['balance_infos'][0]['total_balance'], '12.3456')

    def test_unconfigured_and_third_party_never_send_key(self):
        for base in ('https://evil.example', 'https://api.deepseek.com.evil.example',
                     'http://api.deepseek.com', 'https://user@api.deepseek.com'):
            self.configure(base)
            balance = DeepSeekBalance(self.root)
            with patch('balance.urllib.request.build_opener') as network:
                self.assertEqual(balance.query()['code'], 'unconfigured')
                network.assert_not_called()

    def test_ambiguous_keys_are_not_silently_selected(self):
        source = {'api_base': 'https://api.deepseek.com', 'key': ['other-key']}
        config = json.loads(self.balance.path.read_text())
        config['provider_sources'].append(source)
        self.balance.path.write_text(json.dumps(config))
        self.assertEqual(self.balance.query()['code'], 'multiple_accounts')

    def test_failures_do_not_expose_exception_or_cache_old_account(self):
        opener = self.opener()
        with patch('balance.urllib.request.build_opener', return_value=opener):
            self.assertEqual(self.balance.query()['status'], 'ok')
            self.balance.last_attempt = 0
            opener.open.side_effect = RuntimeError('TEST_SECRET raw upstream body')
            error = self.balance.query()
        self.assertEqual(error['balance_infos'], [])
        self.assertNotIn('TEST_SECRET', json.dumps(error))

    def test_invalid_money_is_rejected(self):
        for money in ('NaN', 'Infinity', 'not money', '1e9999'):
            payload = self.payload()
            payload['balance_infos'][0]['total_balance'] = money
            with self.assertRaises(Exception):
                self.balance.normalize(payload)

    def test_redirect_is_refused(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.example'))


class BalanceHttpTests(GuestHttpTests):
    def test_balance_requires_admin_before_reading_credentials(self):
        admin, guest = self.sessions()
        with patch('balance.DeepSeekBalance.query', return_value={'status': 'ok'}) as query:
            self.assertEqual(self.request('balance')[0], 401)
            self.assertEqual(self.request('balance', session=guest)[0], 403)
            query.assert_not_called()
            self.assertEqual(self.request('balance', session=admin), (200, {'status': 'ok'}))
            query.assert_called_once()
