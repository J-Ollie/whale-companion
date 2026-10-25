"""Sidecar validity and provenance. Existing memory IDs and raw rows are preserved."""
import json
import re
import time
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Shanghai')
CHANGE=re.compile(r'现在|改为|改成|不再|不是|记错|更正|纠正|其实|已经|换成|取消|完成|结束|不用再|不要再')
PLAN=re.compile(r'计划|打算|准备|约好|约定|答应|承诺|考试|明天|后天|下周|周末|下个月|待办|稍后|继续')
TRANSIENT=re.compile(r'今天|今晚|这会儿|此刻|刚才|现在.{0,6}(困|累|忙|难过|开心|不舒服|生气|心情)')


def supported_quote(quote,source):
    """Do not admit fragments that drop negation, a subject, or a hypothetical prefix."""
    if re.search(r'如果|假如|假设|要是|例如|比如|听说|朋友说|[他她](?:说|觉得)|开玩笑|说着玩|[?？]',quote):return False
    separators='，,。.!！?？；;：:\n\r\t '
    start=source.find(quote)
    while start>=0:
        end=start+len(quote)
        if (start==0 or source[start-1] in separators) and (end==len(source) or source[end] in separators):
            if end<len(source) and source[end] in '?？':return False
            return True
        start=source.find(quote,start+1)
    return False


def due_time(text,at):
    """Resolve only dates actually expressed in the evidence; unknown stays unknown."""
    if not isinstance(at,(int,float)):return None
    local=datetime.fromtimestamp(at,TZ);day=local.date();due=None
    match=re.search(r'(20\d\d)-(\d{2})-(\d{2})',text)
    try:
        if match:due=datetime.strptime(match.group(),'%Y-%m-%d').date()
        elif '后天' in text:due=day+timedelta(days=2)
        elif '明天' in text:due=day+timedelta(days=1)
        elif '下周' in text:due=day+timedelta(days=13-day.weekday())
        elif '周末' in text:due=day+timedelta(days=6-day.weekday())
        elif re.search(r'今天|今晚|这会儿|此刻',text):due=day
    except ValueError:return None
    return datetime.combine(due+timedelta(days=1),datetime.min.time(),TZ).timestamp() if due else None


class Lifecycle:
    def __init__(self,store):
        self.store=store
        store.db.executescript('''
            CREATE TABLE IF NOT EXISTS memory_state(qq TEXT,id TEXT,type TEXT,status TEXT,
                valid_from REAL,valid_to REAL,review_after REAL,source_kind TEXT,version INTEGER,
                PRIMARY KEY(qq,id));
            CREATE TABLE IF NOT EXISTS memory_conflicts(id INTEGER PRIMARY KEY,qq TEXT,entry_id TEXT,
                text TEXT,evidence TEXT,at REAL,resolved INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS conflict_person ON memory_conflicts(qq,entry_id,resolved);
            CREATE TABLE IF NOT EXISTS memory_links(qq TEXT,parent TEXT,child TEXT,relation TEXT,
                PRIMARY KEY(qq,parent,child,relation));
            CREATE TABLE IF NOT EXISTS memory_compendia(qq TEXT,id TEXT,topic TEXT,text TEXT,
                evidence TEXT,refs TEXT,fingerprint TEXT,updated REAL,PRIMARY KEY(qq,id));
            ''')
        # Metadata only: do not rewrite any existing entry, expiry, quote or ID.
        missing=store.db.execute('SELECT e.* FROM entries e LEFT JOIN memory_state s ON e.qq=s.qq AND e.id=s.id WHERE s.id IS NULL').fetchall()
        with store.atomic():
            for row in missing:self.register(dict(row),legacy=True)

    def register(self,row,*,kind=None,at=None,legacy=False):
        return self._register(row,kind=kind,at=at,legacy=legacy)

    def _register(self,row,*,kind=None,at=None,legacy=False):
        text=row['text'];category=row['kind'];now=time.time()
        if at is None:
            ids=json.loads(row['evidence'])
            if ids:
                result=self.store.db.execute('SELECT MAX(at) FROM observations WHERE qq=? AND id IN ('+','.join('?'*len(ids))+')',(row['qq'],*ids)).fetchone()
                at=result[0] if result else None
        if at is None and row['pinned']:at=row['updated']
        inferred=('semantic' if category=='fact' else 'working' if category=='continuity' else
                  'episode' if category=='episode' else 'impression' if category=='impression' else 'self_model')
        if category=='fact':
            inferred='plan' if PLAN.search(text) else 'state' if TRANSIENT.search(text) else 'semantic'
        category_type=kind if kind in ('semantic','plan','state') and category=='fact' else inferred
        end=due_time(text,at) if category_type in ('plan','state','working') else None
        review=(at or now)+(30*86400 if category_type=='impression' else 7*86400) if category_type in ('impression','working','plan','state') else None
        previous=self.store.db.execute('SELECT version FROM memory_state WHERE qq=? AND id=?',(row['qq'],row['id'])).fetchone()
        self.store.db.execute('INSERT OR REPLACE INTO memory_state VALUES(?,?,?,?,?,?,?,?,?)',
            (row['qq'],row['id'],category_type,'active',at,end,review,
             'manual' if row['pinned'] else 'inference' if category_type in ('impression','self_model') else
             'conversation' if category_type in ('episode','working') else 'self_report',
             previous[0]+1 if previous else 1))

    def describe(self,row,now=None):
        now=time.time() if now is None else now
        state=self.store.db.execute('SELECT * FROM memory_state WHERE qq=? AND id=?',(row['qq'],row['id'])).fetchone()
        data=dict(state) if state else {'type':row['kind'],'status':'active','valid_to':row.get('expires')}
        if data['status']=='active' and data.get('valid_to') and now>=data['valid_to']:
            data['status']='past_due' if data['type']=='plan' else 'historical'
        data['review_due']=bool(data.get('review_after') and now>=data['review_after'])
        if self.store.db.execute('SELECT 1 FROM memory_conflicts WHERE qq=? AND entry_id=? AND resolved=0',
                                 (row['qq'],row['id'])).fetchone():data['status']='conflicted'
        return {k:v for k,v in data.items() if k not in ('qq','id')}

    def current(self,row,now=None):
        state=self.describe(row,now)
        return state['status']=='active' and not (state['type'] in ('working','impression','state') and state['review_due'])

    def conflict(self,qq,entry_id,text,ids,now):
        if not self.store.db.execute('SELECT 1 FROM memory_conflicts WHERE qq=? AND entry_id=? AND text=? AND resolved=0',(qq,entry_id,text)).fetchone():
            self.store.db.execute('INSERT INTO memory_conflicts(qq,entry_id,text,evidence,at) VALUES(?,?,?,?,?)',
                                  (qq,entry_id,text,json.dumps(ids),now))
        self.store.db.execute('DELETE FROM memory_conflicts WHERE qq=? AND id NOT IN (SELECT id FROM memory_conflicts WHERE qq=? ORDER BY id DESC LIMIT 100)',(qq,qq))

    def resolve(self,qq,entry_id):
        self.store.db.execute('UPDATE memory_conflicts SET resolved=1 WHERE qq=? AND entry_id=?',(qq,entry_id))

    def forget(self,qq,entry_id,source_ids):
        # Remove derived bundles sharing forgotten evidence; raw administrator archive is separate.
        for row in self.store.db.execute('SELECT id,evidence FROM memory_compendia WHERE qq=?',(qq,)).fetchall():
            if set(json.loads(row['evidence'])) & set(source_ids):
                self.store.db.execute('DELETE FROM memory_compendia WHERE qq=? AND id=?',(qq,row['id']))
                self.store.db.execute('DELETE FROM memory_links WHERE qq=? AND parent=?',(qq,row['id']))
        self.store.db.execute('DELETE FROM memory_links WHERE qq=? AND (parent=? OR child=?)',(qq,entry_id,entry_id))
        self.store.db.execute('DELETE FROM memory_conflicts WHERE qq=? AND entry_id=?',(qq,entry_id))
        self.store.db.execute('DELETE FROM memory_state WHERE qq=? AND id=?',(qq,entry_id))
