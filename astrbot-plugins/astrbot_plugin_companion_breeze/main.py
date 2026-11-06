"""Independent transport using the unified voice contract and receipt policy."""
from pathlib import Path
from astrbot.api.event import filter
from astrbot.api.star import Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from .client import BreezeClient, VOICE_ID
from .compat import peer

_service=peer('astrbot_plugin_companion_chat.breeze.main')
VoiceService=_service.CompanionBreeze
VOICE_IDENTITY=_service.VOICE_IDENTITY
VOICE_ROUTING=_service.VOICE_ROUTING


class CompanionBreeze(VoiceService, Star):
    def __init__(self, context, config):
        Star.__init__(self, context)
        self.context=context
        self.config=config
        # Keep the current standalone client's voice, payload defaults and budget.
        self.client=BreezeClient(Path(get_astrbot_data_path())/'plugin_data/companion_breeze',
                                config.get('voice_id',VOICE_ID))
        self.busy=False

    @filter.on_llm_request(priority=-2100000)
    async def voice_hint(self,event,req):
        await VoiceService.voice_hint(self,event,req)

    @filter.on_llm_response(priority=2000000)
    async def choose_medium(self,event,response):
        await VoiceService.choose_medium(self,event,response)

    @filter.on_agent_done(priority=2000000)
    async def sanitize_voice_history(self,event,run_context,response):
        await VoiceService.sanitize_voice_history(self,event,run_context,response)

    @filter.on_decorating_result(priority=-1900000)
    async def reply_voice(self,event):
        await VoiceService.reply_voice(self,event)
