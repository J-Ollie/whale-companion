# AstrBot 陪伴插件

把下列五个完整目录复制到 AstrBot 的 `data/plugins/`，不要把文件平铺。内部包名是导入和部署标识，保持不变。

| 插件 | 用途 |
| --- | --- |
| `astrbot_plugin_companion_memory` | 分层记忆、原文回忆与共享身份 |
| `astrbot_plugin_companion_friends` | 审批前身份查询、好友权限、归档和睡眠未读 |
| `astrbot_plugin_companion_batch` | 15秒/60秒消息合并与自然收尾 |
| `astrbot_plugin_companion_active` | 主动联系与聊天风格 |
| `astrbot_plugin_companion_phone` | 手机、作息、自主活动、媒体、空间与双日记 |

各插件相互配合，依赖 AstrBot 运行环境；不是五个互不依赖的通用插件安装包。真实账号、provider、包名和授权须自行配置，默认示例不能直接运行。

详细说明：[模块](../docs/MODULES.md)、[部署](../docs/DEPLOYMENT.md)、[当前行为](../docs/CURRENT_BEHAVIOR.md)。
