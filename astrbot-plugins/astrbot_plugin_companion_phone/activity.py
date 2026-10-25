"""Metadata-only receipts for completed reads/actions; never chat claims or UI text."""
import hashlib
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
EVENTS = {
    'space_posts': ('reading', 'QQ空间', '空间阅读', '读取了自己空间的动态列表', True),
    'space_comments': ('reading', 'QQ空间', '评论阅读', '读取了空间中已加载的公开评论', True),
    'site_visitors': ('reading', '网站', '访客摘要', '读取了网站的来访摘要', True),
    'phone_open': ('phone_action', '手机', '打开应用', '完成了打开应用的操作，未据此认定已经阅读', False),
    'phone_screen': ('phone_action', '手机', '观察画面', '成功取得手机当前画面，未保存画面正文', False),
    'phone_scroll': ('phone_action', '手机', '翻页操作', '完成了手机滑动操作，未据此认定已读中间内容', False),
    'phone_input': ('phone_action', '手机', '输入操作', '完成了输入操作，不记录输入内容，不代表已发送', False),
    'phone_tap': ('phone_action', '手机', '点按操作', '完成了点按操作，具体页面结果需另行核验', False),
    'phone_key': ('phone_action', '手机', '系统按键', '完成了一次手机系统按键操作', False),
}


class ActivityLedger:
    def __init__(self, root):
        self.path = Path(root) / 'activity.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS receipts(
                id TEXT PRIMARY KEY,action TEXT NOT NULL,at REAL NOT NULL,last_at REAL NOT NULL,
                origin TEXT NOT NULL,count INTEGER NOT NULL DEFAULT 1)''')
        self.path.chmod(0o600)

    def record(self, action, *, origin='tool', key='', now=None):
        if action not in EVENTS or origin not in ('tool', 'owner_requested', 'background'):
            return None
        now = time.time() if now is None else now
        # Repeated same-kind reads in a short interval are one timeline item.
        digest = hashlib.sha256(f'{action}:{origin}:{key or int(now//300)}'.encode()).hexdigest()
        try:
            with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
                db.execute('''INSERT INTO receipts VALUES(?,?,?,?,?,1)
                    ON CONFLICT(id) DO UPDATE SET last_at=excluded.last_at,count=count+1''',
                    (digest, action, now, now, origin))
            return digest
        except (OSError, sqlite3.Error):
            return None  # Logging failure must not repeat a successful phone action.

    def sources(self, since=0):
        with closing(sqlite3.connect(self.path, timeout=3)) as db:
            rows = db.execute('SELECT id,action,at FROM receipts WHERE at>=? ORDER BY at DESC LIMIT 500', (since,)).fetchall()
        return [{'id':'activity:'+i, 'kind':'activity_receipt', 'action':action, 'timestamp':at,
                 'app':EVENTS[action][1], 'note':EVENTS[action][3], 'views':1,
                 'public_content':True, 'public_scope':'self_activity'}
                for i, action, at in rows if action in EVENTS and EVENTS[action][4]]


def public_source(row):
    from .access import PUBLIC_APPS
    if row.get('public_content') is not True:
        return False
    if row.get('app') in PUBLIC_APPS:
        return True
    event = EVENTS.get(row.get('action'))
    return bool(event and event[4] and row.get('kind') == 'activity_receipt'
                and row.get('public_scope') == 'self_activity' and row.get('app') == event[1]
                and row.get('note') == event[3])
