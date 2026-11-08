"""Synthetic UI trees and mocked device calls; no real screenshot or input."""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get('COMPANION_TEST_PLUGINS', str(Path(__file__).parent)))
from PIL import Image
from astrbot_plugin_companion_phone.ui import parse_ui, visible_nodes, describe_ui
from astrbot_plugin_companion_phone.phone import Phone, PhoneError
from astrbot_plugin_companion_phone.access import enabled_apps,PUBLIC_APPS,scope_text
from astrbot_plugin_companion_phone.intents import browse_request
import test_rhythm as flows

PKG = 'com.coolapk.market'


def tree(text='搜索', bounds='[10,10][90,40]', enabled='true'):
    return ('<hierarchy><node package="'+PKG+'" class="android.widget.FrameLayout" bounds="[0,0][100,200]">'
            '<node package="'+PKG+'" text="'+text+'" resource-id="search" class="android.widget.Button" '
            'clickable="true" enabled="'+enabled+'" bounds="'+bounds+'"/>'
            '<node package="'+PKG+'" class="android.widget.EditText" resource-id="search_input" '
            'clickable="true" focused="true" bounds="[10,50][90,85]"/>'
            '</node></hierarchy>')


class ParserTests(unittest.TestCase):
    def test_douyin_requests_are_recognized_but_not_negations_or_ordinary_chat(self):
        for text in ['去抖音看看','现在去抖音刷一会儿','去抖音刷刷','打开抖音搜索鲸鱼']:
            self.assertTrue(browse_request(text),text)
        for text in ['不要去刷抖音','抖音好玩吗','我刚刷过抖音','你刷抖音了吗','现在刷机']:
            self.assertFalse(browse_request(text),text)

    def test_douyin_is_enabled_only_when_explicitly_configured(self):
        cfg={'autonomous_apps':['com.ss.android.ugc.aweme'],
             'installed_app_names':{'com.ss.android.ugc.aweme':'抖音'}}
        self.assertEqual(enabled_apps(cfg),{'com.ss.android.ugc.aweme':'抖音'})
        self.assertNotIn('抖音',PUBLIC_APPS)
        self.assertIn('消费行为均禁止',scope_text(cfg))
        cfg['autonomous_apps']=[]
        self.assertEqual(enabled_apps(cfg),{})

    def test_geometry_flags_and_bounded_prompt(self):
        parsed = parse_ui(tree(), PKG, 100, 200)
        shown, targets = visible_nodes(parsed, 'test')
        self.assertEqual(targets[shown[0]['node_id']]['_point'], (49, 24))
        self.assertTrue(shown[1]['editable'])
        self.assertEqual(shown[0]['bounds'], [101, 50, 899, 196])
        self.assertLessEqual(len(json.dumps(shown, ensure_ascii=False, separators=(',', ':')).encode()), 4500)
        self.assertNotIn('_attrs', describe_ui({'available': True, 'nodes': shown}))

    def test_password_subtree_foreign_hidden_and_offscreen_nodes_excluded(self):
        xml = ('<hierarchy><node package="'+PKG+'" password="true" bounds="[0,0][90,90]" text="SECRET">'
               '<node text="HIDDEN_CHILD" bounds="[0,0][40,40]"/></node>'
               '<node package="com.other.app" text="FOREIGN" bounds="[0,0][90,90]"/>'
               '<node package="'+PKG+'" visible-to-user="false" text="INVISIBLE" bounds="[0,0][90,90]"/>'
               '<node package="'+PKG+'" text="OFFSCREEN" bounds="[0,300][90,400]"/></hierarchy>')
        self.assertEqual(parse_ui(xml, PKG, 100, 200)['nodes'], [])

    def test_disabled_parent_disables_child(self):
        xml = tree().replace('class="android.widget.FrameLayout"', 'enabled="false" class="android.widget.FrameLayout"')
        self.assertTrue(all(not n['enabled'] for n in parse_ui(xml, PKG, 100, 200)['nodes']))

    def test_bad_xml_entities_and_oversized_documents_rejected(self):
        for xml in ('broken', '<node/>', '<!DOCTYPE hierarchy><hierarchy/>', 'x'*(1024*1024+1)):
            with self.subTest(size=len(xml)), self.assertRaises(ValueError):
                parse_ui(xml, PKG, 100, 200)

    def test_signature_changes_when_target_or_layout_changes(self):
        before = parse_ui(tree(), PKG, 100, 200)['fingerprint']
        for xml in (tree('支付'), tree(bounds='[20,10][95,40]'), tree(enabled='false')):
            self.assertNotEqual(parse_ui(xml, PKG, 100, 200)['fingerprint'], before)


class PhoneNodeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = patch('time.monotonic', return_value=100)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.phone = Phone(self.tmp.name)
        self.phone.screen = (100, 200, 100)
        self.phone.foreground_app = AsyncMock(return_value=PKG)
        self.phone._assert_foreground = AsyncMock()
        self.phone._shell = AsyncMock(return_value='')
        self.phone._read_ui = AsyncMock(return_value=parse_ui(tree(), PKG, 100, 200))

    async def observe(self):
        observation = await self.phone.ui_observation(frozenset([PKG]))
        return observation['nodes'][0]['node_id']

    async def test_tap_revalidates_then_uses_actual_physical_center(self):
        identifier = await self.observe()
        self.assertEqual(self.phone.node_target(identifier)['text'], '搜索')
        receipt = await self.phone.tap_node(identifier, frozenset([PKG]))
        self.phone._shell.assert_awaited_once_with('input', 'tap', 49, 24)
        self.assertEqual(self.phone._read_ui.await_count, 2)
        self.assertIn('不能据此声称任务已完成', receipt)
        self.assertIsNone(self.phone.screen)
        self.assertIsNone(self.phone.ui)

    async def test_changed_tree_blocks_without_input_or_action_reservation(self):
        identifier = await self.observe()
        self.phone._read_ui.return_value = parse_ui(tree('支付'), PKG, 100, 200)
        with self.assertRaisesRegex(PhoneError, '已变化'):
            await self.phone.tap_node(identifier)
        self.phone._shell.assert_not_called()
        self.assertEqual(len(self.phone.actions), 0)

    async def test_unknown_handle_and_stale_screen_cannot_click(self):
        identifier = await self.observe()
        with self.assertRaises(PhoneError):
            await self.phone.tap_node('node:invented:0')
        with patch('time.monotonic', return_value=131):
            with self.assertRaises(PhoneError):
                await self.phone.tap_node(identifier)
        self.phone._shell.assert_not_called()

    async def test_disabled_control_is_not_actionable(self):
        self.phone._read_ui.return_value = parse_ui(tree(enabled='false'), PKG, 100, 200)
        observation = await self.phone.ui_observation()
        target = next(n for n in observation['nodes'] if n['text'] == '搜索')
        with self.assertRaises(PhoneError):
            await self.phone.tap_node(target['node_id'])
        self.phone._shell.assert_not_called()

    async def test_consumption_controls_cannot_use_node_tap(self):
        self.phone._read_ui.return_value = parse_ui(tree('立即订阅'), PKG, 100, 200)
        identifier = await self.observe()
        with self.assertRaisesRegex(PhoneError, '不在授权范围'):
            await self.phone.tap_node(identifier)
        self.phone._shell.assert_not_called()

    async def test_unlabelled_clickable_parent_keeps_child_consumption_restriction(self):
        xml = ('<hierarchy><node package="'+PKG+'" class="android.widget.Button" clickable="true" bounds="[10,10][90,40]">'
               '<node text="支付" class="android.widget.TextView" bounds="[20,15][80,30]"/></node></hierarchy>')
        self.phone._read_ui.return_value = parse_ui(xml, PKG, 100, 200)
        identifier = await self.observe()
        with self.assertRaisesRegex(PhoneError, '不在授权范围'):
            await self.phone.tap_node(identifier)
        self.phone._shell.assert_not_called()

    async def test_paused_and_revoked_package_block_input(self):
        identifier = await self.observe()
        with self.assertRaises(PhoneError):
            await self.phone.tap_node(identifier, frozenset(['com.other.app']))
        self.phone.pause(True)
        with self.assertRaises(PhoneError):
            await self.phone.tap_node(identifier)
        self.phone._shell.assert_not_called()

    async def test_parse_failure_falls_back_without_inventing_nodes(self):
        self.phone._read_ui.side_effect = ValueError('not an XML hierarchy')
        self.assertFalse((await self.phone.ui_observation())['available'])
        self.assertIsNone(self.phone.ui)

    async def test_foreground_change_after_dump_aborts(self):
        self.phone._assert_foreground.side_effect = [None, PhoneError('left allowed app')]
        with self.assertRaises(PhoneError):
            await self.phone.ui_observation(frozenset([PKG]))
        self.assertIsNone(self.phone.ui)
        self.phone._shell.assert_not_called()

    async def test_new_screenshot_invalidates_handles_even_with_same_clock(self):
        identifier = await self.observe()
        output = io.BytesIO()
        Image.new('RGB', (100, 200)).save(output, format='PNG')
        self.phone._connect = AsyncMock()
        self.phone._wake_for_capture = AsyncMock()
        self.phone._run = AsyncMock(return_value=output.getvalue())
        await self.phone.screenshot()
        with self.assertRaises(PhoneError):
            self.phone.node_target(identifier)

    async def test_ui_dump_cleanup_happens_on_malformed_xml(self):
        del self.phone._read_ui
        self.phone._shell.side_effect = ['dump complete', 'malformed', '']
        self.assertFalse((await self.phone.ui_observation())['available'])
        self.assertEqual(self.phone._shell.await_args_list[-1].args[:2], ('rm', '-f'))


class NavigationFlowTests(unittest.IsolatedAsyncioTestCase):
    setUp = flows.RhythmFlowTests.setUp

    def prepare_ui(self, fingerprints):
        self.config['autonomous_interactive'] = True
        self.phone._assert_foreground = AsyncMock()
        self.phone.ui_observation = AsyncMock(side_effect=[
            {'available': bool(f), 'fingerprint': f, 'nodes': [{'node_id': 'node:current:0', 'text': '搜索'}] if f else []}
            for f in fingerprints])
        self.phone.node_target = lambda identifier: {'text': '搜索', 'enabled': 'true'}
        self.phone.tap_node = AsyncMock()
        self.phone.target_info = AsyncMock()

    async def test_model_can_select_current_node_with_no_coordinate_guess(self):
        self.prepare_ui(['a', 'b'])
        self.model.side_effect = [json.dumps({'activity': 'browse', 'app': PKG}),
            json.dumps({'page_type': 'content', 'action': 'tap', 'intent': 'navigate', 'node_id': 'node:current:0'}),
            json.dumps({'page_type': 'content', 'action': 'stop'})]
        with patch('asyncio.sleep', AsyncMock()):
            await self.a._consider_session()
        self.phone.tap_node.assert_awaited_once_with('node:current:0', allowed_packages=frozenset([PKG]))
        self.phone.target_info.assert_not_called()
        self.assertIn('node:current:0', self.model.await_args_list[1].args[0])
        self.assertEqual(self.a.state['last_navigation_check']['result'], 'changed')

    async def test_two_unchanged_navigation_results_stop_without_third_attempt(self):
        self.config['autonomous_max_views'] = 0
        self.prepare_ui(['a', 'a', 'a'])
        tap = json.dumps({'page_type': 'content', 'action': 'tap', 'intent': 'navigate', 'node_id': 'node:current:0'})
        self.model.side_effect = [json.dumps({'activity': 'browse', 'app': PKG}), tap, tap]
        with patch('asyncio.sleep', AsyncMock()):
            await self.a._consider_session()
        self.assertEqual(self.phone.tap_node.await_count, 2)
        self.assertEqual(self.model.await_count, 3)
        self.assertEqual(self.a.state['last_result'], 'navigation_stalled')

    async def test_missing_ui_is_unknown_rather_than_unchanged(self):
        self.prepare_ui(['a', None])
        self.model.side_effect = [json.dumps({'activity': 'browse', 'app': PKG}),
            json.dumps({'page_type': 'content', 'action': 'tap', 'intent': 'navigate', 'node_id': 'node:current:0'}),
            json.dumps({'page_type': 'content', 'action': 'stop'})]
        with patch('asyncio.sleep', AsyncMock()):
            await self.a._consider_session()
        self.assertEqual(self.a.state['last_navigation_check']['result'], 'unavailable')
        self.assertEqual(self.a.state['last_result'], 'viewed')

    async def test_changed_image_with_same_controls_is_not_called_stuck(self):
        self.prepare_ui(['a', 'a'])
        self.phone.screenshot.side_effect = [(b'first image', (100, 200), (100, 200)),
                                             (b'second image', (100, 200), (100, 200))]
        self.model.side_effect = [json.dumps({'activity': 'browse', 'app': PKG}),
            json.dumps({'page_type': 'content', 'action': 'tap', 'intent': 'navigate', 'node_id': 'node:current:0'}),
            json.dumps({'page_type': 'content', 'action': 'stop'})]
        with patch('asyncio.sleep', AsyncMock()):
            await self.a._consider_session()
        self.assertEqual(self.a.state['last_navigation_check']['result'], 'changed')

    async def test_node_reference_keeps_outbound_action_restrictions(self):
        self.prepare_ui([])
        for label in ['支付','立即订阅','打赏','续费','下单','发送','删除']:
            self.phone.node_target = lambda identifier: {'text': label}
            self.assertFalse(await self.a.interactive_action(PKG,
                {'action': 'tap', 'intent': 'navigate', 'node_id': 'node:current:0'}))
        self.phone.tap_node.assert_not_called()

    async def test_via_native_address_bar_allowed_but_web_form_rejected(self):
        self.phone._assert_foreground = AsyncMock()
        self.phone.type_ascii = AsyncMock()
        self.phone._input_field = AsyncMock(return_value={'resource-id': 'mark.via:id/url', 'text': '地址'})
        self.assertTrue(await self.a.interactive_action('mark.via', {'action': 'input', 'text': '鲸类声音'}))
        self.phone._input_field.return_value = {'resource-id': 'url', 'text': '网址'}
        self.assertFalse(await self.a.interactive_action('mark.via', {'action': 'input', 'text': '鲸类声音'}))
        self.phone._input_field.return_value = {'resource-id': 'comment_edit', 'text': '说点什么'}
        self.assertFalse(await self.a.interactive_action(PKG, {'action': 'input', 'text': '你好'}))
        self.assertEqual(self.phone.type_ascii.await_count, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
