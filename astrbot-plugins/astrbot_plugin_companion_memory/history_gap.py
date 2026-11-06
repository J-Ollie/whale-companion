"""Find bounded, verifiable recent user context omitted from an anchored window.

No summary generation, transcript rewrite, cross-person lookups or new model calls.
The window's oldest matching user text anchors the range, so intentional ordinary
trimming does not reload everything before that window or bridge an empty reset.
"""
import re
import time
from .budget import clip
from .memory import stamp


def normalized(text):
    return re.sub(r'\s+', '', str(text))


def authored_text(message):
    if not isinstance(message, dict) or message.get('role') != 'user':
        return ''
    content = message.get('content', '')
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    return '\n'.join(p.get('text', '') for p in content if isinstance(p, dict)
        and p.get('type') == 'text' and not p.get('_no_save')
        and not p.get('text', '').lstrip().startswith(('<system_reminder>', '<companion_', '[Image ')))


def user_texts(contexts):
    return [normalized(text) for row in contexts or [] if (text := authored_text(row)).strip()]


def retrieval_query(query, contexts):
    """An attachment has no textual query; use nearby user words for relevance."""
    placeholders = {'[图片]', '[表情]', '[image]', '[收到非文字消息，附件内容未保存到文本记忆]'}
    if query.strip() and query.strip().lower() not in placeholders:
        return query
    texts = []
    for row in (contexts or [])[-12:]:
        text = authored_text(row)
        lines = [line for line in text.splitlines() if line.strip() and line.strip().lower() not in placeholders]
        if lines:
            texts.append('\n'.join(lines))
    return clip('\n'.join(texts[-2:]), 900)


def recent_gap(cognition, qq, contexts, *, origin, query='', before_id=None, at=None):
    """Return at most four missing user excerpts, each with its readable source ID."""
    represented = user_texts(contexts)
    if not represented:
        return []
    at = time.time() if at is None else at
    rows = cognition.store.latest_messages(qq, limit=48)
    suppressed = cognition.suppressed(qq)
    eligible = []
    for row in rows:
        if (row['role'] != 'user' or row['id'] in suppressed or row.get('at') is None
                or not 0 <= at-row['at'] <= 3600 or row['text'].lstrip().startswith('/')
                or row['text'].startswith(('[收到非文字消息，', '[分享了引用或转发资料，'))
                or row.get('origin') not in (origin, 'verified_friend_archive')
                or before_id is not None and row['id'] >= before_id):
            continue
        text = normalized(row['text'])
        if not text or before_id is None and text == normalized(query):
            continue
        eligible.append((row, text))
    anchors = [row['id'] for row, text in eligible if len(text) >= 4 and text in represented]
    if not anchors:
        return []
    anchor = min(anchors)
    # A retired source already present in history may locate a gap, but must
    # never be reintroduced as active context by this recovery path.
    missing = [row for row, text in eligible if row['id'] > anchor
               and not any(text in body for body in represented)
               and not cognition.retired_source(qq,row['id'])]
    result = []
    for row in missing[-4:]:
        # An excerpt is source material, never a replacement for the full record.
        text = clip(row['text'], 420)
        result.append({'id': 'obs:'+str(row['id']), 'role': 'user', 'time': stamp(row['at']),
                       'text': text, 'excerpt': text != row['text']})
    return result
