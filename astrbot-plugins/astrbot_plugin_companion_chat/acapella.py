"""Owner-approved RVC clips, with fixed titles and verified playback bytes."""
import hashlib
import json
import re


CLIPS = {
    '小半': ('xiaoban-acapella.wav', '5148a0c773dab1979924791716e1aab6c042344ec8e05818747b0f8fb15b87ab'),
    '去年夏天': ('last-summer-acapella.wav', '2b95fd16ebaa1b82e5ed408d671c380d1e4ee6ed9d3d4dd687b184b2a975ec97'),
}
MAX_BYTES = 1_600_000
TITLES = '|'.join(re.escape(title) for title in CLIPS)
INTENT = re.compile(
    r'(?:小鲸鱼[，,\s]*)?(?:请|能不能|可以|你能)?(?:给我|我想听你?|想听你?)?\s*'
    r'(?:清唱|唱|点播|播放|点歌|翻唱)\s*(?:一下|一段|一首)?\s*'
    rf'(?:《({TITLES})》|({TITLES}))(?:吧|吗|呀|嘛)?[。！？!?]*'
)
MENU = '清唱曲库有' + '、'.join(f'《{title}》' for title in CLIPS) + '。想听就直接说，我也可以自己选。'
TOOL = 'companion_acapella'
SELECTION = 'companion_acapella_selection'
PLAYBACK = 'companion_acapella_playback'
DECLINE = re.compile(r'(?:别|不要|不用|先不|不想听)\s*(?:你|给我|再|听|发){0,3}\s*(?:清唱|翻唱|唱|歌|语音)')
OPEN_INVITE = re.compile(
    r'(?:小鲸鱼[，,\s]*)?(?:(?:我(?:还|又|也)?想听|想听|给我|你能|能不能|可以|你|请|随便|随意|再)\s*){0,6}'
    r'(?P<action>清唱|翻唱|唱|换)\s*(?P<count>另一首|另外一首|别的一首|一首|一段|首|段|一下)?\s*'
    r'(?P<object>清唱|歌曲|歌)?\s*(?:试试|听听|给我听|好不好|可以吗|吧|呗|吗|呀|嘛)?[。！？!?]*'
)
RETRY = re.compile(
    r'(?:那(?:就)?[，,\s]*|好[，,\s]*|可以[，,\s]*)?(?:现在|这次|这回)?\s*(?:你)?\s*'
    r'(?:再|重新)?\s*(?:试(?:一下|一次|试|试看)?|来(?:一下|一次)|发(?:一下|一次))'
    r'(?:吧|呢|呀|吗|嘛)?[。！？!?]*'
)
GO_AHEAD = re.compile(r'(?:你)?(?:清唱|唱)(?:吧|呀|呗|嘛)?[。！？!?]*')
MAX_RETRY_AGE = 30 * 60
STYLE = {'小半': '舒缓，含蓄', '去年夏天': '更明亮，起伏较大'}
PROMPT = (
    '[companion_acapella] 私聊中你可以用 companion_acapella 直接发自己的清唱片段。'
    '“唱首歌试试”“随便唱一首清唱吧”“你挑一首”都是邀请。没有指定歌名时自己选一首并调用工具。'
    '结合前文接住“可以，小半吧”“好，就这首”等确认，也可以在正在聊唱歌且场合合适时自己选曲分享。'
    '可以直接调用，无需对方输入命令、再次确认或重复点歌。单纯聊歌、明确不想听或要求文字时，照常聊天。'
    '对方明确点了曲库外的歌时，说明目前缺少这首。工具成功只代表已选好本轮要发的音频，'
    '正常结束这一轮，留一句很短的过渡，别再调用其他发送工具、输出静默或声称已经送达。[/companion_acapella]'
)


def declines(text):
    return bool(DECLINE.search(text or ''))


def requested_title(text):
    match = INTENT.fullmatch(text.strip())
    return (match[1] or match[2]) if match else None


def open_invite(text):
    match = OPEN_INVITE.fullmatch(text.strip())
    if not match or match['action'] == '换' and not (match['count'] or match['object']):
        return None
    return ('different' if match['action'] == '换' or match['count'] in ('另一首', '另外一首', '别的一首')
            else 'any')


def retry_invitation(rows, scope, current_id, now):
    """Continue only the latest recent singing request from the same owner."""
    saw_go_ahead = False
    for row in reversed(rows):
        if row.get('role') != 'user' or row.get('origin') != scope:
            continue
        if current_id is not None and (row.get('id') or 0) >= current_id:
            continue
        at = row.get('at')
        if not isinstance(at, (int, float)) or not 0 <= now - at <= MAX_RETRY_AGE:
            break
        text = row.get('text', '').strip()
        if declines(text):
            return None, None
        title = requested_title(text)
        if title:
            return title, None
        mode = open_invite(text)
        if mode:
            if GO_AHEAD.fullmatch(text):
                saw_go_ahead = True
                continue
            return None, mode
        if not RETRY.fullmatch(text):
            break
    return None, 'any' if saw_go_ahead else None


def last_played(path, scope):
    try:
        row = json.loads(path.read_text(encoding='utf-8'))
        if row['scope'] == scope and row['receipt'] and row['title'] in CLIPS:
            return row['title']
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def remember_playback(path, scope, title, receipt):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps({'scope': scope, 'title': title, 'receipt': receipt}, ensure_ascii=False), encoding='utf-8')
    temp.chmod(0o600)
    temp.replace(path)


def read_clip(root, title):
    filename, digest = CLIPS[title]
    path = root / filename
    if path.is_symlink():
        raise ValueError('unexpected_acapella_path')
    with path.open('rb') as stream:
        audio = stream.read(MAX_BYTES + 1)
    if len(audio) > MAX_BYTES or hashlib.sha256(audio).hexdigest() != digest:
        raise ValueError('unverified_acapella_audio')
    return audio
