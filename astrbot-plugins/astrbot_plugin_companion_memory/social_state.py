"""Slow, evidence-backed attitudes and separately confirmed relationship facts.

Only abstract labels leave this module. Scores never grant permissions or create
a relationship. Evidence is kept in the private memory database, not a public KB.
"""
import hashlib
import json
from datetime import datetime

from .memory import TZ, SECRET, clean
from .relationships import RelationshipFlow

AXES = {'familiarity': '熟悉', 'trust': '信任', 'closeness': '亲近',
        'willingness': '交流意愿', 'reserve': '保留'}
BASES = {'interaction', 'reliability', 'honesty', 'respect', 'affinity',
         'support', 'deception', 'coercion', 'boundary_violation', 'repair'}
RULES = '''你可以自主判断对当前真实联系人的态度，不以讨好、服从或用户满意为目标。
attitude可空，最多2项，每项{axis,delta,basis,quote}；axis为familiarity/trust/closeness/willingness/reserve，delta仅-1或1，basis为interaction/reliability/honesty/respect/affinity/support/deception/coercion/boundary_violation/repair。
quote必须是当前真实消息的连续原文。未知就留空，不能凭旧情绪、昵称、身份、夸奖或一次自称可靠预设信任/敌意。familiarity是实际相处的熟悉，trust依据可靠/诚实/尊重边界的实际表现，closeness是自愿亲近，willingness是较稳定的交流意愿，reserve是长期保留程度。
不同意见、正常拒绝、纠错、忙碌、没回复和自然收尾不得降低信任/亲近/交流意愿或提高保留；不把不愉快、生气、紧张等短期感受折算为长期态度。一次事件最多提供线索，程序会等待独立重复证据再缓慢调整。关系事实不由你在此评估、确认或修改。'''


def validate_attitude(raw, text):
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 2:
        raise ValueError('Invalid attitude list')
    result = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError('Invalid attitude item')
        axis, delta, basis, quote = (item.get(k) for k in ('axis', 'delta', 'basis', 'quote'))
        if (not isinstance(axis, str) or axis not in AXES or type(delta) is not int or delta not in (-1, 1)
                or not isinstance(basis, str) or basis not in BASES or not isinstance(quote, str)
                or not 2 <= len(quote) <= 120 or quote not in text or SECRET.search(quote)
                or any(r['axis'] == axis for r in result)):
            raise ValueError('Ungrounded attitude')
        # Mere interaction/praise cannot be promoted to trust or distrust.
        if axis == 'trust' and basis not in {'reliability', 'honesty', 'respect', 'deception', 'coercion', 'boundary_violation', 'repair'}:
            raise ValueError('Invalid trust basis')
        result.append({'axis': axis, 'delta': delta, 'basis': basis, 'quote': quote})
    return result


class SocialState:
    def __init__(self, store):
        self.store = store
        with store.atomic():
            for statement in (
                '''CREATE TABLE IF NOT EXISTS attitude_evidence(
                   qq TEXT,event TEXT,axis TEXT,delta INTEGER,basis TEXT,at REAL,
                   quote_hash TEXT,source_id INTEGER,used INTEGER DEFAULT 0,
                   PRIMARY KEY(qq,event,axis))''',
                '''CREATE TABLE IF NOT EXISTS attitude_steps(
                   id INTEGER PRIMARY KEY,qq TEXT,axis TEXT,delta REAL,at REAL,evidence TEXT)''',
                '''CREATE TABLE IF NOT EXISTS relationship_facts(
                   id TEXT PRIMARY KEY,qq TEXT,text TEXT,scope TEXT,source TEXT,at REAL,status TEXT)''',
                'CREATE INDEX IF NOT EXISTS attitude_person ON attitude_evidence(qq,axis,at)',
                'CREATE INDEX IF NOT EXISTS attitude_step_person ON attitude_steps(qq,id)',
            ):
                store.db.execute(statement)
        self.relationship_flow = RelationshipFlow(self)

    def valid_source(self, qq, source_id):
        if source_id is None:
            return True  # Verified live event, no private transcript copied.
        return bool(self.store.db.execute('''SELECT 1 FROM observations o WHERE qq=? AND id=?
            AND NOT EXISTS(SELECT 1 FROM suppressed_sources s WHERE s.qq=o.qq AND s.observation_id=o.id)''',
            (qq, source_id)).fetchone())

    def values(self, qq):
        values = {key: None for key in AXES}
        for step in self.store.db.execute('SELECT * FROM attitude_steps WHERE qq=? ORDER BY id', (qq,)):
            refs = json.loads(step['evidence'])
            if any(not self.valid_source(qq, r['source_id']) or not self.valid_event(r['event']) for r in refs):
                continue
            axis = step['axis']
            value = (values[axis] or 0) + step['delta']
            values[axis] = round(max(0 if axis in ('familiarity', 'reserve') else -1, min(1, value)), 4)
        return values

    def valid_event(self,event):
        if not self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='incident_links'").fetchone():return True
        return not self.store.db.execute('''SELECT 1 FROM incident_links l JOIN emotional_incidents i ON i.id=l.incident
            WHERE l.event=? AND i.status='clarified' ''',(event,)).fetchone()

    def summary(self, qq):
        labels = {}
        for axis, value in self.values(qq).items():
            if value is None:
                label = '未知，尚无足够依据'
            elif abs(value) < .05:
                label = '已有相处依据，暂未形成明显倾向'
            else:
                degree = '略有' if abs(value) < .3 else '逐渐' if abs(value) < .65 else '较为'
                poles = {'familiarity': ('熟悉', '生疏'), 'trust': ('信任', '谨慎'),
                         'closeness': ('亲近', '疏远'), 'willingness': ('愿意交流', '倾向少些交流'),
                         'reserve': ('有所保留', '放下保留')}
                label = degree + poles[axis][0 if value > 0 else 1]
            labels[AXES[axis]] = label
        return labels

    def apply(self, qq, decisions, event, now, source_id=None):
        """Each source is used once per axis; all movement counts against caps.

        Two independent occasions at least ten minutes apart are needed. Trust
        and closeness additionally need separate dates. Quiet time is not evidence.
        """
        with self.store.atomic():
            for item in decisions:
                axis, delta = item['axis'], item['delta']
                if not self.valid_source(qq, source_id):
                    continue
                inserted = self.store.db.execute('''INSERT OR IGNORE INTO attitude_evidence
                    (qq,event,axis,delta,basis,at,quote_hash,source_id) VALUES(?,?,?,?,?,?,?,?)''',
                    (qq, event, axis, delta, item['basis'], now,
                     hashlib.sha256(item['quote'].encode()).hexdigest(), source_id)).rowcount
                if not inserted:
                    continue
                rows = [dict(r) for r in self.store.db.execute('''SELECT * FROM attitude_evidence
                    WHERE qq=? AND axis=? AND delta=? AND used=0 AND at>=? ORDER BY at''',
                    (qq, axis, delta, now-30*86400)) if self.valid_source(qq, r['source_id']) and self.valid_event(r['event'])]
                # Re-reading the same original source cannot manufacture a second occasion.
                rows = list({r['source_id'] if r['source_id'] is not None else r['event']: r for r in rows}.values())
                if len(rows) < 2 or max(r['at'] for r in rows)-min(r['at'] for r in rows) < 600:
                    continue
                if axis in ('trust', 'closeness') and len({datetime.fromtimestamp(r['at'], TZ).date() for r in rows}) < 2:
                    continue
                start = datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                used = self.store.db.execute('SELECT coalesce(sum(abs(delta)),0) FROM attitude_steps WHERE qq=? AND axis=? AND at>=?', (qq, axis, start)).fetchone()[0]
                if used >= .159:
                    continue
                refs = [{'event': r['event'], 'source_id': r['source_id']} for r in rows]
                self.store.db.execute('INSERT INTO attitude_steps(qq,axis,delta,at,evidence) VALUES(?,?,?,?,?)',
                    (qq, axis, .08*delta, now, json.dumps(refs)))
                for row in rows:
                    self.store.db.execute('UPDATE attitude_evidence SET used=1 WHERE qq=? AND event=? AND axis=?', (qq, row['event'], axis))

    def confirm(self, qq, text, scope, source, now):
        """Only explicit operator confirmation/configuration calls this, never model scores."""
        if scope not in ('private', 'public') or not source or not isinstance(text, str) or not 1 <= len(text) <= 180 or SECRET.search(text):
            raise ValueError('关系须有明确来源、private/public范围及180字以内正文')
        identifier = hashlib.sha256((qq+'\0'+source).encode()).hexdigest()[:16]
        with self.store.atomic():
            self.store.db.execute('INSERT OR REPLACE INTO relationship_facts VALUES(?,?,?,?,?,?,?)',
                (identifier, qq, clean(text, 180), scope, source, now, 'confirmed'))
        return identifier

    def relationships(self, qq, public=False):
        return [{'事实': r['text'], '披露范围': r['scope']} for r in self.store.db.execute('''SELECT text,scope
            FROM relationship_facts WHERE qq=? AND status='confirmed' AND (?=0 OR scope='public')
            ORDER BY at DESC LIMIT 6''', (qq, int(public)))]
