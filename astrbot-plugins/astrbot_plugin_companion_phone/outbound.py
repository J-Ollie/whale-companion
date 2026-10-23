"""Owner-only media replies and at most three proactive content shares/day."""
import asyncio
import base64
import hashlib
import json
import time
from datetime import datetime
from .autonomy import TZ
from .phone import PhoneError


class Outbound:
    def __init__(self,root,library,send,notify_allowed):
        self.library,self.send,self.notify_allowed=library,send,notify_allowed
        self.path=root/'outbound-state.json'
        self.state={'paused':False,'shares':[],'replies':[]}; self.lock=asyncio.Lock()
        if self.path.exists():
            try: self.state.update(json.loads(self.path.read_text(encoding='utf-8')))
            except (ValueError,OSError): self.state['paused']=True

    def save(self):
        tmp=self.path.with_suffix('.tmp'); tmp.write_text(json.dumps(self.state,ensure_ascii=False),encoding='utf-8')
        tmp.chmod(0o600); tmp.replace(self.path)

    def used(self):
        day=datetime.fromtimestamp(time.time(),TZ).date().isoformat()
        return sum(r['day']==day for r in self.state['shares'])

    async def deliver(self,segments,*,share=False,source='',request_id=''):
        async with self.lock:
            now=time.time(); local=datetime.fromtimestamp(now,TZ)
            if share and self.state['paused']: return {'result':'paused'}
            if share and (self.used()>=3 or not 9<=local.hour<23 or not self.notify_allowed()):
                return {'result':'not_due','used_today':self.used()}
            history=self.state['shares']+self.state['replies']
            if sum(now-r['at']<60 for r in history)>=3: raise PhoneError('图片发送过快，请稍后再发。')
            if share and self.state['shares'] and now-self.state['shares'][-1]['at']<3600:
                return {'result':'not_due','used_today':self.used()}
            identity=source if share else request_id
            digest=hashlib.sha256(identity.encode()).hexdigest() if identity else ''
            if digest and any(r.get('digest')==digest for r in history): return {'result':'duplicate'}
            caption=''.join(s.get('data',{}).get('text','') for s in segments if s.get('type')=='text')[:180]
            row={'at':now,'day':local.date().isoformat(),'digest':digest,'result':'reserved','caption':caption}
            key='shares' if share else 'replies'
            self.state[key]=self.state[key][-299:]+[row]; self.save()
            # A timeout remains counted; neither the model nor the scheduler retries it.
            try:
                receipt=await self.send(segments)
                row.update(result='sent',message_id=str(receipt))
            except BaseException:
                row['result']='uncertain'; self.save(); raise
            self.save(); return {'result':'sent','message_id':str(receipt),'used_today':self.used()}

    async def image(self,identifier,caption='',*,share=False,source='',request_id=''):
        row,path=self.library.asset(identifier,share=True)
        segments=[]
        if caption: segments.append({'type':'text','data':{'text':str(caption)[:180]}})
        segments.append({'type':'image','data':{'file':'base64://'+base64.b64encode(path.read_bytes()).decode('ascii'),
                                               'sub_type':1 if row['animated'] else 0}})
        return await self.deliver(segments,share=share,source=source or identifier,request_id=request_id)

    async def face(self,number,request_id=''):
        if type(number) is not int or not 0<=number<=400: raise PhoneError('QQ表情编号应为0–400整数。')
        return await self.deliver([{'type':'face','data':{'id':str(number)}}],request_id=request_id)

    async def share(self,caption,source,identifier=''):
        if not isinstance(caption,str) or not caption.strip() or len(caption)>180: return {'result':'invalid_caption'}
        if identifier: return await self.image(identifier,caption,share=True,source=identifier)
        return await self.deliver([{'type':'text','data':{'text':caption}}],share=True,source=caption.strip())
