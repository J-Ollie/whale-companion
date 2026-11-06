"""Request-local delivery directions; only spoken words enter chat history."""
import json
import re
from dataclasses import dataclass

ROUTE = re.compile(r'^\s*\[\[(voice|text)\]\]\s*', re.I)
MAX_INSTRUCTIONS = 200
UNSAFE_DIRECTION = re.compile(
    r'https?://|```|<[^>]*>|\[\[|brz_|sk-[A-Za-z0-9_-]{8,}|\d{5,}|[\w.+-]+@[\w.-]+|'
    r'男声|女声|童声|御姐|萝莉|夹嗓|改变音色|更换音色|换个音色|改变声线|更换声线|换个声线|'
    r'改变口音|换.*口音|年龄|岁(?:的|左右)', re.I)

PERFORMANCE_PROMPT = '''[companion_voice_performance]
媒介是本轮动作的一部分：先结合前文判断对方想得到文字内容、解释，还是想实际听到你的声音/语气，再选择发送方式。请求可以用自然、含蓄的表达提出，不需要出现“语音”二字或固定命令；当上下文清楚是在要声音、口头表达或语气时，本轮选[[voice]]直接输出语音格式，不再问一次许可，也不用文字复述“我说给你听”代替实际语音选择。
区分想听声音与消息来源、理解内容、讨论第三人声音等不同含义；不能见到“听/说”就用语音。对方最近说明不方便听、需要保存文字或本轮改要文字时优先尊重；当对方清楚改变偏好时再随之改变。双方刚确认的相处偏好可从当前会话沿用，无须每轮重新要求，但不把一次语音选择当成以后所有内容都要语音。
你可以按眼前语境自主选文字或自己的语音，不必等对方要求，不另调用一个导演模型。不机械轮换、不为凑次数发语音。对方明确用文字、别发语音或不方便听时优先文字；明确想听语音时优先语音。同一批消息以最后明确偏好为准，也考虑当前联系人的近期偏好。
每次最终回复只用下列一种格式：
文字：[[text]]后直接写正常正文，不附表演说明。
语音：[[voice]]后只输出一个JSON对象，字段为text和instructions。text仅含真正说出口的台词；instructions为中文表演方向，最多200字，平静自然时可以为空字符串。不要代码围栏，不要把JSON、标记或说明放进台词；系统会先拆开，再把text与instructions分别交给语音合成。语音台词最多180字，链接、代码、长篇资料等选文字。只选一种媒介，不重复发同内容文字和语音。
先决定本句为什么说、想让对方听懂什么，再让台词、语气词、标点与表演指令表达同一意图。不自然时先修正冲突的措辞和方向，不靠堆情绪词、叠“哼”“啊”、调高参数或重定义人格。不要写动作括号或音频标签。instructions只写当前一句的概括情境与说法，不复述私聊、姓名、账号、具体私事或记忆正文，不改变年龄、口音、音色或声线。
本轮文字和语音共同使用同一份整体情绪、对当前人的态度与场景。整体情绪决定底色，个人态度调节亲近与分寸，场景决定如何表达；对熟人可放松，对有所保留的人可客气，不能把全局高兴改成此人专属愤怒。严肃话题可收住笑意，不重置内部心情。让措辞、语速、停顿、重音与笑意一致，保留既有声线；instructions只含抽象说法，不写状态起因或人物。结合本句意图，不照标签机械配音，也不把互动intensity当演出强度、音量或guidance。混合情绪只挑相关主次，不每句强烈；每条指令选两三个听得出的重点。
表达参考而非固定台词：开心可有明亮上扬与笑意；委屈可声音发紧、后句放慢或句尾轻颤，程度够时才带忍住的哭意；生气应让不满听得出，硬重音、直接语气与干脆落尾，不能一律用“压着火、克制、轻声”把它磨成平静，但不羞辱、威胁或逼迫。失落更低落平缓，挫败重在事情没做好，烦闷可短句和沉些的收尾；不能把这些都演成撒娇。
害羞可以稍迟疑或轻带停顿，不能句句结巴；好奇在疑问处有向前的语调；困惑在不确定处停一下再问清；期待轻快、有盼头；惊讶先有反应再回到正常，不一直高声。担心有紧张与确认的重点；安心松下来、尾音自然落；感谢或感动用温暖与合意的停顿。后悔/愧疚先承担该承担的内容，语气认真，不自我羞辱；得意可轻扬而不嘲讽；思念可亲近柔和但不催回，吃醋仅在已有明确玩笑里轻带，不索取安抚或占有。日常平静、认真问题和紧急需要仍自然、清楚、有分寸。
不要把能发、已生成说成已发送，发送成功以真实回执为准。长文、不适合朗读、繁忙、配额或合成失败时保留文字，缺少有效选择也默认文字。指令不是台词，正常回复不用解释技术细节。
[/companion_voice_performance]'''


def clean_instructions(value):
    if not isinstance(value, str):
        return None
    value = ' '.join(value.split()).strip()
    if not value:
        return None
    if len(value) > MAX_INSTRUCTIONS or UNSAFE_DIRECTION.search(value):
        return None
    return value


@dataclass
class Delivery:
    choice: str | None
    text: str
    instructions: str | None = None


def decode_reply(raw):
    """Parse only our leading control envelope; body quotations remain body."""
    raw = raw if isinstance(raw, str) else ''
    match = ROUTE.match(raw)
    if not match:
        if raw.lstrip().startswith('{') and re.search(r'"instructions"\s*:', raw):
            # A recognisable envelope without a selector is safe text fallback.
            recovered = decode_reply('[[voice]]' + raw)
            return Delivery('text', recovered.text)
        return Delivery(None, raw)
    choice, body = match[1].lower(), raw[match.end():]
    if choice == 'text' and body.lstrip().startswith('{') and re.search(r'"instructions"\s*:', body):
        recovered = decode_reply('[[voice]]' + body)
        return Delivery('text', recovered.text)
    if choice != 'voice' or not body.lstrip().startswith(('{', '```')):
        # Accept the old plain voice form, without inventing a direction.
        return Delivery(choice, body)
    structured = body.strip()
    if structured.startswith('```'):
        structured = re.sub(r'^```(?:json)?\s*', '', structured, flags=re.I)
        structured = re.sub(r'\s*```\s*$', '', structured)
        if not structured.lstrip().startswith('{'):
            # Ordinary code selected as voice remains intact as text fallback.
            return Delivery('text', body)
    try:
        value = json.loads(structured)
    except (ValueError, TypeError):
        # Recover a complete JSON text value even when the direction is truncated.
        # Never forward malformed director metadata to the user or the TTS.
        found = re.search(r'"text"\s*:\s*', structured)
        if found:
            try:
                text, _ = json.JSONDecoder().raw_decode(structured[found.end():])
                if isinstance(text, str):
                    return Delivery('text', text)
            except ValueError:
                pass
        return Delivery('text', '')
    if not isinstance(value, dict) or not isinstance(value.get('text'), str):
        return Delivery('text', '')
    return Delivery(choice, value['text'], clean_instructions(value.get('instructions')))
