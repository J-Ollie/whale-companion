"""Public character words and tentative time windows; no private context or actions."""
import json
import math
import re
import threading
import time
import urllib.request
from pathlib import Path
from balance import DeepSeekBalance, NoRedirect

STATES = ('idle', 'browsing', 'chatting', 'resting', 'asleep')
KINDS = {'entertainment': '娱乐倾向', 'social': '社交倾向', 'alone': '独处倾向'}


def safe_line(value):
    if not isinstance(value, str):
        return ''
    value = ' '.join(value.split()).strip('“”"')
    if not 2 <= len(value) <= 80:
        return ''
    if re.search(r'https?://|密码|验证码|密钥|token|api.?key|bearer|/opt/|/sdcard|\d{5,}', value, re.I):
        return ''
    return value


def minute(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise ValueError('Invalid time')
    h, m = map(int, value.split(':'))
    return h * 60 + m


def clock(value):
    value = int(value) % 1440
    return f'{value // 60:02d}:{value % 60:02d}'


def rest_window(wake, hours):
    if (not isinstance(wake, (int, float)) or isinstance(wake, bool) or not math.isfinite(wake)
            or not 0 <= wake < 1440 or not isinstance(hours, (int, float)) or isinstance(hours, bool)
            or not math.isfinite(hours) or not 6 <= hours <= 10):
        return None
    return {'start': clock(wake - hours * 60), 'end': clock(wake)}


def validate(payload, wake, hours):
    if not isinstance(payload, dict) or not isinstance(payload.get('words'), dict):
        raise ValueError('Invalid public presence')
    words = {state: safe_line(payload['words'].get(state)) for state in STATES}
    if not all(words.values()):
        raise ValueError('Missing state words')
    rest = rest_window(wake, hours)
    if rest is None:
        raise ValueError('Invalid rhythm')
    wake = minute(rest['end'])
    awake_minutes = 1440 - int(hours * 60)
    windows = {}
    for kind in KINDS:
        values = payload.get('windows', {}).get(kind)
        if not isinstance(values, list) or not 1 <= len(values) <= 2:
            raise ValueError('Missing windows')
        clean = []
        for row in values:
            start, end = minute(row['start']), minute(row['end'])
            length = (end - start) % 1440
            offset = (start - wake) % 1440
            if not 10 <= length <= 180 or offset + length > awake_minutes:
                raise ValueError('Window crosses rest')
            clean.append({'start': clock(start), 'end': clock(end)})
        windows[kind] = clean
    return {'words': words, 'windows': windows}


def project(saved, state, wake, hours, now):
    """Revalidate cached text before returning any of it to visitors."""
    rest = rest_window(wake, hours)
    periods = [{'kind': 'rest', 'label': '休息倾向', 'windows': [rest] if rest else []}]
    result = {'quote': '', 'source': 'pending', 'written_at': None, 'periods': periods}
    try:
        written = saved['written_at']
        if (not isinstance(written, (int, float)) or not math.isfinite(written)
                or not -60 <= now - written <= 24 * 3600 or state not in STATES):
            raise ValueError()
        # Revalidate periods against today's slowly changing wake anchor.
        clean = validate(saved, wake, hours)
        result.update(quote=clean['words'][state], source='self_written', written_at=written)
        for kind, label in KINDS.items():
            periods.append({'kind': kind, 'label': label, 'windows': clean['windows'][kind]})
    except (ValueError, TypeError, KeyError, AttributeError):
        for kind, label in KINDS.items():
            periods.append({'kind': kind, 'label': label, 'windows': []})
    return result


class PublicPresence:
    def __init__(self, root, archive):
        self.root = Path(root)
        self.archive = archive
        self.path = self.root / 'recovery-data/public-presence.json'
        self.lock = threading.Lock()
        self.retry_at = 0

    def credentials(self):
        config = json.loads((self.root / 'data/astrbot/cmd_config.json').read_text(encoding='utf-8-sig'))
        key = DeepSeekBalance(self.root)._key()
        source_ids = {s.get('id') for s in config.get('provider_sources', [])
                      if s.get('enable') is not False and s.get('api_base', '').rstrip('/')
                      in ('https://api.deepseek.com', 'https://api.deepseek.com/v1')}
        providers = [p for p in config.get('provider', []) if p.get('enable') is not False
                     and p.get('provider_source_id') in source_ids]
        preferred = config.get('agent_runner', {}).get('config', {}).get('model', {}).get('provider_id')
        chosen = next((p for p in providers if p.get('id') == preferred), None)
        if chosen is None and len(providers) == 1:
            chosen = providers[0]
        if chosen is None or not re.fullmatch(r'[a-zA-Z0-9._-]{1,80}', str(chosen.get('model', ''))):
            raise ValueError('No unambiguous official model')
        return key, chosen['model']

    def generate(self, snapshot):
        key, model = self.credentials()
        routine = snapshot['routine']
        wake = minute(routine['wake_time'])
        hours = routine['sleep_hours']
        rest = rest_window(wake, hours)
        if rest is None:
            raise ValueError('No rhythm')
        context = {'name': snapshot['profile']['name'], 'traits': snapshot['profile']['traits'],
                   'state': snapshot['current']['label'], 'rest': rest,
                   'time': time.strftime('%Y-%m-%d %H:%M', time.gmtime(time.time() + 8 * 3600))}
        instructions = (
            '你是小鲸鱼，也叫岚。为自己的公开档案写简短的话和今天偏好的活动时段。'
            '只依据提供的公开角色资料，用第一人称、自然简短、不讨好，不解释技术。'
            '这是你清醒时的公开自我表达，不是聊天消息。不要声称已经完成某事、发出消息、'
            '看过特定内容，不涉及任何他人、私聊、记忆、访客留言或隐私。'
            '分别为idle清醒空闲、browsing玩手机、chatting近期有交流、resting短休、asleep睡觉写一句2至45字。'
            'resting和asleep是你此刻提前准备的休息留言，表达先休息、醒后再看，不能表现睡着仍在读消息。'
            '娱乐、社交、独处各自选择1或2段倾向，每段10至180分钟，必须完全位于休息时段以外，'
            '起止HH:MM精确到分钟，跨午夜自然表示。这些只是可调整的偏好，不是强制计划或已执行事实。'
            '休息时段以提供的rest为准，不自行更改。只输出json：'
            '{"words":{"idle":"…","browsing":"…","chatting":"…","resting":"…","asleep":"…"},'
            '"windows":{"entertainment":[{"start":"12:30","end":"13:00"}],'
            '"social":[{"start":"19:00","end":"19:30"}],"alone":[{"start":"21:00","end":"21:40"}]}}'
        )
        request = urllib.request.Request('https://api.deepseek.com/chat/completions',
            data=json.dumps({'model': model, 'messages': [{'role': 'system', 'content': instructions},
                {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}],
                'response_format': {'type': 'json_object'}, 'thinking': {'type': 'disabled'},
                'max_tokens': 900, 'stream': False}).encode(),
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=35) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('Oversized response')
        payload = json.loads(json.loads(raw)['choices'][0]['message']['content'])
        return validate(payload, wake, hours)

    def tick(self):
        with self.lock:
            now = time.time()
            if now < self.retry_at:
                return False
            snapshot = self.archive.snapshot(guest=True)
            mode = self.archive.read(self.root / 'data/astrbot/plugin_data/companion_phone/rhythm.json').get('mode')
            # Never ask the model to speak during sleep/rest or an uncertain state.
            if mode not in ('awake', 'browsing') or snapshot['current']['stale'] or snapshot['current']['state'] not in ('idle', 'browsing', 'chatting'):
                return False
            saved = self.archive.read(self.path)
            if (saved.get('written_at') and snapshot['current'].get('voice_source') == 'self_written'
                    and 0 <= now - saved['written_at'] < 3 * 3600):
                return False
            self.retry_at = now + 30 * 60
            try:
                content = self.generate(snapshot)
                # Do not update the public voice after it has entered sleep/rest.
                latest = self.archive.snapshot(guest=True)['current']
                latest_mode = self.archive.read(self.root / 'data/astrbot/plugin_data/companion_phone/rhythm.json').get('mode')
                if latest_mode not in ('awake', 'browsing') or latest['stale'] or latest['state'] not in ('idle', 'browsing', 'chatting'):
                    return False
                content['written_at'] = time.time()
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                temp = self.path.with_suffix('.tmp')
                temp.write_text(json.dumps(content, ensure_ascii=False), encoding='utf-8')
                temp.chmod(0o600)
                temp.replace(self.path)
                self.retry_at = time.time() + 3 * 3600
                return True
            except Exception:
                # No credentials, upstream body or private error detail in logs.
                return False

    def run(self):
        while True:
            try:
                self.tick()
            except Exception:
                pass
            time.sleep(60)
