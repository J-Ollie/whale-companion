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
                return {**self.result, 'cached': True}
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
            return {**self.result, 'cached': False}
