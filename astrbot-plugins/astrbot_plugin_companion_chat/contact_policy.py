"""One durable availability policy, independent of each outbound channel's quota."""
import hashlib
import math
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')


def policy(state):
    return state.setdefault('contact_policy_v1', {'hold': None, 'topics': [], 'reduced_until': 0, 'seen': []})


def blocked(state, now, *, unanswered=True):
    if state.get('paused'):
        return 'paused'
    p = state.get('contact_policy_v1', {})
    hold = p.get('hold') or {}
    if hold.get('mode') == 'return' or hold.get('mode') == 'until' and now < hold.get('until', 0):
        return 'owner_do_not_disturb'
    if now < state.get('snooze_until', 0):
        return 'owner_do_not_disturb'
    if unanswered and state.get('unanswered', 0) >= 1:
        return 'previous_outreach_unanswered'
    return ''


def direct(state, text, now):
    """Immediate restrictive controls; semantic refinement shares affect's call.

    Only current self-authored text enters. Unambiguous direct phrases are a
    fallback when the semantic model is unavailable or its budget is exhausted.
    """
    p = policy(state)
    if re.search(r'假如|假设|如果|例如|比如|[“「『"]|他说|她说|你说', text):
        return None
    if re.search(r'^(?:我)?(?:忙完了|回来了|醒了|起床了)(?:[，,。！!\s]|$)', text):
        p['hold'] = None
        state['snooze_until'] = 0
        return 'returned'
    if re.search(r'恢复主动|开启主动|可以主动找我', text):
        state['paused'] = False
        state['snooze_until'] = 0
        p['hold'] = None
        return 'resumed'
    if re.search(r'(?:忙完|回来|有空)(?:了)?(?:我)?(?:再|会)?(?:来)?找你|等我(?:忙完|回来)', text):
        p['hold'] = {'mode': 'return', 'until': 0}
        return 'waiting_return'
    until = None
    if re.search(r'今天.{0,8}(?:别|不要|不用)(?:再)?主动', text):
        until = (datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0)+timedelta(days=1)).timestamp()
    elif re.search(r'(?:忙到|到).{0,8}[点:：]', text):
        m = re.search(r'(上午|下午|晚上|今晚|明早|明天)?\s*([零一二三四五六七八九十两\d]{1,3})[点:：](?:([\d]{1,2})分?)?(半)?', text)
        if m:
            numeral = {'零':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}
            token = m[2]
            if token.isdigit():hour = int(token)
            elif '十' in token:
                a, b = token.split('十');hour = numeral.get(a, 1)*10+numeral.get(b, 0)
            else:hour = numeral.get(token, -1)
            if m[1] in ('下午','晚上','今晚') and hour < 12:hour += 12
            if 0 <= hour < 24:
                local = datetime.fromtimestamp(now, TZ)
                minute = 30 if m[4] else int(m[3] or 0)
                if minute < 60:
                    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if m[1] in ('明早','明天'):target += timedelta(days=1)
                    if now < target.timestamp() <= now+7*86400:until = target.timestamp()
    if until:
        p['hold'] = {'mode':'until','until':until}
        state['snooze_until'] = until
        return 'until'
    m = re.search(r'(?:别|不要|不用)(?:再)?提(?:起)?(.{1,25}?)(?:了|[。！!，,]|$)', text)
    if m:
        topic = m[1].strip()
        p['topics'] = list(dict.fromkeys(p['topics']+[topic]))[-8:]
        return 'topic'
    if re.search(r'最近.{0,5}忙|这几天.{0,5}忙', text):
        p['reduced_until'] = now+3*86400
        return 'reduced'
    return None


def semantic(state, raw, text, event, now):
    if raw is None:return False
    if not isinstance(raw, dict):raise ValueError('Invalid contact policy')
    mode, quote = raw.get('mode'), raw.get('quote')
    if mode not in ('until','return','reduced','topic','resume_topic','pause','resume') or not isinstance(quote,str) or not quote.strip() or quote not in text:
        raise ValueError('Ungrounded contact policy')
    p = policy(state)
    key = hashlib.sha256((event+'\0'+quote+'\0'+mode).encode()).hexdigest()
    if key in p['seen']:return False
    if mode == 'until':
        value = datetime.fromisoformat(raw.get('until',''))
        if value.tzinfo is None:raise ValueError('Contact time needs timezone')
        until = value.timestamp()
        if not math.isfinite(until) or not now < until <= now+366*86400:raise ValueError('Invalid contact time')
        p['hold'] = {'mode':'until','until':until};state['snooze_until'] = until
    elif mode == 'return':p['hold'] = {'mode':'return','until':0}
    elif mode == 'reduced':p['reduced_until'] = now+3*86400
    elif mode in ('topic','resume_topic'):
        topic = raw.get('topic','')
        if not isinstance(topic,str) or not 1 <= len(topic) <= 30 or topic not in quote:
            raise ValueError('Topic needs exact source')
        if mode == 'resume_topic':p['topics'] = [x for x in p['topics'] if x != topic]
        else:p['topics'] = list(dict.fromkeys(p['topics']+[topic]))[-8:]
    elif mode == 'pause':state['paused'] = True
    elif mode == 'resume':
        state['paused'] = False;state['snooze_until'] = 0;p['hold'] = None
    p['seen'] = (p['seen']+[key])[-64:]
    return True


def context(state, now):
    p = state.get('contact_policy_v1', {})
    return {'联系限制': blocked(state,now), '等待条件':p.get('hold'),
            '减少联系':now < p.get('reduced_until',0), '不再主动提及的话题':p.get('topics',[]),
            '说明':'这些限制只决定主动联系；正常来信可回应。想念不等于被忽视，情绪不能解除联系边界。'}
