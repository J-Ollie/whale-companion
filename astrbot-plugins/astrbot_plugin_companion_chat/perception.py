"""Passive, ownership-aware observers of actual caption/STT/spectrum returns.

No new media requests, imports of ASR adapters, text prompts or transport calls.
"""
import asyncio
from contextvars import ContextVar
from functools import wraps
import inspect
import sys
import time
from astrbot.api import logger
from astrbot.core.platform.astr_message_event import current_message_event
from .perception_store import PerceptionStore

FRAME=ContextVar('companion_perception_frame',default=None)
BUSY=ContextVar('companion_perception_busy',default=frozenset())


class PerceptionObserver:
    def __init__(self,chat):
        self.chat=chat;self.active=True;self.bindings=[]
        self.store=PerceptionStore(chat.friends.policy.root)

    def frame(self,event):
        try:
            if not self.active or not self.chat.ready or self.chat.friends.sleeping() or not self.chat.batch.allowed(event):return None
            if event.get_group_id():return None
            scope=str(event.unified_msg_origin);mid=str(event.message_obj.message_id or '')
            if scope!=str(event.get_platform_id())+':FriendMessage:'+str(event.get_sender_id()):return None
            if not scope or not mid:return None
            started=event.get_extra('companion_perception_started_at',None)
            if type(started) not in (int,float):
                started=time.time();event.set_extra('companion_perception_started_at',started)
            return {'owner':self,'scope':scope,'message_id':mid,'count':0}
        except Exception:return None

    def begin(self,frame,refs,kind):
        if not self.active or not frame:return []
        identifiers=[]
        for ref in refs[:8]:
            if frame['count']>=32:break
            frame['count']+=1
            try:
                identifier=self.store.begin(frame['scope'],frame['message_id'],ref,kind)
                if identifier:identifiers.append(identifier)
            except Exception as exc:
                logger.warning('Perception metadata deferred after %s',type(exc).__name__)
        return identifiers

    def finish(self,frame,identifiers,status,result='',error_type=''):
        if not self.active or not frame:return
        for identifier in identifiers:
            try:self.store.finish(identifier,frame['scope'],status,result,error_type)
            except Exception as exc:logger.warning('Perception metadata deferred after %s',type(exc).__name__)

    def _replace(self,target,name,factory,required):
        current=getattr(target,name,None)
        if not callable(current):return
        check=current
        for _ in range(12):
            if getattr(check,'_companion_perception_owner',None) is self:return
            check=getattr(check,'__wrapped__',None)
            if check is None:break
        try:
            signature=inspect.signature(current)
            if not set(required)<=set(signature.parameters):return
        except (ValueError,TypeError):return
        wrapper=factory(current,signature)
        wrapper._companion_perception_owner=self
        setattr(target,name,wrapper);self.bindings.append((target,name,current,wrapper))

    def _scope_factory(self,original,signature):
        @wraps(original)
        async def wrapped(*args,**kwargs):
            if not self.active:return await original(*args,**kwargs)
            values=signature.bind_partial(*args,**kwargs).arguments
            event=values.get('event');frame=self.frame(event)
            # An old outer observer may survive another plugin's wrapper; do not
            # turn one processing task into multiple evidence operations.
            previous=FRAME.get()
            if previous and previous.get('owner') is self and frame and previous['scope']==frame['scope'] and previous['message_id']==frame['message_id']:
                return await original(*args,**kwargs)
            token=FRAME.set(frame)
            try:
                try:self._refresh_asr()
                except Exception as exc:logger.warning('Perception STT observer deferred after %s',type(exc).__name__)
                return await original(*args,**kwargs)
            finally:FRAME.reset(token)
        return wrapped

    def _result_factory(self,kind,parameter,event_scope=False):
        def factory(original,signature):
            @wraps(original)
            async def wrapped(*args,**kwargs):
                if not self.active:return await original(*args,**kwargs)
                values=signature.bind_partial(*args,**kwargs).arguments
                frame=self.frame(current_message_event.get(None)) if event_scope else FRAME.get()
                if not frame or frame.get('owner') is not self:return await original(*args,**kwargs)
                raw=values.get(parameter)
                refs=list(raw[:8]) if isinstance(raw,(list,tuple)) else [raw]
                refs=[r for r in refs if isinstance(r,str) and r]
                if not refs:return await original(*args,**kwargs)
                key=(id(self),kind,frame['scope'],frame['message_id'],tuple(refs))
                if key in BUSY.get():return await original(*args,**kwargs)
                token=BUSY.set(BUSY.get()|{key});ids=self.begin(frame,refs,kind)
                try:
                    result=await original(*args,**kwargs)
                except BaseException as exc:
                    self.finish(frame,ids,'cancelled' if isinstance(exc,asyncio.CancelledError) else 'failed',error_type=type(exc).__name__)
                    raise
                else:
                    text=result if isinstance(result,str) else ''
                    self.finish(frame,ids,'returned' if text.strip() else 'empty',text)
                    return result
                finally:BUSY.reset(token)
            return wrapped
        return factory

    def _refresh_asr(self):
        # Read the already registered module. Importing it here could repeat the
        # provider registration and reintroduce the repaired reload conflict.
        meta=self.chat.context.get_registered_star('astrbot_plugin_companion_asr')
        star=getattr(meta,'star_cls',None) if meta and getattr(meta,'activated',False) else None
        module=sys.modules.get(type(star).__module__) if star is not None else None
        adapter=getattr(module,'CompanionDashscopeSTT',None)
        if isinstance(adapter,type):self._replace(adapter,'get_text',self._result_factory('audio_transcript','audio_url'),('audio_url',))

    def refresh(self):
        if not self.active:return
        try:
            module=sys.modules.get('astrbot.core.astr_main_agent')
            if module:self._replace(module,'_request_img_caption',self._result_factory('image_caption','image_urls',True),('image_urls',))
            meta=self.chat.context.get_registered_star('astrbot_plugin_multimodal_enhance')
            star=getattr(meta,'star_cls',None) if meta and getattr(meta,'activated',False) else None
            pipeline=getattr(star,'pipeline',None)
            if pipeline is not None:
                cls=type(pipeline)
                self._replace(cls,'_run',self._scope_factory,('event',))
                self._replace(cls,'_spectrum_for',self._result_factory('audio_spectrum','path'),('path',))
            self._refresh_asr()
        except Exception as exc:logger.warning('Perception observer refresh deferred after %s',type(exc).__name__)

    def close(self):
        self.active=False
        for target,name,original,wrapper in reversed(self.bindings):
            if getattr(target,name,None) is wrapper:setattr(target,name,original)
        self.bindings.clear()

    def visible(self,scope,message_id):
        try:
            meta=self.chat.context.get_registered_star('astrbot_plugin_companion_memory')
            memory=getattr(meta,'star_cls',None) if meta and getattr(meta,'activated',False) else None
            if memory is None:return False
            qq=scope.rsplit(':',1)[-1]
            row=memory.store.db.execute('SELECT 1 FROM observations o JOIN suppressed_sources s ON s.qq=o.qq AND s.observation_id=o.id WHERE o.qq=? AND o.dedupe=? LIMIT 1',(qq,'qq:'+qq+':'+message_id)).fetchone()
            return row is None
        except Exception:return False

    def recent(self,scope):return self.store.recent(scope,visible=lambda mid:self.visible(scope,mid))

    def evidence(self,event):
        try:
            started=event.get_extra('companion_perception_started_at',None)
            if started is None:return None
            frame=self.frame(event)
            if not frame or not self.visible(frame['scope'],frame['message_id']):return None
            return self.store.for_request(frame['scope'],frame['message_id'],started)
        except Exception:return None
