"""Native DashScope STT adapter; contains no credentials or outbound chat actions."""
import base64
import io
import wave

import aiohttp
from astrbot.api.star import Star, register
from astrbot.core.provider.entities import ProviderType
from astrbot.core.provider.provider import STTProvider
from astrbot.core.provider.register import register_provider_adapter
from astrbot.core.utils.media_utils import MediaResolver

TYPE = 'companion_dashscope_stt'
PROVIDER_ID = 'aliyun/qwen-audio-3.0-asr-flash'
ENDPOINT = 'https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation'
_context = None


def awake():
    """Use the same live sleep policy as the companion's incoming-message gate."""
    if _context is None:
        return False
    meta = _context.get_registered_star('astrbot_plugin_companion_friends')
    friends = meta.star_cls if meta and getattr(meta, 'activated', False) else None
    return bool(friends and not friends.sleeping())


def build_payload(model, audio_uri, sample_rate):
    return {'model': model, 'input': {'messages': [{'role': 'user', 'content': [
        {'type': 'input_audio', 'input_audio': {'data': audio_uri}}
    ]}]}, 'parameters': {'format': 'wav', 'sample_rate': str(sample_rate)}}


def parse_transcript(payload):
    output = payload.get('output') or {}
    # Normal responses return output.text; accept nested output as well.
    text = output.get('text')
    if not isinstance(text, str):
        text = (output.get('output') or {}).get('text')
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError('DashScope returned no transcription')
    return text.strip()


@register_provider_adapter(TYPE, '小鲸鱼百炼语音识别', provider_type=ProviderType.SPEECH_TO_TEXT)
class CompanionDashscopeSTT(STTProvider):
    def __init__(self, provider_config, provider_settings):
        super().__init__(provider_config, provider_settings)
        self.key = str(provider_config.get('api_key') or '').strip()
        self.set_model(provider_config.get('model') or 'qwen-audio-3.0-asr-flash')

    async def get_text(self, audio_url):
        if not awake():
            raise RuntimeError('Companion asleep; recognition deferred to existing unread handling')
        if not self.key:
            raise RuntimeError('DashScope API key is missing')
        audio = await MediaResolver(audio_url, media_type='audio', default_suffix='.wav').to_base64_data(
            strict=True, target_format='wav')
        if audio is None:
            raise ValueError('Audio conversion failed')
        raw = base64.b64decode(audio.base64_data, validate=True)
        if len(raw) > 10 * 1024 * 1024:
            raise ValueError('Audio chunk exceeds 10 MiB')
        with wave.open(io.BytesIO(raw), 'rb') as wav:
            rate = wav.getframerate()
            if rate <= 0 or wav.getnframes() / rate > 300:
                raise ValueError('Audio chunk exceeds five minutes')
        if not awake():
            raise RuntimeError('Companion asleep; no recognition request sent')
        payload = build_payload(self.model_name, audio.to_data_url(), rate)
        headers = {'Authorization': 'Bearer '+self.key, 'X-DashScope-SSE': 'disable'}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
            async with session.post(ENDPOINT, json=payload, headers=headers) as response:
                if response.status != 200:
                    # Do not echo audio, credentials, or the provider response into user chat/logs.
                    raise RuntimeError('DashScope recognition failed: HTTP '+str(response.status))
                result = await response.json()
        return parse_transcript(result)


@register('astrbot_plugin_companion_asr', 'local', 'Native DashScope speech recognition', '0.1.0')
class CompanionASR(Star):
    async def initialize(self):
        global _context
        _context = self.context
        # On a full AstrBot start, third-party adapters register after native providers.
        # Load only this adapter's own previously saved entries, without changing settings.
        manager = self.context.provider_manager
        for cfg in self.context.get_config().get('provider', []):
            if cfg.get('type') == TYPE and cfg.get('enable'):
                if cfg['id'] in manager.inst_map:
                    await manager.terminate_provider(cfg['id'])
                await manager.load_provider(cfg)

    async def terminate(self):
        global _context
        manager = self.context.provider_manager
        for cfg in self.context.get_config().get('provider', []):
            if cfg.get('type') == TYPE and cfg['id'] in manager.inst_map:
                await manager.terminate_provider(cfg['id'])
        _context = None
