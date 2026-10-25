"""Full-history sender-filtered SQLite FTS; no vector service or LLM on writes."""
import re


def tokens(text):
    result=set(re.findall(r'[a-z0-9_]{2,}',str(text).lower()))
    for chunk in re.findall(r'[\u4e00-\u9fff]+',str(text)):
        result.update(chunk[i:i+2] for i in range(len(chunk)-1))
        # Single-character names still have an exact lexical fallback token.
        if len(chunk)==1:result.add(chunk)
    return result


class RecallIndex:
    def __init__(self,store):
        self.store=store
        store.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(qq UNINDEXED,ref UNINDEXED,kind UNINDEXED,tokens,tokenize='unicode61')")
        store.db.commit()
        if store.get_meta('recall_index_version',0)!=1:
            with store.atomic():
                store.db.execute('DELETE FROM memory_fts')
                for row in store.db.execute('SELECT qq,id,text FROM observations').fetchall():
                    self.put(row['qq'],'obs:'+str(row['id']),'observation',row['text'])
                for row in store.db.execute('SELECT qq,id,kind,topic,text FROM entries').fetchall():
                    self.put(row['qq'],row['id'],row['kind'],row['topic']+' '+row['text'])
                store.set_meta('recall_index_version',1)

    def delete(self,qq,ref):
        self.store.db.execute('DELETE FROM memory_fts WHERE qq=? AND ref=?',(qq,ref))

    def put(self,qq,ref,kind,text):
        self.delete(qq,ref)
        self.store.db.execute('INSERT INTO memory_fts(qq,ref,kind,tokens) VALUES(?,?,?,?)',
                              (qq,ref,kind,' '.join(sorted(tokens(text)))))

    def search(self,qq,queries,limit=100):
        # Query syntax comes exclusively from tokenization, never raw user SQL/FTS syntax.
        wanted=[]
        for query in queries[:5]:
            for token in sorted(tokens(query),key=lambda t:(-len(t),t))[:24]:
                if token not in wanted:wanted.append(token)
        if not wanted:return []
        match=' OR '.join('"'+t+'"' for t in wanted[:60])
        return [dict(row) for row in self.store.db.execute(
            'SELECT ref,kind,bm25(memory_fts) AS rank FROM memory_fts WHERE memory_fts MATCH ? AND qq=? ORDER BY rank LIMIT ?',
            (match,qq,min(200,limit)))]
