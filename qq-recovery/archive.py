"""Read-only character snapshot and bounded activity archive, projected by role."""
import json
import math
import re
import sqlite3
import time
from collections import Counter
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Shanghai')
BASE_TRAITS='有一点嘴硬、懒散和好奇，会吐槽，也会认真。表达自然简短，性格随真实经历慢慢变化。'


def number(value):
    return float(value) if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) else None


def text(value,limit=240):
    if not isinstance(value,str):return ''
    value=' '.join(value.split())[:limit]
    if re.search(r'密码|密钥|验证码|token|api.?key|bearer|https?://|/sdcard|/opt/',value,re.I):return ''
    return re.sub(r'\d{5,}','[数字已隐藏]',value)


class Archive:
    def __init__(self,root):
        self.data=Path(root)/'data/astrbot/plugin_data'
        self.phone=self.data/'companion_phone'

    @staticmethod
    def read(path):
        try:
            if path.stat().st_size>2*1024*1024:return {}
            data=json.loads(path.read_text(encoding='utf-8'))
            return data if isinstance(data,dict) else {}
        except (OSError,ValueError):return {}

    def last_chat(self):
        path=self.data/'companion_memory/memory.sqlite3'
        if not path.exists():return None
        try:
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
                return number(db.execute('SELECT MAX(last_bot) FROM people').fetchone()[0])
        except sqlite3.Error:return None

    @staticmethod
    def journal(path):
        try:
            with path.open('rb') as stream:
                stream.seek(0,2);start=max(0,stream.tell()-2*1024*1024);stream.seek(start)
                if start:stream.readline()
                lines=stream.read().decode('utf-8',errors='replace').splitlines()[-1000:]
            rows=[]
            for line in lines:
                try:
                    row=json.loads(line)
                    if isinstance(row,dict):rows.append(row)
                except ValueError:continue
            return rows
        except OSError:return []

    @staticmethod
    def trigger(row):
        if row.get('spontaneous') is True or row.get('trigger') in ('spontaneous','background'):
            return '自主活动'
        if row.get('trigger') in ('owner_requested','manual','requested'):
            return '指令触发'
        return '触发方式未记录'

    def other_activities(self):
        rows=[]
        def event(at,kind,app,note,**extra):
            at=number(at)
            if not at or at<0:return
            try:day=datetime.fromtimestamp(at,TZ).date().isoformat()
            except (ValueError,OverflowError,OSError):return
            # URLs and storage metadata are never part of the administration timeline.
            note=re.sub(r'https?://\S+','[链接已省略]',str(note))
            rows.append({'at':at,'day':day,'kind':kind,'app':app,'note':text(note,700) or '该条内容未在时间线展开。',
                         'duration_seconds':None,'started_at':None,'views':None,'observations':None,**extra})
        state=self.read(self.phone/'qzone-state.json')
        attempts=state.get('attempts',[])
        attempts=attempts[-1000:] if isinstance(attempts,list) else []
        # Receipt snapshots may be older than reconciled state, especially audience restrictions.
        posts={}
        for row in self.journal(self.phone/'qzone-publications.jsonl')+attempts:
            if not isinstance(row,dict) or not number(row.get('at')):continue
            tid=row.get('tid');key=('tid',str(tid)) if tid else ('at',row['at'])
            posts[key]={**posts.get(key,{}),**row}
        spontaneous=number(state.get('last_spontaneous_consider_at'))
        for row in posts.values():
            verified=row.get('result')=='verified'
            status={'verified':'自身回读确认','uncertain':'发布结果待确认','reserved':'发布尝试待确认','failed':'发布失败','not_sent':'未发送'}.get(row.get('result'),'发布尝试')
            audience={'platform_restricted':'平台限制已确认','owner_confirmed_visible':'管理员确认可见','owner_reported_missing':'管理员反馈不可见'}.get(row.get('audience_status'),'他人可见性未确认')
            origin=self.trigger(row)
            # This marker is written alongside the reserved publication, not inferred from author.
            if origin=='触发方式未记录' and spontaneous and abs(row['at']-spontaneous)<1:origin='自主发布'
            author={'chat_agent':'对话中创作','background_agent':'后台创作'}.get(row.get('authored_by'),'创作来源未记录')
            note=(row.get('readback_text') or row.get('published_text') or row.get('text') or row.get('attempted_text')) if verified else (row.get('text') or row.get('attempted_text'))
            event(row['at'],'qzone','QQ空间',note or '未保存文案',title='空间动态',result=status,
                  origin=origin,audience=audience,author=author,has_image=bool(row.get('image') or row.get('has_image')),
                  confirmed=verified)
        outbound=self.read(self.phone/'outbound-state.json')
        for key,kind,title in [('shares','share','主动分享'),('replies','media_reply','聊天媒体回复')]:
            history=outbound.get(key,[])
            if not isinstance(history,list):continue
            for row in history[-300:]:
                if not isinstance(row,dict):continue
                event(row.get('at'),kind,'QQ',row.get('caption') or title,title=title,
                      origin='自主分享' if kind=='share' else self.trigger(row),
                      result={'sent':'发送回执确认','reserved':'发送结果待确认','uncertain':'发送结果待确认'}.get(row.get('result'),'发送未确认'))
        autonomy=self.read(self.phone/'autonomy.json')
        failure={'device_unavailable_or_left_app':'手机操作未完成，可能中断或离开应用；具体原因未记录',
                 'offline':'手机连接不可达','session_timeout':'本次运行到达时限',
                 'interrupted':'被新消息或暂停打断','error':'本次异常，已停止','no_content':'未读到有效内容'}
        if autonomy.get('last_result') in failure:
            event(autonomy.get('last_checked_at'),'phone_attempt',text(autonomy.get('last_app'),40) or '手机',
                  failure[autonomy['last_result']],title='访问尝试',result='访问未完成',
                  origin=self.trigger({'trigger':autonomy.get('last_trigger')}))
        return rows

    def snapshot(self,guest=False,day=''):
        now=time.time()
        today=datetime.fromtimestamp(now,TZ).date().isoformat()
        if day:
            try:
                if datetime.strptime(day,'%Y-%m-%d').strftime('%Y-%m-%d')!=day:raise ValueError()
            except (ValueError,TypeError):raise ValueError('日期格式应为 YYYY-MM-DD')
        day=day or today
        rhythm=self.read(self.phone/'rhythm.json')
        profile=self.read(self.data/'companion_memory/public-profile.json')
        updated=number(rhythm.get('updated_at'))
        mode=rhythm.get('mode')
        stale=updated is None or not -60<=now-updated<=600
        since=number(rhythm.get('mode_since'))
        until=number(rhythm.get('until'))
        if mode in ('asleep','resting') and until and until<now:stale=True
        if mode=='browsing' and (since is None or now-since>600):stale=True
        state='unknown' if stale else {'asleep':'asleep','resting':'resting','browsing':'browsing','awake':'idle'}.get(mode,'unknown')
        # Only a confirmed outgoing message timestamp, never message text or a person identifier.
        last_chat=self.last_chat()
        if last_chat and 0<=now-last_chat<=90:state='chatting';updated=last_chat;stale=False
        labels={'unknown':'状态待更新','asleep':'睡觉中','resting':'休息中','browsing':'玩手机中','idle':'清醒 · 空闲','chatting':'聊天活跃'}
        wake=number(rhythm.get('wake_anchor'))
        wake=int(wake)%1440 if wake is not None else None
        traits=text(profile.get('traits'),400)
        result={'generated_at':now,'timezone':'Asia/Shanghai',
                'profile':{'name':text(profile.get('name'),24) or '小鲸鱼',
                           'traits':traits or BASE_TRAITS,'traits_kind':'成长后的公开性格' if traits else '初始气质',
                           'updated_at':number(profile.get('updated_at'))},
                'current':{'state':state,'label':labels[state],'updated_at':updated,'stale':stale,
                           'note':'依据最近90秒内已确认的回复记录，不表示持续聊天。' if state=='chatting' else '作息模型记录的角色状态；空闲不等同于无聊。'},
                'routine':{'wake_time':f'{wake//60:02d}:{wake%60:02d}' if wake is not None else None,
                           'sleep_hours':number(rhythm.get('sleep_hours')),
                           'until':until if state in ('asleep','resting') and until and until>now else None,
                           'note':'北京时间；起床倾向会随习惯缓慢调整，并非固定闹钟。'}}
        if guest:return result  # Do not even read activity notes for guests.
        rows=[]
        for row in self.journal(self.phone/'experiences.jsonl'):
            try:
                at=number(row.get('timestamp'))
                if not at or at<0:continue
                row_day=datetime.fromtimestamp(at,TZ).date().isoformat()
                start_at=number(row.get('started_at'));end_at=number(row.get('ended_at'))
                duration=end_at-start_at if start_at is not None and end_at is not None and 0<=end_at-start_at<=86400 else None
                rows.append({'at':at,'day':row_day,'app':text(row.get('app'),40) or '未命名应用',
                             'kind':'browse','title':'手机浏览','origin':self.trigger(row),'result':'浏览经历已保存',
                             'note':text(row.get('note'),300) or '本次没有保存浏览摘要。',
                             'views':number(row.get('views')),'observations':number(row.get('observations')),
                             'duration_seconds':duration,'started_at':start_at if duration is not None else None})
            except (ValueError,TypeError,OverflowError,OSError):continue
        rows.extend(self.other_activities())
        dates=sorted({r['day'] for r in rows}|{today},reverse=True)
        selected=sorted((r for r in rows if r['day']==day),key=lambda r:r['at'],reverse=True)
        apps=Counter(r['app'] for r in selected)
        result['activity']={'day':day,'dates':dates,'entries':selected,
                            'total':len(selected),'apps':[{'name':name,'count':count} for name,count in apps.most_common()],
                            'known_duration_seconds':sum(r['duration_seconds'] or 0 for r in selected),
                            'missing_duration':sum(r['duration_seconds'] is None for r in selected),
                            'browse_count':sum(r['kind']=='browse' for r in selected),
                            'qzone_count':sum(r['kind']=='qzone' and r.get('confirmed') is True for r in selected),
                            'notice':'合并浏览经历、空间发布回执、分享与最近一次未完成的访问尝试。浏览时间为记录完成时间，动态时间为发布尝试时间，异常时间为检查时间；自身回读不代表他人可见。旧记录缺少起止时间或触发来源时如实标注，不补造；浏览源只保留最近100条，异常源仅保留最近一次，不是完整操作日志。'}
        result['tendencies']={k:number(rhythm.get(k)) for k in ('energy','curiosity','social')}
        return result
