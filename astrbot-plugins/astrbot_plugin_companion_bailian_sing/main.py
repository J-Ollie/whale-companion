"""Owner-only audition adapter; no automatic sends or normal-voice changes.

Reuses companion_batch for configuration, quota, sleep and validated guides.
It intentionally does not claim native Bailian voice-ID singing support.
"""
import asyncio
import hashlib
import json
import re
import time

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Star
from .backend import GenerationError, checked_wave, decode_audio, generate_raw


class BailianSingingPreview(Star):
    def __init__(self, context, config):
        super().__init__(context)
        self.context=context
        self.config=config
        self.lock=asyncio.Lock()

    def voice(self, event):
        meta=self.context.get_registered_star('astrbot_plugin_companion_batch')
        if not meta or not meta.activated:
            raise GenerationError('companion_batch_required')
        voice=meta.star_cls.voice
        if (not voice.permitted(event)
            or str(event.get_sender_id())!=str(voice.plugin.config.get('owner_qq','100000001'))):
            raise GenerationError('owner_private_only')
        if not voice.awake():
            raise GenerationError('role_sleeping_or_unavailable')
        return voice

    @filter.llm_tool(name='companion_bailian_sing_preview')
    async def preview(self, event:AstrMessageEvent, action:str='status', guide_reference:str='', reference:str=''):
        """百炼双参考唱歌实验，默认关闭。只生成试听文件，无自动发送，无正式采用。

        Args:
            action(string): status 查看能力边界；preview 用已验证导唱生成一次试音；result 查询该消息对应的试音结果。
            guide_reference(string): preview 时必填，来自 companion_sing 的 score-曲库编号或 song-已完成音频编号；不能只填歌名、歌词或网址。
            reference(string): result 时必填，preview 返回的 bpreview-编号。
        """
        try:
            voice=self.voice(event)
            if action=='status':
                return json.dumps({'enabled':bool(self.config.get('enabled',False)),
                    'mode':'experimental_two_audio_references','voice_id_directly_supported':False,
                    'arbitrary_song_by_title_supported':False,'auto_send':False,
                    'quality_accepted':False,'requires_real_guide':True},ensure_ascii=False)
            if not self.config.get('enabled',False):
                raise GenerationError('experimental_preview_disabled')
            root=voice.root/'bailian-preview-jobs'
            if action=='result':
                if not re.fullmatch(r'bpreview-[a-f0-9]{20}',reference):
                    raise GenerationError('invalid_preview_reference')
                path=root/reference/'state.json'
                if not path.is_file():raise GenerationError('preview_not_found')
                return path.read_text(encoding='utf-8')
            if action!='preview':raise GenerationError('unsupported_action')
            if not re.search(r'试听|试唱|预览',event.message_str or ''):
                raise GenerationError('explicit_audition_request_required')
            identity=str(getattr(event.message_obj,'message_id','') or '')
            if not identity:raise GenerationError('real_message_identity_required')
            if guide_reference.startswith('score-'):
                row,guide,_=voice.singing.clip(guide_reference)
            else:
                row,guide=voice.singing.sample(guide_reference)
            # Both assets are local, checked audio. No paths or URLs supplied by the model.
            speech_path=voice.root/'singing-reference.mp3'
            expected=json.loads((voice.root/'singing-reference.json').read_text(encoding='utf-8'))['sha256']
            if speech_path.is_symlink() or speech_path.stat().st_size>1800000:
                raise GenerationError('invalid_speech_reference')
            speech_bytes=speech_path.read_bytes()
            if hashlib.sha256(speech_bytes).hexdigest()!=expected:
                raise GenerationError('speech_reference_changed')
            speaking,_=await asyncio.to_thread(decode_audio,speech_bytes)
            checked_wave(guide)
            fingerprint=hashlib.sha256(json.dumps([event.unified_msg_origin,identity,
                expected,hashlib.sha256(guide).hexdigest()],sort_keys=True).encode()).hexdigest()
            identifier='bpreview-'+fingerprint[:20]
            folder=root/identifier
            async with self.lock:
                if folder.exists():
                    # The directory reserves the request even across reloads or crashes.
                    state=folder/'state.json'
                    return state.read_text(encoding='utf-8') if state.exists() else json.dumps({
                        'reference':identifier,'state':'outcome_unknown_do_not_retry'})
                self.voice(event)
                root.mkdir(parents=True,exist_ok=True,mode=0o700)
                if any('"state": "running"' in p.read_text(encoding='utf-8')
                       for p in root.glob('bpreview-*/state.json')):
                    raise GenerationError('another_preview_running_or_needs_reconciliation')
                folder.mkdir(mode=0o700)
                if not await voice.reserve('singing',limit=2):
                    self.save(folder,{'reference':identifier,'state':'daily_singing_limit'})
                    raise GenerationError('daily_singing_limit')
                if not await voice.reserve('tts'):
                    self.save(folder,{'reference':identifier,'state':'daily_tts_limit'})
                    raise GenerationError('daily_tts_limit')
                self.save(folder,{'reference':identifier,'state':'running','created_at':time.time()})
            from astrbot_plugin_companion_batch.voice_cloud import settings
            try:
                self.voice(event)
                raw,report=await asyncio.to_thread(generate_raw,settings(voice.root),row['lyrics'],speaking,guide)
                (folder/'provider-audio.bin').write_bytes(raw)
                (folder/'provider-audio.bin').chmod(0o600)
                wav,seconds=await asyncio.to_thread(decode_audio,raw)
                report.update(seconds=seconds,output_sha256=hashlib.sha256(wav).hexdigest())
                (folder/'preview.wav').write_bytes(wav)
                (folder/'preview.wav').chmod(0o600)
                state={'reference':identifier,'state':'awaiting_audio_and_human_checks',
                    'source_title':row['title'],'guide_reference':guide_reference,
                    'report':report,'sent':False,'adopted':False}
            except Exception as exc:
                state={'reference':identifier,'state':'failed_no_retry','sent':False,
                    'reason':str(exc) if isinstance(exc,GenerationError) else 'preview_internal_error'}
            self.save(folder,state)
            self.voice(event)
            return json.dumps(state,ensure_ascii=False)
        except (GenerationError,OSError,ValueError,KeyError):
            # Don't expose exception locals, credentials, signed URLs or other contacts' files.
            return json.dumps({'ok':False,'state':'preview_unavailable','sent':False,
                'note':'本实验不可用或未通过检查；不能称作已唱、已发送或已采用。'},ensure_ascii=False)

    @staticmethod
    def save(folder, value):
        path=folder/'state.json';temporary=folder/'state.tmp'
        temporary.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
        temporary.chmod(0o600);temporary.replace(path)
