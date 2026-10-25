import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from platform_billing import PlatformBilling, LoginExpired, matching_key, normalize_token, ENDPOINT
from billing import bounds
from test_guest import GuestHttpTests

TOKEN='TEST_PLATFORM_SESSION_1234567890'
# Deliberately synthetic; split the prefix so source-export secret scans stay strict.
KEY='sk-'+'abcdefghijklmnop1234'


class PlatformBillingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.service=PlatformBilling(self.root)
        self.config=self.root/'data/astrbot/cmd_config.json';self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({'provider_sources':[{'api_base':'https://api.deepseek.com','key':[KEY]}]}))

    def payload(self,start,end):
        return {'start':start,'end':end,'bucket':'1d','data':[{'currency':'CNY','series':[
            {'api_key':{'tracking_id':'whale-id','name':'小鲸鱼','sensitive_id':'sk-abcdef...1234'},
             'model':'flash','buckets':[{'time':start,'cost':'0.30000001'}]},
            {'api_key':{'tracking_id':'other-id','name':'other','sensitive_id':'sk-other...5678'},
             'model':'flash','buckets':[{'time':start,'cost':'1.7'}]}]}]}

    def fetch(self,token,start,end):
        self.assertEqual(token,TOKEN)
        data=self.payload(start,end)
        for group in data['data']:
            for row in group['series']:
                for bucket in row['buckets']:bucket['time']=bounds(time.time())[1]
        return data

    def establish(self):
        with patch.object(self.service,'fetch',side_effect=self.fetch):
            connection=self.service.connect(TOKEN,'owner-session')
            self.service.select(connection['connection_id'],connection['keys'][0]['id'],'owner-session',True)

    def test_matching_current_key_and_no_api_key_or_token_echo(self):
        with patch.object(self.service,'fetch',side_effect=self.fetch):
            connection=self.service.connect(TOKEN,'owner-session')
        self.assertEqual(len(connection['keys']),1)
        self.assertTrue(connection['keys'][0]['match'])
        encoded=json.dumps(connection)
        for secret in (TOKEN,KEY,'whale-id','sensitive_id'):
            self.assertNotIn(secret,encoded)
        self.assertFalse(self.service.path.exists())

    def test_explicit_confirmation_session_and_key_binding(self):
        with patch.object(self.service,'fetch',side_effect=self.fetch):
            connection=self.service.connect(TOKEN,'owner-session')
            for session,confirmed in [('other-session',True),('owner-session',False)]:
                with self.assertRaises(ValueError):
                    self.service.select(connection['connection_id'],connection['keys'][0]['id'],session,confirmed)
            self.assertFalse(self.service.path.exists())
            self.service.select(connection['connection_id'],connection['keys'][0]['id'],'owner-session',True)
        self.assertTrue(self.service.path.exists())
        if os.name!='nt':self.assertEqual(stat.S_IMODE(self.service.path.stat().st_mode),0o600)
        self.assertEqual(json.loads(self.service.path.read_text())['token'],TOKEN)
        self.assertNotIn(TOKEN,self.service.ledger.path.read_text())

    def test_sync_correct_totals_caches_and_keeps_model_configuration(self):
        original=self.config.read_bytes();self.establish()
        with patch.object(self.service,'fetch',side_effect=AssertionError('unexpected request')):
            value=self.service.query()
        self.assertEqual(value['source'],'official_sync');self.assertTrue(value['connected'])
        self.assertEqual(value['costs'][0]['whale_cost'],'0.30000001')
        self.assertEqual(value['costs'][0]['account_cost'],'2.00000001')
        self.assertNotIn(TOKEN,json.dumps(value));self.assertNotIn(KEY,json.dumps(value))
        self.assertEqual(original,self.config.read_bytes())

    def test_background_refresh_cache_and_errors_do_not_retry_rapidly(self):
        self.establish();self.service.last_attempt=0
        with patch.object(self.service,'fetch',side_effect=RuntimeError('SECRET_UPSTREAM_BODY')) as request:
            a=self.service.query();b=self.service.query()
        self.assertEqual(request.call_count,1)
        self.assertEqual(a['costs'],[]);self.assertEqual(b['costs'],[])
        self.assertNotIn('SECRET_UPSTREAM_BODY',json.dumps(a))

    def test_expired_login_reports_reconnect_without_exposing_balance_or_token(self):
        self.establish();self.service.last_attempt=0
        with patch.object(self.service,'fetch',side_effect=LoginExpired()):
            value=self.service.query()
        self.assertEqual(value['status'],'reauth_required');self.assertEqual(value['costs'],[])
        self.assertNotIn(TOKEN,json.dumps(value))

    def test_rotated_api_key_cannot_silently_reuse_different_scope(self):
        self.establish();self.config.write_text(self.config.read_text().replace(KEY,'sk-ROTATED'))
        with patch.object(self.service,'fetch',side_effect=AssertionError('wrong account fetch')):
            self.assertEqual(self.service.query()['status'],'key_changed')
            self.assertEqual(self.service.query()['costs'],[])

    def test_midnight_does_not_serve_yesterdays_totals(self):
        self.establish();tomorrow=bounds(time.time())[2]+1
        with patch('platform_billing.time.time',return_value=tomorrow),patch.object(self.service,'fetch',side_effect=RuntimeError()):
            value=self.service.query()
        self.assertEqual(value['day'],bounds(tomorrow)[0]);self.assertEqual(value['costs'],[])

    def test_disconnect_deletes_credentials_totals_pending_and_memory_cache(self):
        self.establish();self.service.disconnect()
        self.assertFalse(self.service.path.exists());self.assertFalse(self.service.ledger.path.exists())
        self.assertFalse(self.service.pending);self.assertIsNone(self.service.cache)
        with patch.object(self.service,'fetch',side_effect=AssertionError('disconnected request')):
            self.assertFalse(self.service.query()['connected'])

    def test_removed_credential_file_does_not_claim_cached_auto_sync(self):
        self.establish();self.service.path.unlink()
        value=self.service.query()
        self.assertFalse(value['connected']);self.assertEqual(value['costs'],[])

    def test_pending_authorization_expires_and_is_not_saved(self):
        with patch.object(self.service,'fetch',side_effect=self.fetch):
            connection=self.service.connect(TOKEN,'owner-session')
        self.service.pending[connection['connection_id']]['expires']=0
        with self.assertRaises(ValueError):
            self.service.select(connection['connection_id'],connection['keys'][0]['id'],'owner-session',True)
        self.assertFalse(self.service.path.exists());self.assertFalse(self.service.pending)

    def test_key_rotation_during_confirmation_requires_reconnection(self):
        with patch.object(self.service,'fetch',side_effect=self.fetch):
            connection=self.service.connect(TOKEN,'owner-session')
        self.config.write_text(self.config.read_text().replace(KEY,'sk-ROTATED'))
        with self.assertRaises(ValueError):self.service.select(connection['connection_id'],connection['keys'][0]['id'],'owner-session',True)
        self.assertFalse(self.service.path.exists())

    def test_confirmed_valid_login_survives_billing_failure_without_fake_amounts(self):
        with patch.object(self.service,'fetch',side_effect=self.fetch):
            connection=self.service.connect(TOKEN,'owner-session')
        with patch('platform_billing.daily_costs',side_effect=RuntimeError('TOKEN_VALUE')):
            result=self.service.select(connection['connection_id'],connection['keys'][0]['id'],'owner-session',True)
        self.assertFalse(result['billing_ready']);self.assertTrue(self.service.path.exists())
        value=self.service.query();self.assertTrue(value['connected']);self.assertEqual(value['costs'],[])
        self.assertNotIn('TOKEN_VALUE',json.dumps(value));self.assertNotIn(TOKEN,json.dumps(value))
        self.assertFalse(self.service.ledger.path.exists())
        self.service.last_attempt=0
        with patch.object(self.service,'fetch',side_effect=self.fetch):value=self.service.query()
        self.assertEqual(value['status'],'ok');self.assertTrue(value['connected'])

    def test_key_change_while_today_is_being_verified_does_not_save_token(self):
        with patch.object(self.service,'fetch',side_effect=self.fetch):
            connection=self.service.connect(TOKEN,'owner-session')
        from billing import daily_costs
        def changed(data,start,end,tracking):
            result=daily_costs(data,start,end,tracking)
            self.config.write_text(self.config.read_text().replace(KEY,'sk-ROTATED'))
            return result
        with patch('platform_billing.daily_costs',side_effect=changed):
            with self.assertRaises(ValueError):self.service.select(connection['connection_id'],connection['keys'][0]['id'],'owner-session',True)
        self.assertFalse(self.service.path.exists())

    def test_confirmation_reuses_verified_report_without_short_range_request(self):
        with patch.object(self.service,'fetch',side_effect=self.fetch) as fetch:
            connection=self.service.connect(TOKEN,'owner-session')
            self.service.select(connection['connection_id'],connection['keys'][0]['id'],'owner-session',True)
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(self.service.query()['costs'][0]['whale_cost'],'0.30000001')

    def test_export_contains_only_own_cost_and_balance_without_secrets(self):
        self.establish()
        with patch('platform_billing.DeepSeekBalance.query',return_value={'status':'ok','checked_at':time.time(),'balance_infos':[{'currency':'CNY','total_balance':'20'}]}):
            self.service.export_rations()
        output=(self.root/'data/astrbot/plugin_data/companion_rations/summary.json').read_text()
        for private in (TOKEN,KEY,'account_cost','key_name','tracking_id','fingerprint'):
            self.assertNotIn(private,output)
        self.assertEqual(json.loads(output)['whale_costs'][0]['amount'],'0.30000001')

    def test_wider_report_counts_today_only_and_rejects_malformed_history(self):
        from billing import daily_costs
        day,start,end=bounds(time.time())
        data=self.payload(start-86400,end)
        row=data['data'][0]['series'][0]
        row['buckets'].append({'time':start,'cost':'0.125'})
        value=daily_costs(data,start,end,'whale-id')[0]
        self.assertEqual(value['whale_cost'],'0.125')
        self.assertEqual(value['account_cost'],'0.125')
        row['buckets'][0]['cost']='-1'
        with self.assertRaises(ValueError):daily_costs(data,start,end,'whale-id')

    def test_current_official_numeric_bucket_including_function_charges(self):
        from billing import daily_costs
        day,start,end=bounds(time.time())
        data=self.payload(start,end);data['bucket']=86400
        data['data'][0]['function_series']=[
            {'api_key':{'tracking_id':'whale-id','name':'whale'},'model':'flash',
             'buckets':[{'time':start,'cost':'0.02'}]},
            {'api_key':None,'model':'search','buckets':[{'time':start,'cost':'0.03'}]}]
        row=daily_costs(data,start,end,'whale-id')[0]
        self.assertEqual(row['whale_cost'],'0.32000001')
        self.assertEqual(row['account_cost'],'2.05000001')

    def test_numeric_subdaily_buckets_reject_crossing_or_unaligned_windows(self):
        from billing import daily_costs
        day,start,end=bounds(time.time())
        data=self.payload(start,end);data['bucket']=3600
        data['data'][0]['series'][0]['buckets'].append({'time':start+3600,'cost':'0.1'})
        self.assertEqual(daily_costs(data,start,end,'whale-id')[0]['whale_cost'],'0.40000001')
        for bucket in (True,0,-1,172800,12345,None):
            data['bucket']=bucket
            with self.assertRaises(ValueError):daily_costs(data,start,end,'whale-id')
        data['bucket']=3600;data['data'][0]['series'][0]['buckets'][0]['time']=start+1
        with self.assertRaises(ValueError):daily_costs(data,start,end,'whale-id')

    def test_complete_connection_accepts_current_official_numeric_bucket(self):
        def fetch(token,start,end):
            data=self.fetch(token,start,end);data['bucket']=86400
            return data
        with patch.object(self.service,'fetch',side_effect=fetch):
            connection=self.service.connect(TOKEN,'owner-session')
            self.service.select(connection['connection_id'],connection['keys'][0]['id'],'owner-session',True)
        self.assertEqual(self.service.query()['status'],'ok')
        self.assertEqual(self.service.query()['costs'][0]['whale_cost'],'0.30000001')

    def test_token_input_and_masked_key_matching(self):
        self.assertEqual(normalize_token('Bearer '+TOKEN),TOKEN)
        for value in (KEY,'short','line\n'+TOKEN,{},None):
            with self.assertRaises(ValueError):normalize_token(value)
        self.assertTrue(matching_key(KEY,KEY));self.assertFalse(matching_key('sk-other...5678',KEY))
        self.assertIsNone(matching_key('opaque-id',KEY))

    def test_fixed_official_endpoint_response_projection_and_error_codes(self):
        start,end=bounds(time.time())[1:]
        response=Mock();response.__enter__=Mock(return_value=Mock(read=Mock(return_value=json.dumps({'code':0,'data':{'biz_code':0,'biz_data':self.payload(start,end)}}).encode())))
        response.__exit__=Mock(return_value=False);opener=Mock();opener.open.return_value=response
        with patch('platform_billing.urllib.request.build_opener',return_value=opener):
            self.service.fetch(TOKEN,start,end)
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,f'{ENDPOINT}?start={start}&end={end}&tz=28800')
        self.assertEqual(request.get_header('Authorization'),'Bearer '+TOKEN)
        response.__enter__.return_value.read.return_value=b'{"code":40003}'
        with patch('platform_billing.urllib.request.build_opener',return_value=opener):
            with self.assertRaises(LoginExpired):self.service.fetch(TOKEN,start,end)


class PlatformBillingHttpTests(GuestHttpTests):
    def test_connect_select_disconnect_and_costs_are_admin_only(self):
        admin,guest=self.sessions()
        with patch('platform_billing.PlatformBilling.connect',return_value={'connection_id':'nonce','keys':[]}) as connect,patch('platform_billing.PlatformBilling.select',return_value={'ok':True}) as select,patch('platform_billing.PlatformBilling.disconnect',return_value={'ok':True}) as disconnect:
            for route in ('usage/connect','usage/select','usage/disconnect'):
                self.assertEqual(self.request(route,{},session=guest)[0],403)
                self.assertEqual(self.request(route,{})[0],401)
            connect.assert_not_called();select.assert_not_called();disconnect.assert_not_called()
            self.assertEqual(self.request('usage/connect',{'token':TOKEN},session=admin)[0],200)
            self.assertEqual(self.request('usage/select',{'connection_id':'nonce','key_id':'key','confirmed':True},session=admin)[0],200)
            self.assertEqual(self.request('usage/disconnect',{},session=admin)[0],200)
            connect.assert_called_once_with(TOKEN,admin)
            select.assert_called_once_with('nonce','key',admin,True)
            disconnect.assert_called_once()


if __name__=='__main__':unittest.main()
