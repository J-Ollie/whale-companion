"""Scoped event appraisal and repair. Source references, never hidden reasoning."""
import hashlib
import json
import re
from .memory import SECRET, clean

SCENES = {'praise', 'success', 'good_news', 'user_distress', 'disagreement', 'correction',
          'refusal', 'joke', 'ambiguous', 'insult', 'conflict', 'apology', 'clarification',
          'busy', 'goodbye', 'absence', 'missed_agreement', 'return', 'boundary',
          'other_people', 'task_failure', 'technical_fault', 'serious', 'ordinary'}
PROTECTED = {'busy', 'goodbye', 'absence', 'correction', 'disagreement', 'refusal',
             'other_people', 'technical_fault'}
CONTACT_RULE = '''可选contact={mode,until,topic,quote}，仅提取当前人直接表达的联系边界，引用/假设/第三人不登记。
mode取until(到明确时间)/return(等我回来或忙完)/reduced(最近忙，减少联系)/topic(别再提某话题)/resume_topic(明确允许重提某话题)/pause(以后不主动)/resume(明确恢复主动)。quote连续引用原话；until为带+08:00的ISO时间，不能编时间；topic为对方原话中连续的话题短语。没有边界省略contact，不从情绪推断授权。'''
APPRAISAL = '''先辨认事件和主体，区分原话、玩笑、引用、假设及不确定解释，不预设性格或情绪。可不产生情绪。
情绪变化或resolve非空时必须view={scene,confidence,op,id,topic}，优先保留view，attitude可省略。view.quote可省略沿用顶层quote。scene取praise/success/good_news/user_distress/disagreement/correction/refusal/joke/ambiguous/insult/conflict/apology/clarification/busy/goodbye/absence/missed_agreement/return/boundary/other_people/task_failure/technical_fault/serious/ordinary；confidence取clear/uncertain。
op取new/reinforce/repair/clarify/none。id仅能选当前可见事件；reinforce是同一件事的新刺激，repair是道歉或实际修复，clarify仅用于有新依据确认原先误会。不能把泛泛好话当清除全部旧事。多件事无法确定对应哪件时op=none，不猜id。
新情绪topic最多20字，仅描述当前事件；quote连续引用当前原话。当前人只能修复自己的可见事件。第三方转述、引语不是当前人做的事，不能借相同字句reinforce旧事件；疑惑不等于攻击，求助者难过不等于在伤害你；普通拒绝/纠错/忙碌/沉默不扣长期态度。'''


class Incidents:
    def __init__(self, store):
        self.store = store
        with store.atomic():
            schema = '''CREATE TABLE IF NOT EXISTS emotional_incidents(
                id TEXT PRIMARY KEY,qq TEXT,scope TEXT,scene TEXT,confidence TEXT,
                topic TEXT,source_id INTEGER,event TEXT,at REAL,updated REAL,status TEXT,
                effect REAL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS incident_person ON emotional_incidents(qq,scope,updated);
                CREATE TABLE IF NOT EXISTS incident_links(event TEXT PRIMARY KEY,incident TEXT);
                CREATE TABLE IF NOT EXISTS incident_repairs(event TEXT PRIMARY KEY,incident TEXT,
                at REAL,action TEXT,source_id INTEGER);'''
            for sql in schema.split(';'):
                if sql.strip():store.db.execute(sql)

    def valid(self, row):
        if row['source_id'] is None:
            return True
        return bool(self.store.db.execute('''SELECT 1 FROM observations o WHERE qq=? AND id=?
            AND NOT EXISTS(SELECT 1 FROM suppressed_sources s WHERE s.qq=o.qq AND s.observation_id=o.id)''',
            (row['qq'], row['source_id'])).fetchone())

    def visible(self, qq, scope, now):
        rows = self.store.db.execute('''SELECT * FROM emotional_incidents WHERE qq=? AND scope=?
            AND updated>=? AND status IN ('open','repairing') ORDER BY updated DESC LIMIT 12''',
            (qq, scope, now-14*86400)).fetchall()
        return [{'id': r['id'], 'topic': r['topic'], 'scene': r['scene'], 'status': r['status']}
                for r in rows if self.valid(r)][:4]

    def appraisal(self, raw, text, qq, scope, now, quote=''):
        if re.search(r'(?:他|她|别人|朋友|同事|老师|有人).{0,6}(?:说|问|骂|写|发).{0,3}[“「『"]',text):
            return {'scene':'ambiguous','confidence':'uncertain','op':'none','id':'','topic':'转述第三方言语'}
        if raw is None:
            return None  # legacy bounded output still accepted
        if not isinstance(raw, dict) or raw.get('scene') not in SCENES or raw.get('confidence') not in ('clear', 'uncertain'):
            raise ValueError('Invalid appraisal')
        op = raw.get('op', 'none')
        if op not in ('new', 'reinforce', 'repair', 'clarify', 'none'):
            raise ValueError('Invalid appraisal operation')
        # The scene is the declared interpretation; normalize synonymous repair
        # wording so a confirmed misunderstanding also retracts its attribution.
        if raw['scene'] == 'clarification' and op == 'repair':op = 'clarify'
        if raw['scene'] == 'apology' and op == 'clarify':op = 'repair'
        quote = raw.get('quote', quote)
        if not isinstance(quote, str) or not 1 <= len(quote) <= 180 or quote not in text or SECRET.search(quote):
            raise ValueError('Appraisal lacks current evidence')
        target = raw.get('id', '')
        offered = {r['id'] for r in self.visible(qq, scope, now)}
        if op in ('reinforce', 'repair', 'clarify') and target not in offered:
            raise ValueError('Unknown or private incident')
        topic = raw.get('topic') or quote[:20]
        if not isinstance(topic, str) or not 1 <= len(topic) <= 30 or SECRET.search(topic):
            raise ValueError('Invalid incident topic')
        return {'scene': raw['scene'], 'confidence': raw['confidence'], 'op': op,
                'id': target, 'topic': clean(topic, 30)}

    def constrain(self, decision, attitudes, view):
        decision = dict(decision)
        if view and (view['confidence'] == 'uncertain' or view['scene'] in PROTECTED):
            decision['intensity'] = 1
            decision['emotions'] = [k for k in decision['emotions'] if k not in ('hurt', 'jealous', 'irritated')]
            if view['scene'] in ('busy', 'goodbye', 'absence', 'other_people', 'technical_fault', 'correction', 'disagreement'):
                decision['delta'] = max(0, decision['delta'])
            attitudes = [a for a in attitudes if a['delta'] > 0 and a['axis'] != 'reserve'
                         or a['delta'] < 0 and a['axis'] == 'reserve']
            if view['confidence'] == 'uncertain':
                attitudes = []
        # The repair target is an event, never every emotion of the same name.
        decision['resolve'] = decision['resolve'] if view and view['op'] in ('repair', 'clarify') and view['confidence'] == 'clear' else []
        return decision, attitudes

    def bind(self, view, decision, qq, scope, event, now, source_id):
        """Within Affect's atomic commit; no model or external I/O."""
        target = None
        if view and view['op'] in ('reinforce', 'repair', 'clarify') and view['confidence'] == 'clear':
            target = view['id']
        if not target and decision['emotions']:
            target = hashlib.sha256((qq+'\0'+scope+'\0'+event).encode()).hexdigest()[:12]
            self.store.db.execute('''INSERT OR IGNORE INTO emotional_incidents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (target, qq, scope, view['scene'] if view else 'ordinary', view['confidence'] if view else 'uncertain',
                 view['topic'] if view else '一次真实交流', source_id, event, now, now, 'open', 0))
        if target:
            self.store.db.execute('INSERT OR IGNORE INTO incident_links VALUES(?,?)', (event, target))
        return target

    def update(self, target, view, event, now, source_id, effect):
        if not target:
            return
        row = self.store.db.execute('SELECT * FROM emotional_incidents WHERE id=?', (target,)).fetchone()
        old_effect = row['effect'] * 0.5**(max(0, now-row['updated'])/7200)
        action = view['op'] if view else 'new'
        status = 'clarified' if action == 'clarify' else 'repairing' if action == 'repair' else 'open'
        self.store.db.execute('UPDATE emotional_incidents SET updated=?,status=?,effect=? WHERE id=?',
                              (now, status, old_effect+effect, target))
        if action in ('repair', 'clarify'):
            self.store.db.execute('INSERT OR IGNORE INTO incident_repairs VALUES(?,?,?,?,?)',
                                  (event, target, now, action, source_id))

    def guide(self, qq, scope, now):
        rows = self.visible(qq, scope, now)
        return {'未完事件': rows, '处理分寸': '先接眼前内容；可表达自己的看法和边界，不迁怒、不索取安抚。'
                '求助或紧急情况优先认真处理；不确定先保留疑惑或澄清。道歉只回应相应事件，好消息不等于旧事已解决。'}
