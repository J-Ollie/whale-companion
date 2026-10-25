# 模块与实现索引

更新：2026-10-25。功能概览见仓库首页；接手顺序见[项目交接](HANDOFF.md)。下面的运行数据都属于私有部署，不在仓库中。

## 消息从接收到回复

1. QQ 接入端把消息或好友请求交给 AstrBot。
2. `companion_friends` 接收申请、核实好友，睡眠时保存未读；管理命令仅允许真实管理员。
3. `companion_batch` 按联系人合并消息，必要时判断是否自然收尾。
4. `companion_memory` 注入当前联系人范围内的记忆、时间与共享身份。普通好友只保留限定的个人回忆工具。
5. AstrBot 调用模型；手机和空间工具由相应授权检查决定是否执行。
6. 回复前再次检查必要的好友范围；发送后归档和整理记忆。没有真正执行的工具动作不能写成已完成经历。

各插件通过 AstrBot 钩子和少量内部适配器补充行为，不是相互独立的通用安装包。升级 AstrBot 时要验证事件顺序、工具隔离和消息发送检查。

## 记忆插件

路径：`astrbot-plugins/astrbot_plugin_companion_memory/`。

| 文件 | 责任 |
| --- | --- |
| `main.py` | 对接事件、后台整理、好友归档分批导入、个人回忆工具 |
| `memory.py` | SQLite 原始观察、事实、印象、版本、更正与忘记来源记录 |
| `cognition.py` | 常驻核心、近期话题、引用式共同经历、日期检索与依据读取 |
| `lifecycle.py` | 类型、有效期、来源、冲突与版本旁表 |
| `recall_index.py` | 全库FTS5索引，检索时按联系人隔离 |
| `reflection.py` | 有界增量材料、长消息分段和持久化处理游标 |
| `budget.py` | UTF-8预算和完整历史轮／工具链裁剪 |
| `profile.py` | 共享名字、别名、表达习惯与整体成长投影 |

记忆分层不是简单“超过几天就转长期”：稳定知识、计划、状态、话题、经历、印象和自我档案分开，另管时间、来源、版本与可见范围。过期计划不推断完成，冲突先核对。共同经历保留原文，机器人说过不自动证明现实事件。

自动联系人记忆上限2600字节，公开简述900字节，近期请求历史7000字节／12条消息。通过FTS5和有界同义改写按需检索，整理有每日次数、单次输入输出和持久化游标；详见[记忆与预算](MEMORY.md)。新对话不清空独立记忆，好友不再重复注入整份近期归档。

`companion_memory_recall(query, start_day, end_day, limit)` 和 `companion_memory_read(identifier)` 不接受 QQ、SQL 或路径参数；身份来自真实事件。检索按人隔离，忘记来源不会因为后续原文检索又被自动捞回，但不等于删除管理员保存的原始归档。

数据：`plugin_data/companion_memory/memory.sqlite3`，包含 `observations`、`entries`、`entry_revisions`、`suppressed_sources` 等表。整体成长以已有管理员互动支持为基础，普通好友提取不直接改写整体人格。

## 好友插件

路径：`astrbot-plugins/astrbot_plugin_companion_friends/`。

| 文件 | 责任 |
| --- | --- |
| `main.py` | 入站拦截、好友列表检查、白名单例外、工具隔离、睡眠通知与醒后投递 |
| `policy.py` | 申请落盘、去重、身份查询调度、管理员审批与结果状态 |
| `identity.py` | 固定只读的 UID → QQ 资料查询和响应校验 |
| `archive.py` | 按联系人保存消息及尽力缓存图片 |
| `sleep_inbox.py` | 持久化未读消息与投递状态，防止重启重复处理 |
| `social.py` | 有限公共关系与同一联系人上下文，不投影其他人的私聊正文 |

申请状态为待审批、处理中、已同意、已拒绝或结果待核对。启动时遇到上次未完成的处理中申请，会转为待核对，不擅自重试。超过 24 小时不处理旧申请。

查询资料不是审批：查不到 QQ 就禁止同意；查询成功后仍须管理员另发同意命令。数字 QQ 和昵称的区别、重查节奏见 [身份核验](FRIEND_APPROVAL.md)。

数据：`plugin_data/companion_friends/requests.json`、`chats.sqlite3`、`sleep-inbox.sqlite3` 及图片缓存。申请 flag、UID 和聊天均不得公开上传。

## 消息合并插件

路径：`astrbot-plugins/astrbot_plugin_companion_batch/`。

- `main.py` 维护按联系人隔离的消息批次、计时和合并投递。
- `turn_end.py` 先筛选低信息候选，再结合上下文判定是否静默；首次离开告知与已接住的结束确认分开处理。
- `quiet_seconds=15`、`max_seconds=60`；命令、睡眠收件和模型生成有各自处理过程，不能把等待上限理解成总回复延迟承诺。

## 主动联系插件

路径：`astrbot-plugins/astrbot_plugin_companion_active/`。

- `main.py` 判断每日额度、时间窗、间隔、未回应与暂停，使用会话锁协调普通聊天；主动对象是管理员。
- `chat_style.py` 清理普通回复中不自然的句末句号。
- 默认每日主动聊天上限为 4 次；内容分享、好友审批和空间发布使用各自通道与计数，不是一套混用的额度。
- 数据位于 `plugin_data/companion_active/`，保留计数和控制状态，不因重启清空。

表情包概率属于外部表情包插件的运行配置，本插件不包含该第三方管理器。

## 手机插件

路径：`astrbot-plugins/astrbot_plugin_companion_phone/`。

| 文件 | 责任 |
| --- | --- |
| `main.py` | 注册工具、执行路由、睡眠/授权检查、模型截图识别 |
| `connection_control.py` | 供管理员网站使用的受限本地连接socket，验证失败保留旧端点 |
| `phone.py` | ADB 连接、前台应用、观察、UI 树、点按、输入与连续滑动 |
| `autonomy.py` | 自主访问流程、模型选择、会话时长、访问额度与动作回执 |
| `rhythm.py` | 精力、好奇、社交需求、作息状态、习惯及调度时间 |
| `access.py` / `intents.py` | 应用交互范围与管理员委托/自主请求识别 |
| `media.py` / `public_images.py` | 相册分页、预览、图片候选与公开图片来源 |
| `outbound.py` / `quiet.py` | 发送内容边界、互动检查和减少无意义进度话术 |
| `publisher.py` | 空间稿件、配图、提交、幂等回执和回读 |
| `qzone_client.py` | 有界的账号自身空间接口读取与发布支持 |
| `discussions.py` | 动态/评论分页、屏幕补全、导航、回复和核验 |
| `books.py` | 独立的公开与私密日记 |
| `activity.py` | 已确认动作元数据回执；不保存页面、输入或网址正文 |
| `site_visitors.py` | 只读网站导出的有限来访与留言摘要 |

连续滑动可以略过中间页面，点按、输入和发送需要当前依据。图片发送先预览，空间与评论先核对目标和可公开内容。提交不确定时保留回执，不盲目重试。

作息包含确定性状态推进与模型选择，使用真实已清醒时长避免时钟相位绕回导致异常入睡。服务停机后只推进状态，不补执行错过的手机动作。睡眠/休息延后管理员手机任务和后台整理，早期强制执行覆盖规则已撤回。

数据位于 `plugin_data/companion_phone/`：`rhythm.json`、`autonomy.json`、讨论/发布回执、日记和媒体状态等，具体文件由相应模块维护。不要用旧快照覆盖当前配额或回执。

## 管理中心

路径：`management-center/`。Python 标准库服务，静态网页由同一服务提供，不要以 `file://` 直接打开 HTML 操作后台。

| 文件 | 责任 |
| --- | --- |
| `server.py` | 本地监听、网关诊断、认证路由、只读查看及明确触发的恢复 |
| `panel_auth.py` | 初始化密码、scrypt 哈希、登录会话和限制 |
| `library.py` | 好友聊天、公开/私密日记及记忆依据的受限查询 |
| `archive.py` | 档案、公开身份与记忆摘要 |
| `presence.py` | 清醒时缓存公开状态话语与时间倾向，不调用私人上下文 |
| `visitors.py` | 来访合并、自愿昵称、私密留言及有限摘要导出 |
| `balance.py` | 官方 DeepSeek 余额查询、固定目标与缓存，不返回密钥 |
| `billing.py` / `platform_billing.py` | 官方费用导入／显式连接、Key选择确认、金额聚合与有限摘要导出 |
| `phone_health.py` | ADB连通检查、作息分离显示和管理员指定连接端口 |
| `web/` | 管理员/访客页面、交互与样式 |

业务数据位于私有 `recovery-data/` 和 AstrBot `plugin_data/`。网站管理员查看归档的权限与机器人向其他人说话的权限不同：管理员可查看不意味着机器人可以对其他联系人复述。

公开代理必须覆盖可信来源头；仅在代理验证通过时使用 IP 摘要。原始 IP 不落库，同一来源 30 分钟内只算一次网站访问。自填昵称不能作为 QQ 身份或授权依据。

## 测试与发布工具

- `tests/`：插件单元与流程隔离测试。
- `management-center/test_*.py`：网站认证、访客、查询、恢复与余额等测试。
- `tools/run_tests.py`：两个独立解释器运行测试，避免同名模块冲突。
- `tools/export_public.py`：只选择源码/测试文件，替换实例标识并拦截凭据形状；不复制运行目录。
- `docs/source-manifest.json`：本次白名单导出文件的字节数与 SHA-256；不包含文档、服务模板等仓库自有补充。

部署者仍需人工审阅公开 diff。脱敏扫描不构成“任何私人内容都绝不可能遗漏”的证明。
