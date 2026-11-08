"""Resolve sibling plugins in the same loader namespace, without global aliases."""
from importlib import import_module


def peer(module):
    prefix = __package__.rpartition('.')[0]
    return import_module((prefix+'.' if prefix else '')+module)


def brain(context):
    """Runtime service lookup: no global aliases or dependency on load order."""
    lookup = getattr(context, 'get_registered_star', None)
    meta = lookup('astrbot_plugin_companion_chat') if lookup else None
    chat = getattr(meta, 'star_cls', None) if meta and getattr(meta, 'activated', False) else None
    return getattr(chat, 'brain', None) if chat and getattr(chat, 'ready', False) else None
