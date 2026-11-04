"""Tool integration using runtime dependencies, mocked screenshots and inputs."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.environ.get('COMPANION_TEST_PLUGINS', str(Path(__file__).parent)))
import test_phone as base


class UIToolTests(unittest.IsolatedAsyncioTestCase):
    setUp = base.PhoneTests.setUp
    plugin = base.PhoneTests.plugin

    async def test_screenshot_returns_controls_with_existing_screen_consent(self):
        p = self.plugin()
        self.phone.screenshot = AsyncMock(return_value=(b'jpg', (100, 200), (100, 200)))
        self.phone.ui_observation = AsyncMock(return_value={'available': True, 'nodes': [{'node_id': 'node:test:0'}]})
        result = await p.tool_screenshot(base.Event())
        self.assertFalse(result.isError)
        self.assertIn('node:test:0', result.content[0].text)

    async def test_denied_screen_consent_does_not_read_ui(self):
        p = self.plugin()
        p.config['allow_model_screen'] = False
        self.phone.screenshot = AsyncMock()
        self.phone.ui_observation = AsyncMock()
        self.assertTrue((await p.tool_screenshot(base.Event())).isError)
        self.phone.screenshot.assert_not_called()
        self.phone.ui_observation.assert_not_called()

    async def test_other_contact_is_rejected_before_observation(self):
        p = self.plugin()
        self.phone.screenshot = AsyncMock()
        self.phone.ui_observation = AsyncMock()
        self.assertTrue((await p.tool_screenshot(base.Event('1'))).isError)
        self.phone.screenshot.assert_not_called()
        self.phone.ui_observation.assert_not_called()

    async def test_existing_tap_tool_accepts_node_and_preserves_action_counter(self):
        p = self.plugin()
        self.phone.tap_node = AsyncMock(return_value='执行后需要观察')
        self.phone.tap = AsyncMock()
        event = base.Event()
        self.assertFalse((await p.tool_tap(event, node_id='node:test:0')).isError)
        self.phone.tap_node.assert_awaited_once_with('node:test:0')
        self.phone.tap.assert_not_called()
        self.assertEqual(event.get_extra('companion_phone_actions'), 1)

    async def test_sleep_still_blocks_node_tap(self):
        p = self.plugin()
        p.autonomy = SimpleNamespace(rhythm=SimpleNamespace(advance=lambda: None, state={'mode': 'asleep'}))
        self.phone.tap_node = AsyncMock()
        self.assertTrue((await p.tool_tap(base.Event(), node_id='node:test:0')).isError)
        self.phone.tap_node.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
