"""Private intake gate survives plugin reload through the owning Context.

The gate holds raw messages in the existing durable inbox. It never changes a
sleep state or replays a send with an unknown outcome.
"""
import asyncio
import inspect
import json
import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

LEGACY = ('active', 'batch', 'friends', 'breeze')


class SectionConfig:
    def __init__(self, config, section):
        self.config, self.section = config, section

    def get(self, key, default=None):
        common = key in ('owner_qq', 'platform_id', 'bot_qq')
        return self.config.get(key if common else self.section + '_' + key, default)


class ComponentContext:
    """Resolve legacy names privately; do not register extra Stars or hooks."""
    def __init__(self, context, owner):
        self.context, self.owner = context, owner

    def get_registered_star(self, name):
        prefix = 'astrbot_plugin_companion_'
        part = name.removeprefix(prefix)
        if name.startswith(prefix) and part in LEGACY:
            service = getattr(self.owner, part, None)
            return SimpleNamespace(activated=True, star_cls=service) if service else None
        return self.context.get_registered_star(name)

    def __getattr__(self, name):
        return getattr(self.context, name)


class ConsumerContext:
    """Adapt already-running memory/phone instances without restarting their jobs."""
    def __init__(self, context):
        self._companion_chat_consumer_base = getattr(context, '_companion_chat_consumer_base', context)

    def get_registered_star(self, name):
        context = self._companion_chat_consumer_base
        prefix = 'astrbot_plugin_companion_'
        part = name.removeprefix(prefix)
        if name.startswith(prefix) and part in LEGACY:
            merged = context.get_registered_star(prefix + 'chat')
            owner = merged.star_cls if merged and merged.activated else None
            service = getattr(owner, part, None) if owner and getattr(owner, 'ready', False) else None
            if service is not None:
                return SimpleNamespace(activated=True, star_cls=service)
        return context.get_registered_star(name)

    def __getattr__(self, name):
        return getattr(self._companion_chat_consumer_base, name)


def bind_live_consumers(context):
    for suffix in ('memory', 'phone', 'breeze'):
        meta = context.get_registered_star('astrbot_plugin_companion_' + suffix)
        if meta and meta.activated and meta.star_cls:
            meta.star_cls.context = ConsumerContext(meta.star_cls.context)


def coroutine_frames(task):
    current = task.get_coro()
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        frame = getattr(current, 'cr_frame', None) or getattr(current, 'gi_frame', None)
        if frame is not None:
            yield frame
        current = getattr(current, 'cr_await', None) or getattr(current, 'gi_yieldfrom', None)


def pipeline_tasks(platform):
    """Read event identities only; do not inspect message text or log frames."""
    result = set()
    own = asyncio.current_task()
    for task in asyncio.all_tasks():
        if task is own or task.done():
            continue
        for frame in coroutine_frames(task):
            if '/pipeline/' not in frame.f_code.co_filename.replace('\\', '/'):
                continue
            event = frame.f_locals.get('event')
            getter = getattr(event, 'get_platform_id', None)
            if callable(getter) and getter() == platform:
                result.add(task)
                break
    return result


def outreach_busy(active):
    task = getattr(active, 'task', None)
    return bool(task and not task.done() and any(
        frame.f_code.co_name in ('consider', 'send_outreach') for frame in coroutine_frames(task)))


class IntakeGate:
    def __init__(self, context, friends, original):
        self.context, self.friends, self.original = context, friends, original
        self.holding = True
        self.request_busy = False
        self.request_path = Path(get_astrbot_data_path()) / 'plugin_data/companion_chat/request-inbox.sqlite3'

        async def handle(adapter, message):
            f = self.friends
            raw = getattr(message, 'raw_message', None)
            if (self.holding and adapter is context.get_platform_inst(f.platform_id)
                    and isinstance(raw, dict) and str(raw.get('self_id')) == f.bot_qq):
                if raw.get('post_type') == 'message' and raw.get('message_type') == 'private':
                    if await f.policy.is_friend(str(raw.get('user_id', ''))):
                        f.sleep_inbox.add(raw)
                    return
                if raw.get('post_type') == 'request' and raw.get('request_type') == 'friend':
                    self.hold_request(raw)
                    return
            return await self.original(adapter, message)

        self.handle = handle

    def hold_request(self, raw):
        self.request_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flag = raw.get('flag')
        if not isinstance(flag, str) or not 1 <= len(flag) <= 1024:
            return
        key = hashlib.sha256(flag.encode()).hexdigest()
        with sqlite3.connect(self.request_path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, digest TEXT UNIQUE NOT NULL, raw TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO requests(digest,raw) VALUES(?,?)', (key, json.dumps(raw, ensure_ascii=False)))
        self.request_path.chmod(0o600)

    async def drain_requests(self):
        if self.holding or self.request_busy or not self.request_path.exists():
            return
        self.request_busy = True
        try:
            with sqlite3.connect(self.request_path) as db:
                rows = db.execute('SELECT id,raw FROM requests ORDER BY id LIMIT 10').fetchall()
            for identifier, raw in rows:
                if self.holding:
                    return
                # receive() persists and deduplicates the request; it never approves.
                await self.friends.policy.receive(json.loads(raw))
                with sqlite3.connect(self.request_path) as db:
                    db.execute('DELETE FROM requests WHERE id=?', (identifier,))
        finally:
            self.request_busy = False


def gate_for(context, friends, legacy=None):
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
    gate = getattr(context, '_companion_chat_intake_gate', None)
    if gate is not None and not isinstance(gate, IntakeGate):
        # Plugin modules are purged on reload. Adopt the current implementation
        # while retaining the durable journal and original transport delegate.
        previous = gate
        gate = IntakeGate(context, friends, previous.original)
        gate.legacy_handle = previous.legacy_handle
        context._companion_chat_intake_gate = gate
        if AiocqhttpAdapter.handle_msg is previous.handle:
            AiocqhttpAdapter.handle_msg = gate.handle
    if gate is None:
        original = AiocqhttpAdapter.handle_msg
        legacy_handle = original
        if legacy is not None:
            # The audited legacy restore closure owns the adapter's original method.
            restore = getattr(legacy, 'restore', None)
            original = inspect.getclosurevars(restore).nonlocals['original']
        gate = IntakeGate(context, friends, original)
        gate.legacy_handle = legacy_handle
        context._companion_chat_intake_gate = gate
        AiocqhttpAdapter.handle_msg = gate.handle
    elif legacy is not None:
        gate.legacy_handle = AiocqhttpAdapter.handle_msg
        AiocqhttpAdapter.handle_msg = gate.handle
    gate.friends, gate.holding = friends, True
    return gate


def write_status(value):
    folder = Path(get_astrbot_data_path()) / 'plugin_data/companion_chat'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = folder / 'runtime-status.json'
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(value), encoding='utf-8')
    temp.chmod(0o600)
    temp.replace(target)
