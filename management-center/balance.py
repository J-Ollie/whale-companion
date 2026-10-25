"""Read-only official DeepSeek balance. Credentials never leave this module."""
import json
import re
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def ration_voice(result):
    unknown={'tone':'unknown','label':'饭缸暂时看不清',
             'text':'我没看清饭缸，不代表已经没米了。等会儿再看看',
             'notice':'按账户余额切换的页面文案，不是实时聊天，也不估算还能使用多久。'}
    if result.get('status')!='ok':return unknown
    rows=result.get('balance_infos',[])
    try:
        available=[r for r in rows if Decimal(r['total_balance'])>0]
        row=next((r for r in available if r['currency']=='CNY'),available[0] if available else None)
        amount=Decimal(row['total_balance']) if row else Decimal(0)
    except (InvalidOperation,ValueError,KeyError,TypeError):return unknown
    if amount>0 and not result.get('is_available'):
        return {'tone':'unknown','label':'账户当前不可用','text':'账上还有余粮，但饭暂时端不出来。先看看账户状态，不能直接当成没米了','notice':unknown['notice']}
    if amount<=0:
        tone,label,text='empty','饭碗空了','碗先抱着，等你方便时再添点米。吃白饭的大肥鱼也有空碗的时候嘛'
    else:
        thresholds=[100,30,10,1] if row['currency']=='CNY' else [20,5,2,0.2]
        level=next((i for i,value in enumerate(thresholds) if amount>=Decimal(str(value))),4)
        tone,label,text=[
            ('full','饭缸满满','米缸挺满的嘛，今天可以安心吃白饭了。先说好，我也会认真干活的'),
            ('steady','余粮够吃','还有饭，尾巴先放松一下。你慢慢说，我边吃边想'),
            ('careful','慢慢吃米','余粮薄了一点，我慢慢吃。该认真说的话，还是会认真说'),
            ('low','快见碗底','快看见碗底了，我先把这点米护好。等你方便时再添就行'),
            ('tiny','还剩一点点','还剩几粒米，我数清楚了。先不乱扑腾，饭碗放这儿等你')][level]
    return {'tone':tone,'label':label,'text':text,'notice':unknown['notice']}


class DeepSeekBalance:
    ENDPOINT = 'https://api.deepseek.com/user/balance'

    def __init__(self, root):
        self.path = Path(root) / 'data/astrbot/cmd_config.json'
        self.lock = threading.Lock()
        self.last_attempt = 0
        self.snapshot = None
        self.result = None

    def _key(self):
        config = json.loads(self.path.read_text(encoding='utf-8-sig'))
        candidates = []
        for source in config.get('provider_sources', []):
            if source.get('enable') is False:
                continue
            base = urlsplit(str(source.get('api_base', '')).strip())
            if (base.scheme != 'https' or base.netloc != 'api.deepseek.com'
                    or base.path.rstrip('/') not in ('', '/v1') or base.query or base.fragment):
                continue
            keys = source.get('key', [])
            if isinstance(keys, str):
                keys = [keys]
            key = next((k.strip() for k in keys if isinstance(k, str) and k.strip()), '')
            if key:
                candidates.append(key)
        unique = list(dict.fromkeys(candidates))
        if len(unique) != 1:
            raise ValueError('unconfigured' if not unique else 'multiple_accounts')
        return unique[0]

    @staticmethod
    def normalize(payload):
        if not isinstance(payload, dict) or not isinstance(payload.get('is_available'), bool):
            raise ValueError('invalid_response')
        rows = payload.get('balance_infos')
        if not isinstance(rows, list) or not rows or len(rows) > 4:
            raise ValueError('invalid_response')
        result = []
        for row in rows:
            if not isinstance(row, dict) or row.get('currency') not in ('CNY', 'USD'):
                raise ValueError('invalid_response')
            clean = {'currency': row['currency']}
            for field in ('total_balance', 'granted_balance', 'topped_up_balance'):
                value = str(row.get(field, ''))
                if not re.fullmatch(r'-?\d{1,13}(?:\.\d{1,12})?', value):
                    raise ValueError('invalid_response')
                number = Decimal(value)
                if not number.is_finite() or abs(number) > Decimal('1000000000000'):
                    raise ValueError('invalid_response')
                clean[field] = format(number, 'f')
            result.append(clean)
        return {'is_available': payload['is_available'], 'balance_infos': result}

    def query(self):
        # All callers share a one-minute cache, including failed attempts.
        with self.lock:
            now = time.time()
            if self.result is not None and now - self.last_attempt < 60:
                return {**self.result, 'cached': True, 'voice':ration_voice(self.result)}
            self.last_attempt = now
            try:
                key = self._key()
                request = urllib.request.Request(self.ENDPOINT, headers={
                    'Authorization': 'Bearer ' + key, 'Accept': 'application/json'})
                with urllib.request.build_opener(NoRedirect()).open(request, timeout=8) as response:
                    raw = response.read(65537)
                if len(raw) > 65536:
                    raise ValueError('invalid_response')
                clean = self.normalize(json.loads(raw))
                self.snapshot = {**clean, 'checked_at': time.time()}
                self.result = {**self.snapshot, 'status': 'ok', 'stale': False}
            except Exception as exc:
                # Never expose upstream bodies, exception strings, config or keys.
                code = 'unavailable'
                if isinstance(exc, FileNotFoundError):
                    code = 'unconfigured'
                elif isinstance(exc, urllib.error.HTTPError):
                    code = {401: 'invalid_key', 403: 'invalid_key', 429: 'rate_limited'}.get(exc.code, 'unavailable')
                    exc.close()
                elif isinstance(exc, (ValueError, InvalidOperation)):
                    code = str(exc) if str(exc) in ('unconfigured', 'multiple_accounts') else 'invalid_response'
                # Do not retain old balances if credentials are removed or changed.
                self.snapshot = None
                self.result = {'status': 'error', 'code': code, 'stale': False,
                               'balance_infos': [], 'checked_at': None}
            return {**self.result, 'cached': False, 'voice':ration_voice(self.result)}
