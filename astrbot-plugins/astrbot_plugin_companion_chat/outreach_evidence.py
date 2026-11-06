"""Contact-scoped operational facts, without chat text, credentials or raw logs."""
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import re
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
STAGES = {'eligibility', 'context', 'decision', 'generation', 'voice', 'send', 'worker'}
RESULTS = {'started', 'blocked', 'selected', 'quiet', 'ready', 'unavailable',
           'attempted', 'submitted', 'rejected', 'uncertain', 'failed', 'interrupted'}
REASONS = {'policy_or_interval', 'model_disabled', 'whitelist_denied', 'not_awake',
           'conversation_unavailable', 'history_unavailable', 'unsupported_generated_claim',
           'unknown_exploration', 'state_changed', 'conversation_changed', 'reservation_unavailable',
           'voice_service_unavailable', 'voice_busy', 'synthesis_failed', 'medium_preference_changed'}


class OutreachEvidence:
    def __init__(self, root, scope):
        self.path = Path(root)/'outreach-events.sqlite3'
        self.scope = scope
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS meta(scope TEXT PRIMARY KEY, started REAL NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, at REAL NOT NULL,
                kind TEXT NOT NULL, stage TEXT NOT NULL, result TEXT NOT NULL,
                reason TEXT NOT NULL, error_type TEXT NOT NULL, run_id TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS events_scope_at ON events(scope, at)')
            db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)', (scope, time.time()))
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        return db

    def record(self, stage, result, *, kind='spontaneous', reason='', error_type='', run_id='', now=None):
        if stage not in STAGES or result not in RESULTS or kind not in ('spontaneous', 'commitment', 'outcome_notice'):
            raise ValueError('invalid_operational_event')
        if reason and reason not in REASONS:
            raise ValueError('invalid_operational_reason')
        # Do not accept exception messages, model output, URLs or arbitrary text.
        if error_type and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', error_type):
            error_type = 'UnknownError'
        run_id=run_id or uuid.uuid4().hex[:16]
        if not re.fullmatch(r'[a-f0-9]{16}',run_id):raise ValueError('invalid_run_id')
        now = time.time() if now is None else now
        with closing(self.connect()) as db, db:
            last = db.execute('SELECT * FROM events WHERE scope=? ORDER BY id DESC LIMIT 1', (self.scope,)).fetchone()
            if (last and result == 'blocked' and now-last['at'] < 1800
                    and (last['kind'], last['stage'], last['result'], last['reason']) == (kind, stage, result, reason)):
                return
            db.execute('INSERT INTO events(scope,at,kind,stage,result,reason,error_type,run_id) VALUES(?,?,?,?,?,?,?,?)',
                       (self.scope, now, kind, stage, result, reason, error_type, run_id))

    def snapshot(self, day='', *, limit=18, now=None):
        now = time.time() if now is None else now
        today = datetime.fromtimestamp(now, TZ).date()
        if day and (not isinstance(day,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day)):
            raise ValueError('invalid_day')
        target = datetime.strptime(day, '%Y-%m-%d').date() if day else today
        if not today-timedelta(days=6) <= target <= today:
            raise ValueError('day_out_of_range')
        start = datetime.combine(target, datetime.min.time(), TZ).timestamp()
        end = min(start+86400, now)
        limit = max(1, min(int(limit), 40))
        with closing(self.connect()) as db:
            started = db.execute('SELECT started FROM meta WHERE scope=?', (self.scope,)).fetchone()['started']
            args = (self.scope, start, end)
            rows = db.execute('''SELECT at,kind,stage,result,reason,error_type,run_id FROM events
                WHERE scope=? AND at>=? AND at<=? ORDER BY id DESC LIMIT ?''', (*args, limit)).fetchall()
            count = db.execute('SELECT COUNT(*) FROM events WHERE scope=? AND at>=? AND at<=?', args).fetchone()[0]
            counts = db.execute('''SELECT stage,result,COUNT(*) AS count FROM events
                WHERE scope=? AND at>=? AND at<=? GROUP BY stage,result''', args).fetchall()
        return {'source': 'own_outreach_events', 'day': target.isoformat(),
                'recording_started_at': datetime.fromtimestamp(started, TZ).isoformat(),
                'as_of': datetime.fromtimestamp(now, TZ).isoformat(),
                'events': [dict(row) | {'at': datetime.fromtimestamp(row['at'], TZ).isoformat()} for row in reversed(rows)],
                'counts': [dict(row) for row in counts], 'total': count, 'more': count > len(rows),
                'notice': '只覆盖启用后的当前管理员主动联系流程，不是全部聊天或永久完整日志。按run_id区分每轮，不混用不同轮次的结果；more为true时仅列末尾事件。没有事件不证明没想过或没行动。selected只证明本次模型选择联系；voice失败表示未完成语音准备，不是QQ发送失败；attempted才是调用发送，submitted仅证明平台接受，不代表对方收到、已读或听到。failed不能抹掉同轮更早的submitted。不可据此判定人格、疏忽或道德责任，也不能用当前状态替代历史。'}
