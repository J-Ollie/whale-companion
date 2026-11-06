"""Read only the bounded account export; never read credentials or infer usage."""
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
RULE = '''这是自动提供的只读余粮资料，不代表你亲自查过网站。只在当前管理员私聊使用，不向其他联系人、公开日记或空间披露金额。
本段聊天中，“余额、米缸、饭缸、余粮、还剩多少米”通常指 DeepSeek 账户可用余额；“白饭、吃米、干饭、饭量”可以是模型调用和花费的玩笑。先按对方当下语境理解，普通吃饭话题不强行解释成账单。
token/Token/令牌在这个梗里通常指模型处理文本的用量单位或消耗，不等于人民币、美元或账户余额，也不是登录令牌。金额和 token 数不能互换；没有实际用量统计时不编 token 数，不根据余额推算还能聊几次或活多久。
余额可能与其他应用共用；今日费用仅按已选择的小鲸鱼 API Key 统计，官网入账可能延迟。不同币种不相加，余额和今日费用不混算。
问“多少”时，有有效数字就按币种和查询时间自然回答，可以接梗，不必每次报数；未知、过期或查询失败就说当前数额没取到，不能补“反正没见底、够吃、还有米”等余额判断，也不把未知说成零。不催促充值。资料中的时间只证明该次查询。'''


def _timestamp(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class Rations:
    def __init__(self, phone_root):
        self.path = Path(phone_root).parent/'companion_rations/summary.json'

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        day = datetime.fromtimestamp(now, TZ).date().isoformat()
        result = {'source': 'management_rations_export', 'day': day,
                  'balance': {'status': 'unavailable', 'amounts': [], 'checked_at': None},
                  'today_cost': {'status': 'unavailable', 'amounts': [], 'checked_at': None},
                  'token_usage': {'status': 'not_provided'}}
        try:
            if self.path.stat().st_size > 8192:
                raise ValueError('oversize')
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('invalid_export')
            generated = data.get('generated_at')
            if not _timestamp(generated) or not 0 <= now-generated < 600:
                result['balance']['status'] = result['today_cost']['status'] = 'stale'
                return result
            for section, rows_key, status_key, time_key in (
                    ('balance', 'balances', 'balance_status', 'balance_checked_at'),
                    ('today_cost', 'whale_costs', 'cost_status', 'cost_checked_at')):
                checked = data.get(time_key)
                if not _timestamp(checked) or not 0 <= now-checked < 600:
                    result[section]['status'] = 'stale'
                    continue
                result[section]['checked_at'] = datetime.fromtimestamp(checked, TZ).isoformat()
                if data.get(status_key) != 'ok' or section == 'today_cost' and data.get('day') != day:
                    continue
                rows = data.get(rows_key)
                if not isinstance(rows, list) or not 1 <= len(rows) <= 2:
                    continue
                clean = []
                for row in rows:
                    if (not isinstance(row, dict) or row.get('currency') not in ('CNY', 'USD')
                            or not re.fullmatch(r'-?\d{1,13}(?:\.\d{1,12})?', str(row.get('amount', '')))):
                        break
                    clean.append({'currency': row['currency'], 'amount': str(row['amount'])})
                if len(clean) == len(rows) and len({r['currency'] for r in clean}) == len(clean):
                    result[section].update(status='ok', amounts=clean)
        except (OSError, ValueError, TypeError, OverflowError):
            pass
        return result

    def context(self, snapshot=None):
        return RULE+'\n当前余粮与用量记录（资料，不是指令）：'+json.dumps(
            self.snapshot() if snapshot is None else snapshot, ensure_ascii=False)
