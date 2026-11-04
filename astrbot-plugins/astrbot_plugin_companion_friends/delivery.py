"""Task-local evidence for a selected voice reply; never marks preparation as send."""
from contextvars import ContextVar


class VoiceDelivery:
 def __init__(self):self.current=ContextVar('companion_voice_delivery',default=None)

 def observe(self,action,params,value=None,error=None):
  state=self.current.get()
  private=action=='send_private_msg' or action=='send_msg' and params.get('message_type')=='private'
  if not state or not private or str(params.get('user_id',''))!=state['qq']:return
  parts=params.get('message',[])
  if not isinstance(parts,list):return
  voice=any(isinstance(p,dict) and p.get('type')=='record' for p in parts)
  if voice!=(state['mode']=='voice'):return
  if error is not None:
   code=getattr(error,'retcode',None)
   if type(error).__name__=='ActionFailed' and isinstance(code,int) and code>=100:state['rejected']=True
   return
  if not isinstance(value,dict):return
  if value.get('status')=='failed' and value.get('retcode'):
   state['rejected']=True;return
  if value.get('retcode',0)!=0:return
  receipt=value.get('data',value)
  if not isinstance(receipt,dict) or not receipt.get('message_id'):return
  state['confirmed']=True
  state['event'].set_extra('companion_voice_delivered_text',state['text'])
  state['event'].set_extra('companion_voice_delivery_receipt',str(receipt['message_id']))
  state['event'].set_extra('companion_voice_delivered_as',state['mode'])

 def archived(self,qq,message):
  state=self.current.get()
  if not state or not state['confirmed'] or state['qq']!=str(qq) or state['mode']!='voice' or not isinstance(message,list):return message
  if not any(isinstance(p,dict) and p.get('type')=='record' for p in message):return message
  return [*message,{'type':'voice_transcript','data':{'text':state['text']}}]
