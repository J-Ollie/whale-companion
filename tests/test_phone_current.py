"""Current device/owner guards; old Qzone wrappers were removed before this change."""
import unittest
import test_phone as historical
from astrbot_plugin_companion_phone.phone import TOOL_NAMES

class CurrentPhoneTests(unittest.IsolatedAsyncioTestCase):
    setUp=historical.PhoneTests.setUp
    plugin=historical.PhoneTests.plugin
    def test_current_tools_have_no_shell_or_file_access(self):
        self.assertEqual(len(TOOL_NAMES),10)
        self.assertIn('companion_chat_media',TOOL_NAMES)
        self.assertFalse(any('shell' in name or 'file' in name for name in TOOL_NAMES))

EXCLUDED={'test_space_reason_keeps_quota_and_uncertainty', 'test_space_requires_own_draft_not_another_generator', 'test_space_exception_after_rejection_does_not_enable_blind_retry', 'test_standing_initiative_still_rejects_other_owner_and_stop', 'test_standing_initiative_needs_no_publish_phrase', 'test_space_image_must_be_previewed_and_exact_id_is_forwarded', 'test_tool_set_has_no_shell_or_file_access', 'test_space_continuation_has_bounded_owner_context', 'test_space_tool_once_per_turn_and_owner_only', 'test_space_correction_budget_and_uncertain_send_are_bounded', 'test_space_rejected_draft_can_be_corrected_but_not_repeated', 'test_space_delegation_routes_without_phone_task'}
for name,value in historical.PhoneTests.__dict__.items():
    if name.startswith('test_') and name not in EXCLUDED:setattr(CurrentPhoneTests,name,value)
