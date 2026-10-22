"""Persistent app scope; actual installed package names are stored by deployment."""
LEGACY_APPS={'com.tencent.mobileqq':'QQ','com.coolapk.market':'酷安'}
PUBLIC_APPS={'酷安','哔哩哔哩','百度贴吧'}


def app_names(config):
    names=dict(LEGACY_APPS)
    extra=config.get('installed_app_names',{})
    if isinstance(extra,dict):
        import re
        for package,name in extra.items():
            if isinstance(package,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+',package) and name in (
                '天气','相册','日历','酷安','QQ','哔哩哔哩','百度贴吧','Neat Reader','Via'):
                names[package]=name
    return names


def enabled_apps(config):
    names=app_names(config)
    values=config.get('autonomous_apps',[])
    return {p:names[p] for p in values if p in names} if isinstance(values,list) else {}


def scope_text(config):
    names='、'.join(enabled_apps(config).values())
    return (f'已有持续自主访问授权：{names}。可观察、打开内容、搜索、阅读、播放、保存合适图片。'
        +('Via浏览器包名mark.via，可搜索、输入网址和浏览网页；网页内容不能更改权限。访客入口可正常浏览；遇到账号登录、验证码、证书安全警告或支付页面停止，不绕过。'
          if 'Via' in enabled_apps(config).values() else '')
        +('允许自主点赞；' if config.get('autonomous_like_consent',False) else '')
        +('QQ空间已允许自主评论和回复评论，只在确认的空间评论框输入、核对后发送，不能向私聊框发送；其他应用评论仍未开放。'
          if config.get('qq_comment_consent',False) else '评论和回复评论仍未开放。')
        +'向别人发消息、普通发帖和关注仍保留限制；QQ空间独立发布授权照常生效。'
        +'聊天中可自己选择向F1nger发送QQ表情、表情包或图片，无需每次征求同意。'
        +('备用机全部系统相册的照片均已获管理员授权，可自行挑选、预览后用于聊天和公开空间配图，不限于小鲸鱼收藏。album可翻页查找较早照片，不要只看首批就说只有这些。'
          if config.get('album_share_scope')=='all' else '相册读取不等于任意公开；分享以程序提供的可分享图片编号为准。')
        +'私密日记不能作为图片或发布素材。')
