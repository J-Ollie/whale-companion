"""Full-history sender-filtered SQLite FTS; no vector service or LLM on writes."""
import re
import time

# Query routing only. These phrases remain untouched in stored source messages.
QUERY_FILLER=re.compile(r'你还记不记得|还记不记得|你记不记得|记不记得|你还记得|还记得|记得吗|'
    r'我们之前|我们以前|我们上次|之前聊过的|以前聊过的|上次聊过的|聊过的|说过的|'
    r'那件事情|这件事情|那件事|这件事|那个|这个|上次|以前|之前|最近|今天|昨天|前天|请问|告诉我')
QUERY_STOPWORDS={'什么','我们','你们','他们','一个','一些','事情','时候','内容','对话','一下'}


def query_tokens(text):
    return tokens(QUERY_FILLER.sub(' ',str(text))) - QUERY_STOPWORDS


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

    def search(self,qq,queries,limit=100,*,start=None,end=None,kinds=None,types=None,current=False,exclude=(),now=None,browse=False):
        # Query syntax comes exclusively from tokenization, never raw user SQL/FTS syntax.
        wanted=[]
        for query in queries[:5]:
            for token in sorted(query_tokens(query),key=lambda t:(-len(t),t))[:24]:
                if token not in wanted:wanted.append(token)
        if not wanted and not browse:return []
        match=' OR '.join('"'+t+'"' for t in wanted[:60])
        where=['memory_fts.qq=?'];args=[qq]
        if not browse:where.insert(0,'memory_fts MATCH ?');args.insert(0,match)
        # Filter kind, validity, suppression and already-shown IDs before top-k;
        # unrelated channels must not crowd the requested channel out of memory.
        joins=""" LEFT JOIN observations o ON o.qq=memory_fts.qq AND substr(memory_fts.ref,1,4)='obs:'
                    AND o.id=CAST(substr(memory_fts.ref,5) AS INTEGER)
                LEFT JOIN entries e ON e.qq=memory_fts.qq AND e.id=memory_fts.ref
                LEFT JOIN memory_state s ON s.qq=e.qq AND s.id=e.id """
        where.append("""((memory_fts.kind='observation' AND o.id IS NOT NULL AND NOT EXISTS
            (SELECT 1 FROM suppressed_sources b WHERE b.qq=memory_fts.qq AND b.observation_id=o.id))
            OR (memory_fts.kind!='observation' AND e.id IS NOT NULL AND NOT EXISTS
            (SELECT 1 FROM json_each(e.evidence) j JOIN suppressed_sources b ON b.observation_id=j.value
             WHERE b.qq=e.qq)))""")
        for column,values in (('memory_fts.kind',kinds),('s.type',types)):
            if values is not None:
                if not values:return []
                where.append(column+' IN ('+','.join('?' for _ in values)+')');args.extend(values)
        excluded=list(exclude)[:60]
        if excluded:where.append('memory_fts.ref NOT IN ('+','.join('?' for _ in excluded)+')');args.extend(excluded)
        if current:
            now=time.time() if now is None else now
            where.append("""(e.expires IS NULL OR e.expires>?) AND COALESCE(s.status,'active')='active'
                AND (s.valid_to IS NULL OR s.valid_to>?)
                AND NOT (s.type IN ('working','impression','state') AND s.review_after IS NOT NULL AND s.review_after<=?)
                AND NOT EXISTS(SELECT 1 FROM memory_conflicts c WHERE c.qq=e.qq AND c.entry_id=e.id AND c.resolved=0)""")
            args.extend((now,now,now))
        if start is not None or end is not None:
            # Apply the time range BEFORE top-k. Entry time follows its evidence,
            # exactly as the caller does, rather than the extraction/update date.
            at="""CASE WHEN memory_fts.kind='observation' THEN o.at ELSE
                COALESCE((SELECT MAX(s.at) FROM observations s JOIN json_each(e.evidence) j ON s.id=j.value
                          WHERE s.qq=e.qq),e.updated) END"""
            if start is not None:where.append('('+at+')>=?');args.append(start)
            if end is not None:where.append('('+at+')<?');args.append(end)
        args.append(min(200,limit))
        rank='0' if browse else 'bm25(memory_fts)'
        order='e.pinned DESC,e.updated DESC,e.id' if browse else 'rank'
        return [dict(row) for row in self.store.db.execute(
            'SELECT memory_fts.ref,memory_fts.kind,'+rank+' AS rank FROM memory_fts'+joins+
            ' WHERE '+' AND '.join(where)+' ORDER BY '+order+' LIMIT ?',args)]
