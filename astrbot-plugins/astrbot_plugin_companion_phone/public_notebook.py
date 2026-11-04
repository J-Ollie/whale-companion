"""Local source-backed public retrieval. Never imports private chats as knowledge."""
from pathlib import Path
from contextlib import closing
import hashlib
import json
import re
import sqlite3

STOP={'什么','怎么','可以','一下','有没有','告诉','知道','这个','那个','现在','自己','我们','你们','给我','帮我','请你','一下','哪些','如何','一个'}


def terms(text):
 text=str(text).lower();found=set(re.findall(r'[a-z][a-z0-9_-]{2,30}',text))
 for chunk in re.findall(r'[\u4e00-\u9fff]+',text):found.update(chunk[i:i+2] for i in range(len(chunk)-1))
 return sorted(found-STOP)[:4096]


class PublicNotebook:
 def __init__(self,root):self.path=Path(root)/'public-notes.sqlite3'

 def connect(self,write=False):
  if write:self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
  db=sqlite3.connect(self.path.resolve().as_uri()+('?mode=rwc' if write else '?mode=ro'),uri=True,timeout=3);db.row_factory=sqlite3.Row
  version=db.execute('PRAGMA user_version').fetchone()[0]
  if version==0 and write and not db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
   db.executescript('CREATE TABLE documents(ref TEXT PRIMARY KEY,url TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,content_hash TEXT NOT NULL,retrieved_at REAL NOT NULL); CREATE INDEX document_urls ON documents(url,retrieved_at); CREATE VIRTUAL TABLE document_fts USING fts5(ref UNINDEXED,terms); PRAGMA user_version=1;')
   self.path.chmod(0o600)
  elif version!=1:db.close();raise ValueError('public_notes_unknown_schema')
  return db

 def store(self,article):
  from .reading import public_url
  if not isinstance(article,dict) or article.get('material')!='web_text':raise ValueError('public_excerpt_required')
  url=public_url(article['url']);body=article.get('text','');title=article.get('title','')
  if not isinstance(body,str) or not 100<=len(body.encode())<=8000:raise ValueError('invalid_public_excerpt')
  checksum=hashlib.sha256(body.encode()).hexdigest()
  if checksum!=article.get('content_hash'):raise ValueError('public_excerpt_hash_mismatch')
  ref='pub:'+hashlib.sha256((url+'\n'+checksum).encode()).hexdigest()[:20]
  with closing(self.connect(True)) as db,db:
   if db.execute('SELECT 1 FROM documents WHERE ref=?',(ref,)).fetchone():return ref
   if db.execute('SELECT count(*) FROM documents').fetchone()[0]>=300:raise ValueError('public_notebook_full')
   db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?)',(ref,url,str(title)[:160],body,checksum,float(article['retrieved_at'])))
   db.execute('INSERT INTO document_fts VALUES(?,?)',(ref,' '.join(terms(str(title)+' '+body))))
  return ref

 def lookup(self,query,limit=3):
  if not self.path.exists():return []
  tokens=terms(str(query)[:500])[:18]
  if not tokens:return []
  match=' OR '.join('"'+t.replace('"','""')+'"' for t in tokens)
  with closing(self.connect()) as db:
   rows=db.execute('''SELECT d.*,bm25(document_fts) AS score FROM document_fts JOIN documents d ON d.ref=document_fts.ref
    WHERE document_fts MATCH ? AND d.rowid=(SELECT latest.rowid FROM documents latest WHERE latest.url=d.url ORDER BY latest.retrieved_at DESC,latest.rowid DESC LIMIT 1)
    ORDER BY score,d.retrieved_at DESC LIMIT ?''',(match,max(1,min(3,limit)))).fetchall()
  result=[]
  for row in rows:
   lower=row['body'].lower();locations=[lower.find(t) for t in tokens if lower.find(t)>=0];start=max(0,min(locations)-80) if locations else 0
   result.append({'reference':row['ref'],'url':row['url'],'title':row['title'],'excerpt':row['body'][start:start+550],
    'retrieved_at':row['retrieved_at'],'content_hash':row['content_hash'],'scope':'cached_public_excerpt','verification':'source_material_not_verified_fact'})
  return result

 def count(self):
  if not self.path.exists():return 0
  with closing(self.connect()) as db:return db.execute('SELECT count(*) FROM documents').fetchone()[0]
