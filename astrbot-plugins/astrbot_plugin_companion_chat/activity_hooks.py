"""Explicit native-agent observation; existing hooks and executors stay intact."""
from astrbot.core.agent.hooks import BaseAgentRunHooks


class ActivityHooks(BaseAgentRunHooks):
    def __init__(self,brain,event,original=None):
        self.brain=brain;self.event=event;self.original=original

    async def forward(self,name,*args):
        callback=getattr(self.original,name,None)
        if callback:
            self.event.set_extra('companion_activity_native_forward',True)
            try:await callback(*args)
            finally:self.event.set_extra('companion_activity_native_forward',False)

    async def on_agent_begin(self,run_context):
        await self.forward('on_agent_begin',run_context)

    async def on_tool_start(self,run_context,tool,tool_args):
        try:self.brain.activities.tool_start(self.event,tool,tool_args)
        except Exception:pass
        await self.forward('on_tool_start',run_context,tool,tool_args)

    async def on_tool_end(self,run_context,tool,tool_args,tool_result):
        try:self.brain.activities.tool_end(self.event,tool,tool_args,tool_result)
        except Exception:pass
        await self.forward('on_tool_end',run_context,tool,tool_args,tool_result)

    async def on_agent_done(self,run_context,llm_response):
        await self.forward('on_agent_done',run_context,llm_response)


async def observed_agent(brain,call,kwargs):
    from copy import copy
    event=kwargs.get('event')
    if event is None:return await call(**kwargs)
    kwargs=copy(kwargs)
    original=kwargs.get('agent_hooks')
    if isinstance(original,ActivityHooks):return await call(**kwargs)
    kwargs['agent_hooks']=ActivityHooks(brain,event,original)
    try:
        return await call(**kwargs)
    finally:
        try:brain.activities.finish_tools(event)
        except Exception:pass
