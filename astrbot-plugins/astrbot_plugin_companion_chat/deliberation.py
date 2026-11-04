"""Low-effort reasoning for explicitly marked private dialogue requests only.

AstrBot 4.29 ProviderRequest has no extra_body override, and OpenAI _query
overwrites per-call extras with shared config. Copy the provider for this runner
at reset; never toggle the shared provider or affect auxiliary LLM calls.
Native ThinkPart serialization owns reasoning/tool history; do not rewrite it.
"""
from copy import copy, deepcopy
from functools import wraps
from inspect import signature
import re

from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot.core.exceptions import EmptyModelOutputError
from astrbot.api.message_components import Plain

REQUEST_MARKER = '_companion_dialogue_low_reasoning'


def require_visible_output(response):
    """Reasoning is not delivery; leave tools, errors and actual media alone."""
    if response.role != 'assistant' or response.tools_call_name:
        return response
    text = (response.completion_text or '').strip()
    parts = getattr(getattr(response, 'result_chain', None), 'chain', []) or []
    media = any(not isinstance(p, Plain) for p in parts)
    if not media and (not text or re.fullmatch(r'\[\[(?:text|voice)\]\]', text, re.I)):
        # Use the framework's existing bounded empty-output retry and error path.
        # Never invent a receipt or replay a QQ send here.
        raise EmptyModelOutputError('Companion final response has no visible text, media or tool call')
    return response


def for_dialogue(provider, model=None):
    """Restrict to the exact model/endpoint verified in the deployment probe."""
    if type(provider) is not ProviderOpenAIOfficial:
        return provider
    config = provider.provider_config
    if (config.get('id') != 'deepseek/deepseek-flash'
            or (model or provider.get_model()) != 'deepseek-flash'
            or provider.client.base_url.host != 'api.deepseek.com'):
        return provider
    scoped = copy(provider)
    scoped.provider_config = deepcopy(config)
    extra = dict(scoped.provider_config.get('custom_extra_body') or {})
    extra.update(thinking={'type': 'enabled'}, reasoning_effort='low')
    # Preserve current max_tokens/context ceiling and every independent quota.
    scoped.provider_config['custom_extra_body'] = extra
    parse = scoped._parse_openai_completion

    async def parse_visible(*args, **kwargs):
        return require_visible_output(await parse(*args, **kwargs))

    scoped._parse_openai_completion = parse_visible
    return scoped


def install():
    original = ToolLoopAgentRunner.reset
    sig = signature(original)

    @wraps(original)
    async def reset(self, *args, **kwargs):
        bound = sig.bind(self, *args, **kwargs)
        request = bound.arguments['request']
        if getattr(request, REQUEST_MARKER, False) is True:
            bound.arguments['provider'] = for_dialogue(bound.arguments['provider'], request.model)
            if bound.arguments.get('fallback_providers'):
                bound.arguments['fallback_providers'] = [
                    for_dialogue(p) for p in bound.arguments['fallback_providers']]
        return await original(*bound.args, **bound.kwargs)

    ToolLoopAgentRunner.reset = reset

    def restore():
        if ToolLoopAgentRunner.reset is reset:
            ToolLoopAgentRunner.reset = original
    return restore
