"""Suppress phone tool preambles without modifying AstrBot installed sources."""
from .intents import browse_request


def install(plugin):
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    original=ToolLoopAgentRunner.step
    # Reload-safe; a previous plugin wrapper is removed by terminate.
    if getattr(original,'_companion_quiet',False): return lambda:None

    async def step(runner,*args,**kwargs):
        event=getattr(getattr(getattr(runner,'run_context',None),'context',None),'event',None)
        quiet=plugin.owner_event(event) and (plugin.task_authorized(event)
            or getattr(plugin,'qzone_authorized',lambda e:False)(event)
            or browse_request(getattr(event,'message_str','')))
        buffered=[]
        async for response in original(runner,*args,**kwargs):
            if quiet and response.type in ('llm_result','streaming_delta'):
                buffered.append(response)
            else:
                yield response
        # Intermediate responses have pending tools; only the actual final answer is sent.
        if not quiet or runner.done():
            for response in buffered: yield response

    step._companion_quiet=True
    ToolLoopAgentRunner.step=step
    def restore():
        if ToolLoopAgentRunner.step is step: ToolLoopAgentRunner.step=original
    return restore
