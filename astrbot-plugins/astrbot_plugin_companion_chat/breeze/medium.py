"""Time-limited reply preferences scoped to the actual QQ private conversation."""
import math
import re
import sqlite3
from contextlib import closing
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
NUMBER = r'[0-9]{1,3}|[一二两三四五六七八九十]{1,3}|半'
WINDOW = re.compile(
    rf'^(?:今(?:天|日|晚)|明(?:天|日|晚)|(?:接下来|未来|从现在起|往后)(?:{NUMBER})(?:个)?(?:小时|钟头|分钟|天|周)|'
    rf'(?:到|直到)(?:(?:今[天晚]|明[天晚])?(?:上午|中午|下午|晚上)?(?:{NUMBER})(?:点半|点|[:：][0-5][0-9])|(?:今天|今日|明天|今晚)结束|午夜)(?:前|为止)?)')
VOICE = re.compile(
    r'(?:(?:我|你)?(?:都|一直|全程|每条|尽量)*(?:可以|能不能|请|想|想要|希望|只想|要|改成|改回|换成)?'
    r'(?:都|一直|只|全程)*(?:(?:用|发|回|给我发|给我回|听你发|听)语音(?:回复|回我|跟我聊|说话)?|听(?:你(?:一直)?(?:说话|说|讲)|你的声音))|'
    r'(?:都|只|全程)?语音(?:回复|回我|聊天|就好|就行))')
TEXT = re.compile(
    r'(?:(?:我|你)?(?:都|一直|全程|每条)*(?:可以|能不能|请|想|希望|只想|要|改回|改成|换成)?'
    r'(?:都|一直|只|全程)*(?:(?:用|发|回|给我发|给我回)文字(?:回复|回我|聊天)?|打字(?:回我|回复|聊天)?)|'
    r'(?:都|只|全程)?文字(?:回复|回我|聊天|就好|就行)|(?:别|不要|不用)发?语音|不方便听语音)')
CANCEL = re.compile(r'(?:取消(?:之前的)?(?:语音|文字|回复方式|媒介)偏好|(?:回复方式|媒介)?(?:恢复|改回)默认|(?:语音|文字)偏好取消)')
AMBIGUOUS = re.compile(r'^(?:这段时间|最近|这几天|一会儿|暂时|以后|今后|这阵子|接下来)(?:都|一直|先|只)?')


def clean_clause(text):
    return re.sub(r'[，。！!？?、；;～~\s]+$', '', text.strip()).replace(' ', '')


def direction(text):
    phrase = re.sub(r'(?:吧|呀|啊|哦|好吗|可以吗|行吗|就好|就行)$', '', clean_clause(text))
    change = re.fullmatch(r'(?:都)?(?:改回|改成|换成)(文字|打字|语音)(?:回复)?', phrase)
    if change:
        return 'voice' if change[1] == '语音' else 'text'
    # Keep endings such as “文字就好” valid even after removing a politeness suffix.
    if VOICE.fullmatch(phrase) or VOICE.fullmatch(clean_clause(text)):
        return 'voice'
    if TEXT.fullmatch(phrase) or TEXT.fullmatch(clean_clause(text)):
        return 'text'
    return None


def number(text):
    if text.isascii() and text.isdigit():
        return int(text)
    if text == '半':
        return .5
    digits = dict(zip('一二三四五六七八九两', (1, 2, 3, 4, 5, 6, 7, 8, 9, 2)))
    if '十' in text:
        tens, _, ones = text.partition('十')
        return 10 * (digits[tens] if tens else 1) + (digits[ones] if ones else 0)
    return digits[text]


def interval(window, issued):
    local = datetime.fromtimestamp(issued, TZ)
    midnight = datetime.combine(local.date(), time(), TZ)
    if window in ('今天', '今日', '明天', '明日', '今晚', '明晚'):
        day = midnight + timedelta(days=window.startswith('明'))
        start = day + (timedelta(hours=18) if window.endswith('晚') else timedelta())
        return max(issued, start.timestamp()), (day + timedelta(days=1)).timestamp()
    relative = re.fullmatch(rf'(?:接下来|未来|从现在起|往后)({NUMBER})(?:个)?(小时|钟头|分钟|天|周)', window)
    if relative:
        unit = {'分钟': 60, '小时': 3600, '钟头': 3600, '天': 86400, '周': 604800}
        return issued, issued + number(relative[1]) * unit[relative[2]]
    end = re.sub(r'^(?:直到|到)|(?:前|为止)$', '', window)
    day = midnight + timedelta(days=end.startswith('明'))
    if end == '午夜' or end.endswith('结束'):
        return issued, (day + timedelta(days=1)).timestamp()
    clock = re.fullmatch(rf'(?:今[天晚]|明[天晚])?(上午|中午|下午|晚上)?({NUMBER})(点半|点|[:：][0-5][0-9])', end)
    if not clock:
        raise ValueError('unsupported clock')
    hour = number(clock[2])
    minute = 30 if clock[3] == '点半' else int(clock[3][1:]) if len(clock[3]) == 3 else 0
    if (clock[1] in ('下午', '晚上') and hour < 12) or (clock[1] == '中午' and hour < 6):
        hour += 12
    if not isinstance(hour, int) or not 0 <= hour <= 24 or hour == 24 and minute:
        raise ValueError('invalid hour')
    return issued, (day + timedelta(hours=hour, minutes=minute)).timestamp()


def rule(text, issued):
    clause = clean_clause(text)
    if CANCEL.fullmatch(clause):
        return ('auto', issued, None)
    clause = re.sub(r'^(?:那我|那你|我|你|那)(?=(?:今|明|接下来|未来|到|直到|从现在起|往后))', '', clause)
    match = WINDOW.match(clause)
    medium = direction(clause[match.end():].lstrip('，,：:')) if match else None
    if not match:
        # “用语音回复到晚上十点” has the same window as “到晚上十点都用语音”.
        for endpoint in re.finditer(r'直到|到', clause):
            match = WINDOW.fullmatch(clause[endpoint.start():])
            if match:
                medium = direction(clause[:endpoint.start()])
                break
    if medium is None:
        return None
    try:
        starts, expires = interval(match[0], issued)
    except (KeyError, ValueError, OverflowError):
        return None
    return medium, starts, expires


def ambiguous(text):
    match = AMBIGUOUS.match(clean_clause(text))
    return bool(match and direction(clean_clause(text)[match.end():]))


class TimedMedium:
    def __init__(self, root):
        self.path = root / 'medium-preferences.sqlite3'

    def save(self, scope, parsed, issued, now):
        medium, starts, expires = parsed
        if not math.isfinite(issued) or issued > now + 5 or (expires is not None and (expires <= now or expires <= starts)):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(self.path, timeout=1)) as db, db:
            self.path.chmod(0o600)
            db.execute('''CREATE TABLE IF NOT EXISTS preferences(
                scope TEXT NOT NULL, issued REAL NOT NULL, medium TEXT NOT NULL,
                starts REAL NOT NULL, expires REAL,
                UNIQUE(scope, issued, medium, starts))''')
            db.execute('DELETE FROM preferences WHERE expires<=?', (now,))
            if medium == 'auto':
                db.execute('DELETE FROM preferences WHERE scope=? AND issued<=?', (scope, issued))
            db.execute('INSERT OR IGNORE INTO preferences VALUES(?,?,?,?,?)', (scope, issued, medium, starts, expires))
        return True

    def current(self, scope, now):
        if not self.path.is_file():
            return None
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=1)) as db:
            row = db.execute('''SELECT medium, expires FROM preferences
                WHERE scope=? AND starts<=? AND (expires IS NULL OR expires>?)
                  AND issued>=COALESCE((SELECT max(issued) FROM preferences WHERE scope=? AND medium='auto'),0)
                ORDER BY issued DESC, rowid DESC LIMIT 1''', (scope, now, now, scope)).fetchone()
        return row if row and row[0] in ('voice', 'text') else None
