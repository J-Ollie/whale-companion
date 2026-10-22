# 部署与复现说明

本仓库是分批整理的脱敏源码。它需要已有的 Linux、Docker、AstrBot 和 QQ 接入环境；不包含 SnowLuma / AstrBot 二进制、镜像、账号或模型凭据。

## 需要自行准备

1. Linux 主机，Python 3.10+（原部署 Python 3.12）、时区数据库、Docker，以及可用的 AstrBot 4.29.x 环境。原服务器为 2 核 2 GB、50 GB 存储，仅供资源规模参考。
2. QQ 接入端。当前部署使用 SnowLuma，须按其自身文档安装并连接 AstrBot。平台 ID 在本项目中默认是 `qq-companion`，与实际配置保持一致。
3. 模型服务及 AstrBot provider / persona 配置。示例默认 provider 名称可能不可用，需要改为自己的真实配置。
4. 若使用手机功能，准备安卓设备、Tailscale 私有网络和已授权的 ADB 配对。无线调试连接端口可能变化，需保持后台网络和调试连接可用。

## 安装插件

将 `plugins/` 下的五个 `astrbot_plugin_companion_*` 目录放入 AstrBot 的 `data/plugins/`，按 AstrBot 方式加载。不要把所有源码文件平铺在同一个插件目录。

每个目录的 `metadata.yaml` 与 `_conf_schema.json` 给出版本和配置项。示例账号 `100000001` 是管理员，`100000002` 是机器人；必须替换为自己的账号。示例手机地址 `100.64.0.10` 和网站地址 `203.0.113.10` 不是本项目的实际服务地址。

有些原有实现仍在源码常量中读取账号、地址或路径，例如手机插件 `phone.py` 的 PHONE_IP、`qzone_client.py` 的机器人标识，以及网站 `server.py` 的 OWNER / BOT。复现时需同时检查相关源码和配置，不能只改一个配置文件。后续可将这些常量进一步配置化。

首次启用前，在插件设置中检查授权软件、截图识别、公开发布、评论与配额。`_conf_schema.json` 中的默认值是安装默认值，不代表原实例的最终运行设置；原实例的带密钥配置未公开。具体期望行为见 CURRENT_BEHAVIOR.md。

插件依赖 AstrBot 和其相关 Python 包；手机模块还使用 Pillow、mcp 等运行环境依赖。ADB 命令须在运行 AstrBot 的环境中可用。当前归档未提供锁定依赖文件，需根据实际 AstrBot 版本校验，不能假定仅复制插件就完成安装。

## 管理中心

网站源码是 Python 标准库 HTTP 服务，默认根目录 `/opt/qq-companion`，也可通过 `QQ_RECOVERY_ROOT` 指定。需保证 Docker 容器命名、数据目录和源码期望一致。

常见目录布局：

```text
/opt/qq-companion/
  qq-recovery/                 # 本仓库网站源码
  recovery-data/               # 本机私有认证、来访数据库，不提交
  data/astrbot/plugins/        # 插件源码
  data/astrbot/plugin_data/    # 记忆、日记、消息与状态，不提交
```

`server.py` 默认仅监听 `127.0.0.1:6190`。管理页面还需要本地 bootstrap key 和用户自行设置的面板密码；端口转发本身不会自动完成授权。原实例使用独立的启动入口和 HTTPS 反向代理，完整代理凭据与本地快捷方式未上传。

提供的 `qq-companion-recovery.service` 是基础单元。启用 systemd 的只读文件系统约束时，还须使用 `systemd/visitors.conf` 的追加目录权限，并提前创建对应私有目录。服务需要访问 Docker；部署者应自行配置适合其环境的权限。

如部署 HTTPS 公网入口，需设置 `QQ_RECOVERY_PUBLIC_ORIGIN`、`QQ_RECOVERY_PUBLIC_PROXY_KEY_FILE`，并由受信任的本机代理注入正确代理凭据与转发头。不能把 bootstrap/proxy 密钥写进前端或公开仓库。`X-Real-IP` 应由代理以真实来源覆盖；客户端自带的头不应被直接信任。

## 验证

插件测试在带有 AstrBot 运行依赖的环境执行，例如将 COMPANION_TEST_PLUGINS 指向 `plugins/` 后，在 `tests/` 中运行 unittest。网站测试在 `qq-recovery/` 中执行 `python -m unittest discover`。这些测试使用隔离目录和模拟事件，不应向真实账号发送测试消息。

实际复现仍应验证：普通好友权限、睡眠未读处理、公开/私密隔离、面板认证、手机配额、实际发布回读。账号风控或手机省电设置可能影响可用性，不能仅凭工具成功回执断言别人一定看得到动态。
