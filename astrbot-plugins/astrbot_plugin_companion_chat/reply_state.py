"""Private reply lifecycle metadata. Never store chat text or model reasoning."""
import asyncio
from functools import wraps
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

from astrbot.api import logger
from astrbot.core.platform.astr_message_event import current_message_event

KEY = 'companion_reply_state_id'
SILENT = re.compile(r'^\s*(?:\[\[text\]\]\s*)?\[\[silent:(closing_ack|pause)\]\]\s*$', re.I)
TERMINAL = {'silent', 'submitted', 'merged', 'send_rejected', 'send_uncertain',
            'aborted', 'interrupted', 'no_confirmed_delivery', 'model_error', 'partial_send'}
LABELS = {'received':'已接收，等待合并', 'merged':'已合并到同一轮', 'generating':'正在处理回复请求',
          'generated':'已生成，尚无发送回执', 'empty_output':'正文为空，尚未确认发送',
          'prepared':'已准备回复，尚无发送回执', 'sending':'正在提交发送',
          'submitted':'平台已接受发送（不代表客户端已读）', 'silent':'明确选择静默',
          'send_rejected':'平台明确拒绝发送', 'send_uncertain':'发送结果不确定',
          'partial_send':'部分消息有回执，后续发送异常', 'aborted':'处理被取消',
          'interrupted':'处理被重载或重启中断', 'no_confirmed_delivery':'处理结束但无确认发送，需排查',
          'model_error':'模型返回错误'}
REASONS = {'closing_ack':'自然收尾', 'pause':'本轮暂不展开', 'followup_wait':'已接入等待补话的原轮次'}


def resilient(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            logger.warning('Reply-state metadata unavailable (%s)', type(exc).__name__)
            return None
    return wrapped


class ReplyState:
    def __init__(self, root):
        root = Path(root); root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root/'reply-state.sqlite3'
        self.db = sqlite3.connect(path, timeout=1)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''CREATE TABLE IF NOT EXISTS turns(
          id TEXT PRIMARY KEY, scope TEXT NOT NULL, input_id TEXT NOT NULL,
          received REAL NOT NULL, updated REAL NOT NULL, state TEXT NOT NULL,
          reason TEXT NOT NULL DEFAULT '', merged_into TEXT NOT NULL DEFAULT '',
          receipts TEXT NOT NULL DEFAULT '[]', response_chars INTEGER NOT NULL DEFAULT 0);
          CREATE INDEX IF NOT EXISTS turns_scope_time ON turns(scope,received);''')
        with self.db:
            marks = ','.join('?' for _ in TERMINAL)
            self.db.execute(f"UPDATE turns SET state='interrupted',updated=? WHERE state NOT IN ({marks})",
                            (time.time(), *TERMINAL))
            self.db.execute('DELETE FROM turns WHERE received<?', (time.time()-7*86400,))
            self.db.execute('DELETE FROM turns WHERE id NOT IN (SELECT id FROM turns ORDER BY received DESC LIMIT 10000)')
        path.chmod(0o600)
        self.begun = 0

    @resilient
    def begin(self, event, task=None):
        if event.get_extra(KEY):
            return
        identifier = uuid.uuid4().hex[:16]; now = time.time()
        input_id = str(getattr(getattr(event, 'message_obj', None), 'message_id', '') or '')[:80]
        with self.db:
            self.db.execute('INSERT INTO turns(id,scope,input_id,received,updated,state) VALUES(?,?,?,?,?,?)',
                            (identifier, str(event.unified_msg_origin), input_id, now, now, 'received'))
            self.begun += 1
            if self.begun % 64 == 0:
                self.db.execute('DELETE FROM turns WHERE received<?', (now-7*86400,))
                self.db.execute('DELETE FROM turns WHERE id NOT IN (SELECT id FROM turns ORDER BY received DESC LIMIT 10000)')
        event.set_extra(KEY, identifier)
        if isinstance(task, asyncio.Task):
            task.add_done_callback(lambda finished: self.finished(event, finished))

    @resilient
    def mark(self, event, state, reason=None):
        if not event or not event.get_extra(KEY):
            return
        with self.db:
            self.db.execute('UPDATE turns SET state=?,reason=COALESCE(?,reason),updated=? WHERE id=?',
                            (state, reason[:80] if reason is not None else None, time.time(), event.get_extra(KEY)))

    @resilient
    def merge(self, follower, leader):
        if not leader or not follower.get_extra(KEY) or not leader.get_extra(KEY):
            return
        # Never link one contact's metadata to another contact.
        if follower.unified_msg_origin != leader.unified_msg_origin:
            return
        with self.db:
            self.db.execute("UPDATE turns SET state='merged',merged_into=?,updated=? WHERE id=?",
                            (leader.get_extra(KEY), time.time(), follower.get_extra(KEY)))

    @resilient
    def response(self, event, response):
        text = response.completion_text or ''
        choice = SILENT.fullmatch(text)
        if choice and response.role == 'assistant' and not response.tools_call_name:
            self.mark(event, 'silent', choice[1].lower())
            response.completion_text = ''
            event.should_call_llm(True)
            event.stop_event()
            return
        if response.role == 'tool' or response.tools_call_name:
            return
        self.mark(event, 'model_error' if response.role == 'err' else 'generated' if text.strip() else 'empty_output',
                  'model_error' if response.role == 'err' else None)
        if event.get_extra(KEY):
            with self.db:
                self.db.execute('UPDATE turns SET response_chars=? WHERE id=?', (len(text),event.get_extra(KEY)))

    @resilient
    def delivery(self, action, params, value=None, error=None, attempted=False):
        event = current_message_event.get()
        private = action=='send_private_msg' or action=='send_msg' and params.get('message_type')=='private'
        if (not private or event is None or not event.get_extra(KEY)
                or event.get_extra('companion_reply_state_finished', False)
                or not event.get_extra('companion_reply_state_prepared', False)
                or str(params.get('user_id','')) != str(event.get_sender_id())):
            return
        if attempted:
            self.mark(event, 'sending'); return
        row = self.db.execute('SELECT receipts FROM turns WHERE id=?', (event.get_extra(KEY),)).fetchone()
        receipts = json.loads(row['receipts']) if row else []
        if error is not None:
            code = getattr(error, 'retcode', None)
            rejected = type(error).__name__=='ActionFailed' and isinstance(code, int) and code>=100
            self.mark(event, 'partial_send' if receipts else 'send_rejected' if rejected else 'send_uncertain', type(error).__name__)
            return
        if isinstance(value,dict) and (value.get('status')=='failed' or value.get('retcode',0)!=0):
            self.mark(event, 'partial_send' if receipts else 'send_rejected'); return
        receipt = value.get('data',value) if isinstance(value,dict) else None
        if not isinstance(receipt,dict) or not receipt.get('message_id'):
            self.mark(event, 'send_uncertain', 'missing_receipt'); return
        identifier = str(receipt['message_id'])[:80]
        if identifier not in receipts:
            receipts.append(identifier)
        with self.db:
            self.db.execute("UPDATE turns SET state='submitted',receipts=?,updated=? WHERE id=?",
                            (json.dumps(receipts[-16:]),time.time(),event.get_extra(KEY)))

    @resilient
    def finished(self, event, task):
        event.set_extra('companion_reply_state_finished', True)
        row = self.db.execute('SELECT state FROM turns WHERE id=?', (event.get_extra(KEY),)).fetchone()
        if not row:
            return
        failed = task.cancelled() or task.exception() is not None
        if row['state']=='submitted' and failed:
            self.mark(event, 'partial_send', 'aborted'); return
        if row['state'] in TERMINAL:
            return
        self.mark(event, 'aborted' if failed else 'no_confirmed_delivery', row['state'])

    def latest(self, scope, limit=8):
        return [dict(r) for r in self.db.execute('SELECT * FROM turns WHERE scope=? ORDER BY received DESC LIMIT ?',
                                                (str(scope),max(1,min(20,int(limit)))))][::-1]

    @resilient
    def context_hint(self, event):
        rows = [r for r in self.latest(event.unified_msg_origin)
                if r['id'] != event.get_extra(KEY) and r['state'] != 'merged'][-3:]
        if not rows:
            return ''
        facts = [{'state':r['state'], 'reason':r['reason'],
                  'receipt_count':len(json.loads(r['receipts']))} for r in rows]
        return ('当前同一会话最近回复状态，由程序记录，按时间从旧到新：'+json.dumps(facts,ensure_ascii=False)
                +'。silent才是明确选择静默，closing_ack表示自然收尾，pause表示当轮暂不展开，如实承认这个选择，不必替它编理由或否认静默；'
                'no_confirmed_delivery是缺少对应确认记录，不证明没有发送，也不能解释成主动不理人。'
                '对此只可说明尚未查到对应回执，不能断言“没发出去”或编造网络故障。'
                'submitted只说明平台接受，不证明对方已读。旧版没有记录的轮次仍未知。'
                '日常不用播报这些状态；被问到漏答或发送时按证据解释，不编造静默的心理原因。')

    def close(self):
        self.db.close()
