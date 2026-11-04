"""Scoped workaround for 4.29 context truncation dropping the current user turn.

The framework uses the previous response's total usage to trigger compression,
then drops pairs of messages. With fewer than drop_turns pairs it replaces the
whole input with the FIRST user message, including when a new user turn exists.
Only verified companion requests marked by our request hook use this workaround.
"""
from functools import wraps

from astrbot.core.agent.context.manager import ContextManager
from astrbot.core.agent.context.round_utils import split_into_rounds
from astrbot.core.agent.context.truncator import ContextTruncator
from astrbot.core.platform.astr_message_event import current_message_event

MARKER = 'companion_context_round_guard'


def guarded():
    event = current_message_event.get()
    return event is not None and event.get_extra(MARKER, False) is True


def split(messages):
    first = next((i for i, m in enumerate(messages) if m.role != 'system'), len(messages))
    return messages[:first], split_into_rounds(messages[first:])


def recent(truncator, systems, rounds, keep):
    # Never cut through a user/tool round, or remove the current user input.
    chosen = rounds[-max(1, keep):]
    return systems + truncator.fix_messages([m for turn in chosen for m in turn])


def install():
    """Return an ownership-aware restore callback for normal plugin hot reload."""
    changed = []

    def replace(cls, name, factory):
        original = getattr(cls, name)
        wrapper = factory(original)
        setattr(cls, name, wrapper)
        changed.append((cls, name, original, wrapper))

    def dropping(original):
        @wraps(original)
        def wrapped(self, messages, drop_turns=1):
            if not guarded() or drop_turns <= 0:
                return original(self, messages, drop_turns)
            systems, rounds = split(messages)
            return recent(self, systems, rounds, len(rounds)-drop_turns)
        return wrapped

    def limiting(original):
        @wraps(original)
        def wrapped(self, messages, keep_most_recent_turns, drop_turns=1):
            if not guarded() or keep_most_recent_turns == -1:
                return original(self, messages, keep_most_recent_turns, drop_turns)
            systems, rounds = split(messages)
            if len(rounds) <= keep_most_recent_turns:
                return messages
            return recent(self, systems, rounds, keep_most_recent_turns-drop_turns+1)
        return wrapped

    def halving(original):
        @wraps(original)
        def wrapped(self, messages):
            if not guarded():
                return original(self, messages)
            systems, rounds = split(messages)
            return recent(self, systems, rounds, (len(rounds)+1)//2)
        return wrapped

    def processing(original):
        @wraps(original)
        async def wrapped(self, messages, trusted_token_usage=0):
            if not guarded():
                return await original(self, messages, trusted_token_usage)
            # Historical total usage includes old tool schemas/output and is not
            # the size of this request. Recount current messages using the existing
            # framework counter; keep the configured context ceiling unchanged.
            return await original(self, messages, trusted_token_usage=0)
        return wrapped

    replace(ContextTruncator, 'truncate_by_dropping_oldest_turns', dropping)
    replace(ContextTruncator, 'truncate_by_turns', limiting)
    replace(ContextTruncator, 'truncate_by_halving', halving)
    replace(ContextManager, 'process', processing)

    def restore():
        for cls, name, original, wrapper in reversed(changed):
            if getattr(cls, name) is wrapper:
                setattr(cls, name, original)
    return restore
