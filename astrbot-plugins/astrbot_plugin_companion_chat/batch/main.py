"""Merge allowed private conversations and let finished turns end naturally."""
import asyncio
import json
import time
from dataclasses import dataclass, field

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from .turn_end import candidate,silence_decision,SYSTEM,departure,needs_sendoff
from astrbot.core.agent.message import TextPart
from .pacing import quiet_window, seconds
from ..behavior import reply_guidance, rhythm_snapshot, recent_reply_hint


@dataclass
class Batch:
    first: float
    last: float
    parts: list = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    count: int = 0
    quiet: float = 15.0
    materials: list = field(default_factory=list)


class CompanionBatch:
    def __init__(self, context: Context, config: AstrBotConfig):
        self.context = context
        self.config = config
        self.pending: dict[str, Batch] = {}
        self.tasks: set[asyncio.Task] = set()
        # Monotonic timestamps only, bounded and separated by the real session.
        self.cadence: dict[str, dict] = {}

    def allowed(self, event):
        return (event.get_platform_id() == self.config.get('platform_id', 'qq-companion')
                and not getattr(event, 'get_group_id', lambda: '')()
                and (str(event.get_sender_id()) == str(self.config.get('owner_qq', '100000001'))
                     or getattr(event, 'get_extra', lambda key, default=False: default)('companion_friend_verified', False)))

    def pacing(self, event, now, ceiling):
        if not self.config.get('adaptive_pacing', False):
            return ceiling
        key = event.unified_msg_origin
        self.cadence = {k: row for k, row in self.cadence.items() if now-max(row.values()) < 1800}
        prior = self.cadence.get(key, {})
        hot = 'reply' in prior and 0 <= now-prior['reply'] < 90
        cold = not prior or now-max(prior.values()) >= 600
        self.cadence[key] = prior | {'received': now}
        if len(self.cadence) > 512:
            oldest = min(self.cadence, key=lambda k: max(self.cadence[k].values()))
            self.cadence.pop(oldest, None)
        energy = rhythm_snapshot(self.context).get('energy')
        parts = getattr(event.message_obj, 'message', []) or []
        return quiet_window(event.message_str, ceiling=ceiling, hot=hot, cold=cold,
                            energy=energy, media=any(not isinstance(p, Plain) for p in parts))

    async def replied(self, event):
        if not self.config.get('adaptive_pacing', False) or not self.allowed(event):
            return
        if (event.message_str or '').lstrip().startswith('/'):
            return
        extra = getattr(event, 'get_extra', lambda key, default=None: default)
        if extra('companion_voice_prepared', False) and not extra('companion_voice_delivery_receipt'):
            return
        key = event.unified_msg_origin
        # Only track a session which actually passed this service's merge gate.
        if key in self.cadence:
            self.cadence[key]['reply'] = asyncio.get_running_loop().time()






    async def merge(self, event: AstrMessageEvent):
        if not self.config.get('enabled', True):
            return
        if not self.allowed(event):
            return
        owner=str(event.get_sender_id())==str(self.config.get('owner_qq','100000001'))
        verified=getattr(event,'get_extra',lambda key,default=False:default)('companion_friend_verified',False)
        if not owner and not verified:
            return
        # Built-in commands keep their normal immediate behavior.
        text = event.message_str or ''
        if text.lstrip().startswith('/'):
            return
        if not text.strip() and not event.message_obj.message:
            return
        followups = getattr(self, 'followups', None)
        if followups is not None and followups.offer(event):
            audit = getattr(self, 'reply_state', None)
            if audit:
                audit.mark(event, 'merged', 'followup_wait')
            event.should_call_llm(True)
            event.stop_event()
            return

        loop = asyncio.get_running_loop()
        now = loop.time()
        ceiling = seconds(self.config.get('quiet_seconds', 15), 15.0)
        maximum = max(ceiling, seconds(self.config.get('max_seconds', 60), 60.0))
        key = event.unified_msg_origin
        batch = self.pending.get(key)
        # A late arrival belongs to a new batch even if the old waiter has not run yet.
        if batch and now >= min(batch.last + batch.quiet, batch.first + maximum):
            batch = None
        quiet = self.pacing(event, now, ceiling)
        leader = batch is None
        if leader:
            batch = Batch(now, now)
            batch.root_event = event
            self.pending[key] = batch
        if batch.count:
            batch.parts.append(Plain('\n'))
        batch.parts.extend(event.message_obj.message)
        batch.texts.append(text)
        material = getattr(event, 'get_extra', lambda k, default=None: default)('companion_material_context', None)
        if material:
            batch.materials.append(material)
        batch.count += 1
        batch.last = now
        batch.quiet = quiet
        batch.changed.set()
        if not leader:
            audit = getattr(self, 'reply_state', None)
            if audit:
                audit.merge(event, getattr(batch, 'root_event', None))
            # AstrBot's call_llm flag means suppress the default model request.
            event.should_call_llm(True)
            event.stop_event()
            return

        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            while True:
                remaining = min(batch.last + batch.quiet, batch.first + maximum) - loop.time()
                if remaining <= 0:
                    break
                batch.changed.clear()
                try:
                    await asyncio.wait_for(batch.changed.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
            event.message_str = '\n'.join(batch.texts)
            event.message_obj.message_str = event.message_str
            event.message_obj.message = batch.parts
            if batch.materials:
                event.set_extra('companion_batch_material_context', batch.materials)
            logger.info('Companion batch ready: %s messages', batch.count)
            if await self.may_end_turn(event):
                audit = getattr(self, 'reply_state', None)
                if audit:
                    audit.mark(event, 'silent', 'closing_ack')
                event.should_call_llm(True)
                event.stop_event()
                logger.info('Companion turn ended naturally; acknowledgement kept without a reply')
        finally:
            self.tasks.discard(task)
            if self.pending.get(key) is batch:
                self.pending.pop(key, None)

    async def may_end_turn(self,event):
        if not self.config.get('natural_closure',True) or not candidate(event.message_str,event.message_obj.message):
            return False
        try:
            # Each participant has a separate source, selected by the real platform sender.
            lookup=getattr(self.context,'get_registered_star',None)
            sender=str(event.get_sender_id())
            if sender==str(self.config.get('owner_qq','100000001')):
                meta=lookup('astrbot_plugin_companion_memory') if lookup else None
                memory=meta.star_cls if meta and meta.activated else None
                if not memory:
                    logger.info('Companion turn-end skipped: owner_context_unavailable')
                    return False
                rows=memory.store.latest_messages(sender,limit=16)
            else:
                if not getattr(event,'get_extra',lambda key,default=False:default)('companion_friend_verified',False):return False
                meta=lookup('astrbot_plugin_companion_friends') if lookup else None
                friends=meta.star_cls if meta and meta.activated else None
                if not friends:
                    logger.info('Companion turn-end skipped: friend_context_unavailable')
                    return False
                rows=friends.archive.context_messages(sender,limit=16)
            last_bot=next((r for r in reversed(rows) if r['role']=='assistant'),None)
            if not last_bot or not last_bot.get('at') or not 0<=time.time()-last_bot['at']<=1800:
                logger.info('Companion turn-end skipped: missing_or_stale_context')
                return False
            if needs_sendoff(event.message_str,last_bot['text']):
                logger.info('Companion turn-end skipped: first_farewell')
                return False
            provider=self.config.get('closure_provider_id','deepseek/deepseek-flash')
            payload={'recent':[{'role':r['role'],'text':r['text'][:800]} for r in rows],
                'current_merged_message':event.message_str,'note':'最新用户消息可能已包含在recent中；这是同一条，不是重复追问。'}
            result=await asyncio.wait_for(self.context.llm_generate(chat_provider_id=provider,
                system_prompt=SYSTEM,prompt=json.dumps(payload,ensure_ascii=False),
                max_tokens=96,response_format={'type':'json_object'}),timeout=10)
            quiet=silence_decision(result.completion_text)
            logger.info('Companion turn-end checked: %s','closing_ack' if quiet else 'continue_or_uncertain')
            return quiet
        except Exception as exc:
            logger.warning('Turn-end check unavailable after %s; replying normally',type(exc).__name__)
            return False

    async def task_truth_hint(self,event,req):
        if event.get_platform_id()!=self.config.get('platform_id','qq-companion') or getattr(event,'get_group_id',lambda:'')():return
        if self.allowed(event):
            guidance = reply_guidance(self.context)
            if guidance:
                req.extra_user_content_parts.append(TextPart(text=guidance).mark_as_temp())
            req.extra_user_content_parts.append(TextPart(text='对方明确说还没说完、等一下或准备补充时，可以调用companion_wait_for_followup等1至8秒，收到补充再统一回答；每轮最多一次，普通完整消息不用等待工具。').mark_as_temp())
            repeated = recent_reply_hint(getattr(req, 'contexts', None))
            if repeated:
                req.extra_user_content_parts.append(TextPart(text=repeated).mark_as_temp())
        req.extra_user_content_parts.append(TextPart(text='所有任务的真实执行规则：先实际调用适用工具完成当前委托，按本轮工具结果或可核对的任务状态说话。聊天中的想法、承诺、旧回复和计划不是执行记录；搜索摘要不等于读过正文，点击或尝试不等于成功，排队不等于完成，发送要以回执为准。未做就说未做，失败或不确定就如实说明，不编造进行中、进展、结果或亲身经历。只有确有持续执行机制和对应有效任务时才能说后台仍在做；没有实际安排或推送机制，不承诺稍后自动继续、提醒或发来。已有可核对的实际活动可自然提及，按证据范围说，不必每次报告工具细节。').mark_as_temp())



    async def sendoff_hint(self,event,req):
        if not self.config.get('natural_closure',True):return
        if event.get_platform_id()!=self.config.get('platform_id','qq-companion'):return
        if getattr(event,'get_group_id',lambda:'')():return
        owner=str(event.get_sender_id())==str(self.config.get('owner_qq','100000001'))
        verified=getattr(event,'get_extra',lambda key,default=False:default)('companion_friend_verified',False)
        if not owner and not verified:return
        if departure(event.message_str):
            req.extra_user_content_parts.append(TextPart(text='对方正在告知要去上课、忙、休息或告别。先用一句自然的短话接住告别，不留空、不追问、不重复说教。措辞由你自己决定，不背固定台词；没有实际操作回执，不声称已去玩手机。等你已回应告别后，对方再回“嗯嗯、好的”等才可以不再接话。').mark_as_temp())

    async def terminate(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
        self.cadence.clear()
