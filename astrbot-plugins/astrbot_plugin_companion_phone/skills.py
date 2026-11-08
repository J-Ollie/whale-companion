from . import quota as q
"""Evidence-bound one-step search-entry recipes; never coordinates or page text."""
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

PACKAGES=frozenset(['com.coolapk.market','tv.danmaku.bili','com.baidu.tieba','mark.via','com.ss.android.ugc.aweme'])
SEARCH=re.compile(r'搜索|查找|search',re.I)
BLOCK=re.compile(r'支付|付款|购买|密码|验证码|发送|发布|关注|删除|pay|buy|send|publish|follow|delete',re.I)


def locator(package,node):
    if package not in PACKAGES or not isinstance(node,dict):return None
    rid=node.get('resource_id',node.get('resource-id',''))
    label=' '.join(str(node.get(k,'')) for k in ('text','description','content-desc','resource_id','resource-id'))
    enabled=node.get('enabled') in (True,'true')
    if not enabled or not SEARCH.search(label) or BLOCK.search(label):return None
    if not isinstance(rid,str) or not re.fullmatch(re.escape(package)+r':id/[A-Za-z][A-Za-z0-9_]{0,90}',rid):return None
    if node.get('_consuming') or node.get('_protected_navigation'):return None
    return {'resource_id':rid,'semantic':'search'}


def search_field(ui):
    return bool(ui.get('available') is True and any(n.get('enabled') is True and n.get('editable') is True
        and n.get('focused') is True and locator(p,n) is not None
        for n in ui.get('nodes',[]) for p in PACKAGES if str(n.get('resource_id','')).startswith(p+':id/')))


class Skills:
    def __init__(self,root):self.path=root/'skills.sqlite3'

    def db(self):
        db=sqlite3.connect(self.path,timeout=3);db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE IF NOT EXISTS recipes(id TEXT PRIMARY KEY,package TEXT,locator TEXT,state TEXT,created REAL,updated REAL)')
        db.execute('CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,recipe TEXT,session TEXT,day TEXT,at REAL,result TEXT,before_hash TEXT,after_hash TEXT,UNIQUE(recipe,session,result))')
        self.path.chmod(0o600);return db

    def attempt(self,package,target,before,session):
        loc=locator(package,target)
        if not loc or search_field(before) or not before.get('available') or not before.get('fingerprint'):return None
        # Only a live, unambiguous search target. An existing editor is not a learned entry.
        matches=[n for n in before.get('nodes',[]) if locator(package,n)==loc and n.get('clickable') is True and not n.get('editable')]
        if len(matches)!=1:return None
        return {'package':package,'locator':loc,'before_hash':before['fingerprint'],'session':session,'at':time.time()}

    def observe(self,attempt,after):
        if not attempt or not after.get('available') or not after.get('fingerprint'):return None
        if not 0<=time.time()-attempt['at']<=25:return None
        # A changed picture or model declaration is insufficient: require the focused search editor.
        success=search_field(after) and after['fingerprint']!=attempt['before_hash'] and any(
            n.get('focused') and n.get('editable') and locator(attempt['package'],n) for n in after.get('nodes',[]))
        result='focused_search_editor' if success else 'postcondition_missing'
        if not q.enabled() and self.path.exists() and self.path.stat().st_size>=8*1024*1024:return None
        raw=json.dumps(attempt['locator'],sort_keys=True,separators=(',',':'))
        key=hashlib.sha256((attempt['package']+raw).encode()).hexdigest()[:24]
        now=time.time();day=datetime.fromtimestamp(now,ZoneInfo('Asia/Shanghai')).date().isoformat()
        with closing(self.db()) as db,db:
            # A single verified failure suspends an adopted recipe. Missing observations never count as failure.
            db.execute('INSERT OR IGNORE INTO recipes VALUES(?,?,?,?,?,?)',(key,attempt['package'],raw,'candidate',now,now))
            row=db.execute('SELECT state FROM recipes WHERE id=?',(key,)).fetchone()
            inserted=db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,key,attempt['session'],day,now,result,attempt['before_hash'],after['fingerprint'])).rowcount
            if inserted:
                rows=db.execute('SELECT result,day,session FROM evidence WHERE recipe=? ORDER BY at DESC LIMIT 20',(key,)).fetchall()
                good=[]
                for r in rows:
                    if r['result']!='focused_search_editor':break
                    good.append(r)
                state='active' if len({r['session'] for r in good})>=3 and len({r['day'] for r in good})>=2 else 'suspended' if not success or row['state']=='suspended' else 'candidate'
                db.execute('UPDATE recipes SET state=?,updated=? WHERE id=?',(state,now,key))
        return {'skill_id':key,'result':result}

    def hints(self,package,ui):
        if package not in PACKAGES or not self.path.exists() or not ui.get('available') or search_field(ui):return []
        with closing(self.db()) as db:
            rows=db.execute("SELECT id,locator FROM recipes WHERE package=? AND state='active' ORDER BY updated DESC LIMIT 12",(package,)).fetchall()
        out=[]
        for row in rows:
            nodes=[n for n in ui.get('nodes',[]) if locator(package,n)==json.loads(row['locator']) and n.get('clickable') and not n.get('editable')]
            if len(nodes)==1 and nodes[0].get('node_id'):
                out.append({'skill':'打开搜索输入框','skill_id':row['id'],'node_id':nodes[0]['node_id'],
                    'verified_goal':'出现聚焦的搜索输入框；不代表已提交搜索或找到答案'})
        return out[:2]
