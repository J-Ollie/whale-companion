"""Route current owner requests; phone UI tasks and space publishing are distinct."""
import re


def publishing_declined(text):
    # Reject stopping instructions, not "don't ask me what to post" or "要不要".
    text = (text or '').replace('要不要', '是否要')
    return bool(re.search(r'不要发|不用发|别发|先不发|禁止(?:发布|发)|不许发|暂停空间|停止(?:空间|发布)', text))


def qzone_request(text):
    text = (text or '').strip()
    negation_text = text.replace('要不要', '是否要')
    if re.search(r'别|不要|不用|不许|禁止|暂停|先不发|不想发|不是让你|不要再', negation_text):
        return False
    if re.search(r'发过|发了|发完|发出去了吗|发动态了吗|发说说了吗|为什么|怎么发|如何发|什么意思|截图里|引用|他说', text):
        return False
    if re.search(r'能.{0,4}发.*吗[？?]?$|会.{0,4}发.*吗[？?]?$', text) and not re.search(r'试|帮我|要不要|去|发一', text):
        return False
    return bool(re.search(r'(?:发|发布|发表|尝试发|试着发).{0,8}(?:动态|说说|空间)', text)
                or re.search(r'(?:QQ空间|空间|说说|动态).{0,12}(?:发一|发个|发布|发吧)', text))


def qzone_followup(text):
    return bool(re.fullmatch(r'(?:好了|好|行|可以了|现在可以了|继续|再试(?:一下|试)?|试试|发吧)[！!。\s]*', (text or '').strip()))


def browse_request(text):
    text = (text or '').strip()
    negation_text = text.replace('要不要', '是否要')
    if re.search(r'别|不要|不许|禁止|暂停|不用|不是让你|玩过|玩了吗|浏览了吗|操作手机了吗|打开了吗|访问了吗|为什么|如何|他说|她说|引用|截图里', negation_text):
        return False
    apps = r'手机|备用机|QQ|酷安|天气|相册|日历|哔哩哔哩|B站|贴吧|Neat Reader|Via|浏览器|网页|网站|https?://'
    explicit=bool(re.search(apps,text,re.I) and re.search(r'打开|访问|进入|刷新|搜索|搜一下|看看|看一下',text))
    return explicit or bool(re.search(r'玩|逛|浏览', text) and re.search(apps, text, re.I)
                and re.search(r'现在|试试|去|玩吧|逛吧|浏览一下', text))
