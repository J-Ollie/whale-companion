"""Persistent owner approval, real friendship checks and no auto-accept."""
import asyncio
import hashlib
import json
import re
import secrets
import time
from .identity import resolve as resolve_uid


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
        if not isinstance(flag,str) or not 1<=len(flag)<=1024 or any(ord(c)<32 for c in flag):return
        # SnowLuma can provide an opaque request handle before resolving a
        # stranger's numeric account. Retain that request for explicit approval;
        # never substitute a guessed account or grant friendship from the handle.
        unresolved=qq in ('','0')
        if not unresolved and not re.fullmatch(r'[1-9]\d{4,10}',qq):return
        if unresolved:qq=''
        digest=hashlib.sha256(flag.encode()).hexdigest()
        async with self.lock:
            if any(row['digest']==digest for row in self.rows):return
            # A renewed request by the same person updates the pending handle;
            # it never approves and does not create repeated notifications.
            pending=next((r for r in self.rows if qq and r['qq']==qq and r['status']=='pending'),None)
            comment=re.sub(r'[\x00-\x1f\x7f]',' ',str(raw.get('comment','')))[:160]
            if pending:
                pending.update(flag=flag,digest=digest,comment=comment,at=time.time())
                self.save();return
            identifier=secrets.token_hex(3).upper()
            while any(r['id']==identifier for r in self.rows):identifier=secrets.token_hex(3).upper()
            row={'id':identifier,'qq':qq,'identity_unresolved':unresolved,'flag':flag,'digest':digest,
                 'comment':comment,'at':time.time(),'status':'pending','notified':False}
            self.rows.append(row);self.save()
            await self.resolve_identity(row)
            await self.notify_pending(row)

    async def resolve_identity(self,row,force=False):
        if row.get('qq'):return True
        now=time.time()
        if now-row.get('identity_last_attempt',0)<60:return False
        if not force and row.get('identity_attempts',0)>=3:return False
        row.update(identity_last_attempt=now,identity_attempts=row.get('identity_attempts',0)+1)
        self.save()
        try:
            result=await resolve_uid(self.call,row['flag'])
            row.update(qq=result['qq'],identity_unresolved=False,identity_source=result['source'],
                       nickname=result['nickname'],identity_resolved_at=time.time(),identity_last_error='')
            self.save();return True
        except Exception as exc:
            # Preserve the request and a bounded diagnostic, never infer a QQ.
            error=str(exc) if type(exc) is ValueError else type(exc).__name__
            row.update(identity_unresolved=True,identity_last_error=error[:80])
            self.save();return False

    async def notify_pending(self,row):
        try:
            commands=('\n同意：/同意好友 '+row['id'] if row['qq'] else
                '\n身份尚未核实，暂不能同意。重查：/核对好友 '+row['id'])
            ok=await self.notify('收到好友申请\nQQ：'+(row['qq'] or '账号待解析，请按申请编号核对；不会猜测账号')+'\n验证信息（对方填写）：'+(row['comment']or'无')
                +('\n昵称（QQ资料）：'+row['nickname'] if row.get('nickname') and row['qq'] else '')
                +commands+'\n拒绝：/拒绝好友 '+row['id']
                +'\n未得到你的明确同意，不会添加对方')
            if ok is not False:
                row['notified']=True;self.save()
        except Exception:
            pass  # Pending approval remains persisted and fail-closed.

    async def retry_notifications(self):
        async with self.lock:
            for row in self.rows:
                if row['status']=='pending' and not row.get('qq') and time.time()-row['at']<=86400:
                    if await self.resolve_identity(row):
                        row['notified']=False;self.save()
                if row['status']=='pending' and not row['notified']:
                    await self.notify_pending(row)

    async def command(self,sender,text):
        if str(sender)!=self.owner:return None
        text=text.strip()
        if text in ('/好友申请','好友申请'):
            rows=[r for r in self.rows if r['status']in('pending','uncertain')]
            return '\n'.join(r['id']+' · '+('QQ '+r['qq'] if r['qq'] else '账号待解析')+' · '+('待审批' if r['status']=='pending' else '结果待核对') for r in rows) or '当前没有待处理的好友申请'
        lookup=re.fullmatch(r'/?核对好友\s+([0-9A-F]{6})',text,re.I)
        if lookup:
            async with self.lock:
                row=next((r for r in self.rows if r['id']==lookup[1].upper()),None)
                if not row or row['status']!='pending':return '没有这条待处理申请'
                if time.time()-row['at']>86400:return '申请已超过24小时，请让对方重新申请'
                if await self.resolve_identity(row,force=True):
                    return '已核实申请 '+row['id']+' 的QQ：'+row['qq']+'\n核对后可发送 /同意好友 '+row['id']
                return '身份仍未核实，暂不能同意；查询间隔至少60秒。请在官方QQ核对，或稍后再发送 /核对好友 '+row['id']
        match=re.fullmatch(r'/?(同意好友|拒绝好友)\s+([0-9A-F]{6})',text,re.I)
        if not match:return None
        async with self.lock:
            row=next((r for r in self.rows if r['id']==match[2].upper()),None)
            if not row or row['status']!='pending':return '没有这条待处理申请，请发送 /好友申请 查看'
            if time.time()-row['at']>86400:return '申请已超过24小时，请让对方重新申请，不处理旧请求'
            approve=match[1]=='同意好友'
            if approve and not row.get('qq'):
                return '申请人QQ尚未核实，暂不能同意。请先发送 /核对好友 '+row['id']+'，看到真实QQ并核对后再决定'
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
