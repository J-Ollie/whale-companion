"""BreezeBlue API bridge. Credentials and counters stay in private plugin data."""
import json
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from .performance import clean_instructions

VOICE_ID = 'voc_example_voice'
MODEL_ID = 'breeze-tts-2'
MAX_AUDIO = 1800000
VOICE_SETTINGS = {'guidance_scale': 1.0, 'use_speaker_boost': False, 'speed': 1.0, 'volume': 1.0}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def suitable(text):
    return isinstance(text, str) and 1 <= len(text.strip()) <= 180 and not re.search(
        r'https?://|```|<[^>]+>|(?:brz_|sk-)[A-Za-z0-9_-]{12,}', text)


def preference(text):
    """The last explicit direction in a merged batch wins."""
    positive = list(re.finditer(r'(?:用|发|来|回|说).{0,8}语音|语音.{0,4}(?:回复|回我|说|读)|读给我听|想听.{0,6}(?:你说|你的声音)', text or ''))
    negative = list(re.finditer(r'(?:不要|不想|别|不用|不方便|不需要).{0,8}(?:语音|声音)|文字.{0,4}(?:就好|回复)|(?:只|仅)(?:用|发|回|要).{0,3}文字|不方便听', text or ''))
    # End offsets also handle an embedded positive match in "不要发语音".
    if positive and max(m.end() for m in positive) > max((m.end() for m in negative), default=-1):
        return 'voice'
    return 'text' if negative else None


def requested(text):
    return preference(text) == 'voice'


class BreezeClient:
    def __init__(self, root, voice_id=VOICE_ID):
        self.root = Path(root)
        if not re.fullmatch(r'voc_[A-Za-z0-9]+', voice_id):
            raise ValueError('invalid_voice_id')
        self.voice_id = voice_id

    def key(self):
        value = json.loads((self.root / 'credentials.json').read_text(encoding='utf-8-sig'))
        key = value.get('api_key', '')
        if not re.fullmatch(r'brz_[A-Za-z0-9_-]+', key):
            raise ValueError('breeze_key_missing')
        return key

    def reserve(self):
        path = self.root / 'state.json'
        state = json.loads(path.read_text()) if path.exists() else {}
        day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        if state.get('day') != day:
            state = {'day': day, 'tts': 0}
        if state.get('tts', 0) >= 20:
            raise RuntimeError('breeze_daily_limit')
        state['tts'] = state.get('tts', 0) + 1
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state), encoding='utf-8')
        temporary.chmod(0o600)
        temporary.replace(path)

    def synthesize(self, text, instructions=None):
        if not suitable(text):
            raise ValueError('voice_text_unsuitable')
        direction = clean_instructions(instructions)
        if instructions and not direction:
            raise ValueError('voice_direction_unsuitable')
        key = self.key()  # A missing credential consumes no quota.
        self.reserve()  # Attempts are durable; uncertain failures are never retried.
        payload = {'text': text.strip(), 'model_id': MODEL_ID, 'language_code': 'zh',
                   'voice_settings': dict(VOICE_SETTINGS)}
        if direction:
            payload['instructions'] = direction
        request = urllib.request.Request(
            'https://api.breeze.blue/v1/text-to-speech/' + self.voice_id + '?output_format=mp3&delivery=sync',
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={'xi-api-key': key, 'Content-Type': 'application/json',
                     'Accept': 'audio/mpeg', 'User-Agent': 'WhaleCompanion-Breeze/1.0'})
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=55) as response:
                if response.status != 200:
                    raise RuntimeError('breeze_audio_not_complete')
                audio = response.read(MAX_AUDIO + 1)
        except urllib.error.HTTPError as exc:
            # Do not expose response bodies, headers, request text or credentials.
            raise RuntimeError('breeze_http_' + str(exc.code)) from None
        if not (100 < len(audio) <= MAX_AUDIO and
                (audio.startswith(b'ID3') or audio[0] == 255 and audio[1] & 224 == 224)):
            raise ValueError('invalid_breeze_mp3')
        return audio
