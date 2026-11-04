"""Final display formatting; preserve code, quotations and meaningful punctuation."""
import re


def clean_chat_text(text):
    lines=[]
    fence=None
    for line in text.splitlines(keepends=True):
        stripped=line.lstrip()
        marker=re.match(r'(`{3,}|~{3,})',stripped)
        if marker:
            token=marker.group(1)
            if fence is None:
                fence=token[0]
            elif token[0]==fence:
                fence=None
            lines.append(line)
            continue
        if fence or stripped.startswith('>') or line.startswith(('    ','\t')):
            lines.append(line)
        else:
            lines.append(re.sub(r'。+(?=[ \t]*(?:\r?\n)?$)','',line))
    cleaned=''.join(lines)
    return cleaned if cleaned.strip() else text


def clean_event_reply(event, owner_umo):
    from astrbot.api.message_components import Plain
    if getattr(event,'unified_msg_origin',None)!=owner_umo:
        return
    result=event.get_result()
    if not result or not result.is_llm_result():
        return
    for component in result.chain:
        if isinstance(component,Plain):
            component.text=clean_chat_text(component.text)
