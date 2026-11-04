"""One bounded generation attempt. A returned audio file is not a quality approval.

Qwen Audio Next accepts audio references, not the voice IDs used by Qwen TTS.
The caller supplies a real Bailian speech sample and a real singing guide.
"""
import base64
import hashlib
import io
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import wave

MODEL = 'qwen-audio-3.1-tts-next'
MAX_AUDIO = 1800000


class GenerationError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def endpoint(host):
    if host != 'dashscope.aliyuncs.com' and not re.fullmatch(
        r'[A-Za-z0-9][A-Za-z0-9-]{0,62}\.cn-beijing\.maas\.aliyuncs\.com', host
    ):
        raise GenerationError('unapproved_api_host')
    return 'https://' + host + '/api/v1/services/audio/tts/SpeechSynthesizer'


def checked_wave(raw):
    if not isinstance(raw, bytes) or not 100 < len(raw) <= MAX_AUDIO:
        raise GenerationError('invalid_reference_size')
    try:
        with wave.open(io.BytesIO(raw), 'rb') as wav:
            valid = (wav.getnchannels() == 1 and wav.getsampwidth() == 2
                     and wav.getframerate() == 24000 and wav.getcomptype() == 'NONE')
            seconds = wav.getnframes() / 24000
            frames = wav.readframes(wav.getnframes())
            valid = valid and len(frames) == wav.getnframes() * 2
    except (wave.Error, EOFError):
        raise GenerationError('invalid_reference_wave') from None
    if not valid or not 2 <= seconds <= 20:
        raise GenerationError('invalid_reference_properties')
    return seconds


def request_body(lyrics, speaking, guide):
    if not isinstance(lyrics, str) or not re.fullmatch(r'[\u4e00-\u9fff]{2,20}', lyrics):
        raise GenerationError('unsupported_lyrics')
    checked_wave(speaking)
    checked_wave(guide)
    instruction = (
        '生成一段单人无伴奏清唱。@voice1 是唯一的目标音色，保留其年轻、偏甜、'
        '柔软且略带气声的声线。@voice2 只提供本次歌曲的旋律、节奏和歌词发音，'
        '不要采用 @voice2 的音色。逐句沿用 @voice2 的旋律和节奏，用 @voice1 的'
        '声线完整演唱下面的歌词，不作词、不作曲、不加说话、前奏或背景声：'
        '“' + lyrics + '”。'
    )
    return {'model': MODEL, 'input': {
        'text_prompt': instruction,
        'references': [{'audio_data': 'data:audio/wav;base64,' + base64.b64encode(r).decode()}
                       for r in (speaking, guide)],
        'format': 'wav', 'sample_rate': 24000, 'channels': 1, 'seed': 42,
    }}


def decode_audio(raw, command=('ffmpeg',)):
    # Decode a maximum of 21 seconds; >20 is rejected, not silently clipped.
    try:
        proc = subprocess.run(
            [*command, '-v', 'error', '-xerror', '-i', 'pipe:0', '-t', '21',
             '-ar', '24000', '-ac', '1', '-f', 's16le', 'pipe:1'],
            input=raw, capture_output=True, check=True, timeout=12)
    except (OSError, subprocess.SubprocessError):
        raise GenerationError('generated_audio_decode_failed') from None
    pcm = proc.stdout
    if len(pcm) % 2 or not 2 * 48000 <= len(pcm) <= 20 * 48000:
        raise GenerationError('generated_audio_duration_out_of_range')
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(pcm)
    return output.getvalue(), len(pcm) / 48000


def generate_raw(cfg, lyrics, speaking, guide):
    from astrbot_plugin_companion_batch.voice_cloud import audio_source
    body = request_body(lyrics, speaking, guide)
    url = endpoint(cfg['api_host'])
    if not isinstance(cfg.get('api_key'), str) or not 16 <= len(cfg['api_key']) <= 512:
        raise GenerationError('invalid_key')
    request = urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + cfg['api_key'], 'Content-Type': 'application/json'})
    try:
        # Exactly one POST; a timeout may already have incurred a charge.
        with urllib.request.build_opener(NoRedirect).open(request, timeout=100) as response:
            raw_reply = response.read(1024 * 1024 + 1)
        if len(raw_reply) > 1024 * 1024:
            raise GenerationError('generation_response_too_large')
        reply = json.loads(raw_reply)
        audio_url = audio_source(reply.get('output', {}).get('audio', {}).get('url'))
        with urllib.request.build_opener(NoRedirect).open(audio_url, timeout=20) as response:
            raw = response.read(MAX_AUDIO + 1)
        if len(raw) > MAX_AUDIO:
            raise GenerationError('generation_audio_too_large')
    except urllib.error.HTTPError as exc:
        raise GenerationError('bailian_http_' + str(exc.code) + '_no_retry') from None
    except (OSError, ValueError):
        raise GenerationError('generation_unavailable_no_retry') from None
    return raw, {'model': MODEL,
        'voice_reference_sha256': hashlib.sha256(speaking).hexdigest(),
        'guide_sha256': hashlib.sha256(guide).hexdigest(),
        'provider_audio_sha256': hashlib.sha256(raw).hexdigest(),
        'voice_id_directly_supported': False, 'human_approved': False,
        'melody_verified': False, 'transcript_verified': False,
        'usage': reply.get('usage', {}), 'request_id': reply.get('request_id')}


def generate(cfg, lyrics, speaking, guide):
    raw, report = generate_raw(cfg, lyrics, speaking, guide)
    wav, seconds = decode_audio(raw)
    return wav, {**report, 'seconds': seconds, 'output_sha256': hashlib.sha256(wav).hexdigest()}
