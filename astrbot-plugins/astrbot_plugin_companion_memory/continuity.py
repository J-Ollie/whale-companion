"""Source-bound event times, revision meaning and unresolved reference clues.

These are internal navigation aids, not instructions or independent evidence.
No model calls, retrospective extraction, deadline completion or outgoing chat.
"""
import calendar
import hashlib
import json
import re
from datetime import datetime, timedelta
from .memory import TZ


TIME = re.compile(r'20\d{2}(?:年\d{1,2}月(?:\d{1,2}[日号])?|-\d{1,2}-\d{1,2})|前天|昨天|今天|明天|后天|上个月|这个月|本月|下个月|去年|今年|明年')
VAGUE = re.compile(r'^(?:那件事|这件事|那个|这个|它|后来|还是)?(?:已经|后来)?(?:取消了|完成了|不去了|不要了|改了|不是我的|不是那个|原来那个|我一定会实现的)[了吧啊呀呢]*$')
CORRECTION = re.compile(r'记错|说错|搞错|纠正|更正|不是.*而是')
FUTURE = re.compile(r'打算|计划|准备|将要|以后|明天|后天|下个月|明年')


def unresolved(text):
    return bool(VAGUE.fullmatch(re.sub(r'[\s，。！？!?；;、]', '', text)))


def future_change(quote):
    if re.search(r'取消|不去了|不要了|完成|结束|已经|做完',quote):return False
    return bool(re.search(r'(?:打算|计划|准备|将要).*(?:改|换|搬|不再|去|做)|(?:明天|后天|下个月|明年).*(?:改|换|搬)',quote))


def terminal_status(quote):
    # Negated results, questions and future intentions cannot close a plan.
    if not re.search(r'已经|已取消|已完成',quote) and re.search(r'(?:打算|准备|将要|我计划|我们计划|计划在).*(?:取消|完成|结束|做完)|(?:明天|后天|下个月|明年)(?:我|我们)?(?:要|会|将|再|才|能|就|得)*(?:完成|结束|取消|做完)',quote):return None
    if re.search(r'[?？]|(?:没|未|不|别|不要)(?:有|会|想|能|再|要|曾|被|已经|真|打算|准备|决定|过){0,3}(?:取消|完成|结束|做完)',quote):return None
    if re.search(r'取消|不去了|不要了|不用再|不要再',quote):return 'cancelled'
    if re.search(r'完成|结束|做完',quote):return 'completed'
    return None


def event_time(quote, source_at):
    """One explicit time at the start of one assertion, anchored to its message.

    End dates are inclusive. A month/year remains an interval; never invent a
    day or borrow a time from a different clause. Ambiguous ranges stay raw.
    """
    if not source_at or not isinstance(quote, str):return None
    matches=list(TIME.finditer(quote))
    if len(matches)!=1:return None
    m=matches[0]
    if not re.fullmatch(r'(?:我|其实|是|在|我在|我说的是|对了|已经|大概|大约)*',quote[:m.start()]):return None
    if re.search(r'如果|假如|要是|听说|他说|她说|[？?“”「」]',quote):return None
    anchor=datetime.fromtimestamp(source_at,TZ).date();token=m.group();precision='day'
    try:
        if token in ('前天','昨天','今天','明天','后天'):
            lo=hi=anchor+timedelta(days={'前天':-2,'昨天':-1,'今天':0,'明天':1,'后天':2}[token])
        elif token in ('上个月','这个月','本月','下个月'):
            month=anchor.year*12+anchor.month-1+{'上个月':-1,'这个月':0,'本月':0,'下个月':1}[token]
            year,index=divmod(month,12);lo=anchor.replace(year=year,month=index+1,day=1)
            hi=lo.replace(day=calendar.monthrange(year,index+1)[1]);precision='month'
        elif token in ('去年','今年','明年'):
            year=anchor.year+{'去年':-1,'今年':0,'明年':1}[token]
            lo=anchor.replace(year=year,month=1,day=1);hi=lo.replace(month=12,day=31);precision='year'
        else:
            nums=list(map(int,re.findall(r'\d+',token)));year,month=nums[:2]
            lo=anchor.replace(year=year,month=month,day=nums[2] if len(nums)>2 else 1)
            hi=lo if len(nums)>2 else lo.replace(day=calendar.monthrange(year,month)[1])
            precision='day' if len(nums)>2 else 'month'
    except (ValueError,OverflowError,OSError):return None
    return {'start':lo.isoformat(),'end':hi.isoformat(),'precision':precision,'phrase':token,
            'kind':'planned' if FUTURE.search(quote) or lo>anchor else 'reported',
            'basis':'user_quote_message_time'}


def relation(quote):
    if CORRECTION.search(quote):return 'correction'
    status=terminal_status(quote)
    if status:return {'cancelled':'cancellation','completed':'completion'}[status]
    if re.search(r'也|还|补充|另外',quote) and not re.search(r'改|不再|现在',quote):return 'addition'
    if re.search(r'改为|改成|换成|不再|现在|已经',quote):return 'change'
    return 'revision'


class MemoryContinuity:
    def __init__(self,store):
        self.store=store
        store.db.executescript('''
          CREATE TABLE IF NOT EXISTS memory_continuity(id INTEGER PRIMARY KEY,qq TEXT,entry_id TEXT,
            updated REAL,signature TEXT,text_digest TEXT,event_time TEXT,relation TEXT,previous TEXT,previous_signature TEXT);
          CREATE INDEX IF NOT EXISTS continuity_entry ON memory_continuity(qq,entry_id,id);
          CREATE TABLE IF NOT EXISTS memory_clues(qq TEXT,source_id INTEGER,signature TEXT,reason TEXT,
            PRIMARY KEY(qq,source_id));
        ''')
        store.db.commit()
        from .clarification import ReferenceClarification
        self.references=ReferenceClarification(store,self)

    def clue(self,qq,source_id,reason='unresolved_reference'):
        row={'qq':qq,'evidence':json.dumps([source_id])}
        self.store.db.execute('INSERT OR REPLACE INTO memory_clues VALUES(?,?,?,?)',
            (qq,source_id,self.store.lifecycle.signature(row),reason))

    def clue_note(self,qq,source_id):
        clarified=self.references.describe(qq,source_id)
        if clarified:return clarified
        saved=self.store.db.execute('SELECT * FROM memory_clues WHERE qq=? AND source_id=?',(qq,source_id)).fetchone()
        if not saved or not self.valid({'qq':qq,'evidence':json.dumps([source_id])},saved['signature']):return None
        return {'reason':saved['reason'],'meaning':'原话线索，指向或生效时间尚未确定，不表示事项已完成或旧事实已被替代'}

    def valid(self,row,signature):
        blocked={r[0] for r in self.store.db.execute('SELECT observation_id FROM suppressed_sources WHERE qq=?',(row['qq'],))}
        return bool(signature and not set(json.loads(row['evidence']))&blocked and self.store.lifecycle.signature(row)==signature)

    def capture(self,row,previous=None):
        if row['kind']!='fact':return
        ids=json.loads(row['evidence']);timing=None;quote=row['text'].split('说：',1)[-1]
        if len(ids)==1:
            source=self.store.db.execute('SELECT * FROM observations WHERE qq=? AND id=?',(row['qq'],ids[0])).fetchone()
            from .lifecycle import supported_quote
            if source and source['role']=='user' and supported_quote(quote,source['text']):timing=event_time(quote,source['at'])
        self.store.db.execute('INSERT INTO memory_continuity(qq,entry_id,updated,signature,text_digest,event_time,relation,previous,previous_signature) VALUES(?,?,?,?,?,?,?,?,?)',
            (row['qq'],row['id'],row['updated'],self.store.lifecycle.signature(row),hashlib.sha256(row['text'].encode()).hexdigest(),
             json.dumps(timing,ensure_ascii=False) if timing else None,relation(quote) if previous else None,
             json.dumps(previous,ensure_ascii=False) if previous else None,self.store.lifecycle.signature(previous) if previous else None))

    def describe(self,row,history=False):
        saved=self.store.db.execute('SELECT * FROM memory_continuity WHERE qq=? AND entry_id=? ORDER BY id DESC LIMIT 1',(row['qq'],row['id'])).fetchone()
        if not saved or saved['updated']!=row['updated'] or saved['text_digest']!=hashlib.sha256(row['text'].encode()).hexdigest() or not self.valid(row,saved['signature']):return {}
        result={}
        if saved['event_time']:result['event_time']=json.loads(saved['event_time'])
        if saved['relation']:result['revision_kind']=saved['relation']
        if history:
            chain=[]
            for item in self.store.db.execute('SELECT * FROM memory_continuity WHERE qq=? AND entry_id=? AND previous IS NOT NULL ORDER BY id DESC LIMIT 3',(row['qq'],row['id'])):
                old=json.loads(item['previous'])
                if not self.valid(old,item['previous_signature']):continue
                if item['id']==saved['id']:new=row
                else:
                    successor=self.store.db.execute('SELECT previous FROM memory_continuity WHERE qq=? AND entry_id=? AND id>? ORDER BY id LIMIT 1',(row['qq'],row['id'],item['id'])).fetchone()
                    if not successor or not successor['previous']:continue
                    new=json.loads(successor['previous'])
                    if new['updated']!=item['updated'] or hashlib.sha256(new['text'].encode()).hexdigest()!=item['text_digest']:continue
                if not self.valid(new,item['signature']):continue
                link={'relation':item['relation'],'previous_text':old['text'][:180],
                      'previous_source_ids':json.loads(old['evidence'])[:4],'current_source_ids':json.loads(new['evidence'])[:4]}
                if item['relation']=='correction':link['meaning']='旧记法被纠正，不代表过去真实如此'
                chain.append(link)
            if chain:result['history']=chain
        return result

    def forget(self,qq,entry_id,source_ids):
        self.store.db.execute('DELETE FROM memory_continuity WHERE qq=? AND entry_id=?',(qq,entry_id))
        for sid in source_ids:self.store.db.execute('DELETE FROM memory_clues WHERE qq=? AND source_id=?',(qq,sid))
        self.references.forget(qq,source_ids)
