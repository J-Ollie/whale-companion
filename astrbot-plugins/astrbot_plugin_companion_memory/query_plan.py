"""Conservative, local recall planning. Dates constrain message-record time only."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
import time

from .memory import TZ

LEAD=re.compile(r'^(?:(?:请|帮我|查一下|查询|回忆一下|回忆|看看|找找|我们|你我|我|在)\s*)*$')
ABSOLUTE=re.compile(r'(?P<y>20\d{2})[-年](?P<m>\d{1,2})[-月](?P<d>\d{1,2})日?')
RELATIVE=re.compile(r'前天|昨天|今天|上周|本周|这周|上个月|本月|这个月|最近(?P<n>\d{1,2})天')
PART=re.compile(r'\s*(凌晨|早上|上午|中午|下午|傍晚|晚上)')
HOURS={'凌晨':(0,6),'早上':(6,12),'上午':(6,12),'中午':(11,14),
       '下午':(12,18),'傍晚':(17,20),'晚上':(18,24)}
TIME_ONLY=re.compile(r'^(?:[，,。？！?\s]|我们|你我|都|的|(?:讨论|聊|说)(?:了|过)(?:什么|啥)|聊天记录|原话|有哪些交流|有什么交流)*$')
COMPARISON=re.compile(r'^(?:请|帮我)?\s*(?:比较一下|对比一下|比较|对比)?\s*'
 r'(?P<a>[^，,。？！?]{1,36}?)\s*(?:和|与|跟|\bvs\.?\b)\s*'
 r'(?P<b>[^，,。？！?]{1,36}?)\s*(?:有什么区别|有什么不同|有何区别|有何不同|的区别|的差别|相比有什么变化)[？?\s]*$',re.I)
TEMPORAL_SIDE=re.compile(r'^(?:以前|现在|过去|最近|上次|这次|之前|之后)(?:的)?(?:方案|情况|样子|状态)?$')


@dataclass(frozen=True)
class QueryPlan:
    query: str
    start_day: str=''
    end_day: str=''
    window: tuple|None=None
    part: str=''
    sides: tuple=()
    variants: tuple=()


def comparison_sides(query):
    quoted=re.fullmatch(r'(?:请|帮我)?\s*(?:比较一下|对比一下|比较|对比)?\s*[“"《](.+?)[”"》]\s*(?:和|与|跟|vs\.?)\s*[“"《](.+?)[”"》]\s*(?:有什么区别|有什么不同|有何区别|有何不同|的区别|的差别)[？?\s]*',query.strip(),re.I)
    if quoted:
        sides=tuple(x.strip() for x in quoted.groups())
        return sides if all(1<=len(x)<=36 for x in sides) and sides[0]!=sides[1] else ()
    match=COMPARISON.fullmatch(query.strip())
    if not match:return ()
    sides=tuple(match.group(k).strip(' \t“”"\'《》') for k in ('a','b'))
    if any(not side or TEMPORAL_SIDE.fullmatch(side) or re.search(r'[和与跟“”"《》]',side) for side in sides) or sides[0]==sides[1]:return ()
    return sides


def make_plan(query,start_day='',end_day='',now=None):
    today=datetime.fromtimestamp(time.time() if now is None else now,TZ).replace(hour=0,minute=0,second=0,microsecond=0)
    absolute=list(ABSOLUTE.finditer(query));relative=RELATIVE.search(query)
    match=None;first=last=None
    if len(absolute)==1 and LEAD.fullmatch(query[:absolute[0].start()]) and (start_day or end_day or
       re.search(r'原话|说|聊|讨论|告诉|提到|点(?:的|过的)?歌|交流|约定',query[absolute[0].end():]) and not re.search(r'生日|出生|有效期|保质期',query)):
        match=absolute[0]
        try:first=last=datetime(int(match['y']),int(match['m']),int(match['d']),tzinfo=TZ)
        except ValueError:raise ValueError('回忆日期无效') from None
    elif not absolute and relative and LEAD.fullmatch(query[:relative.start()]) and not (len(list(RELATIVE.finditer(query)))>1 and re.search(r'区别|不同|差别',query)):
        match=relative;label=match.group()
        if label in ('前天','昨天','今天'):first=last=today-timedelta(days={'前天':2,'昨天':1,'今天':0}[label])
        elif label in ('上周','本周','这周'):
            first=today-timedelta(days=today.weekday()+(7 if label=='上周' else 0));last=first+timedelta(days=6)
        elif label in ('上个月','本月','这个月'):
            first=today.replace(day=1)
            if label=='上个月':last=first-timedelta(days=1);first=last.replace(day=1)
            else:
                following=(first.replace(day=28)+timedelta(days=4)).replace(day=1);last=following-timedelta(days=1)
        else:
            n=int(match['n'])
            if not 1<=n<=31:raise ValueError('最近天数应为1至31')
            first=today-timedelta(days=n-1);last=today
    start,end=start_day,end_day
    use_match=False
    if match:
        f,l=first.date().isoformat(),last.date().isoformat()
        if not start and not end:start,end=f,l;use_match=True
        elif start==f and end==l:use_match=True
    cleaned=query;window=None;part=''
    if use_match:
        cut=match.end();part_match=PART.match(query,cut)
        if first==last and part_match:
            part=part_match.group(1);a,b=HOURS[part]
            window=((first+timedelta(hours=a)).timestamp(),(first+timedelta(hours=b)).timestamp())
            cut=part_match.end()
        cleaned=query[cut:].lstrip(' ，,：:')
        if TIME_ONLY.fullmatch(cleaned):cleaned=''
    variants=('点歌','唱歌') if re.search(r'点(?:的|过的|了的)歌(?!手)',cleaned) else ()
    return QueryPlan(cleaned,start,end,window,part,comparison_sides(cleaned),variants)
