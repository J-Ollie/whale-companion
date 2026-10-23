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
            commands=('\nͬ�⣺/ͬ����� '+row['id'] if row['qq'] else
                '\n������δ��ʵ���ݲ���ͬ�⡣�ز飺/�˶Ժ��� '+row['id'])
            ok=await self.notify('�յ���������\nQQ��'+(row['qq'] or '�˺Ŵ��������밴�����ź˶ԣ�����²��˺�')+'\n��֤��Ϣ���Է���д����'+(row['comment']or'��')
                +('\n�ǳƣ�QQ���ϣ���'+row['nickname'] if row.get('nickname') and row['qq'] else '')
                +commands+'\n�ܾ���/�ܾ����� '+row['id']
                +'\nδ�õ������ȷͬ�⣬�������ӶԷ�')
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
        if text in ('/��������','��������'):
            rows=[r for r in self.rows if r['status']in('pending','uncertain')]
            return '\n'.join(r['id']+' �� '+('QQ '+r['qq'] if r['qq'] else '�˺Ŵ�����')+' �� '+('������' if r['status']=='pending' else '������˶�') for r in rows) or '��ǰû�д������ĺ�������'
        lookup=re.fullmatch(r'/?�˶Ժ���\s+([0-9A-F]{6})',text,re.I)
        if lookup:
            async with self.lock:
                row=next((r for r in self.rows if r['id']==lookup[1].upper()),None)
                if not row or row['status']!='pending':return 'û����������������'
                if time.time()-row['at']>86400:return '�����ѳ���24Сʱ�����öԷ���������'
                if await self.resolve_identity(row,force=True):
                    return '�Ѻ�ʵ���� '+row['id']+' ��QQ��'+row['qq']+'\n�˶Ժ�ɷ��� /ͬ����� '+row['id']
                return '������δ��ʵ���ݲ���ͬ�⣻��ѯ�������60�롣���ڹٷ�QQ�˶ԣ����Ժ��ٷ��� /�˶Ժ��� '+row['id']
        match=re.fullmatch(r'/?(ͬ�����|�ܾ�����)\s+([0-9A-F]{6})',text,re.I)
        if not match:return None
        async with self.lock:
            row=next((r for r in self.rows if r['id']==match[2].upper()),None)
            if not row or row['status']!='pending':return 'û���������������룬�뷢�� /�������� �鿴'
            if time.time()-row['at']>86400:return '�����ѳ���24Сʱ�����öԷ��������룬������������'
            approve=match[1]=='ͬ�����'
            if approve and not row.get('qq'):
                return '������QQ��δ��ʵ���ݲ���ͬ�⡣���ȷ��� /�˶Ժ��� '+row['id']+'��������ʵQQ���˶Ժ��پ���'
            row['status']='processing';self.save()
            try:
                await self.call('set_friend_add_request',flag=row['flag'],approve=approve)
            except Exception:
                row['status']='uncertain';self.save()
                return '���������δȷ�ϣ������Զ����ԣ�������QQ�˶Ժ����б�'
            row['status']='approved' if approve else 'rejected';row['decided_at']=time.time();self.save()
            if not approve:return '�Ѿܾ�������������'
            if await self.is_friend(row['qq']):return '��ͬ�� QQ '+row['qq']+'�����ڿ���˽��'
            return '���ύͬ�����룻�ȴ�QQ�����б�ͬ����ȷ���Ǻ��Ѻ�ŻῪ��˽��'
