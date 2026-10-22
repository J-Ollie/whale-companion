"""Test outgoing QQ formatting without model calls or real sends."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_active.chat_style import clean_chat_text,clean_event_reply
from astrbot.api.message_components import Plain,Image
from astrbot.core.message.message_event_result import MessageEventResult,ResultContentType

OWNER='qq-companion:FriendMessage:100000001'


class ChatStyleTests(unittest.TestCase):
    def test_remove_period_at_each_line_end(self):
        self.assertEqual(clean_chat_text('来了。\n知道了。  \n好。'),'来了\n知道了  \n好')

    def test_keep_questions_ellipsis_decimal_and_url(self):
        text='你呢？\n行！\n……\n嗯...\n版本1.2\nhttps://example.com/a.html'
        self.assertEqual(clean_chat_text(text),text)

    def test_keep_fenced_indented_code_and_quotes(self):
        text='```python\n# 保留。\nprint("你好。")\n```\n> 原话。\n    # 注释。\n“原话。”\n好。'
        self.assertEqual(clean_chat_text(text),text.removesuffix('。'))

    def test_actual_model_message_chain_only_changes_plain(self):
        image=Image.fromURL('https://example.com/image.png')
        result=MessageEventResult(chain=[Plain('嗯。'),image],result_content_type=ResultContentType.LLM_RESULT)
        event=SimpleNamespace(unified_msg_origin=OWNER,get_result=lambda:result)
        clean_event_reply(event,OWNER)
        self.assertEqual(result.chain[0].text,'嗯')
        self.assertIs(result.chain[1],image)

    def test_other_users_and_command_results_preserved(self):
        for umo,kind in [('other',ResultContentType.LLM_RESULT),(OWNER,ResultContentType.GENERAL_RESULT)]:
            result=MessageEventResult(chain=[Plain('检查完成。')],result_content_type=kind)
            event=SimpleNamespace(unified_msg_origin=umo,get_result=lambda:result)
            clean_event_reply(event,OWNER)
            self.assertEqual(result.chain[0].text,'检查完成。')
