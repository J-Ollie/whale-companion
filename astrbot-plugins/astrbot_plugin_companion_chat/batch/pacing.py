"""Local timing policy; no model calls, text retention, or artificial typing."""
import math
import re


def seconds(value, default):
    try:
        value = float(value)
        return max(.01, value) if math.isfinite(value) else default
    except (ValueError, TypeError):
        return default


def quiet_window(text, *, ceiling, hot=False, cold=False, energy=None, media=False):
    """Wait for a plausible message boundary; the configured quiet is a ceiling."""
    wait = 1.2 if hot else 4.0 if cold else 2.4
    text = (text or '').rstrip()
    if media:
        wait = max(wait, 3.5)
    if len(text) > 160:
        wait = max(wait, 3.2)
    # A surface hint only, never a semantic decision to suppress a reply.
    if re.search(r'(?:[,，、:：;；…]|\.{2,}|(?:然后|但是|因为|比如|还有|就是|我想说))$', text):
        wait = max(wait, 5.0)
    if type(energy) in (int, float) and math.isfinite(energy) and 0 <= energy < .25:
        wait = max(wait, 3.5)
    return min(seconds(ceiling, 15.0), wait)
