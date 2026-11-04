"""Owner-scoped exploration outreach receipts. No model, messaging or phone I/O."""
import copy
import hashlib
import json
import math
import re
import time
import uuid
import urllib.parse
from pathlib import Path

WEEK = 7 * 86400
MAX_ROWS = 64


def stamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def normalized(text):
    return re.sub(r'[\s。！？!?，,、；;：:]+', '', str(text))


def valid_candidate(row):
    if not isinstance(row, dict):
        return False
    source = row.get('source')
    web = isinstance(source, dict) and source.get('app') == '公开网页'
    if web:
        try:
            url = urllib.parse.urlsplit(source.get('url', ''))
            if (source.get('kind') != 'web_text' or source.get('scope') != 'extracted_text'
                    or not isinstance(source.get('content_hash'),str) or not re.fullmatch(r'[a-f0-9]{64}',source['content_hash'])
                    or type(source.get('truncated')) is not bool or url.scheme != 'https'
                    or not url.hostname or url.username is not None or url.password is not None
                    or url.port not in (None, 443) or not isinstance(source.get('title'), str)
                    or not 0 < len(source['title']) <= 160):
                return False
        except (ValueError, TypeError):
            return False
    return (isinstance(row.get('id'), str) and re.fullmatch(r'explore:[a-f0-9]{12}', row['id']) is not None
        and isinstance(row.get('revision'), str) and re.fullmatch(r'[a-f0-9]{64}', row['revision']) is not None
        and row.get('status') in ('open', 'resolved')
        and all(isinstance(row.get(k), str) and 0 < len(row[k]) <= n
                for k, n in [('question', 80), ('progress', 160)])
        and isinstance(row.get('next_step'), str) and len(row['next_step']) <= 100
        and isinstance(source, dict) and source.get('public_content') is True
        and isinstance(source.get('id'), str) and source['id'].startswith('web:' if web else 'phone:') and len(source['id']) <= 80
        and stamp(source.get('timestamp'))
        and source.get('app') in ('酷安', '哔哩哔哩', '百度贴吧', '公开网页')
        and type(source.get('views')) is int and source['views'] > 0
        and isinstance(source.get('note'), str) and 0 < len(source['note']) <= 160)


class GoalLedger:
    def __init__(self, root, owner):
        self.path = Path(root) / 'exploration_outreach.json'
        self.owner = owner
        self.healthy = True
        self.rows = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
                assert data['version'] == 1 and data['owner'] == owner
                assert isinstance(data['rows'], dict) and len(data['rows']) <= MAX_ROWS
                for identifier, row in data['rows'].items():
                    assert valid_candidate(row['goal']) and row['goal']['id'] == identifier
                    assert row['delivery'] in ('reserved', 'sent', 'partial', 'uncertain')
                    assert isinstance(row['attempt'], str) and re.fullmatch(r'[a-f0-9]{32}', row['attempt'])
                    assert stamp(row['at']) and type(row['blocked']) is bool
                    assert isinstance(row['parts'], list) and len(row['parts']) <= 180
                    assert all(isinstance(x, str) and 0 < len(x) <= 180 for x in row['parts'])
                    assert sum(map(len, row['parts'])) <= 180
                    assert row['feedback'] is None or (isinstance(row['feedback'], dict)
                        and row['feedback']['kind'] in ('received', 'declined', 'resumed')
                        and stamp(row['feedback']['at'])
                        and re.fullmatch(r'[a-f0-9]{64}', row['feedback']['event']))
                self.rows = data['rows']
            except (OSError, ValueError, KeyError, TypeError, AssertionError):
                self.healthy = False  # Preserve unreadable evidence; do not reset delivery history.

    def _commit(self, rows):
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps({'version': 1, 'owner': self.owner, 'rows': rows},
            ensure_ascii=False, indent=2), encoding='utf-8')
        temp.chmod(0o600)
        temp.replace(self.path)
        self.rows = rows

    def offers(self, candidates, now=None):
        now = time.time() if now is None else now
        if not self.healthy or not isinstance(candidates, list):
            return {}
        result = {}
        for goal in candidates[:12]:
            if not valid_candidate(goal) or not 0 <= now - goal['source']['timestamp'] <= WEEK:
                continue
            old = self.rows.get(goal['id'])
            if old and (old['blocked'] or old['goal']['revision'] == goal['revision']
                    or normalized(old['goal']['progress']) == normalized(goal['progress'])
                    or old['goal']['source']['id'] == goal['source']['id']
                    or old['goal']['source']['timestamp'] >= goal['source']['timestamp']):
                continue
            result[goal['id']] = copy.deepcopy(goal)
        return result

    def context(self, offers, limit=2400):
        selected = []
        for goal in sorted(offers.values(), key=lambda g: g['source']['timestamp'], reverse=True):
            candidate = selected + [goal]
            if len(json.dumps(candidate, ensure_ascii=False).encode()) <= limit:
                selected = candidate
            if len(selected) == 3:
                break
        return selected

    def reserve(self, goal, candidates, now):
        fresh = self.offers(candidates, now).get(goal.get('id'))
        if not fresh or fresh != goal:
            return None
        rows = copy.deepcopy(self.rows)
        if goal['id'] not in rows and len(rows) >= MAX_ROWS:
            current = {g.get('id') for g in candidates if isinstance(g, dict)}
            obsolete = [key for key in rows if key not in current]
            if not obsolete:
                return None
            del rows[min(obsolete, key=lambda key: rows[key]['at'])]
        attempt = uuid.uuid4().hex
        rows[goal['id']] = {'goal': copy.deepcopy(goal), 'attempt': attempt, 'at': now,
            'delivery': 'reserved', 'parts': [], 'blocked': False, 'feedback': None}
        self._commit(rows)  # Reserve before any network send; uncertain outcomes are not retried.
        return attempt

    def finish(self, identifier, attempt, parts, complete):
        if not self.healthy or self.rows.get(identifier, {}).get('attempt') != attempt:
            return
        if not isinstance(parts, list) or any(not isinstance(p, str) or not p for p in parts) or sum(map(len, parts)) > 180:
            raise ValueError('Invalid delivered parts')
        rows = copy.deepcopy(self.rows)
        rows[identifier].update(parts=list(parts), delivery='sent' if complete else ('partial' if parts else 'uncertain'))
        self._commit(rows)

    def feedback(self, quotes, text, event_id, now):
        """Only typed, self-authored quotes with matching text AND send time can bind."""
        if not self.healthy or not isinstance(event_id, str) or not event_id or not isinstance(text, str):
            return False
        fingerprint = hashlib.sha256(event_id.encode()).hexdigest()
        if any(r['feedback'] and r['feedback']['event'] == fingerprint for r in self.rows.values()):
            return False
        matches = set()
        for identifier, row in self.rows.items():
            if row['delivery'] not in ('sent', 'partial') or not 0 <= now-row['at'] <= WEEK:
                continue
            bodies = {normalized(p) for p in row['parts']} | {normalized(''.join(row['parts']))}
            for quote in quotes:
                if (isinstance(quote, dict) and stamp(quote.get('at')) and quote['at'] > 0
                        and abs(quote['at']-row['at']) <= 120
                        and len(normalized(quote.get('text', ''))) >= 6
                        and normalized(quote.get('text', '')) in bodies):
                    matches.add(identifier)
        if len(matches) != 1:
            return False  # No topic guessing; ambiguous quotations do not update multiple goals.
        identifier = matches.pop()
        # Narrow, explicit stop instruction. Other linked replies are recorded without sentiment or truth labels.
        declined = re.fullmatch(r'(?:这个话题|这件事|这事|这个)?(?:先)?(?:别再?提|不要再?提|别再?聊|不要再?聊|不用再聊|不想聊|没兴趣)(?:这个话题|这件事|这事|这个)?了?[。!！\s]*', text.strip()) is not None
        resumed = re.fullmatch(r'(?:这个话题|这件事|这事|这个)?(?:现在|以后)?(?:可以|继续)(?:再|继续)?(?:聊|说|提)(?:这个话题|这件事|这事|这个)?(?:了|吧)?[。!！\s]*', text.strip()) is not None
        rows = copy.deepcopy(self.rows)
        rows[identifier]['feedback'] = {'kind': 'declined' if declined else ('resumed' if resumed else 'received'), 'at': now, 'event': fingerprint}
        rows[identifier]['blocked'] = False if resumed else (rows[identifier]['blocked'] or declined)
        self._commit(rows)
        return True

    def feedback_context(self, limit=1200):
        selected = []
        for row in sorted(self.rows.values(), key=lambda r: r['at'], reverse=True):
            if not row['feedback']:
                continue
            item = {'question': row['goal']['question'], 'feedback': row['feedback']['kind'], 'stop_outreach': row['blocked']}
            if len(json.dumps(selected+[item], ensure_ascii=False).encode()) <= limit:
                selected.append(item)
            if len(selected) == 3:
                break
        return selected
