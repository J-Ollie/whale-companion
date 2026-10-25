import json
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from billing import DeepSeekBilling, bounds, aggregate, unwrap
from test_guest import GuestHttpTests


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.billing=DeepSeekBilling(self.root)
        self.config=self.root/'data/astrbot/cmd_config.json';self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({'provider_sources':[{'api_base':'https://api.deepseek.com','key':['TEST_SECRET']}]}))
        self.day,self.start,self.end=bounds(time.time())
        self.report={'code':0,'data':{'biz_code':0,'biz_data':{'start':self.start,'end':self.end,'bucket':'1d',
            'data':[{'currency':'CNY','series':[
                {'api_key':{'tracking_id':'whale','name':'小鲸鱼','sensitive_id':'TEST_SECRET'},'model':'flash','buckets':[{'time':self.start,'cost':'0.10000001'}]},
                {'api_key':{'tracking_id':'whale','name':'小鲸鱼'},'model':'pro','buckets':[{'time':self.start,'cost':'0.2'}]},
                {'api_key':{'tracking_id':'other','name':'other'},'model':'flash','buckets':[{'time':self.start,'cost':'1.7'}]},
                {'api_key':None,'model':'flash','buckets':[{'time':self.start,'cost':'0.5'}]}]}]}}}

    def test_exact_decimal_key_scope_and_account_include_other_and_deleted_keys(self):
        value=self.billing.import_report(self.report,'whale',True)
        row=value['costs'][0]
        self.assertEqual(row['whale_cost'],'0.30000001')
        self.assertEqual(row['account_cost'],'2.50000001')
        self.assertEqual(value['source'],'official_import')
        saved=self.billing.path.read_text()
        self.assertNotIn('TEST_SECRET',saved)
        self.assertNotIn('tracking_id',saved)
        self.assertNotIn('sensitive_id',saved)

    def test_import_requires_explicit_all_keys_and_selected_key(self):
        with self.assertRaises(ValueError):self.billing.import_report(self.report,'whale',False)
        with self.assertRaises(ValueError):self.billing.import_report(self.report,'wrong',True)
        self.assertFalse(self.billing.path.exists())

    def test_unknown_is_not_zero_and_key_rotation_invalidates(self):
        self.assertEqual(self.billing.query()['costs'],[])
        self.billing.import_report(self.report,'whale',True)
        self.config.write_text(self.config.read_text().replace('TEST_SECRET','ROTATED_SECRET'))
        value=self.billing.query()
        self.assertEqual(value['status'],'key_changed');self.assertEqual(value['costs'],[])

    def test_new_beijing_day_does_not_reuse_yesterdays_bill(self):
        self.billing.import_report(self.report,'whale',True)
        with patch('billing.time.time',return_value=self.end+1):
            self.assertEqual(self.billing.query()['status'],'new_day')
            self.assertEqual(self.billing.query()['costs'],[])

    def test_timezone_midnight_is_not_utc_midnight(self):
        day,start,end=bounds(1792771200)  # Fixed timestamp, not host timezone.
        self.assertEqual(end-start,86400)
        self.assertEqual(start%86400,16*3600)

    def test_wrong_window_and_overlapping_buckets_rejected(self):
        data=unwrap(self.report)
        with self.assertRaises(ValueError):aggregate(data,self.start-28800,self.end-28800,'whale')
        data['data'][0]['series'].append(data['data'][0]['series'][0])
        with self.assertRaises(ValueError):aggregate(data,self.start,self.end,'whale')

    def test_missing_invalid_and_negative_costs_fail_closed(self):
        for amount in (None,'NaN','Infinity','-1','1e100',True):
            report=json.loads(json.dumps(self.report))
            report['data']['biz_data']['data'][0]['series'][0]['buckets'][0]['cost']=amount
            with self.assertRaises(ValueError):self.billing.import_report(report,'whale',True)

    def test_scientific_zero_and_extended_decimal_costs_keep_exact_value(self):
        from billing import money
        self.assertEqual(money('0E-8'),Decimal(0))
        self.assertEqual(money('1.234567890123456789E-8'),Decimal('0.00000001234567890123456789'))
        value='0.123456789012345678901234567890123456789'
        self.report['data']['biz_data']['data'][0]['series'][0]['buckets'][0]['cost']=value
        result=self.billing.import_report(self.report,'whale',True)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['costs'][0]['whale_cost'],'0.323456789012345678901234567890123456789')
        self.assertEqual(result['costs'][0]['account_cost'],'2.523456789012345678901234567890123456789')

    def test_explicit_zero_is_valid_and_currencies_are_separate(self):
        data=unwrap(self.report)
        data['data'][0]['series'][0]['buckets'][0]['cost']='0'
        data['data'][0]['series'][1]['buckets'][0]['cost']='0'
        data['data'].append({'currency':'USD','series':[{'api_key':{'tracking_id':'whale'},'model':'flash','buckets':[{'time':self.start,'cost':'0.03'}]}]})
        costs=self.billing.import_report(self.report,'whale',True)['costs']
        self.assertEqual(costs[0]['whale_cost'],'0')
        self.assertEqual(costs[1]['currency'],'USD');self.assertEqual(costs[1]['whale_cost'],'0.03')

    def test_bad_saved_data_never_leaks_raw_errors(self):
        self.billing.path.parent.mkdir(parents=True)
        self.billing.path.write_text('BROKEN_SECRET')
        value=self.billing.query()
        self.assertEqual(value['status'],'unavailable')
        self.assertNotIn('SECRET',json.dumps(value))

    def test_failed_api_payload_cannot_be_used_as_bill(self):
        for payload in ({'code':40003},{'code':0,'data':{'biz_code':2}},{'code':0,'data':{'biz_code':0,'biz_data':{}}}):
            with self.assertRaises(ValueError):unwrap(payload)


class BillingHttpTests(GuestHttpTests):
    def test_usage_read_and_import_are_admin_only_before_data_access(self):
        admin,guest=self.sessions()
        with patch('billing.DeepSeekBilling.query',return_value={'status':'not_connected'}) as read, patch('billing.DeepSeekBilling.import_report',return_value={'status':'ok'}) as write:
            self.assertEqual(self.request('usage')[0],401)
            self.assertEqual(self.request('usage',session=guest)[0],403)
            self.assertEqual(self.request('usage/import',{},session=guest)[0],403)
            self.assertEqual(self.request('usage/import',{})[0],403)
            read.assert_not_called();write.assert_not_called()
            self.assertEqual(self.request('usage',session=admin)[0],200)
            self.assertEqual(self.request('usage/import',{'report':{},'key_id':'whale','confirmed_all_keys':True},session=admin)[0],200)
            write.assert_called_once_with({},'whale',True)


if __name__=='__main__':unittest.main()
