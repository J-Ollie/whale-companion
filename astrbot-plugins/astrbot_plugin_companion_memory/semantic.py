"""Private KB bridge reusing the installed Obsidian sync engine, not its public staging UI."""
import asyncio
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

from .vault import Vault, fingerprint
from .memory import TZ
from .kb_access import CAPABILITY,install as install_boundary
from datetime import datetime

PREFIX = 'WhaleMemoryPrivate-'


def index_bucket(card):
    return ('' if card['kind']=='observation' else 'shallow-')+card['bucket']


def bucket_label(bucket):
    return ('浅层记忆卡-' if bucket.startswith('shallow-') else '深层原话-')+('历史' if bucket.endswith('history') else '当前')


class SemanticMemory:
    def __init__(self, plugin):
        self.plugin = plugin
        self.root = plugin.store.folder/'semantic'
        self.root.mkdir(parents=True, exist_ok=True); self.root.chmod(0o700)
        self.vault = Vault(plugin.cognition(), self.root/'vault',owner=plugin.owner)
        self.db = sqlite3.connect(self.root/'index.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''CREATE TABLE IF NOT EXISTS scopes(qq TEXT,bucket TEXT,kb_id TEXT,kb_name TEXT,PRIMARY KEY(qq,bucket));
          CREATE TABLE IF NOT EXISTS docs(qq TEXT,ref TEXT,bucket TEXT,doc_id TEXT,fingerprint TEXT,PRIMARY KEY(qq,ref));
          CREATE TABLE IF NOT EXISTS budget(day TEXT,kind TEXT,units INTEGER,chars INTEGER,PRIMARY KEY(day,kind));''')
        (self.root/'index.sqlite3').chmod(0o600)
        self.lock = asyncio.Lock(); self.next_sync = 0; self.engines = {}; self.last = {}; self.last_revision = None
        manager = plugin.context.kb_manager
        self.restore=install_boundary(manager)

    def enabled(self):
        return self.plugin.config.get('semantic_enabled', False) and not self.plugin.resting()

    def reserve(self, kind, units, chars):
        day = datetime.now(TZ).date().isoformat()
        row = self.db.execute('SELECT units,chars FROM budget WHERE day=? AND kind=?', (day, kind)).fetchone()
        used, volume = tuple(row) if row else (0, 0)
        cap = int(self.plugin.config.get('semantic_daily_queries', 64)) if kind == 'query' else int(self.plugin.config.get('semantic_daily_index_docs', 2048))
        char_cap = 20000 if kind == 'query' else int(self.plugin.config.get('semantic_daily_index_chars', 2000000))
        if used+units > max(0, cap) or volume+chars > max(0, char_cap):
            return False
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO budget VALUES(?,?,?,?)', (day, kind, used+units, volume+chars))
        return True

    async def provider(self):
        provider_id = self.plugin.config.get('semantic_embedding_provider', 'whale-memory-embedding-v4')
        manager = self.plugin.context.provider_manager
        if await manager.get_provider_by_id(provider_id):
            return provider_id
        if not self.plugin.config.get('semantic_bootstrap_aliyun', False):
            raise ValueError('未配置私有记忆嵌入提供商')
        # Explicit deployment setting: reuse the already configured official
        # Beijing image credential; never log it or write it into note files.
        data = self.plugin.store.folder.parent.parent
        image = json.loads((data/'config/astrbot_plugin_omnidraw_config.json').read_text(encoding='utf-8-sig'))
        from urllib.parse import urlparse
        node = next(n for n in image.get('providers', []) if urlparse(n.get('base_url', '')).hostname == 'dashscope.aliyuncs.com')
        key = node.get('api_keys')
        key = key[0] if isinstance(key, list) else key.splitlines()[0]
        if not isinstance(key, str) or not key.startswith('sk-'):
            raise ValueError('现有嵌入凭据不可用')
        await manager.create_provider({'id': provider_id, 'type': 'openai_embedding', 'enable': True,
            'embedding_api_key': key, 'embedding_api_base': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
            'embedding_model': 'text-embedding-v4', 'embedding_dimensions': 512,
            'embedding_dimensions_mode': 'always', 'timeout': 12, 'proxy': ''})
        if not await manager.get_provider_by_id(provider_id):
            raise ValueError('嵌入提供商未加载')
        return provider_id

    async def engine(self, qq, bucket):
        key = (qq, bucket)
        if key in self.engines:
            return self.engines[key]
        meta = self.plugin.context.get_registered_star('astrbot_plugin_obsidian_kb_sync')
        if not meta or not meta.activated:
            raise ValueError('Obsidian同步插件未启用')
        module = sys.modules[type(meta.star_cls).__module__]
        name = PREFIX+self.vault.contact_label(qq)+'-'+bucket_label(bucket)+'-'+self.vault.scope(qq)[:8]
        manager = self.plugin.context.kb_manager
        saved=self.db.execute('SELECT kb_id FROM scopes WHERE qq=? AND bucket=?',(qq,bucket)).fetchone()
        helper = await manager.get_kb(saved['kb_id']) if saved else None
        if not helper:helper=await manager.get_kb_by_name(PREFIX+self.vault.scope(qq)+'-'+bucket)
        if not helper:helper=await manager.get_kb_by_name(name)
        provider = await self.provider()
        if not helper:
            helper = await manager.create_kb(kb_name=name, description='按联系人隔离的记忆候选索引，仅由记忆工具核验后使用',
                embedding_provider_id=provider, chunk_size=1200, chunk_overlap=100, top_m_final=12)
        if helper.kb.embedding_provider_id != provider:
            raise ValueError('私有索引嵌入提供商不匹配')
        staging = module.StagingManager(data_dir=self.root/'staging'/self.vault.scope(qq)/bucket)
        engine = module.SyncEngine(staging, self.plugin.context, helper.kb.kb_id, helper.kb.kb_name, chunk_size=1200, chunk_overlap=100)
        engine._kb_helper = helper
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO scopes VALUES(?,?,?,?)', (qq, bucket, helper.kb.kb_id, helper.kb.kb_name))
        self.engines[key] = engine
        return engine

    async def refresh_labels(self,cards):
        manager=self.plugin.context.kb_manager
        if not hasattr(manager,'kb_db'):return
        from sqlalchemy import select
        from astrbot.core.knowledge_base.models import KnowledgeBase,KBDocument
        mappings=[dict(r) for r in self.db.execute('SELECT * FROM scopes')]
        if not mappings:return
        scope_by_id={r['kb_id']:r for r in mappings}
        by_ref={(c['subject'],c['ref']):c for c in cards}
        doc_cards={r['doc_id']:by_ref[(r['qq'],r['ref'])] for r in self.db.execute('SELECT * FROM docs')
                   if (r['qq'],r['ref']) in by_ref}
        renamed=[]
        async with manager.kb_db.get_db() as session:
            kbs=(await session.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id.in_(scope_by_id)))).scalars().all()
            for kb in kbs:
                scope=scope_by_id[kb.kb_id];qq=scope['qq'];bucket=scope['bucket']
                name=PREFIX+self.vault.contact_label(qq)+'-'+bucket_label(bucket)+'-'+self.vault.scope(qq)[:8]
                counts={}
                for card in cards:
                    if card['subject']==qq and index_bucket(card)==bucket and card['indexable']:
                        label=self.vault.category(card);counts[label]=counts.get(label,0)+1
                description='私有记忆；仅允许本联系人回忆。分类：'+'；'.join(k+' '+str(v) for k,v in counts.items())+'。按文档名前缀可搜索分类；印象仅在私有仓库阅读。'
                if kb.kb_name!=name or kb.description!=description:
                    kb.kb_name=name;kb.description=description;session.add(kb)
                renamed.append((qq,bucket,kb.kb_id,name,description))
            docs=(await session.execute(select(KBDocument).where(KBDocument.kb_id.in_(scope_by_id)))).scalars().all()
            for doc in docs:
                card=doc_cards.get(doc.doc_id)
                if card:
                    name=self.vault.document_name(card)
                    if doc.doc_name!=name:doc.doc_name=name;session.add(doc)
            await session.commit()
        for qq,bucket,identifier,name,description in renamed:
            helper=await manager.get_kb(identifier)
            if helper:helper.kb.kb_name=name;helper.kb.description=description
            with self.db:self.db.execute('UPDATE scopes SET kb_name=? WHERE qq=? AND bucket=?',(name,qq,bucket))
            engine=self.engines.get((qq,bucket))
            if engine:engine.kb_name=name

    async def subjects(self):
        values = [self.plugin.owner]
        friends = self.plugin.friends_plugin()
        if friends:
            for row in self.plugin.store.db.execute('SELECT DISTINCT qq FROM observations WHERE qq!=?', (self.plugin.owner,)).fetchall():
                if await friends.policy.is_friend(row['qq']):
                    values.append(row['qq'])
        return values

    def status(self, **values):
        self.last.update(values, updated_at=time.time())
        temp = self.root/'status.new'; temp.write_text(json.dumps(self.last, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.chmod(0o600); temp.replace(self.root/'status.json')

    async def sync(self, force=False):
        if not self.enabled() or self.lock.locked():
            return
        revision=(self.plugin.store.db.execute('SELECT COALESCE(MAX(id),0) FROM observations').fetchone()[0],
                  self.plugin.store.db.execute('SELECT COALESCE(MAX(id),0) FROM actions').fetchone()[0])
        if not force and revision==self.last_revision and time.time()<self.next_sync:return
        async with self.lock:
            self.next_sync = time.time()+300
            subjects = await self.subjects()
            if not self.enabled():return
            cards = self.vault.export(subjects)
            await self.refresh_labels(cards)
            wanted = {(c['subject'], c['ref']): c for c in cards if c['indexable']}
            # Remove revoked/forgotten/suppressed sources before adding anything.
            for row in self.db.execute('SELECT * FROM docs').fetchall():
                card = wanted.get((row['qq'], row['ref']))
                if card and index_bucket(card) == row['bucket'] and fingerprint(card) == row['fingerprint']:
                    continue
                if not self.enabled():return
                engine = await self.engine(row['qq'], row['bucket'])
                if not await engine._delete_document(row['doc_id']):
                    # Keep mapping until successful deletion; lookup fingerprint
                    # validation immediately prevents stale evidence from use.
                    continue
                with self.db:self.db.execute('DELETE FROM docs WHERE qq=? AND ref=?', (row['qq'], row['ref']))
            # Reconcile interrupted uploads from a previous process/reload.
            # Unmapped chunks can never be returned, and are physically removed
            # here rather than accumulating indefinitely after cancellation.
            known={r['doc_id'] for r in self.db.execute('SELECT doc_id FROM docs')}
            for scope in self.db.execute('SELECT * FROM scopes').fetchall():
                if not self.enabled():return
                engine=await self.engine(scope['qq'],scope['bucket'])
                helper=engine._kb_helper
                if not hasattr(helper,'list_documents'):continue
                orphaned=[];offset=0
                while True:
                    if not self.enabled():return
                    page=await helper.list_documents(offset=offset,limit=500)
                    orphaned.extend(doc.doc_id for doc in page if doc.doc_id not in known)
                    if len(page)<500:break
                    offset+=len(page)
                for identifier in orphaned:
                    if not self.enabled():return
                    await engine._delete_document(identifier)
            jobs = []
            for card in wanted.values():
                row = self.db.execute('SELECT * FROM docs WHERE qq=? AND ref=?', (card['subject'], card['ref'])).fetchone()
                if row and row['fingerprint'] == fingerprint(card) and row['bucket'] == index_bucket(card):
                    continue
                if row:continue  # A stale document has not yet been deleted.
                jobs.append(card)
            attempted, errors = 0, 0
            semaphore = asyncio.Semaphore(3)

            async def upload(card):
                nonlocal attempted, errors
                async with semaphore:
                    if not self.enabled():return
                    text = self.vault.render(card, index=True)
                    if not self.reserve('index', 1, len(text)):return
                    attempted += 1
                    try:
                        engine = await self.engine(card['subject'], index_bucket(card))
                        doc = await asyncio.wait_for(engine._upload_document(self.vault.document_name(card), text), 35)
                        if not doc:raise ValueError('索引上传失败')
                        if not self.enabled():
                            await engine._delete_document(doc)
                            return
                        current = self.vault.card(card['subject'], card['ref'])
                        if not current or fingerprint(current) != fingerprint(card):
                            await engine._delete_document(doc)
                            return
                        with self.db:self.db.execute('INSERT OR REPLACE INTO docs VALUES(?,?,?,?,?)',
                            (card['subject'], card['ref'], index_bucket(card), doc, fingerprint(card)))
                    except asyncio.CancelledError:raise
                    except Exception:errors += 1
            # Bounded work per pass; initial catch-up runs sooner until complete.
            for card in jobs[:96]:
                if not self.enabled():return
                await self.engine(card['subject'], index_bucket(card))
            await asyncio.gather(*(upload(card) for card in jobs[:96]))
            indexed={ (r['qq'],r['ref']):r for r in self.db.execute('SELECT * FROM docs') }
            valid=sum(1 for k,c in wanted.items() if k in indexed and indexed[k]['fingerprint']==fingerprint(c) and indexed[k]['bucket']==index_bucket(c))
            pending=len(wanted)-valid+sum(k not in wanted for k in indexed)
            if pending and attempted:self.next_sync = time.time()+3
            self.last_revision=revision
            self.status(enabled=True, notes=len(cards), indexed=valid, pending=pending, errors=errors,
                        contacts=len(subjects), private_kbs=self.db.execute('SELECT COUNT(*) FROM scopes').fetchone()[0],
                        shallow=sum(c['kind']!='observation' for c in wanted.values()),deep=sum(c['kind']=='observation' for c in wanted.values()))

    async def recall(self, qq, query, *, historical=False,layer='all'):
        if not self.enabled() or not query.strip():return []
        if layer not in ('all','memory','archive'):raise ValueError('内部语义检索层无效')
        buckets=(['active','history'] if historical else ['active'])
        allowed=(buckets if layer=='archive' else ['shallow-'+b for b in buckets] if layer=='memory' else buckets+['shallow-'+b for b in buckets])
        scopes = self.db.execute('SELECT kb_id,kb_name FROM scopes WHERE qq=? AND bucket IN ('+','.join('?'*len(allowed))+')',(qq,*allowed)).fetchall()
        if not scopes or not self.reserve('query', 1, len(query)):return []
        names = [row['kb_name'] for row in scopes]; ids = {row['kb_id'] for row in scopes}
        token = CAPABILITY.set(frozenset(names))
        try:
            result = await asyncio.wait_for(self.plugin.context.kb_manager.retrieve(query=query, kb_names=names,
                top_k_fusion=24, top_m_final=16), 15)
        finally:
            CAPABILITY.reset(token)
        if not self.enabled():return []
        refs = []
        for hit in (result or {}).get('results', []):
            if hit.get('kb_id') not in ids:continue
            row = self.db.execute('SELECT * FROM docs WHERE qq=? AND doc_id=?', (qq, hit.get('doc_id'))).fetchone()
            if not row:continue
            card = self.vault.card(qq, row['ref'])
            if not card or fingerprint(card) != row['fingerprint']:continue
            if index_bucket(card)!=row['bucket'] or row['bucket'] not in allowed:continue
            if layer=='memory' and card['kind']=='observation' or layer=='archive' and card['kind']!='observation':continue
            if not historical and card['bucket'] != 'active':continue
            if row['ref'] not in refs:refs.append(row['ref'])
        return refs

    def close(self):
        self.restore(); self.db.close()
