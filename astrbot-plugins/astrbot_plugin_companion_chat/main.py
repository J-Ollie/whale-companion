"""One registered Star, explicit hooks, four private service modules."""
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Star
from astrbot.core.agent.tool import ToolSet
from astrbot.core.provider.register import llm_tools
from astrbot.core.agent.message import TextPart
from mcp.types import CallToolResult, TextContent

from .active.main import CompanionActive
from .batch.main import CompanionBatch
from .breeze.main import CompanionBreeze
from .friends.main import CompanionFriends
from .context_material import MaterialContext
from .followup import Followups
from .context_guard import MARKER, install as install_context_guard
from .deliberation import REQUEST_MARKER, install as install_deliberation
from .reply_state import ReplyState, KEY as REPLY_STATE_KEY, LABELS, REASONS
from .lifecycle import (ComponentContext, SectionConfig, bind_live_consumers, gate_for, outreach_busy,
                        pipeline_tasks, write_status)


class CompanionChat(Star):
    def __init__(self, context, config):
        super().__init__(context)
        self.config = config
        self.ready = False
        self.owns_services = False
        self.monitor = None
        self.gate = None
        self.restore_context_guard = None
        self.restore_deliberation = None
        self.staged = bool(config.get('maintenance_mode', False))
        old = context.get_registered_star('astrbot_plugin_companion_friends')
        self.legacy = old.star_cls if old and old.activated else None
        if self.legacy:
            if not self.staged:
                raise RuntimeError('Disable legacy chat plugins before activating unified chat')
            self.friends = self.legacy
            meta = context.get_registered_star('astrbot_plugin_companion_active')
            self.active = meta.star_cls if meta and meta.activated else None
            self.batch = self.breeze = None
        else:
            # An independently active legacy scheduler must never run alongside ours.
            for suffix in ('active', 'batch'):
                meta = context.get_registered_star('astrbot_plugin_companion_' + suffix)
                if meta and meta.activated:
                    raise RuntimeError('Legacy chat plugin still active: ' + suffix)
            proxy = ComponentContext(context, self)
            self.friends = CompanionFriends(proxy, SectionConfig(config, 'friends'))
            self.active = CompanionActive(proxy, SectionConfig(config, 'active'))
            self.batch = CompanionBatch(proxy, SectionConfig(config, 'batch'))
            self.breeze = CompanionBreeze(proxy, SectionConfig(config, 'breeze'))
            self.owns_services = True
            self.materials = MaterialContext(self.friends)
            self.followups = Followups(self.friends)
            self.batch.followups = self.followups
            self.reply_state = ReplyState(self.friends.policy.root.parent/'companion_chat')
            self.batch.reply_state = self.friends.reply_state = self.reply_state

    def external_breeze(self):
        meta = self.context.get_registered_star('astrbot_plugin_companion_breeze')
        return meta.star_cls if meta and meta.activated else None

    def voice_busy(self):
        external = self.external_breeze()
        return bool(getattr(external, 'busy', False) or self.breeze and self.breeze.busy)

    async def initialize(self):
        self.gate = gate_for(self.context, self.friends, self.legacy)
        self.friends.draining = True
        if self.legacy:
            # Suspend new autonomous decisions in memory only; preserve disk settings.
            if self.active:
                self.old_active_config = self.active.config
                original = self.active.config
                self.active.config = dict(original) | {'enabled': False}
            self.old_release_unread = self.friends.release_unread

            async def held_unread():
                return None

            self.friends.release_unread = held_unread
        elif not self.staged:
            try:
                self.friends.sleep_inbox.recover_followups()
                await self.friends.initialize()
                await self.active.initialize()
            except BaseException:
                await self.active.terminate()
                await self.friends.terminate()
                raise
            self.friends.draining = False
            self.restore_context_guard = install_context_guard()
            self.restore_deliberation = install_deliberation()
            self.ready = True
            bind_live_consumers(self.context)
            self.gate.holding = False
        self.monitor = asyncio.create_task(self.monitor_status(), name='companion-chat-status')
        self.report_status()
        logger.info('Unified QQ chat initialized; ready=%s', self.ready)

    def report_status(self):
        tasks = pipeline_tasks(self.friends.platform_id)
        write_status({'ready': self.ready, 'holding': self.gate.holding,
                      'pipelines': len(tasks), 'outreach_busy': outreach_busy(self.active),
                      'voice_busy': self.voice_busy(),
                      'batch_pending': len(self.batch.pending) if self.batch else 0,
                      'context_round_guard': self.restore_context_guard is not None,
                      'dialogue_low_reasoning': bool(self.restore_deliberation and self.config.get('dialogue_low_reasoning', True)),
                      'reply_state_tracking': getattr(self, 'reply_state', None) is not None,
                      'voice_performance': getattr(self.breeze, 'performance_version', None)})

    async def monitor_status(self):
        while True:
            await asyncio.sleep(1)
            try:
                self.report_status()
                if self.ready:
                    bind_live_consumers(self.context)
                    await self.gate.drain_requests()
            except OSError:
                logger.warning('Unified chat maintenance status unavailable')

    async def terminate(self):
        if self.gate:
            self.gate.holding = True
        self.friends.draining = True
        if self.owns_services and self.ready:
            prior_config = self.active.config
            # SectionConfig is read-only; replace the view temporarily rather than saving.
            self.active.config = {key: prior_config.get(key, default) for key, default in (
                ('owner_qq', '100000001'), ('platform_id', 'qq-companion'))} | {'enabled': False}
            try:
                for _ in range(45):
                    tasks = pipeline_tasks(self.friends.platform_id)
                    if not tasks and not outreach_busy(self.active) and not self.voice_busy() and not getattr(self.gate, 'request_busy', False):
                        break
                    await asyncio.sleep(1)
                else:
                    raise RuntimeError('Chat still processing; reload deferred without cancelling sends')
            except BaseException:
                self.active.config = prior_config
                self.friends.draining = False
                self.gate.holding = False
                raise
            self.ready = False
            self.active.config = prior_config
            await self.active.terminate()
            await self.batch.terminate()
            await self.friends.terminate()
        if self.restore_context_guard:
            self.restore_context_guard()
            self.restore_context_guard = None
        if self.restore_deliberation:
            self.restore_deliberation()
            self.restore_deliberation = None
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        if self.legacy:
            old = self.context.get_registered_star('astrbot_plugin_companion_friends')
            if old and old.activated:
                # Aborting staging restores the live legacy path and its in-memory view.
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
                if AiocqhttpAdapter.handle_msg is self.gate.handle:
                    AiocqhttpAdapter.handle_msg = self.gate.legacy_handle
                self.friends.draining = False
                self.friends.release_unread = self.old_release_unread
                if self.active and hasattr(self, 'old_active_config'):
                    self.active.config = self.old_active_config
                self.gate.holding = False
        # Intake remains held in Context across reload; the new instance releases it.
        if self.gate:
            self.report_status()
        if getattr(self, 'reply_state', None):
            self.reply_state.close()

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    async def observe(self, event):
        if self.ready:
            if self.batch.allowed(event):
                event.set_extra('companion_pipeline_task', asyncio.current_task())
                if not (event.message_str or '').lstrip().startswith('/') and getattr(self, 'reply_state', None):
                    self.reply_state.begin(event, asyncio.current_task())
            await self.active.observe(event)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=400)
    async def prepare_material(self, event):
        if self.ready and self.batch.allowed(event) and not self.friends.sleeping():
            if not (event.message_str or '').lstrip().startswith('/'):
                await self.materials.prepare(event)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def merge(self, event):
        if self.ready:
            await self.batch.merge(event)

    @filter.on_llm_request(priority=1000000)
    async def friend_persona(self, event, req):
        if self.ready:
            if event.get_extra(REPLY_STATE_KEY) and getattr(self, 'reply_state', None):
                self.reply_state.mark(event, 'generating')
            await self.friends.friend_persona(event, req)

    @filter.on_llm_request()
    async def unread_hint(self, event, req):
        if self.ready:
            await self.friends.unread_hint(event, req)
            await self.active.exploration_feedback(event, req)

    @filter.on_llm_request(priority=-1000000)
    async def isolate_tools(self, event, req):
        if self.ready:
            await self.friends.isolate_tools(event, req)
            if self.batch.allowed(event) and not self.friends.sleeping():
                tools = list(req.func_tool.tools) if req.func_tool else []
                req.func_tool = ToolSet(tools)
                for tool in llm_tools.func_list:
                    if tool.name == 'companion_wait_for_followup' and getattr(tool, 'active', True):
                        req.func_tool.add_tool(tool)

    @filter.on_llm_request(priority=-1390000)
    async def task_truth_hint(self, event, req):
        if self.ready:
            if self.batch.allowed(event) and not self.friends.sleeping():
                event.set_extra(MARKER, True)
                if not (event.message_str or '').lstrip().startswith('/'):
                    setattr(req, REQUEST_MARKER, self.config.get('dialogue_low_reasoning', True) is True)
            await self.batch.task_truth_hint(event, req)
            if getattr(event, 'get_extra', lambda k, default=None: default)(REPLY_STATE_KEY):
                state_hint = self.reply_state.context_hint(event)
                if state_hint:
                    req.extra_user_content_parts.append(TextPart(text=state_hint).mark_as_temp())
                req.extra_user_content_parts.append(TextPart(text=
                    '本轮可以明确选择静默，而非被迫接话：话题已自然结束或只是无需再接的确认时，可仅输出[[silent:closing_ack]]；'
                    '确实暂不想展开普通闲聊时，可仅输出[[silent:pause]]。这是文字/语音格式的明确例外，不再附正文。'
                    '这只是当轮选择，不是睡眠、不屏蔽后续消息，不用来惩罚、吊胃口或索取关注。'
                    '不要用静默掩盖任务失败、漏掉明确待办或应交代的关键结果；故障应如实说明。'
                    '不选静默时仍按正常文字或语音格式回复，不能用空正文表示静默。').mark_as_temp())
            if self.batch.allowed(event) and not self.friends.sleeping():
                self.materials.inject(event, req)

    @filter.llm_tool(name='companion_wait_for_followup')
    async def wait_for_followup(self, event: AstrMessageEvent, seconds: int = 5):
        '''对方说“等一下、还没说完”或明显准备继续补充时，短暂等同一私聊对象接着说，再统一回答；每轮最多一次，不用来假装打字或日常拖延。

        Args:
            seconds(int): 等待秒数，1至8，默认5。
        '''
        try:
            if not self.ready or not self.batch.allowed(event):
                raise ValueError('只接受当前已验证私聊，不查询其他联系人')
            value = await self.followups.wait(event, seconds)
            return CallToolResult(content=[TextContent(type='text', text=json.dumps(value, ensure_ascii=False))])
        except ValueError as exc:
            return CallToolResult(content=[TextContent(type='text', text=str(exc))], isError=True)

    @filter.on_llm_request(priority=-2000000)
    async def sendoff_hint(self, event, req):
        if self.ready:
            await self.batch.sendoff_hint(event, req)

    @filter.on_llm_request(priority=-2100000)
    async def voice_hint(self, event, req):
        if self.ready and not self.external_breeze():
            await self.breeze.voice_hint(event, req)

    @filter.on_llm_response(priority=2000000)
    async def choose_medium(self, event, response):
        if self.ready and not self.external_breeze():
            await self.breeze.choose_medium(event, response)

    @filter.on_llm_response(priority=3000000)
    async def record_reply_decision(self, event, response):
        if self.ready and event.get_extra(REPLY_STATE_KEY) and getattr(self, 'reply_state', None):
            self.reply_state.response(event, response)

    @filter.on_agent_done(priority=2000000)
    async def sanitize_voice_history(self, event, run_context, response):
        if self.ready and not self.external_breeze():
            await self.breeze.sanitize_voice_history(event, run_context, response)

    @filter.on_decorating_result(priority=-100000)
    async def format_chat_reply(self, event):
        if self.ready:
            await self.active.format_chat_reply(event)

    @filter.on_decorating_result(priority=-1900000)
    async def reply_voice(self, event):
        if self.ready and not self.external_breeze():
            await self.breeze.reply_voice(event)

    @filter.on_decorating_result(priority=-2000000)
    async def recheck_before_send(self, event):
        if self.ready:
            await self.friends.recheck_before_send(event)
            if (not event.is_stopped() and event.get_result() and event.get_extra(REPLY_STATE_KEY)
                    and getattr(self, 'reply_state', None)):
                event.set_extra('companion_reply_state_prepared', True)
                self.reply_state.mark(event, 'prepared')

    @filter.after_message_sent()
    async def message_sent(self, event):
        if self.ready:
            await self.friends.unread_answered(event)
            await self.active.replied(event)
            await self.batch.replied(event)
            self.followups.sent(event)

    @filter.command('主动状态')
    async def status(self, event):
        if self.ready:
            async for result in self.active.status(event):
                yield result

    @filter.command('暂停主动')
    async def pause(self, event):
        if self.ready:
            async for result in self.active.pause(event):
                yield result

    @filter.command('恢复主动')
    async def resume(self, event):
        if self.ready:
            async for result in self.active.resume(event):
                yield result

    @filter.command('回复状态')
    async def reply_status(self, event):
        if (not self.ready or not getattr(self, 'reply_state', None)
                or event.unified_msg_origin != self.friends.umo
                or str(event.get_sender_id()) != self.friends.owner or event.get_group_id()):
            return
        rows = self.reply_state.latest(event.unified_msg_origin)
        lines = []
        for row in rows:
            at = datetime.fromtimestamp(row['received'], ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M:%S')
            extra = ('；说明='+REASONS.get(row['reason'], LABELS.get(row['reason'], row['reason']))) if row['reason'] else ''
            lines.append(at+' '+LABELS.get(row['state'], row['state'])+extra)
        yield event.plain_result('\n'.join(lines) if lines else '状态记录刚启用，尚无新记录；旧消息不能追溯判断。')
        event.stop_event()
