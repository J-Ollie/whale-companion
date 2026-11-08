"""Observed model-call metadata; missing provider tokens stay unknown."""
from contextvars import ContextVar
import time

ACTIVITY=ContextVar('companion_brain_activity',default='foreground_or_maintenance')


def tokens(response):
    usage=getattr(response,'usage',None)
    raw=getattr(response,'raw_completion',None)
    if usage is None:usage=raw.get('usage') if isinstance(raw,dict) else getattr(raw,'usage',None)
    if usage is None:return None,None,None
    def number(*names):
        for name in names:
            value=usage.get(name) if isinstance(usage,dict) else getattr(usage,name,None)
            if type(value) is int and value>=0:return value
        return None
    return number('prompt_tokens','input_tokens'),number('completion_tokens','output_tokens'),number('total_tokens')


async def observe(brain,source,call,reserved=False):
    started=time.time();response=None;status='interrupted'
    try:
        response=await call();status='returned';return response
    except Exception:
        status='error';raise
    finally:
        record(brain,source,response,status=status,started=started,reserved=reserved)


def record(brain,source,response,*,status='returned',started=None,reserved=False):
    # Auditing cannot turn a returned model call into a retry. An observed
    # response without its request timing does not invent latency or a request.
    now=time.time()
    try:
        with brain.connect() as db:
            db.execute('INSERT INTO model_calls(at,source,activity,status,elapsed_ms,input_tokens,output_tokens,total_tokens,reserved) VALUES(?,?,?,?,?,?,?,?,?)',
                (now if started is None else started,source,ACTIVITY.get(),status,
                 None if started is None else int((now-started)*1000),*tokens(response),int(reserved)))
    except Exception:pass
