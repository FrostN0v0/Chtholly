# 生产环境运维与调试手册

本文记录 Chtholly 生产服务器的连接、服务管理、日志查询、LLBot / OneBot 排障、数据检查、更新与回滚方式。目标是先保留现场和证据，再定位根因，避免用重启、删库或重装掩盖问题。

## 安全边界

- 仓库可能公开，本文不得记录真实 Token、API Key、密码、Cookie、Bot QQ、群号、用户号、公网 IP 或完整环境变量。
- 生产凭证只保存在服务器受限文件中，不复制到 Issue、聊天记录或诊断附件。
- `3080` 与 `8120` 只监听 `127.0.0.1`，必须通过 IAP SSH 隧道访问，禁止开放公网防火墙。
- 不执行 `git reset --hard`、`git clean`、删除数据库、清空 LLBot 数据目录等破坏性操作。
- 日志包含消息正文、用户标识和外部 API 错误；对外分享前必须人工脱敏。

## 当前生产基线

以下为 2026-08-08 部署时的基线。排障前应通过命令重新确认，不能把本节当作永久事实。

| 项目 | 当前值 |
| --- | --- |
| 操作系统 | Debian GNU/Linux 13 |
| SSH 别名 | `chtholly-gcp` |
| Chtholly 目录 | `/opt/chtholly` |
| Chtholly 服务 | `chtholly.service` |
| Chtholly 运行用户 | `chtholly` |
| Chtholly 环境文件 | `/etc/chtholly/chtholly.env` |
| Entari / Satori / OneBot 监听 | `127.0.0.1:8120` |
| 主数据库 | `/opt/chtholly/data/chtholly.db` |
| LocalData | `/opt/chtholly/.chtholly/data` |
| Entari 日志目录 | `/opt/chtholly/.entari/log` |
| LLBot 目录 | `/opt/llbot` |
| LLBot 服务 | `llbot.service` |
| LLBot 运行用户 | `llbot` |
| LLBot 数据目录 | `/opt/llbot/bin/llbot/data` |
| LLBot WebUI | `127.0.0.1:3080` |
| 已部署提交 | `f1f2b1b` |
| Python | `3.10.20` |
| LLBot | `8.1.7` |

生产环境同时保留官方 QQ 适配器和 LLBot OneBot V11 反向 WebSocket。LLBot 的完整反向地址必须为 `ws://127.0.0.1:8120/onebot/v11/ws`。

## 连接生产服务器

本机 SSH 配置已提供 `chtholly-gcp` 别名，并通过 Google Cloud IAP 建立连接：

```bash
ssh chtholly-gcp
```

如果 SSH 别名丢失，先检查本机 `~/.ssh/config` 中的 `Host chtholly-gcp`。也可使用 gcloud 模板连接，具体项目、区域和实例值从受控的本机 SSH 配置或 Google Cloud Console 获取：

```bash
INSTANCE_NAME="replace-me"
PROJECT_ID="replace-me"
ZONE="replace-me"
gcloud compute ssh "$INSTANCE_NAME" --project="$PROJECT_ID" --zone="$ZONE" --tunnel-through-iap
```

不要把这些基础设施标识和密钥文件路径写入公开文档。

## 访问本地监听服务

默认只转发 LLBot WebUI，并显式把本地端口限制在回环地址：

```bash
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:3080:127.0.0.1:3080 chtholly-gcp
```

隧道建立后访问 `http://127.0.0.1:3080/`。如果本机端口已占用，可把第一个 `3080` 改为未占用端口，例如 `13080`；服务器端地址保持不变。

只有明确需要 Entari 管理面或 Satori API 时，才在同一 SSH 命令中追加：

```bash
-L 127.0.0.1:8120:127.0.0.1:8120
```

随后访问 `http://127.0.0.1:8120/`。LLBot WebUI 使用自身受保护的登录凭证，不与 Entari 的 `WEBUI_PASSWORD` 混用；不要在命令行、聊天或日志中打印任何密码或 Token。

Entari WebUI 在 `127.0.0.1` 本地部署模式下会跳过自身登录鉴权，因此 IAP SSH 隧道就是管理面的安全边界；不得把 `8120` 直接暴露到公网，也不得仅依赖 `WEBUI_PASSWORD` 将该回环地址反向代理到公网。关闭 SSH 会话即可完整回滚，不需要修改或重启服务器服务。

当前锁定的 Satori Server 中，`server.token` 只校验事件 WebSocket 的 Identify token，HTTP action API 不校验该 token；因此即使已配置 `server.token`，`8120` 的 HTTP API 仍必须依赖 loopback 与 IAP SSH 隧道隔离。

## 一分钟健康检查

登录服务器后按顺序执行：

```bash
systemctl is-active chtholly llbot
systemctl is-enabled chtholly llbot
systemctl --failed --no-pager
sudo ss -lntp
sudo ss -ntp '( sport = :8120 or dport = :8120 )'
```

正常状态应满足：

- 两个服务均为 `active` 和 `enabled`。
- `systemctl --failed` 返回零个失败单元。
- `127.0.0.1:8120` 由 Entari 监听。
- `127.0.0.1:3080` 由 LLBot 监听。
- LLBot 与 Entari 之间存在指向 `127.0.0.1:8120` 的 `ESTAB` 连接。

查看资源占用和重启次数：

```bash
systemctl show chtholly llbot -p Id -p ActiveState -p SubState -p NRestarts -p MemoryCurrent
free -h
df -h /
sudo du -sh /opt/chtholly /opt/llbot /var/lib/chtholly /var/lib/llbot
```

## 日志查询

### 实时跟踪

```bash
sudo journalctl -u chtholly -f
sudo journalctl -u llbot -f
```

同时观察两个服务：

```bash
sudo journalctl -u chtholly -u llbot -f
```

### 最近日志

```bash
sudo journalctl -u chtholly -b --no-pager -n 200
sudo journalctl -u llbot -b --no-pager -n 200
sudo journalctl -u chtholly --since "30 minutes ago" --no-pager
sudo journalctl -u llbot --since "30 minutes ago" --no-pager
```

按时间窗口查询比只看末尾更可靠。记录问题首次发生的准确时间，再扩大前后窗口。

### 过滤错误

```bash
sudo journalctl -u chtholly --since "2 hours ago" --grep='Traceback|Exception|\[E\]|ERROR|failed' --no-pager
sudo journalctl -u llbot --since "2 hours ago" --grep='Traceback|Exception|\[E\]|ERROR|failed' --no-pager
```

检查插件加载：

```bash
sudo journalctl -u chtholly -b --grep='loaded plugin|failed to load plugin|llm_chat|registered LLM tools' --no-pager
```

检查 OneBot 握手：

```bash
sudo journalctl -u chtholly -b --grep='WebSocket|connection open|403' --no-pager
sudo journalctl -u llbot -b --grep='Online registered|Connected to the websocket server|Unexpected server response' --no-pager
```

Entari 同时保存文件日志：

```bash
sudo -u chtholly less /opt/chtholly/.entari/log/latest.log
```

优先使用 journald；文件日志主要用于跨服务重启后的补充查询。

## 服务管理

查看实际生效的 systemd 单元：

```bash
sudo systemctl cat chtholly
sudo systemctl cat llbot
```

重启顺序：

```bash
sudo systemctl restart chtholly
sudo systemctl restart llbot
```

如果只重启 Chtholly，LLBot 应自动重新连接。重启后必须重新检查服务状态、监听端口和 WebSocket 连接，不能把“命令没有报错”等同于恢复。

服务进入启动限流时：

```bash
sudo systemctl reset-failed chtholly
sudo systemctl reset-failed llbot
sudo systemctl start chtholly
sudo systemctl start llbot
```

## 常见故障

### Chtholly 启动失败

先查看当前状态和本次启动日志：

```bash
sudo systemctl status chtholly --no-pager
sudo journalctl -u chtholly -b --no-pager -n 300
```

重点检查：

- 环境变量缺失或格式错误。
- 插件加载失败。
- SQLite 文件不可读写。
- Playwright 浏览器缺失。
- systemd 沙箱阻止写入运行目录。

生产单元必须允许写入以下目录：

- `/opt/chtholly/.entari`
- `/opt/chtholly/.chtholly`
- `/opt/chtholly/data`
- `/opt/chtholly/logs`
- `/opt/chtholly/config`
- `/opt/chtholly/resources/image/memes`
- `/var/lib/chtholly`

如果日志包含 `Read-only file system`，检查 `ReadWritePaths`，不要通过关闭整个 `ProtectSystem` 来绕过问题。

### LLBot 缺少或拒绝 Auth Token

典型日志包括 `没有 Auth Token`、`auth_token 校验失败` 或 HTTP `401 / 403`。

Auth Token 文件：

```text
/opt/llbot/bin/llbot/data/auth_token.txt
```

检查权限，不打印内容：

```bash
sudo stat -c '%a %U %G %n' /opt/llbot/bin/llbot/data/auth_token.txt
```

正常权限为 `600 llbot llbot`。新 Token 从 `https://auth.luckylillia.com` 获取。更新时使用受控文件传输或 `sudoedit`，不要通过 `echo` 写入 shell 历史。

更新后：

```bash
sudo chown llbot:llbot /opt/llbot/bin/llbot/data/auth_token.txt
sudo chmod 600 /opt/llbot/bin/llbot/data/auth_token.txt
sudo systemctl restart llbot
```

### LLBot 要求重新扫码

先建立 WebUI 隧道：

```bash
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:3080:127.0.0.1:3080 chtholly-gcp
```

打开 `http://127.0.0.1:3080`，使用 LLBot 自身的生产 WebUI 登录凭证并扫描二维码。扫码成功后确认 LLBot 数据目录内已生成账号会话文件，并检查 systemd `ExecStart` 是否包含 `--qq=<BOT_QQ>` 快速登录参数。

不要把真实 Bot QQ 写入仓库。

### OneBot 反向 WebSocket 返回 403

生产环境完整路径必须为：

```text
ws://127.0.0.1:8120/onebot/v11/ws
```

检查以下三项：

1. LLBot 活跃配置中的 URL 包含末尾 `/ws`。
2. LLBot OneBot Token 与 `/etc/chtholly/chtholly.env` 中的 `ONEBOT_TOKEN` 一致。
3. Entari OneBot reverse adapter 的 `access_token` 使用同一环境变量。

LLBot 活跃配置位于：

```text
/opt/llbot/bin/llbot/data/config_<BOT_QQ>.json
```

使用 `sudoedit` 检查，禁止把整个配置文件复制到聊天或 Issue，因为其中包含 Token。

修改后：

```bash
sudo systemctl restart chtholly
sudo systemctl restart llbot
```

### LLBot WebUI 报 `ERR_SYSTEM_ERROR` 或 errno 97

Node.js 的 `os.networkInterfaces()` 需要 netlink。检查 LLBot 单元是否包含：

```text
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
```

缺少 `AF_NETLINK` 时 WebUI 可能无法启动，但 QQ 协议流程仍可能继续，容易造成误判。

### LLBot 在线但 Entari 收不到消息

依次检查：

```bash
sudo ss -ntp '( sport = :8120 or dport = :8120 )'
sudo journalctl -u llbot --since "10 minutes ago" --grep='WebSocket|Unexpected server response' --no-pager
sudo journalctl -u chtholly --since "10 minutes ago" --grep='connection open|message|403' --no-pager
```

正常链路中，LLBot 会向 Entari 上报事件，Entari 会反向调用 `get_login_info`、`get_group_info`、`get_group_member_info` 等 OneBot API。

### Satori / OneBot 只读 API 冒烟检查

设置 `BOT_QQ` 后执行。HTTP `200` 表示 Entari 已识别 OneBot 登录并能通过适配器处理 API：

```bash
BOT_QQ="replace-me"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8120/satori/v1/login.get -H 'Content-Type: application/json' -H 'Satori-Platform: onebot' -H "Satori-User-ID: ${BOT_QQ}" --data '{}'
```

下面的命令会真实发送一条 QQ 私聊消息，只能在明确需要测试发送链路时使用，并设置两个变量：

```bash
BOT_QQ="replace-me"
TARGET_QQ="replace-me"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8120/satori/v1/message.create -H 'Content-Type: application/json' -H 'Satori-Platform: onebot' -H "Satori-User-ID: ${BOT_QQ}" --data "{\"channel_id\":\"private:${TARGET_QQ}\",\"content\":\"Chtholly production smoke test.\"}"
```

### Playwright 或 Chromium 启动失败

确认环境变量和浏览器目录：

```bash
sudo systemctl show chtholly -p Environment
sudo -u chtholly test -d /opt/chtholly/.playwright
```

执行无页面访问的 Chromium 启动检查：

```bash
sudo -u chtholly env PLAYWRIGHT_BROWSERS_PATH=/opt/chtholly/.playwright /opt/chtholly/.venv/bin/python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); b = p.chromium.launch(headless=True); print(b.version); b.close(); p.stop()"
```

注意 `systemctl show ... -p Environment` 只应包含非敏感固定环境变量；生产密钥来自 `EnvironmentFile`，不要执行会展开其内容的命令。

### 图片标注超时 warning

单张图片标注超时不会导致 Bot 退出。先判断是否为偶发：

```bash
sudo journalctl -u chtholly --since "6 hours ago" --grep='tagging failed|Timeout' --no-pager
```

如果持续大量出现，再检查视觉模型可用性、模型超时、网络和供应商限流。不要因单次 warning 删除图片或标签数据库。

## 数据库检查与备份

在线快速检查：

```bash
sudo -u chtholly sqlite3 /opt/chtholly/data/chtholly.db 'PRAGMA quick_check;'
sudo -u chtholly sqlite3 /opt/chtholly/.chtholly/data/entari_plugin_llm/agno.db 'PRAGMA quick_check;'
```

两个命令均应输出 `ok`。

变更、升级或修复数据库前创建一致性备份：

```bash
timestamp="$(date -u +%Y%m%d-%H%M%S)"
sudo install -d -m 0750 -o chtholly -g chtholly /var/lib/chtholly/backups
sudo systemctl stop chtholly
sudo -u chtholly sqlite3 /opt/chtholly/data/chtholly.db ".backup '/var/lib/chtholly/backups/chtholly-${timestamp}.db'"
sudo -u chtholly sqlite3 /opt/chtholly/.chtholly/data/entari_plugin_llm/agno.db ".backup '/var/lib/chtholly/backups/agno-${timestamp}.db'"
sudo systemctl start chtholly
```

备份后再次执行 `PRAGMA quick_check`。不要直接复制运行中的 SQLite 主文件，不要把数据库提交到 Git。

## 凭证更新

Chtholly 生产环境变量：

```text
/etc/chtholly/chtholly.env
```

编辑与重启：

```bash
sudoedit /etc/chtholly/chtholly.env
sudo chown root:chtholly /etc/chtholly/chtholly.env
sudo chmod 640 /etc/chtholly/chtholly.env
sudo systemctl restart chtholly
```

LLBot 的敏感文件：

```text
/opt/llbot/bin/llbot/data/auth_token.txt
/opt/llbot/bin/llbot/data/webui_token.txt
/opt/llbot/bin/llbot/data/config_<BOT_QQ>.json
```

这些文件权限应为 `600 llbot llbot`。任何 Token 一旦发到聊天、日志或公开 Issue，立即轮换。

## 更新 Chtholly

更新前先备份数据库并记录当前提交：

```bash
sudo -u chtholly git -C /opt/chtholly status --short --branch
sudo -u chtholly git -C /opt/chtholly log -1 --oneline
```

先获取远端更新，确认目标版本后再停止服务并切换代码：

```bash
sudo -u chtholly git -C /opt/chtholly fetch origin
sudo systemctl stop chtholly
sudo -u chtholly git -C /opt/chtholly pull --ff-only
sudo -u chtholly -H sh -c 'cd /opt/chtholly && /var/lib/chtholly/.local/bin/uv sync --locked --no-dev --all-extras --python 3.10'
sudo systemctl start chtholly
```

更新后检查插件加载、数据库、Playwright 和 OneBot 连接。不要在服务器上运行自动修复依赖或未锁定升级。

## 回滚 Chtholly

只有在已记录已知良好提交且数据库结构兼容时回滚代码：

```bash
KNOWN_GOOD_COMMIT="replace-me"
sudo systemctl stop chtholly
sudo -u chtholly git -C /opt/chtholly log --oneline -n 10
sudo -u chtholly git -C /opt/chtholly switch --detach "$KNOWN_GOOD_COMMIT"
sudo -u chtholly -H sh -c 'cd /opt/chtholly && /var/lib/chtholly/.local/bin/uv sync --locked --no-dev --all-extras --python 3.10'
sudo systemctl start chtholly
```

如果新版本执行过不可逆数据库迁移，不能只回滚代码；必须使用对应时间点的数据库备份。恢复后再切回受跟踪分支，避免长期停留在 detached HEAD。

## 更新 LLBot

LLBot 当前固定使用官方 Linux CLI 发行版。不要直接在生产服务中执行无人监督的 `--update`。

更新前：

1. 记录当前 LLBot 版本。
2. 备份 `/opt/llbot/bin/llbot/data`。
3. 从官方 Release 下载目标版本。
4. 对照官方 SHA-256 校验发行包。
5. 替换程序文件时保留数据目录、systemd 单元和文件权限。
6. 重启后验证快速登录和 OneBot reverse WebSocket。

官方来源：

- <https://github.com/LLOneBot/LuckyLilliaBot/releases>
- <https://luckylillia.com/guide/config>

## 收集诊断信息

先限定时间窗口，再导出日志：

```bash
timestamp="$(date -u +%Y%m%d-%H%M%S)"
ssh chtholly-gcp "sudo journalctl -u chtholly -u llbot --since '2 hours ago' --no-pager" > "production-${timestamp}.log"
```

提交问题前删除或替换：

- 消息正文。
- QQ、群号、用户 ID。
- URL query 中的 Token、签名和密钥。
- API 响应中的凭证。
- 本地文件路径中可识别用户的信息。

禁止附带 `.env`、`chtholly.env`、LLBot 配置、Auth Token、WebUI Token、数据库或完整 QR 登录日志。

## Bug 排查记录模板

每次故障至少记录：

- 首次发生时间和时区。
- 受影响服务与功能。
- 预期行为和实际行为。
- 最近一次代码、配置、依赖或凭证变更。
- `systemctl` 状态与重启次数。
- 问题前后至少五分钟日志。
- 是否可稳定复现。
- 数据库 `quick_check` 结果。
- OneBot WebSocket 是否 `ESTAB`。
- 采取过的操作及其结果。

先保留原始现场，再做一次最小变更并复测。避免同时改配置、重装依赖和重启多个服务，否则无法判断真正根因。
