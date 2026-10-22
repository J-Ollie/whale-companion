# 系统结构

```text
QQ 好友 / 管理员
       │
QQ 接入端（当前部署使用 SnowLuma）
       │ OneBot
AstrBot 4.29.x
       ├─ companion_batch：消息合并、回复收尾
       ├─ companion_friends：好友审批、私聊隔离、聊天归档
       ├─ companion_memory：事实、事件、印象、长期记忆与共享身份
       ├─ companion_active：主动联系管理员
       └─ companion_phone：手机工具、活动调度、作息、空间与双日记
                │                    │
             ADB / Tailscale       独立访客摘要
                │                    │
          安卓备用机           Python 管理中心
```

## 模块索引

源码位于 `plugins/`，管理中心位于 `qq-recovery/`，插件测试位于 `tests/`。

- 记忆插件的 `memory.py / cognition.py / profile.py` 管理持久化记录、检索与共享身份；`main.py` 接入 AstrBot 事件。
- 好友插件的 `policy.py` 管理待审批申请，`archive.py` 保存私聊归档，`sleep_inbox.py` 保存睡眠期间待处理消息，`social.py` 提供有限公开关系信息。
- 手机插件的 `rhythm.py` 维护作息状态，`autonomy.py` 调度自主活动，`phone.py` 封装受限 ADB 操作。
- `books.py` 管理公开/私密日记，`publisher.py` 管理发布过程，`qzone_client.py` 与 `discussions.py` 处理空间相关能力。
- `media.py / public_images.py / outbound.py` 处理图片、公开图片来源和输出；权限与配额仍须配置。
- 网站的 `panel_auth.py` 管理密码和会话，`library.py / archive.py` 查询日记和档案，`visitors.py` 保存来访与私密留言。
- `site_visitors.py` 只读取网站导出的有限摘要，不读取管理密码或完整数据库。

## 数据边界

人格与公开身份可以共享，私聊原文及私密内容按联系人隔离。昵称或留言中的自称不能授予管理员身份。网站管理员可查看归档，但访客不能读取聊天、私人日记或留言收件箱。

运行数据位于私有的 AstrBot data/plugin_data 与 recovery-data 中，未上传到本仓库。因此本仓库展示实现机制，不包含小鲸鱼的实际人生记录。

## 版本范围

本次整理来源于 2026-10-22 的开发目录，替换了实例地址和账号。源码编译检查通过；此前线上功能有各自回归验证。本次未重新搭建空白服务器验证整套冷启动，不把归档包称为一键部署包。

管理中心引用的角色插画、图片素材与运行相册未附带。首次复现时可自行提供有权使用的素材，缺失装饰图片不代表聊天或管理逻辑缺失。
