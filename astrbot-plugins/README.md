# AstrBot 陪伴插件

当前主要维护 `astrbot_plugin_companion_chat`、`astrbot_plugin_companion_memory` 和 `astrbot_plugin_companion_phone`；`astrbot_plugin_companion_asr` 为可选语音识别适配。

统一聊天插件的 `active/`、`batch/`、`friends/` 和 `breeze/` 是其内部组件，复制时必须保留完整目录。仓库也保留旧的独立插件源码作为兼容和历史资料；启用统一聊天前应停用旧独立插件，避免重复处理消息或调度。

`astrbot_plugin_companion_bailian_sing` 是隔离试听实验，不代表生产能力。QQ 空间和资料的现行操作需要另行安装、配置相应第三方插件。账号、地址和音色标识均为示例，需要自行配置；不要覆盖已有实例数据。

详见 [项目首页](../README.md)。早期 [模块说明](../docs/MODULES.md) 和 [部署说明](../docs/DEPLOYMENT.md) 是历史资料，可能与现行源码不同。
