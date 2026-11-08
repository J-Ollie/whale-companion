"""Administrator quota policy. Counters survive; permissions and evidence do not change."""
import itertools
import json
from pathlib import Path

POLICY_PATH=Path(__file__).resolve().parents[2]/'plugin_data'/'companion_brain'/'quota-policy.json'

def enabled(config=None):
    if config is not None and config.get('unlimited_quotas',False) is True:return True
    try:return json.loads(POLICY_PATH.read_text(encoding='utf-8')).get('unlimited_quotas') is True
    except (OSError,ValueError,AttributeError):return False

def steps(count,config=None):return itertools.count() if enabled(config) else range(count)
def maximum(count,config=None):return float('inf') if enabled(config) else count
RULE='管理员已取消项目自行设置的消费及次数额度。旧资料中的每日、本轮次数上限已被此设置覆盖；次数仍真实计数，不推算余额或费用。睡眠、暂停、免打扰、联系人隔离、真实回执和能力权限照常生效；证据门槛不是消费额度。可以自主决定停止，不要求用满或不停行动。'
