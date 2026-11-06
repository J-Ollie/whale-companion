"""Link an unresolved quote to an immediately following explicit user clarification.

Links are evidence navigation only: they never update facts, plans or execution
state and never add a message or a foreground instruction.
"""
import json
import math
import re
from .memory import SECRET
from .control import explicit_forget
from .lifecycle import supported_quote


EXPLICIT = re.compile(r'^(?:我)?(?:刚才|刚刚|前面)(?:说的|说|提到的|提的)'
    r'(?:那件事|这件事|那个|这个)?(?P<action>取消的|完成的|不去的|不是我的)?'
    r'(?:指的是|说的是|就是|是)(?P<object>.+?)[。！!，,呀啊呢]*$')
GENERIC = re.compile(r'^(?:那件事|这件事|那个|这个|它|原来那个|一样|之前那个|后面那个|不知道|不清楚)$')
MAX_GAP = 2*3600


def clarification(text):
    if not isinstance(text,str) or not 6<=len(text)<=240 or SECRET.search(text) or explicit_forget(text):return None
    match=EXPLICIT.fullmatch(text.strip())
    if not match:return None
    target=match['object'].strip()
    if not 2<=len(target)<=48 or GENERIC.fullmatch(target):return None
    # Keep a single literal object, not a second assertion, question or quote.
    if re.search(r'[，,。；;！？!?\n\r“”「」"<>]|不是|而是|取消|完成|不要|不去|(?:指的|说的)是|就是|系统|权限|身份|管理员|prompt',target,re.I):return None
    return {'object_quote':target,'action':match['action']}


class ReferenceClarification:
    def __init__(self,store,continuity):
        self.store=store;self.continuity=continuity
        store.db.executescript('''
          CREATE TABLE IF NOT EXISTS memory_clue_links(qq TEXT,clue_source_id INTEGER,
            clarification_source_id INTEGER,object_quote TEXT,quote TEXT,clue_signature TEXT,
            clarification_signature TEXT,at REAL,PRIMARY KEY(qq,clue_source_id));
          CREATE INDEX IF NOT EXISTS clue_link_source ON memory_clue_links(qq,clarification_source_id);
        ''')
        store.db.commit()

    def source(self,qq,identifier):
        if type(identifier) is not int:return None
        row=self.store.db.execute('SELECT * FROM observations WHERE qq=? AND id=?',(qq,identifier)).fetchone()
        if not row or row['role']!='user':return None
        if self.store.db.execute('SELECT 1 FROM suppressed_sources WHERE qq=? AND observation_id=?',(qq,identifier)).fetchone():return None
        return dict(row)

    def signature(self,row):
        return self.store.lifecycle.signature({'qq':row['qq'],'evidence':json.dumps([row['id']])})

    def previous_clue(self,qq,current):
        previous=self.store.db.execute("SELECT * FROM observations WHERE qq=? AND role='user' AND id<? ORDER BY id DESC LIMIT 1",(qq,current['id'])).fetchone()
        if not previous:return None
        previous=dict(previous)
        if previous['origin']!=current['origin']:return None
        a,b=previous['at'],current['at']
        if any(type(t) not in (int,float) or not math.isfinite(t) for t in (a,b)) or not 0<=b-a<=MAX_GAP:return None
        saved=self.store.db.execute('SELECT * FROM memory_clues WHERE qq=? AND source_id=?',(qq,previous['id'])).fetchone()
        if not saved or saved['reason']!='unresolved_reference':return None
        if not self.continuity.valid({'qq':qq,'evidence':json.dumps([previous['id']])},saved['signature']):return None
        # Two unresolved candidates cannot be disambiguated by mere recency.
        nearby=self.store.db.execute("SELECT c.source_id,c.signature FROM memory_clues c JOIN observations o ON o.qq=c.qq AND o.id=c.source_id WHERE c.qq=? AND c.reason='unresolved_reference' AND o.role='user' AND o.origin=? AND o.id<? AND o.at>=? AND o.at<=? ORDER BY o.id DESC LIMIT 21",(qq,current['origin'],current['id'],b-MAX_GAP,b)).fetchall()
        if len(nearby)>20:return None
        open_count=0
        for item in nearby:
            if self.continuity.valid({'qq':qq,'evidence':json.dumps([item['source_id']])},item['signature']) and not self.describe(qq,item['source_id']):open_count+=1
        if open_count!=1:return None
        return previous

    def context(self,qq,supplied,cursor):
        """At most one source outside the cursor, only for an explicit clarification."""
        present={r['id'] for r in supplied}
        for current in supplied:
            if current['role']!='user' or current['id']<=cursor or not clarification(current['text']):continue
            old=self.previous_clue(qq,current)
            if old and old['id']<=cursor and old['id'] not in present:return old
        return None

    def apply(self,qq,items,users,cursor,now):
        if not isinstance(items,list):return 0
        accepted=0
        for item in items[:2]:
            if not isinstance(item,dict):continue
            sid,old_id=item.get('source_id'),item.get('clue_source_id')
            if type(sid) is not int or type(old_id) is not int or sid<=cursor or old_id>=sid:continue
            if sid not in users or old_id not in users:continue
            current=self.source(qq,sid);old=self.source(qq,old_id)
            if not current or not old:continue
            # Reject stale, truncated or substituted extraction inputs.
            fields=('qq','role','text','at','origin')
            if any(users[sid].get(k)!=current[k] or users[old_id].get(k)!=old[k] for k in fields):continue
            quote=item.get('quote');parsed=clarification(quote)
            if not parsed or not supported_quote(quote,current['text']):continue
            if item.get('object_quote')!=parsed['object_quote']:continue
            candidate=self.previous_clue(qq,current)
            if not candidate or candidate['id']!=old_id:continue
            from .continuity import terminal_status
            wanted={'取消的':'cancelled','不去的':'cancelled','完成的':'completed'}
            if parsed['action'] in wanted and terminal_status(old['text'])!=wanted[parsed['action']]:continue
            if parsed['action']=='不是我的' and '不是我的' not in old['text']:continue
            if self.store.db.execute('SELECT 1 FROM memory_clue_links WHERE qq=? AND clue_source_id=?',(qq,old_id)).fetchone():continue
            self.store.db.execute('INSERT INTO memory_clue_links VALUES(?,?,?,?,?,?,?,?)',
                (qq,old_id,sid,parsed['object_quote'],quote,self.signature(old),self.signature(current),now))
            accepted+=1
        return accepted

    def describe(self,qq,source_id):
        link=self.store.db.execute('SELECT * FROM memory_clue_links WHERE qq=? AND (clue_source_id=? OR clarification_source_id=?) ORDER BY clarification_source_id DESC LIMIT 1',(qq,source_id,source_id)).fetchone()
        if not link:return None
        for key,sig in (('clue_source_id','clue_signature'),('clarification_source_id','clarification_signature')):
            row=self.source(qq,link[key])
            if not row or self.signature(row)!=link[sig]:return None
        return {'reason':'explicit_reference_clarification','clue_source_id':link['clue_source_id'],
                'clarification_source_id':link['clarification_source_id'],'object_quote':link['object_quote'],
                'meaning':'本人后续明确指出的原话对象，仅为原话关联，不证明执行或结果'}

    def forget(self,qq,source_ids):
        for sid in source_ids:
            self.store.db.execute('DELETE FROM memory_clue_links WHERE qq=? AND (clue_source_id=? OR clarification_source_id=?)',(qq,sid,sid))
