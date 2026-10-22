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


@dataclass
class Batch:
    first: float
    last: float
    parts: list = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    count: int = 0


class CompanionBatch(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.pending: dict[str, Batch] = {}
        self.tasks: set[asyncio.Task] = set()

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def merge(self, event: AstrMessageEvent):
        if not self.config.get('enabled', True):
            return
        if event.get_platform_id() != self.config.get('platform_id', 'qq-companion'):
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

        loop = asyncio.get_running_loop()
        now = loop.time()
        quiet = max(0.01, float(self.config.get('quiet_seconds', 5)))
        maximum = max(quiet, float(self.config.get('max_seconds', 15)))
        key = event.unified_msg_origin
        batch = self.pending.get(key)
        # A late arrival belongs to a new batch even if the old waiter has not run yet.
        if batch and now >= min(batch.last + quiet, batch.first + maximum):
            batch = None
        leader = batch is None
        if leader:
            batch = Batch(now, now)
            self.pending[key] = batch
        if batch.count:
            batch.parts.append(Plain('\n'))
        batch.parts.extend(event.message_obj.message)
        batch.texts.append(text)
        batch.count += 1
        batch.last = now
        batch.changed.set()
        if not leader:
            # AstrBot's call_llm flag means suppress the default model request.
            event.should_call_llm(True)
            event.stop_event()
            return

        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            while True:
                remaining = min(batch.last + quiet, batch.first + maximum) - loop.time()
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
            logger.info('Companion batch ready: %s messages', batch.count)
            if await self.may_end_turn(event):
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
                if not memory:return False
                rows=memory.store.latest_messages(sender,limit=16)
            else:
                if not getattr(event,'get_extra',lambda key,default=False:default)('companion_friend_verified',False):return False
                meta=lookup('astrbot_plugin_companion_friends') if lookup else None
                friends=meta.star_cls if meta and meta.activated else None
                if not friends:return False
                rows=friends.archive.context_messages(sender,limit=16)
            last_bot=next((r for r in reversed(rows) if r['role']=='assistant'),None)
            if not last_bot or not last_bot.get('at') or not 0<=time.time()-last_bot['at']<=1800:return False
            if needs_sendoff(event.message_str,last_bot['text']):return False
            provider=self.config.get('closure_provider_id','deepseek/deepseek-flash')
            payload={'recent':[{'role':r['role'],'text':r['text'][:800]} for r in rows],
                'current_merged_message':event.message_str,'note':'最新用户消息可能已包含在recent中；这是同一条，不是重复追问。'}
            result=await asyncio.wait_for(self.context.llm_generate(chat_provider_id=provider,
                system_prompt=SYSTEM,prompt=json.dumps(payload,ensure_ascii=False)),timeout=8)
            return silence_decision(result.completion_text)
        except Exception as exc:
            logger.warning('Turn-end check unavailable after %s; replying normally',type(exc).__name__)
            return False

    @filter.on_llm_request(priority=-2000000)
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
