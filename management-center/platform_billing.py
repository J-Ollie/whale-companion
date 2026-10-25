"""Read-only Platform billing sync, explicitly connected by the administrator."""
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from balance import NoRedirect, DeepSeekBalance
from billing import DeepSeekBilling, TZ, bounds, unwrap, keys, aggregate, daily_costs, clean_name, series_rows

ENDPOINT = 'https://platform.deepseek.com/api/v0/usage/by_api_key/cost'


class LoginExpired(Exception):
    pass


def normalize_token(value):
    if not isinstance(value, str):
        raise ValueError('请填写官网登录令牌，不是账户密码或 API Key。')
    value = value.strip()
    if value.lower().startswith('bearer '):
        value = value[7:].strip()
    if (value.startswith('sk-') or not re.fullmatch(r'[A-Za-z0-9._~+/=-]{20,3000}', value)):
        raise ValueError('请填写有效的官网登录令牌，不是账户密码或 API Key。')
    return value


def matching_key(masked, configured):
    """Use only a returned full/masked identifier; never send key material to Platform."""
    if not isinstance(masked, str) or not masked:
        return None
    if masked == configured:
        return True
    parts = re.split(r'\*+|\.{2,}|…+', masked)
    if len(parts) == 2 and len(parts[0]) >= 6 and len(parts[1]) >= 4:
        return configured.startswith(parts[0]) and configured.endswith(parts[1])
    if masked.startswith('sk-') and not re.search(r'\*|\.{2}|…', masked):
        return False
    return None


class PlatformBilling:
    def __init__(self, root):
        self.root = Path(root)
        self.ledger = DeepSeekBilling(root)
        self.path = self.root / 'recovery-data/deepseek-platform-auth.json'
        self.lock = threading.RLock()
        self.pending = {}
        self.last_connect = {}
        self.last_attempt = 0
        self.cache = None
        self.revision = 0

    def cleanup(self):
        now = time.time()
        self.pending = {k:v for k,v in self.pending.items() if v['expires'] > now}
        self.last_connect = {k:v for k,v in self.last_connect.items() if now-v < 60}

    def record_shape(self,data):
        """Bounded diagnostic metadata only: no tokens, Key identifiers or money."""
        shape={'at':time.time(),'start_type':type(data.get('start')).__name__,
               'end_type':type(data.get('end')).__name__,
               'bucket_type':type(data.get('bucket')).__name__}
        for name in ('start','end','bucket'):
            v=data.get(name)
            if type(v) is int:shape[name]=v
            elif name=='bucket' and v in ('1d','1h'):shape[name]=v
        groups=data.get('data',[])
        shape['groups']=len(groups) if isinstance(groups,list) else None
        shape['cost_types']=sorted({type(b.get('cost')).__name__
            for g in groups[:4] if isinstance(g,dict)
            for field in ('series','function_series')
            for r in (g.get(field,[]) if isinstance(g.get(field,[]),list) else [])[:100]
            if isinstance(r,dict)
            for b in (r.get('buckets',[]) if isinstance(r.get('buckets',[]),list) else [])[:32]
            if isinstance(b,dict)})
        costs=[str(b.get('cost','')) for g in groups[:4] if isinstance(g,dict)
            for field in ('series','function_series')
            for r in (g.get(field,[]) if isinstance(g.get(field,[]),list) else [])[:100]
            if isinstance(r,dict)
            for b in (r.get('buckets',[]) if isinstance(r.get('buckets',[]),list) else [])[:32]
            if isinstance(b,dict)]
        shape['money_notation']={'scientific_count':sum(bool(re.search('[eE]',v)) for v in costs),
            'max_characters':max(map(len,costs),default=0),
            'max_fraction_digits':max((len(v.split('.',1)[1].split('e')[0].split('E')[0])
                for v in costs if '.' in v),default=0)}
        self.atomic(self.root/'recovery-data/deepseek-cost-shape.json',shape)

    def fetch(self, token, start, end):
        # Never accept a URL from the browser, follow redirects, or export response bodies.
        request = urllib.request.Request(f'{ENDPOINT}?start={start}&end={end}&tz=28800', headers={
            'Authorization':'Bearer '+token, 'Accept':'application/json',
            'User-Agent':'WhaleCompanion/1.0', 'x-client-platform':'web'})
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=12) as response:
                raw = response.read(2097153)
            if len(raw) > 2097152:
                raise ValueError('账单过大，暂时无法同步。')
            value = json.loads(raw)
            if value.get('code') in (40002,40003) or value.get('data',{}).get('biz_code') in (40002,40003):
                raise LoginExpired()
            return unwrap(value)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code in (401,403):
                raise LoginExpired() from None
            raise ValueError('官网账单暂时无法读取，请稍后重试。') from None

    def connect(self, value, session):
        token = normalize_token(value)
        with self.lock:
            self.cleanup()
            if time.time()-self.last_connect.get(session,0) < 30 or len(self.pending) >= 5:
                raise ValueError('连接请求较频繁，请半分钟后重试。')
            self.last_connect[session] = time.time()
            self.pending = {k:v for k,v in self.pending.items() if v['session'] != session}
            configured = DeepSeekBalance(self.root)._key()
            fingerprint = self.ledger.fingerprint()
            now = time.time()
            start = bounds(now-29*86400)[1]
            end = bounds(now)[2]
            try:
                data = self.fetch(token,start,end)
                self.record_shape(data)
                names = keys(data)
            except LoginExpired:
                raise ValueError('官网登录令牌无效或已过期，请重新登录官网后连接。') from None
            except Exception:
                raise ValueError('暂时无法验证官网授权，请确认令牌来自开放平台后稍后重试。') from None
            matches = {}
            for group in data['data']:
                for _,row in series_rows(group):
                    key = row.get('api_key')
                    if isinstance(key,dict) and key.get('tracking_id') in names:
                        match = matching_key(key.get('sensitive_id'),configured)
                        identity = key['tracking_id']
                        if identity in matches and matches[identity] != match:
                            raise ValueError('官网 Key 标识不一致，请重新核对账户。')
                        matches[identity] = match
            candidates = {secrets.token_urlsafe(18):{'tracking_id':tid,'name':name,'match':matches.get(tid)}
                          for tid,name in names.items() if matches.get(tid) is not False}
            if not candidates:
                raise ValueError('未找到可匹配的小鲸鱼 Key，请核对账号及最近30天该 Key 的用量。')
            nonce = secrets.token_urlsafe(24)
            self.pending[nonce] = {'token':token,'session':session,'fingerprint':fingerprint,
                                   'choices':candidates,'report':data,'expires':time.time()+180}
            return {'connection_id':nonce,'keys':[{'id':key,'name':item['name'],
                     'match':item['match']} for key,item in candidates.items()]}

    def select(self, connection_id, key_id, session, confirmed):
        if confirmed is not True:
            raise ValueError('请确认是同一个 DeepSeek 账户，且所选 Key 就是小鲸鱼使用的 Key。')
        with self.lock:
            self.cleanup()
            pending = self.pending.get(connection_id)
            if not pending or pending['session'] != session:
                raise ValueError('连接步骤已过期，请重新连接。')
            chosen = pending['choices'].get(key_id)
            fingerprint = self.ledger.fingerprint()
            if not chosen or pending['fingerprint'] != fingerprint:
                raise ValueError('小鲸鱼 Key 已变化，请重新连接。')
            day,start,end = bounds(time.time())
            costs=None
            try:
                # Reuse the successfully authenticated report. Short-range requests can
                # use a different bucket resolution than the verified 30-day report.
                costs = daily_costs(pending['report'],start,end,chosen['tracking_id'])
            except LoginExpired:
                self.pending.pop(connection_id,None)
                raise ValueError('官网登录授权已失效，请重新连接。') from None
            except ValueError:
                pass  # Valid login and confirmed Key remain connected, totals stay unknown.
            except Exception:
                pass
            if fingerprint != self.ledger.fingerprint():
                self.pending.pop(connection_id,None)
                raise ValueError('小鲸鱼 Key 在验证期间发生变化，请重新连接。')
            saved = {'token':pending['token'],'tracking_id':chosen['tracking_id'],
                     'key_name':chosen['name'],'fingerprint':fingerprint}
            self.atomic(self.path,saved)
            if costs is not None:
                self.save_costs(day,costs,fingerprint,chosen['name'])
            else:
                self.ledger.path.unlink(missing_ok=True)
            self.pending.pop(connection_id,None)
            self.cache = ({**self.ledger.query(),'connected':True} if costs is not None else
                {'status':'unavailable','day':day,'timezone':'Asia/Shanghai','costs':[],
                 'checked_at':None,'connected':True,
                 'message':'官网登录已连接；本程序暂未解析今日费用，将自动重试，无需重复连接。'})
            self.last_attempt = time.time()
            self.revision += 1
            return {'ok':True,'connected':True,'billing_ready':costs is not None}

    @staticmethod
    def atomic(path,value):
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        temp = path.with_name(path.name+'.'+secrets.token_hex(8)+'.tmp')
        try:
            fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                stream.write(json.dumps(value,ensure_ascii=False))
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)

    def save_costs(self,day,costs,fingerprint,name):
        # Ledger contains only totals and the non-reversible key binding, never the login token.
        self.atomic(self.ledger.path,{'day':day,'costs':costs,'checked_at':time.time(),
            'fingerprint':fingerprint,'key_name':clean_name(name),'source':'official_sync'})

    def disconnect(self):
        with self.lock:
            self.path.unlink(missing_ok=True)
            self.ledger.path.unlink(missing_ok=True)
            self.pending.clear()
            self.cache = None
            self.last_attempt = 0
            self.revision += 1
            return {'ok':True,'connected':False}

    def query(self):
        with self.lock:
            self.cleanup()
            day,start,end = bounds(time.time())
            if not self.path.exists():
                result=self.ledger.query()
                if result.get('source')=='official_sync':
                    result={'status':'not_connected','day':day,'timezone':'Asia/Shanghai','costs':[],
                            'checked_at':None,'message':'官网授权已移除，请重新连接用餐记录。'}
                return {**result,'connected':False}
            empty = {'status':'unavailable','day':day,'timezone':'Asia/Shanghai','costs':[],
                     'checked_at':None,'connected':True,'message':'官网登录已连接；今日费用暂未读取成功，将自动重试。'}
            try:
                if self.path.stat().st_size > 8192:
                    raise ValueError()
                saved = json.loads(self.path.read_text(encoding='utf-8'))
                fingerprint = self.ledger.fingerprint()
                if saved.get('fingerprint') != fingerprint:
                    return {**empty,'status':'key_changed','message':'小鲸鱼的 API Key 已变化，请重新连接官网用餐记录。'}
                if self.cache and self.cache['day']==day and time.time()-self.last_attempt < 300:
                    return dict(self.cache)
                self.last_attempt = time.time()
                data = self.fetch(normalize_token(saved['token']),bounds(time.time()-29*86400)[1],end)
                self.record_shape(data)
                costs = daily_costs(data,start,end,saved['tracking_id'])
                self.save_costs(day,costs,fingerprint,saved['key_name'])
                self.cache = {**self.ledger.query(),'connected':True}
            except LoginExpired:
                self.cache = {**empty,'status':'reauth_required','message':'官网饭票过期了，请重新连接；不会把未知金额当作零。'}
            except Exception:
                self.cache = empty
            return dict(self.cache)

    def run(self):
        while True:
            try:
                usage=self.query()
                self.export_rations(usage)
            except Exception:
                pass  # Never log credential-related errors or response bodies.
            time.sleep(60)

    def export_rations(self, usage=None):
        """Only the companion's own totals, never credentials or whole-account costs."""
        balance=DeepSeekBalance(self.root).query()
        usage=usage if usage is not None else self.query()
        value={'generated_at':time.time(),'balance_checked_at':balance.get('checked_at'),
               'balance_status':balance.get('status'),'balances':[
                   {'currency':r['currency'],'amount':r['total_balance']}
                   for r in balance.get('balance_infos',[])],
               'day':usage.get('day'),'cost_status':usage.get('status'),
               'cost_checked_at':usage.get('checked_at'),'whale_costs':[
                   {'currency':r['currency'],'amount':r['whale_cost']}
                   for r in usage.get('costs',[]) if usage.get('status')=='ok']}
        self.atomic(self.root/'data/astrbot/plugin_data/companion_rations/summary.json',value)
