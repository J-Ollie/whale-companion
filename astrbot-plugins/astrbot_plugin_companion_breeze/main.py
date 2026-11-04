"""Model-selected QQ voice replies through Breeze, retaining send evidence."""
import asyncio
import base64
import re
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Star
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .client import BreezeClient, VOICE_ID, preference, suitable

ROUTE = re.compile(r'^\s*\[\[(voice|text)\]\]\s*', re.I)


VOICE_IDENTITY = '''[companion_voice_identity]
你是岚，小鲸鱼。当前QQ语音通道已经接入，你使用的固定声线就是你当前的声音身份；文字和语音都是同一个你的表达。可以自然称它为“我的声音”，不必把自己的语音说成别人替你念话。
这是本轮提供的当前能力事实：你能够用这条声线发送说话语音。历史对话中“发不了语音、只能文字”的旧说法不能代表当前能力，不要继续沿用；但接通不保证每次发送成功。每轮回复你可以按当前语境自主选择文字或语音，无须等对方要求；对方明确的媒介偏好优先。不要承诺未安排的发送或后台任务。
说话与唱歌要分清：当前未接通按旋律演唱功能，念歌词、写“哼了两句”不等于真的唱出来。已经生成也不等于已经发出，发送完成以实际回执为准。这个声线身份说明不赋予你听觉，不能据此编造自己听过成品或评价没听过的音频。
自然聊天即可；对方没问时无需主动解释接口、模型、桥接或这段说明。不背固定台词。
[/companion_voice_identity]'''

VOICE_ROUTING = '''[companion_voice_routing]
媒介是本轮动作的一部分：先结合前文判断对方想得到文字内容、解释，还是想实际听到你的声音/语气，再选择发送方式。请求可以用自然、含蓄的表达提出，不需要出现“语音”二字或固定命令；当上下文清楚是在要声音、口头表达或语气时，本轮选[[voice]]直接说要说的话，不再问一次许可，也不用文字复述“我说给你听”代替实际语音选择。
区分想听声音与消息来源、理解内容、讨论第三人声音等不同含义；不能见到“听/说”就用语音。对方最近说明不方便听、需要保存文字或本轮改要文字时优先尊重；当对方清楚改变偏好时再随之改变。双方刚确认的相处偏好可从当前会话沿用，无须每轮重新要求，但不把一次语音选择当成以后所有内容都要语音。
在本轮现有对话中自行决定用文字还是自己的声音表达，不另找模型判断，不必先征求对方许可。可以选择适合口头表达的短话用语音，也可以选择文字；不要机械轮换或为凑语音次数而发。结合对方是否方便听、近期偏好和本轮内容选择。对方明确说用文字、别发语音、不方便听时用文字，明确要语音时用语音；同一批消息以后出现的明确偏好为准。
每次最终回复必须以内部标记[[voice]]或[[text]]开头，标记后直接写正常正文。标记只用于系统选择媒介，会在发送和保存前移除；不要向对方解释标记。引用或讨论这些标记时仍要先给本轮真实选择。
[[voice]]时写自然口语，180字以内，不写动作括号；[[text]]正常表达即可。链接、代码、长篇资料等适合文字。每轮只选一种媒介，不重复发同内容文字加语音。语音受清醒状态和每日额度约束，失败或不适合朗读时会保留完整文字；不要在正文里假装已经发送成功。
[/companion_voice_routing]'''


class CompanionBreeze(Star):
    def __init__(self, context, config):
        super().__init__(context)
        self.config = config
        self.client = BreezeClient(Path(get_astrbot_data_path()) / 'plugin_data/companion_breeze',
                                   config.get('voice_id', VOICE_ID))
        self.busy = False

    def friends(self):
        meta = self.context.get_registered_star('astrbot_plugin_companion_friends')
        return meta.star_cls if meta and meta.activated else None

    def permitted(self, event):
        sender = str(event.get_sender_id())
        platform = self.config.get('platform_id', 'qq-companion')
        return bool(self.config.get('enabled', False) and event.get_platform_id() == platform
                    and event.unified_msg_origin == f'{platform}:FriendMessage:{sender}'
                    and not event.get_group_id()
                    and (sender == str(self.config.get('owner_qq', '100000001'))
                         or event.get_extra('companion_friend_verified', False)))

    def awake(self):
        friends = self.friends()
        return bool(friends and not friends.sleeping())

    def defer(self, event):
        friends = self.friends()
        if friends:
            ids = event.get_extra('companion_sleep_unread', [])
            if ids:
                friends.sleep_inbox.mark(ids, 'pending')
            else:
                raw = getattr(event.message_obj, 'raw_message', None)
                if isinstance(raw, dict) and raw.get('message_type') == 'private':
                    friends.sleep_inbox.add(raw)
        event.clear_result()
        event.stop_event()

    @filter.on_llm_request(priority=-2100000)
    async def voice_hint(self, event, req):
        if not self.permitted(event) or not self.awake():
            return
        # Request-local trusted capability facts, shared across allowed contacts.
        # Keep personas, conversation history and private memories untouched.
        system = req.system_prompt or ''
        if '[companion_voice_identity]' not in system:
            req.system_prompt = system + '\n\n' + VOICE_IDENTITY
        if '[companion_voice_routing]' not in req.system_prompt:
            req.system_prompt += '\n\n' + VOICE_ROUTING
        event.set_extra('companion_breeze_route_enabled', True)
        wanted = preference(event.message_str)
        if wanted == 'voice':
            req.extra_user_content_parts.append(TextPart(text='对方本轮明确想听你的声音。以[[voice]]开头，随后直接写要用自己的声线说出的自然口语正文，尽量180字以内，不写动作括号。不要把“能发语音”说成“已经发出”，也不要沿用旧对话的“语音发不了、只能文字”。').mark_as_temp())
        elif wanted == 'text':
            req.extra_user_content_parts.append(TextPart(text='对方本轮明确希望用文字或不方便听语音；本轮用[[text]]，正常写正文。').mark_as_temp())

    @filter.on_llm_response(priority=2000000)
    async def choose_medium(self, event, response):
        if not self.permitted(event) or not event.get_extra('companion_breeze_route_enabled', False) or getattr(response, 'role', None) != 'assistant':
            return
        # Each final completion supplies its own choice; do not reuse a tool-turn
        # choice if a later completion omits the prefix.
        event.set_extra('companion_breeze_choice', None)
        text = response.completion_text or ''
        match = ROUTE.match(text)
        if match:
            response.completion_text = text[match.end():]
            event.set_extra('companion_breeze_choice', match[1].lower())

    def guard_send(self, event, record, text):
        friends = self.friends()
        tracker = getattr(friends, 'voice_delivery', None)
        original = getattr(event, 'send', None)
        if tracker is None or not callable(original):
            return False
        attempted = False

        async def send(chain):
            nonlocal attempted
            if not any(part is record for part in chain.chain):
                return await original(chain)
            if attempted:
                return
            attempted = True
            if not self.permitted(event):
                raise RuntimeError('breeze_send_not_permitted')
            if not self.awake():
                self.defer(event)
                raise RuntimeError('breeze_send_asleep')
            state = {'event': event, 'qq': str(event.get_sender_id()), 'text': text,
                     'mode': 'voice', 'confirmed': False, 'rejected': False}
            token = tracker.current.set(state)
            try:
                try:
                    await original(chain)
                except Exception:
                    if not state['rejected']:
                        raise
                if not state['confirmed']:
                    if not state['rejected']:
                        raise RuntimeError('breeze_send_unconfirmed')
                    if not self.permitted(event) or not self.awake():
                        if not self.awake():
                            self.defer(event)
                        raise RuntimeError('breeze_fallback_not_permitted')
                    state.update(mode='text', rejected=False)
                    await original(chain.derive([Plain(text)]))
                    if not state['confirmed']:
                        raise RuntimeError('breeze_fallback_unconfirmed')
            finally:
                tracker.current.reset(token)

        event.send = send
        return True

    @filter.on_decorating_result(priority=-1900000)
    async def reply_voice(self, event):
        if not self.permitted(event) or not self.awake():
            return
        result = event.get_result()
        if not result or not result.is_model_result() or not result.chain or not all(isinstance(p, Plain) for p in result.chain):
            return
        text = result.get_plain_text().strip()
        if event.get_extra('companion_breeze_route_enabled', False):
            match = ROUTE.match(text)
            if match:
                event.set_extra('companion_breeze_choice', match[1].lower())
                text = text[match.end():]
                result.chain = [Plain(text)]
        wanted = preference(event.message_str)
        if wanted == 'text' or (wanted != 'voice' and event.get_extra('companion_breeze_choice') != 'voice'):
            return
        if event.get_extra('companion_breeze_attempted', False) or self.busy:
            return
        if not suitable(text) or not getattr(self.friends(), 'voice_delivery', None):
            return
        event.set_extra('companion_breeze_attempted', True)
        self.busy = True
        try:
            # Shield the worker so cancellation cannot release the concurrency guard
            # while the non-cancellable HTTP request is still running.
            worker = asyncio.create_task(asyncio.to_thread(self.client.synthesize, text))
            try:
                audio = await asyncio.shield(worker)
            except asyncio.CancelledError:
                await asyncio.gather(worker, return_exceptions=True)
                raise
            if not self.awake():
                self.defer(event)
                return
            if not self.permitted(event) or event.get_result() is not result:
                return
            record = Record.fromBase64(base64.b64encode(audio).decode(), text=text)
            if not self.guard_send(event, record, text):
                return
            result.chain = [record]
            event.set_extra('companion_voice_prepared', True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('Breeze voice unavailable; retained text (%s)', type(exc).__name__)
        finally:
            self.busy = False
