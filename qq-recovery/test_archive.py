import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from archive import Archive
from test_guest import GuestHttpTests


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.archive=Archive(self.root)
        self.phone=self.archive.phone;self.phone.mkdir(parents=True)
        memory=self.archive.data/'companion_memory';memory.mkdir()
        (memory/'public-profile.json').write_text(json.dumps({'name':'岚','traits':'好奇而认真','private':'SECRET'}))
        self.rhythm={'updated_at':time.time(),'mode':'asleep','mode_since':time.time()-30,
                     'until':time.time()+3600,'wake_anchor':540,'sleep_hours':8,'energy':0.8,
                     'last_activity':{'note':'SECRET'},'pending':[{'text':'SECRET'}]}
        self.save()

    def save(self):
        (self.phone/'rhythm.json').write_text(json.dumps(self.rhythm))

    def test_guest_does_not_read_activity_or_leak_private_state(self):
        (self.phone/'experiences.jsonl').write_text('SECRET invalid activity')
        value=self.archive.snapshot(guest=True)
        self.assertEqual(value['current']['label'],'睡觉中')
        self.assertEqual(value['routine']['wake_time'],'09:00')
        self.assertEqual(set(value),{'generated_at','timezone','profile','current','routine'})
        self.assertNotIn('SECRET',json.dumps(value))

    def test_history_beijing_day_and_duration_are_not_invented(self):
        rows=[{'timestamp':1792515600,'app':'QQ','note':'查看内容','views':2},
              {'timestamp':1792515630,'app':'酷安','note':'看看动态','started_at':1792515600,'ended_at':1792515630},
              {'timestamp':1792515640,'app':'酷安','note':'token SECRET'}]
        p=self.phone/'experiences.jsonl';p.write_text('\n'.join(json.dumps(r) for r in rows)+'\ninvalid')
        before=p.read_bytes()
        value=self.archive.snapshot(day='2026-10-21')['activity']
        self.assertEqual(value['total'],3)
        self.assertEqual(value['known_duration_seconds'],30)
        self.assertEqual(value['missing_duration'],2)
        self.assertNotIn('SECRET',json.dumps(value))
        self.assertEqual(p.read_bytes(),before)

    def test_bad_dates_stale_states_and_missing_data(self):
        with self.assertRaises(ValueError):self.archive.snapshot(day='../../passwd')
        with self.assertRaises(ValueError):self.archive.snapshot(day='2026-02-30')
        self.rhythm['updated_at']=time.time()-700;self.save()
        self.assertEqual(self.archive.snapshot(True)['current']['state'],'unknown')
        (self.phone/'rhythm.json').write_text('broken')
        self.assertEqual(self.archive.snapshot(True)['current']['state'],'unknown')

    def test_actual_chat_timestamp_only_and_no_stale_browsing(self):
        with patch.object(self.archive,'last_chat',return_value=time.time()-20):
            self.assertEqual(self.archive.snapshot(True)['current']['state'],'chatting')
        self.rhythm.update(mode='browsing',mode_since=time.time()-1000);self.save()
        self.assertEqual(self.archive.snapshot(True)['current']['state'],'unknown')

    def test_publication_merge_trigger_and_audience_are_precise(self):
        now=time.time()
        (self.phone/'qzone-publications.jsonl').write_text(json.dumps({'tid':'1','at':now,'result':'verified',
            'published_text':'自己的公开感想','audience_status':'unconfirmed','image':{'path':'SECRET'}})+'\n')
        (self.phone/'qzone-state.json').write_text(json.dumps({'last_spontaneous_consider_at':now+0.001,'attempts':[
            {'tid':'1','at':now,'result':'verified','audience_status':'platform_restricted','text':'自己的公开感想','authored_by':'chat_agent'},
            {'tid':'2','at':now-10,'result':'uncertain','text':'未确认的尝试','authored_by':'background_agent'}]}))
        d=self.archive.snapshot()['activity'];self.assertEqual(d['total'],2);self.assertEqual(d['qzone_count'],1)
        first=d['entries'][0];self.assertEqual(first['origin'],'自主发布')
        self.assertEqual(first['audience'],'平台限制已确认');self.assertEqual(first['result'],'自身回读确认')
        self.assertEqual(d['entries'][1]['origin'],'触发方式未记录')
        self.assertNotIn('SECRET',json.dumps(d));self.assertNotIn('tid',first)
        with patch.object(self.archive,'other_activities',side_effect=AssertionError('guest activity read')):
            self.assertNotIn('activity',self.archive.snapshot(True))

    def test_incomplete_visit_and_shares_are_not_successful_browsing(self):
        now=time.time()
        (self.phone/'autonomy.json').write_text(json.dumps({'last_checked_at':now,'last_result':'device_unavailable_or_left_app','last_app':'Via','last_trigger':'background'}))
        (self.phone/'outbound-state.json').write_text(json.dumps({'shares':[{'at':now-10,'caption':'主动分享的内容','result':'sent','message_id':'SECRET'}]}))
        d=self.archive.snapshot()['activity']
        self.assertEqual(d['total'],2);self.assertEqual(d['browse_count'],0)
        self.assertEqual(d['entries'][0]['kind'],'phone_attempt')
        self.assertEqual(d['entries'][0]['result'],'访问未完成')
        self.assertEqual(d['entries'][1]['result'],'发送回执确认')
        self.assertNotIn('SECRET',json.dumps(d))


class ArchiveHttpTests(GuestHttpTests):
    def test_guest_projection_cannot_be_expanded_by_query(self):
        admin,guest=self.sessions()
        code,value=self.request('archive?role=admin&private=true',session=guest,claimed='admin')
        self.assertEqual(code,200);self.assertNotIn('activity',value);self.assertNotIn('tendencies',value)
        self.assertEqual(self.request('archive')[0],401)
        self.assertIn('activity',self.request('archive',session=admin)[1])
        self.assertEqual(self.request('archive',session=guest,bootstrap=False)[0],403)


if __name__=='__main__':unittest.main()
