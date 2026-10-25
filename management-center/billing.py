"""Read and aggregate exported official cost data. No Platform credentials or login."""
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo
from balance import DeepSeekBalance

TZ = ZoneInfo('Asia/Shanghai')


def bounds(now):
    day = datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return day.date().isoformat(), int(day.timestamp()), int((day + timedelta(days=1)).timestamp())


def money(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError('账单金额格式不正确。')
    value = str(value)
    if len(value)>100 or not re.fullmatch(r'-?\d{1,20}(?:\.\d{1,64})?(?:[eE][+-]?\d{1,3})?', value):
        raise ValueError('本程序暂不能解析官网返回的金额表示。')
    number=Decimal(value)
    if number.is_zero():return Decimal(0)
    if not number.is_finite() or number<0 or number>Decimal('1000000000000') or number.as_tuple().exponent < -64:
        raise ValueError('官网费用金额超出本程序支持范围，暂不展示金额。')
    return number


def clean_name(value):
    if not isinstance(value, str):
        return '未命名 Key'
    if re.search(r'sk-|token|bearer|https?://|\d{5,}', value, re.I):
        return '所选 Key'
    return ' '.join(value.split())[:60] or '未命名 Key'


def unwrap(payload):
    if not isinstance(payload, dict) or payload.get('code') != 0:
        raise ValueError('这不是成功返回的官网费用账单。')
    data = payload.get('data')
    if not isinstance(data, dict) or data.get('biz_code') != 0:
        raise ValueError('官网账单尚未成功返回。')
    data = data.get('biz_data')
    if not isinstance(data, dict) or not isinstance(data.get('data'), list):
        raise ValueError('官网账单格式有变化，暂不展示金额。')
    return data


def keys(data):
    result = {}
    for group in data['data']:
        if not isinstance(group, dict) or not isinstance(group.get('series'), list):
            raise ValueError('官网账单明细不完整。')
        for _, row in series_rows(group):
            key = row.get('api_key')
            if not isinstance(key, dict):
                continue  # Deleted keys still contribute to account totals.
            tracking = key.get('tracking_id')
            if isinstance(tracking, str) and 1 <= len(tracking) <= 160:
                result[tracking] = clean_name(key.get('name'))
    return result


def bucket_seconds(value):
    # Current official frontend treats bucket as seconds, not an ISO duration.
    if value in ('1d','1h'):
        return {'1d':86400,'1h':3600}[value]
    if type(value) is int and 0<value<=86400 and 86400 % value == 0:
        return value
    raise ValueError('官网费用分桶长度不兼容，暂不展示金额。')


def series_rows(group):
    result=[]
    for field in ('series','function_series'):
        rows=group.get(field,[])
        if not isinstance(rows,list):
            raise ValueError('官网费用明细格式异常。')
        for row in rows:
            if not isinstance(row,dict):
                raise ValueError('官网费用明细格式异常。')
            result.append((field,row))
    return result


def aggregate(data, start, end, tracking_id):
    """Use billed costs, not balances; reject incomplete, overlapping or wrong-day exports."""
    seconds=bucket_seconds(data.get('bucket'))
    if data.get('start') != start or data.get('end') != end:
        raise ValueError('请提供北京时间今天00:00至明天00:00的完整账单范围。')
    totals = {}
    seen = set()
    for group in data['data']:
        currency = group.get('currency')
        if currency not in ('CNY', 'USD') or currency in totals or not isinstance(group.get('series'), list):
            raise ValueError('官网账单币种或格式有变化。')
        total = whale = Decimal(0)
        for category,row in series_rows(group):
            key = row.get('api_key')
            identifier = key.get('tracking_id') if isinstance(key, dict) else None
            if not isinstance(row.get('buckets'), list):
                raise ValueError('官网账单明细不完整。')
            for bucket in row['buckets']:
                at = bucket.get('time')
                if not isinstance(at, (int, float)) or isinstance(at, bool) or not start <= at < end:
                    raise ValueError('账单日期不一致，暂不展示金额。')
                if (at-start) % seconds:
                    raise ValueError('官网费用分桶时间未对齐，暂不展示金额。')
                identity = (currency, category, identifier, row.get('model'), at)
                if identity in seen:
                    raise ValueError('账单包含重复分桶，暂不展示金额。')
                seen.add(identity)
                amount = money(bucket.get('cost'))
                with localcontext() as ctx:
                    ctx.prec=128
                    total += amount
                    if identifier == tracking_id:
                        whale += amount
        totals[currency] = {'currency':currency, 'whale_cost':format(whale, 'f'),
                            'account_cost':format(total, 'f')}
    if not totals:
        raise ValueError('官网尚未返回今日费用，不能将缺失数据当作零消费。')
    return list(totals.values())


def daily_costs(data, start, end, tracking_id):
    """Project a validated wider daily/hourly report onto today's interval."""
    left, right = data.get('start'), data.get('end')
    seconds=bucket_seconds(data.get('bucket'))
    if (type(left) is not int or type(right) is not int or left > start or right < end
            or (start-left) % seconds or (end-left) % seconds):
        raise ValueError('官网费用时间范围或分桶格式不兼容，请重新连接。')
    # Validate the entire report before selecting; never hide malformed records.
    aggregate(data,left,right,tracking_id)
    projected = {**data,'start':start,'end':end,'data':[
        {**group,**{field:[{**row,'buckets':[b for b in row['buckets'] if start<=b['time']<end]}
                           for row in group.get(field,[])] for field in ('series','function_series')}}
        for group in data['data']]}
    return aggregate(projected,start,end,tracking_id)


class DeepSeekBilling:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'recovery-data/deepseek-daily-costs.json'
        self.lock = threading.RLock()

    def fingerprint(self):
        return hashlib.sha256(DeepSeekBalance(self.root)._key().encode()).hexdigest()

    def query(self):
        day, _, _ = bounds(time.time())
        empty = {'status':'not_connected','day':day,'timezone':'Asia/Shanghai',
                 'costs':[],'checked_at':None,'message':'用餐记录还没接上，今天吃了多少先不乱报。'}
        with self.lock:
            if not self.path.exists():
                return empty
            try:
                if self.path.stat().st_size > 65536:
                    raise ValueError()
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if data.get('fingerprint') != self.fingerprint():
                    return {**empty,'status':'key_changed','message':'小鲸鱼的 API Key 已变化，请重新确认用餐记录归属。'}
                if data.get('day') != day:
                    return {**empty,'status':'new_day','message':'新的一天，等今天的用餐记录更新。'}
                checked = data.get('checked_at')
                if not isinstance(checked, (int,float)) or not 0 <= time.time()-checked < 86400:
                    raise ValueError()
                costs = data.get('costs')
                if not isinstance(costs,list) or not costs or len(costs)>2:
                    raise ValueError()
                clean = []
                for row in costs:
                    if row.get('currency') not in ('CNY','USD'):
                        raise ValueError()
                    whale, account = money(row['whale_cost']), money(row['account_cost'])
                    if whale > account:
                        raise ValueError()
                    clean.append({'currency':row['currency'],'whale_cost':format(whale,'f'),
                                  'account_cost':format(account,'f')})
                source='official_sync' if data.get('source')=='official_sync' else 'official_import'
                return {'status':'ok','day':day,'timezone':'Asia/Shanghai','costs':clean,
                        'checked_at':checked,'source':source,'key_name':clean_name(data.get('key_name')),
                        'message':('官网用餐记录自动同步 · 每5分钟更新，可能有入账延迟。' if source=='official_sync' else '官网账单导入 · 北京时间；按所选 Key 统计小鲸鱼，账户整体为全部 Key。')}
            except Exception:
                return {**empty,'status':'unavailable','message':'用餐记录暂时没翻开，余额仍可正常查询。'}

    def import_report(self, payload, tracking_id, confirmed):
        if confirmed is not True:
            raise ValueError('请确认账单包含账户全部 Key，并选中小鲸鱼使用的专用 Key。')
        data = unwrap(payload)
        available = keys(data)
        if tracking_id not in available:
            raise ValueError('请从账单中选择小鲸鱼使用的 Key。')
        day, start, end = bounds(time.time())
        costs = aggregate(data, start, end, tracking_id)
        with self.lock:
            saved = {'day':day,'costs':costs,'checked_at':time.time(),'fingerprint':self.fingerprint(),
                     'key_name':available[tracking_id]}
            self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(saved,ensure_ascii=False),encoding='utf-8')
            temp.chmod(0o600)
            temp.replace(self.path)
        return self.query()
