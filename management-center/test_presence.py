import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from archive import Archive
from presence import PublicPresence, project, validate, rest_window


class PresenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = Archive(self.root)
        self.archive.phone.mkdir(parents=True)
        self.path = self.archive.phone / 'rhythm.json'
        self.rhythm = {'mode': 'awake', 'updated_at': time.time(), 'wake_anchor': 525,
                       'sleep_hours': 8, 'energy': .8, 'until': 0}
        self.save()
        self.presence = PublicPresence(self.root, self.archive)
        self.content = {'words': {'idle': '嗯，先发一会儿呆。', 'browsing': '想随便翻翻。',
                        'chatting': '刚聊了几句，慢慢说。', 'resting': '我先歇一下，回来再看。',
                        'asleep': '先睡啦，醒了再读消息。'}, 'windows': {
                        'entertainment': [{'start': '12:20', 'end': '13:00'}],
                        'social': [{'start': '19:00', 'end': '19:40'}],
                        'alone': [{'start': '22:10', 'end': '23:00'}]}}

    def save(self):
        self.path.write_text(json.dumps(self.rhythm))

    def test_tendencies_are_ranges_and_cross_midnight_is_explicit(self):
        self.assertEqual(rest_window(525, 8), {'start': '00:45', 'end': '08:45'})
        self.assertEqual(rest_window(420, 8), {'start': '23:00', 'end': '07:00'})
        self.assertIsNone(rest_window(None, 8))
        clean = validate(self.content, 525, 8)
        clean['written_at'] = time.time()
        value = project(clean, 'idle', 525, 8, time.time())
        self.assertEqual(value['source'], 'self_written')
        self.assertEqual(len(value['periods']), 4)
        self.assertEqual(value['periods'][1]['windows'], self.content['windows']['entertainment'])

    def test_waking_generates_once_and_sleep_shows_prepared_note(self):
        with patch.object(self.presence, 'generate', return_value=self.content) as model:
            original = self.path.read_bytes()
            self.assertTrue(self.presence.tick())
            self.assertFalse(self.presence.tick())
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(model.call_count, 1)
        self.rhythm.update(mode='asleep', until=time.time()+3600)
        self.save()
        data = self.archive.snapshot(guest=True)
        self.assertTrue(data['current']['is_leave_note'])
        self.assertEqual(data['current']['note'], self.content['words']['asleep'])
        self.assertNotIn('activity', data)

    def test_sleep_rest_and_stale_never_call_model_even_with_recent_reply(self):
        for mode in ('asleep', 'resting'):
            self.rhythm.update(mode=mode, until=time.time()+3600)
            self.save()
            with patch.object(self.archive, 'last_chat', return_value=time.time()), patch.object(self.presence, 'generate') as model:
                self.assertFalse(self.presence.tick())
                model.assert_not_called()
        self.rhythm.update(mode='awake', updated_at=time.time()-1000)
        self.save()
        with patch.object(self.presence, 'generate') as model:
            self.assertFalse(self.presence.tick())
            model.assert_not_called()

    def test_sleep_transition_during_request_does_not_publish(self):
        def transition(snapshot):
            self.rhythm.update(mode='resting', until=time.time()+900)
            self.save()
            return self.content
        with patch.object(self.presence, 'generate', side_effect=transition):
            self.assertFalse(self.presence.tick())
        self.assertFalse(self.presence.path.exists())

    def test_network_failure_is_bounded_and_does_not_create_fake_words(self):
        with patch.object(self.presence, 'generate', side_effect=RuntimeError('SECRET')) as model:
            self.assertFalse(self.presence.tick())
            self.assertFalse(self.presence.tick())
            self.assertEqual(model.call_count, 1)
        data = self.archive.snapshot(True)
        self.assertEqual(data['current']['note'], '')
        self.assertNotIn('SECRET', json.dumps(data))
        self.assertEqual(data['routine']['periods'][1]['windows'], [])

    def test_invalid_or_expired_model_output_is_not_public(self):
        self.content['written_at'] = time.time()
        for bad in ('密码 SECRET', 'https://evil.example', 'QQ号12345678', '<script>'*20):
            self.content['words']['idle'] = bad
            self.assertEqual(project(self.content, 'idle', 525, 8, time.time())['source'], 'pending')
        self.content['words']['idle'] = '先安静一下。'
        self.content['written_at'] = time.time()-25*3600
        self.assertEqual(project(self.content, 'asleep', 525, 8, time.time())['quote'], '')

    def test_invalid_ranges_are_never_published(self):
        for start, end in [('01:30','02:00'), ('25:00','26:00'), ('20:00','20:00'), ('23:30','01:30')]:
            self.content['windows']['social'] = [{'start':start,'end':end}]
            with self.assertRaises(ValueError):
                validate(self.content, 525, 8)

    def test_generation_prompt_contains_only_public_snapshot(self):
        payload = {'choices':[{'message':{'content':json.dumps(self.content, ensure_ascii=False)}}]}
        from unittest.mock import Mock
        response = Mock()
        response.__enter__ = Mock(return_value=Mock(read=Mock(return_value=json.dumps(payload).encode())))
        response.__exit__ = Mock(return_value=False)
        opener = Mock();opener.open.return_value = response
        with patch.object(self.presence, 'credentials', return_value=('TEST_SECRET', 'deepseek-flash')), patch('presence.urllib.request.build_opener',return_value=opener):
            self.assertEqual(self.presence.generate(self.archive.snapshot(True)),self.content)
        request = opener.open.call_args.args[0]
        body = json.loads(request.data)
        context = json.loads(body['messages'][1]['content'])
        self.assertEqual(set(context),{'name','traits','state','rest','time'})
        self.assertEqual(request.full_url,'https://api.deepseek.com/chat/completions')
        self.assertNotIn('TEST_SECRET', request.data.decode())


if __name__ == '__main__':
    unittest.main()
