"""Persistent owner approval, real friendship checks and no auto-accept."""
import asyncio
import hashlib
import json
import re
import secrets
import time


class FriendPolicy:
    def __init__(self,root,owner,call,notify):
        self.root,self.owner,self.call,self.notify=root,str(owner),call,notify
        root.mkdir(parents=True,exist_ok=True)
        self.path=root/'requests.json'
        self.rows=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else []
        if any(r['status']=='processing' for r in self.rows):
            for row in self.rows:
                if row['status']=='processing':row['status']='uncertain'
            self.save()
        self.lock=asyncio.Lock()

    def save(self):
        p=self.path.with_suffix('.tmp')
        p.write_text(json.dumps(self.rows,ensure_ascii=False,indent=2),encoding='utf-8')
        p.chmod(0o600);p.replace(self.path)

    async def is_friend(self,qq):
        try:
            rows=await self.call('get_friend_list')
            if not isinstance(rows,list):return False
            return any(str(row.get('user_id'))==str(qq) for row in rows if isinstance(row,dict))
        except Exception:
            return False

    async def receive(self,raw):
        qq=str(raw.get('user_id',''));flag=raw.get('flag')
        if not re.fullmatch(r'[1-9]\d{4,10}',qq) or not isinstance(flag,str) or not flag:return
        digest=hashlib.sha256(flag.encode()).hexdigest()
        async with self.lock:
            if any(row['digest']==digest for row in self.rows):return
            # A renewed request by the same person updates the pending handle;
            # it never approves and does not create repeated notifications.
            pending=next((r for r in self.rows if r['qq']==qq and r['status']=='pending'),None)
            comment=re.sub(r'[\x00-\x1f\x7f]',' ',str(raw.get('comment','')))[:160]
            if pending:
                pending.update(flag=flag,digest=digest,comment=comment,at=time.time())
                self.save();return
            identifier=secrets.token_hex(3).upper()
            while any(r['id']==identifier for r in self.rows):identifier=secrets.token_hex(3).upper()
            row={'id':identifier,'qq':qq,'flag':flag,'digest':digest,
                 'comment':comment,'at':time.time(),'status':'pending','notified':False}
            self.rows.append(row);self.save()
            await self.notify_pending(row)

    async def notify_pending(self,row):
        try:
            ok=await self.notify('收到好友申请\nQQ：'+row['qq']+'\n验证信息（对方填写）：'+(row['comment']or'无')
                +'\n同意：/同意好友 '+row['id']+'\n拒绝：/拒绝好友 '+row['id']
                +'\n未得到你的明确同意，不会添加对方')
            if ok is not False:
                row['notified']=True;self.save()
        except Exception:
            pass  # Pending approval remains persisted and fail-closed.

    async def retry_notifications(self):
        async with self.lock:
            for row in self.rows:
                if row['status']=='pending' and not row['notified']:
                    await self.notify_pending(row)

    async def command(self,sender,text):
        if str(sender)!=self.owner:return None
        text=text.strip()
        if text in ('/好友申请','好友申请'):
            rows=[r for r in self.rows if r['status']in('pending','uncertain')]
            return '\n'.join(r['id']+' · QQ '+r['qq']+' · '+('待审批' if r['status']=='pending' else '结果待核对') for r in rows) or '当前没有待处理的好友申请'
        match=re.fullmatch(r'/?(同意好友|拒绝好友)\s+([0-9A-F]{6})',text,re.I)
        if not match:return None
        async with self.lock:
            row=next((r for r in self.rows if r['id']==match[2].upper()),None)
            if not row or row['status']!='pending':return '没有这条待处理申请，请发送 /好友申请 查看'
            if time.time()-row['at']>86400:return '申请已超过24小时，请让对方重新申请，不处理旧请求'
            approve=match[1]=='同意好友'
            row['status']='processing';self.save()
            try:
                await self.call('set_friend_add_request',flag=row['flag'],approve=approve)
            except Exception:
                row['status']='uncertain';self.save()
                return '处理结果暂未确认，不会自动重试；请先在QQ核对好友列表'
            row['status']='approved' if approve else 'rejected';row['decided_at']=time.time();self.save()
            if not approve:return '已拒绝这条好友申请'
            if await self.is_friend(row['qq']):return '已同意 QQ '+row['qq']+'，现在可以私聊'
            return '已提交同意申请；等待QQ好友列表同步，确认是好友后才会开放私聊'
