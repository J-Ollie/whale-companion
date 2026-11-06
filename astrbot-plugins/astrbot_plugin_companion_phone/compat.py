"""Resolve sibling plugins in the same loader namespace, without global aliases."""
from importlib import import_module


def peer(module):
    prefix = __package__.rpartition('.')[0]
    return import_module((prefix+'.' if prefix else '')+module)
