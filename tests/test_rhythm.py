"""Time-shifted behavioral verification. No real API, device or QQ sends."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_phone.rhythm import Rhythm,TZ
from astrbot_plugin_companion_phone.autonomy import AutonomousBrowser

NOW=datetime(2026,10,20,14,0,tzinfo=TZ).timestamp()
COOL='com.coolapk.market'


class RhythmTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock=patch('time.time',return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.r=Rhythm(self.tmp.name)

    def event(self,at=NOW,identifier='phone:test'):
        return {'id':identifier,'timestamp':at,'app':'酷安','note':'这个技术话题有意思','views':3}

    def test_initiative_decision_budget_and_schedule_preserve_habits(self):
        r=Rhythm(self.tmp.name,initiative=True)
        r.state.update(decision_day='2026-10-20',decisions=11,next_decision=NOW-1,mode='awake')
        self.assertTrue(r.available(NOW))
        anchor=r.state['wake_anchor']; r.reserve_decision(NOW)
        self.assertFalse(r.available(NOW,True))
        self.assertTrue(20*60<=r.state['next_decision']-NOW<=120*60)
        self.assertEqual(r.state['wake_anchor'],anchor)

    def test_initiative_still_respects_sleep(self):
        r=Rhythm(self.tmp.name,initiative=True)
        r.state.update(mode='asleep',until=NOW+3600,next_decision=NOW-1)
        self.assertFalse(r.available(NOW))

    def test_state_survives_restart_without_reset(self):
        self.r.state.update(energy=.34,curiosity=.2)
        self.r.save(force=True)
        restored=Rhythm(self.tmp.name)
        self.assertEqual(restored.state['energy'],.34)
        self.assertEqual(restored.state['curiosity'],.2)
        self.assertEqual(restored.state['next_decision'],self.r.state['next_decision'])

    def test_bad_state_is_preserved_and_automation_stops(self):
        self.r.path.write_text('broken json')
        restored=Rhythm(self.tmp.name)
        self.assertFalse(restored.available(NOW,True))
        self.assertFalse(restored.social_allowed())
        self.assertEqual(self.r.path.read_text(),'broken json')

    def test_awake_time_uses_energy_and_sleep_recovers(self):
        before=self.r.state['energy']
        self.r.advance(NOW+3600)
        self.assertLess(self.r.state['energy'],before)
        self.r.state.update(mode='asleep',until=NOW+10*3600)
        before=self.r.state['energy']
        self.r.advance(NOW+4*3600)
        self.assertGreater(self.r.state['energy'],before)

    def test_sleep_does_not_spend_model_decisions(self):
        self.r.state.update(mode='asleep',until=NOW+8*3600)
        self.assertFalse(self.r.available(NOW+60))
        self.assertEqual(self.r.state['decisions'],0)

    def test_chat_does_not_reset_own_sleep_or_wake_anchor(self):
        self.r.choose_rest('rest',30)
        until=self.r.state['until']
        anchor=self.r.state['wake_anchor']
        self.r.chatted()
        self.assertEqual(self.r.state['mode'],'resting')
        self.assertEqual(self.r.state['until'],until)
        self.assertEqual(self.r.state['wake_anchor'],anchor)

    def test_model_can_choose_nap_but_not_flip_day_from_one_choice(self):
        self.r.choose_rest('sleep')
        self.assertEqual(self.r.state['mode'],'resting')
        self.assertLessEqual(self.r.state['until']-NOW,90*60)

    def test_early_wake_does_not_wrap_into_another_six_hour_sleep(self):
        wake=datetime(2026,10,22,8,38,tzinfo=TZ).timestamp()
        self.r.state.update(mode='asleep',updated_at=wake-60,until=wake,energy=.95)
        self.r.advance(wake)
        self.assertEqual(self.r.state['awake_since'],wake)
        self.r.advance(wake+20*60)
        self.assertEqual(self.r.state['mode'],'awake')
        self.assertEqual(self.r.state['until'],0)
        self.r.advance(wake+2*3600)
        self.assertEqual(self.r.state['mode'],'awake')

    def test_fresh_morning_wake_cannot_choose_full_night_sleep(self):
        wake=datetime(2026,10,22,8,38,tzinfo=TZ).timestamp()
        self.r.state.update(mode='awake',updated_at=wake,awake_since=wake,energy=1)
        self.r.choose_rest('sleep',now=wake+60)
        self.assertEqual(self.r.state['mode'],'resting')
        self.assertLessEqual(self.r.state['until']-(wake+60),90*60)

    def test_normal_night_still_sleeps_after_a_full_day(self):
        morning=datetime(2026,10,22,9,tzinfo=TZ).timestamp()
        self.r.state.update(mode='awake',updated_at=morning,awake_since=morning,energy=1)
        self.r.advance(morning+17*3600)
        self.assertEqual(self.r.state['mode'],'asleep')

    def test_browse_and_short_rest_do_not_reset_wake_duration(self):
        original=self.r.state['awake_since']
        self.r.begin_browse();self.r.finish_browse()
        self.r.choose_rest('rest',10,now=NOW)
        self.r.advance(NOW+11*60)
        self.assertEqual(self.r.state['mode'],'awake')
        self.assertEqual(self.r.state['awake_since'],original)

    def test_legacy_state_without_awake_since_loads(self):
        self.r.state.pop('awake_since')
        self.r.save(force=True)
        restored=Rhythm(self.tmp.name)
        self.assertTrue(restored.healthy)
        self.assertEqual(restored.state['awake_since'],restored.state['mode_since'])

    def test_real_observation_changes_energy_and_preferences(self):
        before=self.r.state['energy']
        self.r.observe(self.event(),['刷机'],'interested','下次看看刷机话题')
        self.assertLess(self.r.state['energy'],before)
        item=self.r.state['interests']['刷机']
        self.assertGreater(item['score'],0)
        self.assertEqual(item['sources'],['phone:test'])
        self.assertEqual(self.r.summary()['interest_observations'][0]['status'],'初步印象')
        self.assertEqual(self.r.state['pending'][0]['source'],'phone:test')

    def test_one_day_cannot_rewrite_wake_habit(self):
        anchor=self.r.state['wake_anchor']
        for n in range(5): self.r.observe(self.event(identifier=str(n)),['刷机'],'interested')
        self.assertEqual(self.r.state['wake_anchor'],anchor)

    def test_three_distinct_days_learn_gradually(self):
        anchor=self.r.state['wake_anchor']
        for n in range(3):
            at=NOW+n*86400
            with patch('time.time',return_value=at):
                self.r.observe(self.event(at,str(n)),['刷机'],'interested')
        self.assertNotEqual(self.r.state['wake_anchor'],anchor)
        self.assertLessEqual(abs(self.r.state['wake_anchor']-anchor),15)
        current=self.r.state['wake_anchor']
        with patch('time.time',return_value=NOW+2*86400):
            self.r.observe(self.event(NOW+2*86400,'another'),['刷机'],'interested')
        self.assertEqual(self.r.state['wake_anchor'],current)
        self.assertEqual(self.r.summary()['interest_observations'][0]['status'],'逐渐稳定')

    def test_owner_forced_timing_does_not_train_own_clock(self):
        self.r.observe(self.event(),['刷机'],'interested',learn_habit=False)
        self.assertEqual(self.r.state['habits'],[])

    def test_more_satiated_state_waits_longer(self):
        self.r.state.update(energy=.9,curiosity=.9)
        self.r.schedule()
        earlier=self.r.state['next_decision']
        self.r.state.update(energy=.3,curiosity=.1)
        self.r.schedule()
        self.assertGreater(self.r.state['next_decision'],earlier)

    def test_model_delay_and_daily_decision_cost_are_bounded(self):
        self.r.schedule(float('inf'))
        self.assertLessEqual(self.r.state['next_decision']-NOW,240*60)
        self.r.state['next_decision']=NOW
        for _ in range(8): self.r.reserve_decision(NOW)
        self.assertFalse(self.r.available(NOW,True))

    def test_diary_requires_real_source_and_is_optional_deduplicated(self):
        event=self.event()
        self.assertFalse(self.r.write_diary('凭空编一件事','invented',[event]))
        self.assertTrue(self.r.write_diary('这个帖子我想留着','phone:test',[event]))
        self.assertFalse(self.r.write_diary('又写同一件事','phone:test',[event]))
        self.assertEqual(len(self.r.diary_rows()),1)
        self.assertFalse(self.r.write_diary('验证码123456','phone:test2',[self.event(identifier='phone:test2')]))

    def test_long_downtime_never_replays_activities(self):
        self.r.state.update(mode='browsing',mode_since=NOW-500)
        self.r.advance(NOW+30*86400)
        self.assertNotEqual(self.r.state['mode'],'browsing')
        self.assertIsNone(self.r.state['last_activity'])
        self.assertFalse(self.r.journal.exists())

    def test_source_excerpt_survives_audit_rotation_and_deletion_is_scoped(self):
        source=self.event()
        self.assertTrue(self.r.write_diary('留着以后看',source['id'],[source]))
        row=self.r.diary_rows()[0]
        self.assertEqual(row['source_note'],source['note'])
        self.assertFalse(self.r.delete_diary('not-a-life-id'))
        self.assertTrue(self.r.delete_diary(row['id']))
        self.assertEqual(self.r.diary_rows(),[])

    def test_invalid_interest_type_does_not_corrupt_observation(self):
        self.r.observe(self.event(),['刷机'],{'invalid':'type'})
        self.assertEqual(self.r.state['interests']['刷机']['score'],0)


class RhythmFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock=patch('time.time',return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.config={'enabled':True,'autonomous_enabled':True,'autonomous_screen_consent':True,
            'rhythm_enabled':True,'autonomous_apps':[COOL],'autonomous_daily_limit':3,'autonomous_max_views':2}
        self.phone=SimpleNamespace(root=Path(self.tmp.name),state={'paused':False},
            status=AsyncMock(return_value='online'),key=AsyncMock(),open_app=AsyncMock(),
            _shell=AsyncMock(return_value='showing=false secure=false'),
            screenshot=AsyncMock(return_value=(b'jpg',(1080,2376),(818,1800))),swipe=AsyncMock(),ready=lambda:None)
        self.model=AsyncMock()
        self.a=AutonomousBrowser(self.phone,self.config,self.model,lambda:'')
        self.a.state.update(last_user_at=NOW-901)
        self.a.rhythm.state['next_decision']=NOW-1

    async def test_rest_decision_needs_no_phone_connection(self):
        self.phone.status.side_effect=RuntimeError('must not query')
        self.model.return_value='{"activity":"rest","rest_minutes":40}'
        await self.a.consider()
        self.assertEqual(self.a.state['last_result'],'resting')
        self.phone.status.assert_not_called()
        self.phone.key.assert_not_called()

    async def test_own_clock_can_be_awake_outside_old_fixed_window(self):
        late=datetime(2026,10,20,23,30,tzinfo=TZ).timestamp()
        self.a.rhythm.state.update(mode='awake',updated_at=late,next_decision=late-1)
        self.a.state['last_user_at']=late-901
        self.assertTrue(self.a.due(late))
        self.a.rhythm.state.update(mode='asleep',until=late+8*3600)
        self.assertFalse(self.a.due(late))

    async def test_asleep_model_is_not_called_but_owner_chat_is_observed(self):
        self.a.rhythm.state.update(mode='asleep',until=NOW+8*3600)
        await self.a.consider()
        self.model.assert_not_called()
        await self.a.interrupt_for_user()
        self.assertEqual(self.a.state['last_user_at'],NOW)
        self.assertEqual(self.a.rhythm.state['mode'],'asleep')

    async def test_completed_visit_updates_state_and_optional_diary(self):
        self.model.side_effect=['{"activity":"browse","app":"com.coolapk.market"}',
            '{"page_type":"content","action":"stop","note":"刷机的话题想接着看","topics":["刷机"],"interest":"interested","continue_topic":"刷机后续","remember":true}']
        with patch('asyncio.sleep',AsyncMock()): await self.a.consider()
        self.assertEqual(self.a.state['last_result'],'viewed')
        self.assertEqual(self.a.rhythm.state['mode'],'awake')
        self.assertIn('刷机',self.a.rhythm.state['interests'])
        diary=self.a.rhythm.diary_rows()
        self.assertEqual(diary[0]['source_id'],self.a.recent()[0]['id'])
        self.assertIn('自己的经历日记',self.a.context())

    async def test_unlimited_visits_allow_rhythm_browse_after_many_visits(self):
        self.config['autonomous_daily_limit']=0
        self.a.state.update(day='2026-10-20',used=1000)
        self.model.side_effect=['{"activity":"browse","app":"com.coolapk.market"}',
            '{"page_type":"content","action":"stop","note":"继续阅读","topics":[],"remember":false}']
        with patch('asyncio.sleep',AsyncMock()): await self.a.consider()
        self.assertEqual(self.a.state['used'],1001)
        self.assertEqual(self.a.state['last_result'],'viewed')

    async def test_owner_requested_rhythm_visit_exempt_at_daily_limit(self):
        self.config['autonomous_daily_limit']=10
        self.a.state.update(day='2026-10-20',used=10)
        self.model.return_value='{"activity":"browse","app":"com.coolapk.market"}'
        self.a._browse=AsyncMock()
        await self.a.consider(True)
        self.a._browse.assert_awaited_once()
        self.assertEqual(self.a.state['used'],10)
        self.assertEqual(self.a.state['requested_used'],1)
        self.assertEqual(self.a.rhythm.state['decisions'],0)

    async def test_budget_blocks_browse_but_allows_own_reflection(self):
        self.a.state.update(day='2026-10-20',used=3)
        self.model.return_value='{"activity":"browse","app":"com.coolapk.market"}'
        await self.a.consider()
        self.phone.open_app.assert_not_called()
        self.assertEqual(self.a.state['last_result'],'daily_limit')
        self.a.rhythm.state['next_decision']=NOW-1
        self.model.return_value='{"activity":"rest"}'
        await self.a.consider()
        self.assertEqual(self.a.state['last_result'],'resting')

    async def test_api_failure_keeps_next_check_in_future(self):
        self.model.side_effect=RuntimeError('simulated')
        await self.a.consider()
        self.assertGreater(self.a.rhythm.state['next_decision'],NOW)
        self.assertEqual(self.a.rhythm.state['decisions'],1)

    async def test_no_experience_does_not_generate_fictional_diary(self):
        self.model.return_value='{"activity":"reflect","source_id":"imaginary","diary":"今天出门玩了"}'
        await self.a.consider()
        self.assertEqual(self.a.rhythm.diary_rows(),[])
        self.phone.open_app.assert_not_called()

    async def test_loading_screen_never_trains_interests_or_diary(self):
        self.model.side_effect=['{"activity":"browse","app":"com.coolapk.market"}',
            '{"page_type":"loading","action":"stop","note":"还在启动页","topics":["启动动画"],"interest":"interested","remember":true}']
        with patch('asyncio.sleep',AsyncMock()): await self.a.consider()
        self.assertEqual(self.a.state['last_result'],'no_content')
        self.assertEqual(self.a.state['last_views'],0)
        self.assertEqual(self.a.rhythm.state['interests'],{})
        self.assertEqual(self.a.rhythm.diary_rows(),[])

    async def test_loading_can_wait_then_learn_from_real_content(self):
        self.model.side_effect=['{"activity":"browse","app":"com.coolapk.market"}',
            '{"page_type":"loading","action":"wait","note":"等一下加载"}',
            '{"page_type":"content","action":"stop","note":"这个帖子讲刷机","topics":["刷机"],"interest":"interested"}']
        with patch('asyncio.sleep',AsyncMock()): await self.a.consider()
        self.assertEqual(self.a.state['last_views'],1)
        self.assertEqual(self.a.state['last_observations'],2)
        self.assertEqual(self.a.recent()[-1]['note'],'这个帖子讲刷机')
        self.assertIn('刷机',self.a.rhythm.state['interests'])


if __name__=='__main__': unittest.main(verbosity=2)
