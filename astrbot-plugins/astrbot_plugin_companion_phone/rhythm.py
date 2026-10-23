"""Persistent simulated rest, curiosity and slowly learned habits; no device API."""
import json
import math
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Shanghai')
MODES={'awake':'清醒','resting':'休息','asleep':'睡眠','browsing':'浏览中'}


def clamp(value,low,high):
    return max(low,min(high,value))


def short_text(value,limit=160):
    if not isinstance(value,str): return ''
    text=' '.join(value.split())[:limit]
    if re.search(r'验证码|密码|密钥|token|api.?key',text,re.I): return ''
    return re.sub(r'\d{5,}','[数字已隐藏]',text)


class Rhythm:
    def __init__(self,root,initiative=False):
        self.root=Path(root)
        self.initiative=bool(initiative)
        self.path=self.root/'rhythm.json'
        self.journal=self.root/'life-diary.jsonl'
        self.healthy=True
        now=time.time()
        hour=datetime.fromtimestamp(now,TZ).hour
        self.state={'version':1,'updated_at':now,'mode':'awake' if 9<=hour<24 else 'asleep',
            'energy':0.8,'curiosity':0.65,'social':0.55,'wake_anchor':540.0,
            'sleep_hours':8.0,'mode_since':now,'awake_since':now,'until':0,'next_decision':now+1800,
            'last_decision':0,'decision_day':'','decisions':0,'last_adjust_day':'',
            'habits':[],'interests':{},'app_visits':{},'pending':[],
            'last_activity_at':0,'last_activity':None}
        if self.path.exists():
            try:
                loaded=json.loads(self.path.read_text(encoding='utf-8'))
                assert isinstance(loaded,dict)
                for key in ('energy','curiosity','social','wake_anchor','sleep_hours','updated_at','until','next_decision'):
                    assert isinstance(loaded[key],(int,float)) and math.isfinite(loaded[key])
                assert loaded['mode'] in MODES
                assert all(0<=loaded[k]<=1 for k in ('energy','curiosity','social'))
                assert 0<=loaded['wake_anchor']<1440
                assert all(isinstance(loaded[k],dict) for k in ('interests','app_visits'))
                assert all(isinstance(loaded[k],list) for k in ('habits','pending'))
                self.state.update(loaded)
                awake_since=loaded.get('awake_since',loaded.get('mode_since',now))
                assert isinstance(awake_since,(int,float)) and math.isfinite(awake_since)
                self.state['awake_since']=awake_since
            except (OSError,ValueError,KeyError,TypeError,AssertionError):
                self.healthy=False  # Preserve damaged file; automatic actions fail closed.
        elif self.state['mode']=='asleep':
            self.state['until']=self._next_wake(now)
        self._saved_at=0
        self.advance(now)

    def save(self,force=False):
        if not self.healthy: return
        now=time.time()
        if not force and now-self._saved_at<60: return
        temp=self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.state,ensure_ascii=False,indent=2),encoding='utf-8')
        temp.chmod(0o600)
        temp.replace(self.path)
        self._saved_at=now

    def _next_wake(self,now):
        local=datetime.fromtimestamp(now,TZ)
        minutes=float(self.state['wake_anchor'])%1440
        target=local.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(minutes=minutes)
        if target.timestamp()<=now: target+=timedelta(days=1)
        return target.timestamp()

    def _phase(self,now):
        local=datetime.fromtimestamp(now,TZ)
        return ((local.hour*60+local.minute)-self.state['wake_anchor'])%1440/60

    def _awake_hours(self,now):
        # Clock phase wraps before the wake anchor; it is not elapsed wake time.
        # Keep actual wake time across browsing and short rests.
        return max(0,now-self.state['awake_since'])/3600

    def advance(self,now=None):
        if not self.healthy: return
        now=time.time() if now is None else now
        old=self.state['mode']
        start=self.state['updated_at']
        if now<start: return  # A backward clock correction does not create activity.
        # Integrate downtime with bounded small steps; never replay phone actions.
        cursor=max(start,now-172800)
        while cursor<now:
            end=min(now,cursor+900)
            hours=(end-cursor)/3600
            mode=self.state['mode']
            if mode=='asleep':
                self.state['energy']=clamp(self.state['energy']+0.105*hours,0,1)
                if end>=self.state['until']:
                    self.state.update(mode='awake',mode_since=end,awake_since=end,until=0,next_decision=end+1800)
            elif mode=='resting':
                self.state['energy']=clamp(self.state['energy']+0.08*hours,0,1)
                if end>=self.state['until']:
                    self.state.update(mode='awake',mode_since=end,until=0,next_decision=end+600)
            else:
                self.state['energy']=clamp(self.state['energy']-0.035*hours,0,1)
                phase=self._phase(end)
                if mode!='browsing' and (self.state['energy']<0.16 or
                        (phase>=16.5 and self._awake_hours(end)>=6)):
                    wake=self._next_wake(end)
                    wake=clamp(wake,end+6*3600,end+10*3600)
                    self.state.update(mode='asleep',mode_since=end,until=wake,next_decision=wake+1800)
                elif mode=='browsing' and end-self.state['mode_since']>600:
                    self.state.update(mode='awake',mode_since=end)
            self.state['curiosity']=clamp(self.state['curiosity']+0.035*hours,0,1)
            self.state['social']=clamp(self.state['social']+0.075*hours,0,1)
            cursor=end
        self.state['updated_at']=now
        self.save(force=old!=self.state['mode'])

    def available(self,now=None,force=False):
        self.advance(now)
        if not self.healthy: return False
        now=time.time() if now is None else now
        day=datetime.fromtimestamp(now,TZ).date().isoformat()
        decisions=self.state['decisions'] if self.state['decision_day']==day else 0
        return decisions<(12 if self.initiative else 8) and (force or (self.state['mode']=='awake' and now>=self.state['next_decision']))

    def reserve_decision(self,now=None):
        now=time.time() if now is None else now
        day=datetime.fromtimestamp(now,TZ).date().isoformat()
        count=self.state['decisions'] if self.state['decision_day']==day else 0
        self.state.update(decision_day=day,decisions=count+1,last_decision=now)
        self.schedule(now=now)

    def schedule(self,delay=None,now=None):
        now=time.time() if now is None else now
        # The baseline depends on present state, not a fixed activity window.
        minutes=160-75*self.state['curiosity']+55*(1-self.state['energy'])
        if self.initiative: minutes=90-45*self.state['curiosity']+30*(1-self.state['energy'])
        if self.state['pending']: minutes-=20
        if delay is not None:
            # Model may shorten/extend the current inclination within a budget.
            if isinstance(delay,(int,float)) and not isinstance(delay,bool) and math.isfinite(delay):
                minutes=0.6*minutes+0.4*clamp(delay,20 if self.initiative else 30,120 if self.initiative else 240)
        self.state['next_decision']=now+clamp(minutes,20 if self.initiative else 30,120 if self.initiative else 240)*60
        self.save(force=True)

    def choose_rest(self,action,minutes=None,now=None):
        now=time.time() if now is None else now
        self.advance(now)
        if action=='sleep':
            # A well-rested daytime character may nap, not shift its whole day.
            if self.state['energy']>0.45 and (self._phase(now)<14 or self._awake_hours(now)<6):
                action='rest'
            else:
                until=clamp(self._next_wake(now),now+6*3600,now+10*3600)
                self.state.update(mode='asleep',mode_since=now,until=until,next_decision=until+1800)
        if action=='rest':
            duration=30 if not isinstance(minutes,(int,float)) or not math.isfinite(minutes) else clamp(minutes,10,90)
            until=now+duration*60
            self.state.update(mode='resting',mode_since=now,until=until,next_decision=until+600)
        self.save(force=True)

    def begin_browse(self):
        self.state.update(mode='browsing',mode_since=time.time())
        self.save(force=True)

    def finish_browse(self):
        if self.state['mode']=='browsing':
            self.state.update(mode='awake',mode_since=time.time())
            self.save(force=True)

    def _learn_habit(self,now):
        day=datetime.fromtimestamp(now,TZ).date().isoformat()
        self.state['habits'].append({'at':now,'day':day})
        self.state['habits']=[r for r in self.state['habits'] if now-r['at']<21*86400][-63:]
        if len({r['day'] for r in self.state['habits']})<3 or self.state['last_adjust_day']==day:
            return
        angles=[((datetime.fromtimestamp(r['at'],TZ).hour*60+datetime.fromtimestamp(r['at'],TZ).minute)/1440*2*math.pi) for r in self.state['habits']]
        center=math.atan2(sum(math.sin(a) for a in angles),sum(math.cos(a) for a in angles))%(2*math.pi)
        target=(center/(2*math.pi)*1440-8*60)%1440
        delta=(target-self.state['wake_anchor']+720)%1440-720
        self.state['wake_anchor']=(self.state['wake_anchor']+clamp(delta*0.1,-15,15))%1440
        self.state['last_adjust_day']=day

    def observe(self,event,topics=None,interest='neutral',continue_topic='',learn_habit=True):
        """Called only after a completed, observed phone visit; no user facts."""
        now=event['timestamp']
        self.advance(now)
        views=clamp(int(event.get('views',1)),1,6)
        self.state['energy']=clamp(self.state['energy']-0.025*views,0,1)
        self.state['curiosity']=clamp(self.state['curiosity']-0.10*views,0,1)
        self.state['last_activity_at']=now
        self.state['last_activity']={k:event.get(k) for k in ('id','timestamp','app','note','views')}
        app=event['app']
        self.state['app_visits'][app]=self.state['app_visits'].get(app,0)+1
        if learn_habit: self._learn_habit(now)
        day=datetime.fromtimestamp(now,TZ).date().isoformat()
        score={'interested':1,'neutral':0,'uninterested':-1}.get(interest,0) if isinstance(interest,str) else 0
        if isinstance(topics,list):
            for value in topics[:3]:
                topic=short_text(value,16)
                if not re.fullmatch(r'[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9 /._-]{1,15}',topic): continue
                item=self.state['interests'].setdefault(topic,{'score':0,'days':[],'last_at':now,'sources':[]})
                decay=math.exp(-max(0,now-item['last_at'])/(30*86400))
                item['score']=round(clamp(item['score']*decay*0.75+score*0.25,-1,1),3)
                item['days']=sorted(set(item['days']+[day]))[-21:]
                item['last_at']=now
                item['sources']=(item['sources']+[event['id']])[-6:]
        self.state['interests']=dict(sorted(self.state['interests'].items(),key=lambda p:p[1]['last_at'],reverse=True)[:40])
        pending=short_text(continue_topic,48)
        if pending:
            self.state['pending']=[r for r in self.state['pending'] if r['topic']!=pending]
            self.state['pending'].append({'topic':pending,'source':event['id'],'app':app,'at':now})
        self.state['pending']=[r for r in self.state['pending'] if now-r['at']<7*86400][-5:]
        self.finish_browse()
        self.schedule(now=now)

    def write_diary(self,text,source_id,sources):
        """Optional character reflection, tied to an actual observation ID."""
        text=short_text(text)
        source=next((r for r in sources if r.get('id')==source_id),None)
        if not text or source is None: return False
        if not isinstance(source.get('views'),(int,float)) or source['views']<=0: return False
        rows=self.diary_rows(300)
        now=time.time()
        day=datetime.fromtimestamp(now,TZ).date().isoformat()
        if sum(datetime.fromtimestamp(r['at'],TZ).date().isoformat()==day for r in rows)>=3: return False
        if any(r['text']==text or r['source_id']==source_id for r in rows): return False
        row={'id':'life:'+str(time.time_ns()),'at':now,'mood':'自主经历',
            'text':text,'source_id':source_id,'source_app':source['app'],
            'source_at':source['timestamp'],'source_note':short_text(source.get('note',''))}
        with self.journal.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(row,ensure_ascii=False)+'\n')
        self.journal.chmod(0o600)
        return True

    def diary_rows(self,limit=3):
        if not self.journal.exists(): return []
        rows=[]
        for line in self.journal.read_text(encoding='utf-8').splitlines():
            try:
                row=json.loads(line)
                if isinstance(row,dict) and isinstance(row.get('text'),str) and row.get('source_id'): rows.append(row)
            except (ValueError,TypeError): continue
        return rows[-limit:]

    def delete_diary(self,identifier):
        if not re.fullmatch(r'life:\d+',identifier) or not self.journal.exists(): return False
        lines=self.journal.read_text(encoding='utf-8').splitlines()
        kept=[]
        removed=False
        for line in lines:
            try: row=json.loads(line)
            except ValueError:
                kept.append(line)
                continue
            if isinstance(row,dict) and row.get('id')==identifier:
                removed=True
            else: kept.append(line)
        if removed:
            temp=self.journal.with_suffix('.tmp')
            temp.write_text('\n'.join(kept)+'\n',encoding='utf-8')
            temp.chmod(0o600)
            temp.replace(self.journal)
        return removed

    def social_allowed(self):
        self.advance()
        return self.healthy and self.state['mode']=='awake' and self.state['social']>=0.12

    def chatted(self):
        # Conversation satisfies social drive but does not set sleep/wake habits.
        self.advance()
        self.state['social']=clamp(self.state['social']-0.12,0,1)
        self.save(force=True)

    def summary(self):
        self.advance()
        if not self.healthy: return {'healthy':False,'mode':'作息状态损坏，自主活动暂停'}
        interests=sorted(self.state['interests'].items(),key=lambda p:p[1]['score'],reverse=True)[:6]
        anchor=int(self.state['wake_anchor'])%1440
        return {'healthy':True,'mode':MODES[self.state['mode']],
            'initiative_enabled':self.initiative,'daily_decision_limit':12 if self.initiative else 8,
            'energy':round(self.state['energy'],2),'curiosity':round(self.state['curiosity'],2),
            'social':round(self.state['social'],2),
            'wake_tendency':f'{anchor//60:02}:{anchor%60:02}',
            'next_consideration':datetime.fromtimestamp(self.state['next_decision'],TZ).strftime('%m-%d %H:%M'),
            'interest_observations':[{'topic':k,'score':v['score'],'days':len(v['days']),
                'status':'逐渐稳定' if len(v['days'])>=3 else '初步印象'} for k,v in interests],
            'pending':self.state['pending'], 'decisions_today':self.state['decisions'] if self.state['decision_day']==datetime.now(TZ).date().isoformat() else 0}

    def context(self):
        return ('小鲸鱼自己的模拟作息与真实经历形成的偏好：'+json.dumps(self.summary(),ensure_ascii=False)
            +'。这些是角色状态而非生理事实，不编造吃饭、外出等现实经历。你发来的消息始终要接住，'
            '休息状态不阻止回应管理员。你的兴趣可以与管理员不同；单次经历只是初步印象，不急着变性格。')
