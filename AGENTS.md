# AGENTS.md

本文档为 AI 编程代理（如 Codex、Claude Code 等）提供本仓库的项目上下文与开发指南。内容应以当前源码为准；修改实现后请同步更新本文档。

> 本仓库以 `master` 为默认主分支，Bot 基于 **Entari**（Arclet Project，基于 Satori 协议）运行；`master` 由原 `etr` 分支改名而来。`resources` 是用户刻意保留的独立资源分支，不得并入应用主线或作为废弃工作分支删除。架构与迁移状态以当前源码为准。

## 项目概述

**Chtholly** 是一款 QQ 娱乐机器人工程，基于 [Entari](https://github.com/ArcletProject/Entari)（Satori 协议）运行。它本身不实现单一业务，而是通过 `entari.yml` 声明加载的社区插件与 `plugins/` 目录下的本地插件组合出功能。

- 协议层：Satori 协议；通过 `entari-plugin-server` + 适配器对接 OneBot V11 / Milky / QQ / Lagrange / Console / 纯 Satori 等协议端。
- 主要职责：提供运行时环境（`entari.yml`）、共享工具（`utils/`）、静态资源（`resources/`）、本地扩展插件（`plugins/`）。
- 使用者可按需在 `entari.yml` 的 `plugins` 段裁剪加载列表，或通过 `external_dirs` 引入额外插件目录。

## 技术栈与关键依赖

- **Python**: >= 3.10, < 4.0（当前运行时使用 3.10；待协议栈完成 Python 3.14 兼容后再升级）
- **Bot 框架**: [arclet-entari](https://pypi.org/project/arclet-entari/)（基于官方 `0.19.0rc2`，当前安装仓库内的 `0.19.0rc2+chtholly.2` 补丁 wheel，完整保留 `arclet-entari[full]`，含 CLI、YAML、文件监听）。补丁修复 staged reload 对象所有权、Scope 冲突、失败模块绑定恢复、子插件/Service 清理与重复替换后的命令残留；失败候选须恢复旧订阅者持有的真实 Alconna 解析器和 formatter 条目。`scripts/build_patched_wheel.py --package entari` 从固定官方 wheel 与仓库补丁重建制品；同一构建器的 `--package webui` 生成 `1.0.3+chtholly.1`，将认证 Cookie 与每个聊天连接的 transport ID 分离，断开只清理自己的连接、waiter 和 login，并让失败的前端认证初始化可以重试。不直接修改第三方安装目录。HTMLRender `0.1.0` 的 Entari 上界通过精确 uv override 保留 `full,pydantic`，升级必须验证真实渲染及失败更新、重复替换和最终卸载。
- **CLI 工具**: [entari-cli](https://pypi.org/project/entari-cli/) —— `entari init / run / new / add / remove / config / gen_main`
- **事件总线**: arclet-letoderea（Entari 内建依赖）
- **命令系统**: arclet-alconna（Entari 内建 `command` 模块）
- **服务管理**: launart（`Service` 基类用于跨插件依赖注入）
- **协议适配器**: `satori-python-adapter-onebot11`（默认）；`entari-plugin-server` 使用 `direct_adapter: true` 与 Entari 直连，此模式不得再配置 `basic.network`。官方 QQ 沙箱群聊与单聊事件必须在 `@qq.websocket` 的 `intent.c2c_group_at_messages` 下启用；写在适配器顶层会被配置模型忽略。QQ WebSocket 的 `token` 字段已废弃，不得配置。当前协议栈仍以 Python 3.10 运行，待 Python 3.14 兼容性确认后升级
- **Satori 服务鉴权**: `server.token` 只校验 Satori 事件 WebSocket 的 Identify token；当前锁定的 Satori Server HTTP action API 不校验该 token，因此共享的 Satori/WebUI 后端必须保持 `127.0.0.1` 监听。公网管理只能通过独立 HTTPS/OIDC 网关与明确的管理路由白名单进入，不得直接反代整个端口；Satori/OneBot 和自动 API 文档不得公开。SSH 隧道保留应急访问。OneBot 适配器的 `access_token` 只保护对应适配器连接，三者不得混用。
- **配置模型**: `BasicConfModel`（默认，dataclass 风格）/ Pydantic `BaseModel`（`arclet.entari.config.models.pyd`）/ msgspec `Struct`
- **HTTP 客户端**: httpx；浏览器截图的受控公网出口使用 aiohttp 自定义 resolver 固定已校验公网 IP
- **JSON 序列化**: orjson（LiteLLM 工具 / MCP 请求路径的显式运行时依赖）
- **LLM 兼容组合**: `entari-plugin-llm` 固定 Git `1845f1c3c65f9df4493e97f96a4c43938b176320`；Agno 固定在 `<3` 的最新兼容系列，当前为 `2.9.0`，因为上游仍导入 Agno 3 已删除的 schema helper。Python 3.10 的 LiteLLM 限制 `<1.97`，当前为 `1.96.2`；`1.97` 的 response schema 无法完成构建，`1.98+` 另有 `typing.NotRequired` 导入错误。以 `uv.lock` 安装，不修改第三方安装目录。
- **LLM 工具执行**: 上游 `tools.available_functions` 的 `(Subscriber, Function)` 是 schema 与依赖注入的唯一权威；模型暂停后，由本地 generation-local 边界调用上游 `run_llm_tools` 执行真实 `RunRequirement`，随后继续生成。不再维护第二份 Function/schema。仅本地 generation 的 Agent 开启内存 `cache_session`，以支持上游按 `run_id` 继续无数据库的暂停轮次；授权、跨暂停调用上限、模型指定的消息链顺序、只读并发、取消结算、错误脱敏和审计仍由本地边界控制。Entari staged reload 必须保留新工具所有权，失败时恢复仍存活的旧订阅者；旧插件清理不得删除替代工具。pytest 通过 `tests/conftest.py` 将 Letoderea effect 任务绑定到当前测试 loop，禁止通过取消异 loop 清理任务伪装卸载成功。
- **LLM 工具线上声明**: Agno `Function.to_dict()` 同时用于组件持久化，包含 `external_execution`、`requires_confirmation` 和 `approval_type` 等本地执行标志，不能直接作为供应商工具声明。`agno_compat.py` 的 LiteLLM `_format_tools` 仅在传输边界保留 `name`、`description`、`parameters` 与 `strict`，不修改上游 Function 或停用外部工具执行；验证必须覆盖真实模型请求、工具暂停与携带结果继续生成，不能只验证模型无工具回复或服务健康。
- `publish_web_preview` 与 `submit_plugin` 的模型源码参数固定使用 `source_files`；禁止用 Agno 媒体保留参数名 `files`，否则上游会将必需业务参数从 schema 移除，形成缺参注入失败与补参校验拒绝。继续以原生 `LLMToolEvent` / `run_llm_tools` 执行和上游 schema 为权威，不补第二套声明或关闭参数检查；网页发布回归必须经过真实工具桥并核对交付 ZIP 的原始字节。
- 网页文件与工坊提交的嵌套字段由 `tools/_submission_models.py` 的严格 Pydantic 输入模型生成上游 schema，并在工具边界校验，随后继续执行存储层既有权限、路径和容量规则。不得将 `dict[str, Any]` 直接用作工坊 manifest：当前 Agno 会把其值错误声明为空对象；必须明确标题、命令、配置、验收检查和布尔字段，并在文件映射 schema 中要求根级 `__init__.py`。验证既要覆盖原生 schema 接受合法载荷、拒绝错误结构，也要使用真实生产模型完成生成、提交与隔离验收，不能只向工具传入人工构造的正确参数。
- 受认证保护的 WebUI 扩展必须兼容上游不含 `allow-same-origin` 的 opaque iframe：会话页、表情页与工坊以内联 nonce 加载可信静态资源，通过原生父页面 `postMessage` API bridge 携带会话认证，响应严格校验父窗口与精确 origin。会话附件和表情图片通过同一桥获取经 MIME/6 MiB 校验的 Blob，切换轮次、翻页或关闭页面时释放 URL 与请求等待；表情上传把 FormData 转成可结构化克隆的 Blob，并保留生成时原始 multipart Content-Type 边界，不能使用会转小写的 Blob.type 重建边界。不得通过匿名资源路由、CORS 通配或关闭 iframe sandbox 恢复功能。
- 公网 SSO 是独立部署能力：`scripts/chtholly-webui-oidc.service` 运行 OAuth2 Proxy，`scripts/webui-oauth2.cfg` 对接 Casdoor OIDC，`scripts/webui.Caddyfile` 只发布管理路径。管理域名通过 Caddy 的 `WEBUI_HOST` 与 `ACME_EMAIL` 配置，Let's Encrypt 签发失败时允许使用 ZeroSSL 正式证书，不放宽 TLS 验证或更改其他站点。独立 `plugins/webui_sso` 在配置的 HTTPS origin 上向固定回环 `/oauth2/auth` 验证 SSO Cookie，再由 `utils/webui_sso.py` 为当前 HTTP 或 WebSocket 请求建立并注入原生 SessionStore 会话；禁止信任未经验证的身份头、开启全局 `local_mode` 或把 SSO 放进 `llm_chat`、工坊业务模块。公网不再要求第二次 WebUI 密码，隧道仍保留原密码；按用户决定，该 Casdoor 应用已认证账号均获得原生管理权限，使用签名 `sub`，不额外限制邮箱验证或组。凭证只进 root-owned 环境文件，OAuth token 不转发到 Bot；网关不可用不得降级直连8120。原生 SPA 的登录跳转会先加载受保护的懒加载脚本，所以部署层浏览器集成必须直接处理退出 API 成功及 API 401，再执行完整页面导航；不能只拦截 history 或返回 XHR 重定向。退出须清除两层 Cookie 并停留在固定退出页，不使用未列入重定向白名单的绝对 rd；插件卸载清理仅撤销自己的路由、中间件与签发会话。
- 公网 SSO 使用独立鉴权回环 Redis 保存加密会话，请求 `offline_access`，`cookie_refresh=5m`、`cookie_expire=12h`；实际续期仍受 Casdoor 令牌寿命与撤销约束。`scripts/build_oauth2_proxy.py` 以固定官方 `7.15.4` 源码和 Go 工具链构建 `v7.15.4-chtholly.1`：供应商请求最多 15 秒，整次存储认证最多 20 秒，Redis 刷新锁租期 30 秒，刷新验证成功后才保存时间戳；网络、Redis 或身份服务故障返回 503 并保留票据，明确撤销与过期返回 401。刷新令牌只能省略 nonce，存在但不匹配的 nonce、签名、issuer、audience、有效期仍须验证；不能序列化 `Refreshed` 绕过 JWT 检查。每次公网请求仍须通过真实 `/oauth2/auth`，本地映射仅按已验证票据的稳定 payload 复用原生会话，不能将签名时间变化当作新身份。受认证的 `/api/webui-sso/session` 供原生 WebSocket 失败后进行合并、退避的 HTTP 恢复；健康读取 25 秒，不延长写入、不重放请求、不隐藏原生离线状态。退出先删除服务端记录再清除两层 Cookie，未确认撤销不得成功；隐藏标签页与 BFCache 恢复继续保留既有重新认证规则，静态资源未认证仅返回 401，CSRF 登录窗口一小时。
- Casdoor 的应用级 `grantTypes` 必须同时包含 `authorization_code` 和 `refresh_token`。`offline_access` 与正数 `refreshExpireInHours` 不会启用标准端点的刷新授权；生产曾仅启用前者，首次登录成功后，`/api/login/oauth/access_token` 返回 `unsupported_grant_type` 并形成持续 503。修复应通过现有应用的受认证管理 API，仅更新 `grantTypes`，保留客户端密钥、回调、权限及令牌寿命；不得改用绕过该检查的独立 `/api/login/oauth/refresh_token` 路由。可用无效的合成刷新令牌验证响应从 `unsupported_grant_type` 变为 `invalid_grant`，但这不能冒充真实会话刷新成功，也不得消耗用户正在使用的轮换刷新令牌。
- **日志与终端**: rich（Entari 内建 log 使用 loguru；凭证化运行环境必须关闭会展开局部变量的 `rich_error`）
- **包管理**: uv（`uv sync` / `uv add` / `uv remove`）
- **代码质量**: Ruff、Pyright（`typeCheckingMode = "standard"`）

### 原生插件工坊

- `plugins/plugin_workshop` 是可信宿主，`utils/plugin_workshop_core` 为 import-safe 不可变源码、manifest、SHA-256、审批、操作日志和崩溃恢复边界，持久化路径来自 LocalData。生成源码只能发布到工坊命名空间，不修改应用源码、配置或依赖；任何文件、命令检查或配置变化都必须成为新版本。原生生命周期通过单一 Service 串行管理，重启只恢复已批准且启用的提交状态，停用和代码回滚不回滚外部数据。
- `utils/plugin_workshop_sandbox` 只通过显式预构建 Linux Docker 镜像验收，禁止回退宿主执行、候选构建镜像、挂载 Docker Socket 或生产目录。构建只发送固定依赖、vendored wheel 和可信 worker，预装锁定 Playwright Chromium、Inter/Noto CJK 与 Pillow；独立 `render_capability` 先实际渲染并解码可信模板，再发布候选源码。HTMLRender 仅授权候选目录，图片限 6 MiB 并完整解码、校验像素及动画预算；单命令 30 秒、默认总验收 180 秒、1 GiB 内存和 256 PID，保留只读根、非 root、drop-all、no-new-privileges、CPU 及 tmpfs 边界。容器保持 `network=none`，只挂载单次任务的只读 Unix Socket：宿主仅允许 Open-Meteo 两个固定 HTTPS GET 路径，校验全部 DNS 回答后固定公网 IP，拒绝重定向、凭证和代理继承，最多 32 请求、4 并发、单体 1 MiB、总计 8 MiB 与每请求 15 秒。取消须等待容器、代理连接和 Socket 清理。真实验收覆盖命令、Session、同源码重载、失败替换、服务及任务清理；协议输出捕获放在协议类外，避免 Entari 把同步辅助方法包装成异步 API。候选与验收器同进程，报告不是安全认证。
- 工坊容器与原生宿主的 HTMLRender 白名单须分别配置。宿主 `htmlrender.resources.local_access.allowed_paths` 保留现有模板目录，并允许与 LocalData 实际路径一致的 `plugin_workshop/runtime`；默认 `app_name: chtholly` 为 `.chtholly/data/plugin_workshop/runtime`。`NativePluginDriver.apply` 仅将已批准、验收通过且哈希匹配的源码发布到该目录，替换失败恢复旧包、停用删除目标包；因此无需给模型或候选追加文件系统权限。不得授权整个 LocalData 根、不可变 `versions`、`staging` 或数据库，也不得开启 `allow_any_path`。容器验收通过不证明宿主权限正确，修复后必须在已激活的真实插件上调用模板渲染并观察图片交付，不只重跑容器示例。
- 三个 generation-local 工具为 `submit_plugin`、`activate_plugin`、`rollback_plugin`，由 `llm_chat.plugin_workshop_enabled` 配置启用。提交只保存并验收，可由模型承接上下文自主发起或修订，不再检查当前原话是否包含创建、修改或重新提交口令，也不得要求用户补说一句授权；活动 generation、当前身份、源码结构、容量和隔离验收约束继续保留。批准只能由已认证 WebUI 或超管 `workshop approve <name> <version>` 授予。仅激活和回滚还需当前超管原文明确授权，后续否定不能被前一肯定覆盖。审计保留真实已提交副作用与取消部分效果，模型历史只保留限幅版本引用，不重放源码或验收日志；源码不是提示词。
- 工坊命令统一使用 `workshop help/list/show/approve/activate/rollback/disable`，不得注册会抢先 BLOCK 后续子命令的裸 `workshop` 响应器。注册函数必须绑定当前子模块的原生插件所有者，staged reload 不得查回旧 owner。WebUI `/extension/plugin-workshop` 和 `/api/plugin-workshop` 复用密码会话认证与同源写入，免密模式拒绝管理；上游 iframe 没有 allow-same-origin，页面以内联 nonce 可信资源和原生 `postMessage` API bridge 工作，消息必须验证父窗口与精确 origin。候选源码、HTML、日志始终纯文本显示，不执行候选前端代码，不放宽第三方 iframe sandbox。
- `webui_auth.py` 在工坊注册时以及 WebUI 的 Startup 之后启用配置密码的真实验证，保持回环监听；无密码或已有免密会话时拒绝加载并要求完整重启，禁止提升旧免密 Cookie。密码哈希只能更新受类型检查的 WebUI 运行时模型，不能对 Entari 的 bound Proxy 赋值触发 YAML 保存；生产配置保护器必须继续拒绝明文或哈希凭证写入。生产通过 Bot 用户的 rootless Docker 与 `DOCKER_HOST` 使用验收器，不授予 rootful Docker 组权限；Socket 放在 systemd 既有可访问的受限路径，内存、CPU、PID 与只读根目录限制必须以真实容器验证。
- 配置应用控制器访问 `/api/config-apply/status` 必须保持原有会话认证，使用配置中的 WebUI 环境引用密码经固定回环 `/api/auth/login` 获得 Cookie，仅在 401 时重新认证一次。禁止为健康探测放开匿名状态接口；控制器拒绝 HTTP 重定向与环境代理，Cookie、密码只存在于短期内存，不写状态文件、日志或 YAML。

## 目录结构（目标形态）

```text
Chtholly/
├── entari.yml             # 主配置：basic.network / log / prefix / plugins
├── .env                   # 环境变量（需 arclet-entari[dotenv]）；存放敏感值，不入库
├── main.py                # 可选：entari gen_main 生成，直接 python main.py 运行
├── plugins/               # 本地插件目录
│   └──...                 # 各插件
├── utils/                 # 跨插件共享工具（纯函数库，不写 Entari 副作用）
│   └── path.py            # 静态资源目录常量
├── resources/             # 静态资源（字体、图片、音频）
├── config/                # 运行期配置文件目录（gitignored）
├── data/                  # 运行期数据文件目录（gitignored，与 .localdata 协同）
├── logs/                  # 运行日志（gitignored，Entari log.save 启用时写入）
├── docs/                  # 文档 / 图片
├── pyproject.toml         # 依赖、Ruff/Pyright 配置
├── uv.lock                # uv 锁文件
└── README.md
```

> 第三方插件实现位于 `.venv` 或 uv 缓存中，请勿直接修改；如需定制先向上游反馈或 fork。迁移期仍残留的 NoneBot2 风格插件需要按本文规范重写为 Entari 插件。

## 运行与常用命令

```bash

# 运行机器人
entari run
# 或生成入口脚本后运行
entari gen_main
uv run main.py

# 安装插件并写入 entari.yml
entari add <plugin-name> [-D] [-O] [-p NUM] [--key KEY]
# 目前add指令会因为未读取到env而报错，所以请使用 uv 安装插件并手动配置 entari.yml

# 同步依赖
uv sync --all-extras 

# 格式化与静态检查
uvx ruff format
uvx ruff check 
uvx ruff check --fix

# 构建发布包
uv build
```

## 插件开发规范

### 插件结构

单文件插件 `plugins/my_plugin.py`：

```python
from arclet.entari import metadata, Plugin, Session, MessageCreatedEvent, plugin

metadata(
    name="my_plugin",
    author=[{"name": "FrostN0v0"}],
    version="0.1.0",
    description="A simple plugin",
    config=Config,  # 可选：声明配置模型
)

plug = Plugin.current()


@plug.dispatch(MessageCreatedEvent)
async def on_message(session: Session):
    if session.content == "ping":
        await session.send("pong")
```

包插件 `plugins/my_plugin/__init__.py`：其下每个 `.py` 或子目录自动成为子插件，可在主配置中用 `my_plugin.foo: {}` 单独配置/禁用。`# entari: plugin` 标记普通导入为插件依赖；`# entari: subplugin` / `# entari: package` 标记子插件依赖。

`metadata` 的 `role` 取 `PluginRole.NORMAL`（默认，可被 `::control` 管理）/ `UTILITY` / `LIBRARY` / `COMPLEX`。

### 事件与响应器

- `@plugin.listen(EventClass)` 或 `@plug.dispatch(EventClass)` 注册事件监听器
- 也可直接用 letoderea：`import arclet.letoderea as leto; @leto.on(EventClass)`
- 常用事件：`MessageCreatedEvent` 等 Satori 事件；生命周期 `Startup` / `Ready` / `Cleanup` / `AccountUpdate`；插件事件 `PluginLoadedSuccess` / `PluginLoadedFailed` / `PluginUnloaded`；`ConfigReload`；指令事件 `CommandReceive` / `CommandParse` / `CommandOutput`；发送事件 `SendRequest` / `SendResponse`
- 生命周期亦可用 `@plug.use("::startup")` / `"::ready"` / `"::cleanup"` 形式
- 依赖注入：handler 参数如 `session: Session`、`app: Entari`、`account: Account`、`channel: Channel` 等按类型自动注入

### 指令系统

Entari 内建 `command` 基于 Alconna：

```python
from arclet.entari import command, MessageChain, Session


@command.on("echo {content}")
def echo_(content: str):
    return content


@command.command("add <a> <b>")
def add(a: int, b: int):
    return f"{a + b = }"


# 复杂指令用 Alconna 实例
from arclet.alconna import Alconna, Args, AllParam

alc = Alconna("echo", Args["content", AllParam])
disp = command.mount(alc)


@disp.handle()
async def echo_(content: command.Match[MessageChain], session: Session):
    await session.send(content.result)
```

通用参数：`need_reply_me`、`need_notice_me`、`use_config_prefix`、`ignore_prefix_filter`。全局指令配置走 `.commands` 插件。

### 过滤器

```python
from arclet.entari import filter_, plugin, MessageCreatedEvent


@plugin.listen(MessageCreatedEvent)
@filter_.public & filter_.user("123456789")
async def on_msg(session: Session):
    await session.send("hi")
```

配置文件中用 `$filter` 表达式，避免在源码里硬编码账号：`$filter: channel.type is public and user.id in ['123']`。可用变量：`channel`、`member`、`guild`、`user`、`env`、`message`、`platform`；支持语义运算符 `eq`/`gt`/`nin` 等、`regex(...)` 函数；禁用乘法/幂/整除/位运算。

### 配置模型

```python
from arclet.entari import BasicConfModel, plugin_config


class MyConfig(BasicConfModel):
    foo: str
    bar: int = 42


config = plugin_config(MyConfig)
```

`BasicConfModel` 通过类参数 `extra="forbid" | "allow" | "ignore"` 控制额外字段。也可用 Pydantic `BaseModel`（`from arclet.entari.config.models.pyd import BaseModel`）或 msgspec `Struct`。

### 数据存储与定时任务

- `.localdata` 插件 + `local_data`：`get_cache_dir()` / `get_data_dir()` / `get_temp_dir()` / `get_xxxx_file(...)`；本地插件统一用它管理持久化路径，不要自造路径。
- `.scheduler` 插件 + `scheduler.cron("0 0 * * *")` / `scheduler.every(5, "minute")` / `scheduler.invoke(10)`（延时任务）。
- 跨重启的状态用 `keeping("name", default, dispose=...)` 包装；插件被卸载时仍保留。

### 副作用与热重载

- 热重载：启用 `::auto_reload`（`watch_dirs`、`watch_config`）。
- Entari 会自动清除事件监听器、指令、上游插件导入等副作用；其他手动副作用用 `collect_disposes(lambda: ...)` 注册清理。
- 任何在运行期会被多次加载/卸载的插件都必须保证幂等清理：全局可变状态要么用 `keeping`，要么在 `collect_disposes` 中还原。

### 服务（Launart）

跨插件能力以 `launart.Service` 子类暴露，通过 `add_service(...)` 注册，其他插件按类型注入。新增服务型能力（浏览器、HTTP 客户端池、模型推理等）优先走 Service 而非全局单例。

### 消息链与自定义元素

- `MessageChain` + 元素 `At`、`Image`、`Text`、`Quote` 等；支持 `+` 拼接、`in` 检测、`[Element]` / `.get(Element)` / `.include(Element)` / `.select(Element)` 提取、`.map(fn)`。
- 自定义渲染元素：`@plugin.component("greet")` 注册后，消息中可用 `<greet name="..."/>` 标签。

## 开发规范

### 代码风格

- 行长度与格式遵循 `ruff` 与项目已有设置。
- Python 目标版本：3.14。
- Ruff lint 规则见 `pyproject.toml` 的 `[tool.ruff.lint]`。
- Pyright 使用 `typeCheckingMode = "standard"`。
- 保持现有代码风格：异步函数、配置模型（`BasicConfModel` 优先，跨框架兼容场景用 Pydantic）、短中文注释风格。

### 插件分层与类型边界

- 插件入口文件保持轻量：`plugins/<name>/__init__.py` 只放 `metadata(...)`、`plugin_config(...)`、`Plugin.current()`、注册函数调用和必要日志；复杂业务必须拆到职责明确的子模块。
- 可被测试、复用或被 Pyright 独立分析的纯逻辑，放到 import-safe 包中，例如 `utils/<domain>_core/`。这些 core 包不得导入 `arclet.entari`、`entari_plugin_llm`、`entari_plugin_database`、`launart`，也不得执行插件注册、服务注册或其他运行时副作用。
- Entari/LLM/数据库/HTTP 等动态边界应与纯算法分离；小插件可以在同一 runtime 模块内组织相关 handler、command、tool 或轻量 IO，但当一个模块同时承载多个变化方向（如事件注册、外部 API、持久化事务、渲染、复杂算法）或文件明显膨胀时，必须按职责拆分。
- 外部 JSON、LiteLLM response、SQLAlchemy row、插件 `_extra` 等动态对象必须在边界处用 `Mapping[str, object]`、`dataclass`、`TypedDict`、`Protocol`、`TypeGuard` 或局部 `cast(...)` 收窄；核心算法不得把 `Any` 贯穿到底。
- WebUI 通过 `metadata(..., config=...)` 为插件配置生成 JSON Schema：`BasicConfModel` 字段必须使用 Entari `SchemaGenerator` 可表示的类型，递归 `TypeAlias`（如 `JsonValue`）不得直接作为配置字段注解；配置边界可用 `dict[str, Any]`，核心 provider 仍以 `JsonObject` / Protocol 收窄。使用本地化 Pydantic adapter 时，每次新增运行时配置字段必须同步翻译映射，并由 metadata schema 回归测试覆盖，生产部署也必须同步 `config_schema.py`。
- 测试直接导入 import-safe core 包，不通过 `sys.path.insert(...)`、synthetic package alias 或文件级 Pyright suppress 绕过插件副作用；pytest 的 import 根通过 `pyproject.toml` 配置。
- 新增 provider / client 类必须支持显式依赖注入测试 seam（例如可传入 HTTP client/transport），测试不得改写私有属性。
- 对会被 `::auto_reload` 重复加载的运行时副作用，注册时同步考虑清理：长任务用 `collect_disposes(...)` 取消，跨卸载状态用 `keeping(...)` 或明确的持久化存储。
- 结构质量门槛：入口文件原则上保持在 120 行以内；本地插件生产文件原则上保持在 250 行以内。超过不是硬错误，但必须能用单一职责解释其存在；否则优先按 `config` / `schemas` / `data_source` / `client` / `render` / `listener` / `command` / `runtime` / `utils` 等自然边界拆分。

### 基础建设

- 项目引进了 `entari-plugin-browser`、`entari-plugin-htmlrender`、`entari-plugin-llm`、`entari-plugin-database`、`entari-plugin-permission` 作为基础建设。既有帮助菜单和状态页继续复用 `entari-plugin-browser`；通用 HTML、Markdown、受控 Jinja 模板转图片优先使用 `entari-plugin-htmlrender` 的 `HtmlRenderer` / `HtmlRenderService`，AI 会话调用、数据库及 ORM、权限管理分别复用其余插件。
- `plugins/webui_config_apply` 接管 WebUI 的两个原生配置 PUT 入口，不注入前端脚本、常驻浮窗或独立重启菜单。`saving.py` 以运行基线比较完整候选，仅允许已加载 LLM 的 `api_key`、`base_url`、`prompt` 与 `models` 变化热生效；工具集合、其他插件、基础配置、适配器及未知字段仍走受管重启，控制器不可用时在写前拒绝，跨文件写入仍需受控部署。`utils/webui_config_core` 恢复当前源文件的环境引用并规范化继承，运行基线和候选统一采用安全解析结果，避免 YAML 引号、多行字符串及数字包装类型误判；不得把布尔值与数字视为相同配置。`persistence.py` 保留文件 inode，并在运行发布前完成写入、fsync、关闭和分离副本解析；失败恢复原字节，不覆盖无法确认归属的外部写入。模型和持久化容器发布成功后才推进 `running_sha256`；退出保存必须保留新配置及环境引用。所有入口保持认证、同源限制和错误脱敏，进程尚未加载的新环境变量不得假装已生效。
- `model_reload.py` 复用已加载上游 `Config` 的字段类型与校验，不加载第二套模型 Schema，也不向全局事件广播解密凭证；允许 Entari YAML 的 `CommentedMap` 配置，但运行对象的发布仍限于已验证的普通字典引用。`utils/llm_model_core` 统一保存模型选择修复和轮次快照逻辑：先原子修复默认模型及会话模型、保留指针和未知字段，再替换现有 `_conf` 对象的完整字段及可见插件配置；不得修改旧模型或其嵌套参数。主聊天入口固定当前主模型，Agno 兼容边界和无工具 finalizer 在纠正重试中复用该快照，其他轮次互不影响。Entari `0.19.0rc2` 本身支持普通、绑定配置和 Service 插件热重载，不能仅因存在 Service 就判定必须重启；但上游 WebUI `1.0.3` 保存不发布 `ConfigReload`，LLM `0.2.2` 原生监听器仍不完整，不能绕过本地保存边界。隔离实验使用真实 `it(Launart)` 并将 Letoderea effect 绑定到当前循环。`scripts/chtholly_config_apply.py` 与插件须配套部署：五次连续健康、运行/磁盘摘要及插件检查通过后，热更新只推进 last-good；显式重启请求仍重启，验证期间的新保存交回控制循环，不得被旧候选误回滚。
- 生产控制器安装在 `/usr/local/sbin/chtholly-config-apply`，systemd 单元必须显式设置 `Environment=PYTHONPATH=/opt/chtholly`；`WorkingDirectory` 不会把项目根目录加入直接执行 Python 脚本的导入路径。部署后必须经真实 `chtholly-config-apply.service` 验证导入和热更新确认，不得只在已注入 `PYTHONPATH` 的测试进程中检查。该设置不改变源码只读边界或配置文件白名单。
- `plugins/tts_service` 以 Launart Service 暴露 GPT-SoVITS GSVI / Fish Audio 合成能力；GPT-SoVITS provider 通过带 Bearer 鉴权的 `/version`、`/models/{version}` 与 `/v1/audio/speech` 动态发现并校验版本、角色模型、参考语言、情绪、合成语言和语速。`llm_chat` 的 `list_tts_voices` 是只读目录工具；用户指定角色、版本、参考或情绪时必须先读取目录，再把精确选项传给 `synthesize_speech`，目录缺项不得替换或猜测。生产 GSVI 以进程级推理锁串行执行，请求超时必须覆盖排队、权重切换和推理；短句使用 `text_split_method: "不切"` 避免按标点拆成多次串行声码器推理，当前生产超时为 `900` 秒。`synthesize_speech` 只准备内联 `data:audio/*;base64` 资源并返回本轮 `media_ref`，由 `send_msg` 显式交给 Satori / OneBot，禁止传递仅 Chtholly 主机可见的 `file://` 临时路径；协议端确认发送成功后才能写入语音历史 marker。
- TTS 目录与默认选择分离：默认声音、版本或参考失效时，目录仍返回真实资源，`default_selection=null` 并附脱敏 `default_selection_error`；合成仍严格检查本次最终选择，不将显式不存在的选项替换为其他声音。HTTP 或损坏目录继续报错，Fish Audio 语义不变。
- `llm_chat` 使用 `default_persona` 和 `personas` 配置多个角色；每个 `PersonaConfig` 包含 `name`、`prompt`、`appearance` 与可选的 `reference_image`。旧顶层 `persona` / `self_reference_image` 已移除，配置迁移必须保留原人格文本、口吻与对应参考图。角色口吻与情景表达由用户填写的 `prompt` 定义，默认人格不得额外拼接剧团式口吻、情景对白或固定说话示例。通用 `SYSTEM_SCAFFOLD` 不得包含固定角色身份、服饰、撒娇口吻或媒体偏好。每轮捕获当前角色的参考图配置，不依赖聊天模型是否支持视觉，也不向聊天消息注入角色像素。角色相关生图或编辑通过 `use_persona_reference=true` 按需加载真实字节并直接交给独立图像模型；无关主体不得附图，指定参考不可用时明确失败，不能悄悄退化为文字生图。输入只在 generation-local 状态中复用，受 `resources/image`、6 MiB 与 JPEG/PNG/WebP/GIF 边界约束，不进入聊天历史或用户图片收藏。
- 仓库角色配置保留默认 `chtholly`，并提供按 PRTS 档案、语音、模组故事与活动剧情整理的 `pepe`；佩佩的原版初始立绘保存为 `resources/image/persona/Pepe.png`，只在该角色的参考图配置中使用，不加入表情目录。
- `personality.py` 以独立 ORM 表保存 ChatScope 的角色选择和 ContextSession 的不可变人格快照，角色身份、人格文本、外观及参考图共同进入 baseline 指纹；历史会话不得被当前配置重新标记。`llmchat persona` 列出角色，超管可用 `llmchat persona <key>` 在当前范围原子切换并创建无 handoff 的干净话题；选中相同有效角色不重复建会话。准备阶段与切换共享短作用域锁，释放后各成员的模型生成仍可并行；每轮及其后台 evaluator 使用捕获的人格文本。关系、画像和长期记忆仍保留既有存储语义，不因换角色清空。
- `llmchat session` 只读查询已有作用域与活动会话，不创建数据库行或调用模型。`session_inspection.py` 区分逐请求实际 Token、旧版整轮统计和上下文估算；未知值为 null，缓存及推理 Token 是子项而非额外总量，真实请求不得与整轮 `model_attempt` 重复累计。列表及用量查询只提取必要 JSON 字段，不为计数加载全部模型输入。嵌套 PersonaConfig 的本地化 schema 必须内联到 Pydantic 适配器，不能直接返回未注册到 Pydantic 图中的外部 `$defs` 引用；当前 Entari 的 `extra="forbid"` 仅体现在生成 schema，不能假设其运行时会拒绝所有未知键。
- 帮助菜单，当前项目拟参考 [`nonebot-plugin-picmenu-next`](https://github.com/lgc-NB2Dev/nonebot-plugin-picmenu-next) 的菜单功能，结合 entari 基建，实现一个自动生成、界面美观、自定义程度高，开发简单的图片帮助基建插件。
- 会话互动系统：`plugins/llm_chat` 已基于 LLM 插件实现公开群聊人格对话，用户轮次使用 `speaker` / `content` JSON 区分多人发言，图片继续走独立视觉链路。主聊天只接收按类别筛选的画像值和相关记忆，关系 evaluator 接收 canonical 画像与 aliases；语义分组和去重只构造可逆读取视图，持久化继续使用 exact key 并保留原始数据。表情、预录语音、TTS 与白名单插件命令均通过实际注册工具按需调用。模型使用 `send_msg` 按有序 segments 发送一条原生 MessageChain，自主决定文字、艾特、图片的位置和消息边界；OneBot 合并转发先经 `prepare_merged_forward` 准备，再由 `send_msg` 单独发送。不支持的组合在发送前拒绝，不自动拆条或回退，未知传输错误、超时和取消不得重发。成功消息按真实顺序投影为一个 assistant 历史行，媒体 marker 留在原消息位置，不提前写入或重复持久化；生成、最终发送或取消失败只尽力保存已确认前缀，不启动 evaluator 或关系更新。发送额度、节拍与媒体上限通过独立 generation-local `DeliveryState` 在运行时、system prompt 和工具处理器之间共享同一组规范化限额。网页能力固定通过 Agno `ExaTools` 提供 `web_search` 与 `read_web_page` 两个只读工具：时效问题按需使用 Exa 搜索，公开页面通过 Exa Contents 获取限幅正文；独立的 generation-local `ContextVar` 同时隔离授权域与 effective budget，运行时、system prompt 和 tool schema 必须共享同一组规范化限额。所有目标必须经过公开 URL 与敏感 query 校验，网页摘要和正文始终视为不可信数据，不得扩大工具权限、覆盖系统规则或索取隐私。公开群聊由 priority `900` 主处理器接管，并以 priority `999` claim guard 在原生 priority `1000` 自动对话前硬阻断失败穿透；精确工具循环耗尽仅允许基于已积累 transcript 执行一次无工具 finalizer，且不得复述已成功发送内容或部分回退已确认前缀；其他生成或最终化失败均记录脱敏 warning 后静默 `BLOCK`。
- `llm_chat` 公开群聊轮次按 `platform + account_id + channel_id + platform_user_id` 执行 participant latest-wins：只有同一成员在同一频道的新已寻址轮次会取消其仍在生成的旧轮次，不同成员的轮次必须并行存活，禁止跨成员抢占。被同一成员后续消息替换的旧轮次只保留已确认交付前缀并跳过 evaluator；priority `999` claim guard 必须无条件阻断原生 priority `1000` 自动对话，避免未寻址消息另起第二套模型回复。
- `llm_chat` 出站由 `utils/group_delivery_core.py` 的纯调度队列和 `group_delivery.py` 的原生运行边界按 `platform + account_id + channel_id` 协调，模型、搜索与媒体准备仍按成员并行；只在真正可发送时选择就绪消息，不为尚未提交或延迟未到的续条占位。每组最多连续 3 条或自首条开始 4 秒，达到边界时让其他就绪轮次公平轮转；共用原有规范化节拍，缺少现成续条立即让位。公平轮转边界不等于引用边界：同一轮未被打断的连续消息只在首条引用，达到条数或时间边界、等待模型或媒体均不重新引用。切换发言轮次、同频道其他成员插话或无关插件出站后恢复时，再用认领时固定的原入站消息 ID 添加原生 `Quote`；不让模型提供 ID，不额外艾特，也不把引用标记写进 assistant 正文历史。`reply_payload.py` 避免 OneBot 语音、视频、文件和合并转发被拆出空引用：保持原生载荷，后续确有正常文字或图片时再引用；整轮仅含这些媒体时直接完成，不生成额外归属文字、空引用或占用文字额度。实际发送限时 30 秒，排队取消不算发送尝试，传输取消或超时保留未知结果且不重发；轮次结束拒绝迟到子任务发送，卸载取消等待者、传输及定时器，旧运行边界清理不得移除替换边界。
- `llm_chat` 用单一可替换的 QQ 系统表情提供粗粒度进度：处理、思考、读取、媒体生成和可恢复错误只保留当前状态；成功、部分交付、失败、明确拒绝及 latest-wins 取代使用固定终态。主动沉默清除瞬态并标记已结束，不追加失败或拒绝表情。表情 ID 是 `reaction_feedback.py` 内部常量，不进入配置；调用短超时、fail-open，不占交付额度，不进入历史、模型或 evaluator。后续同成员轮次保留旧消息的取代终态；卸载只清除未结束的瞬态。
- 生产 `LLOneBot` 的 `set_msg_emoji_like` 请求参数固定为 `message_id`、`emoji_id` 与 `set`；当前 Satori OneBot11 适配器的 LLOneBot 分支仍发送旧 `emoji` 参数并通过独立 `unset_msg_emoji_like` 删除，会被 LLBot 8.1.7 以 `$.emoji_id missing required key` 拒绝。`reaction_feedback.py` 必须按账号缓存 `get_version_info` 结果，仅对 `app_name == "LLOneBot"` 使用内部接口的真实契约，其他 OneBot 实现与非 OneBot 平台继续使用 Entari 通用 reaction API。
- `protocol_compat.py` 修复 Entari 0.19 API instrumentation 对 `internal(..., **kwargs)` 的错误绑定：调用已绑定的 `*bounds.args` / `**bounds.kwargs`，不能把参数再包进顶层 `kwargs`，否则 reaction、合并转发与戳一戳均会缺少真实参数。内部请求保留 `APIRequest` 拦截和 `APIResponse` 观察，失败通知后重新抛出，禁止返回异常对象伪装成功；兼容层覆盖已有及新建账号，重复安装与清理保持所有权，普通 API 和第三方安装文件不变。
- 群聊感知基础设施由 `plugins/channel_perception` 的 Service 接收消息、更新、撤回、成员变化与真实 `SendResponse`，监听器仅标准化并入有界队列。数据按 `platform + account_id + channel_id` 隔离，默认每频道保留 7 天、500 条消息及 90 天、1000 名参与者。原生艾特的有序身份保存在可空 `mentions_json`，启动写入前执行幂等加列迁移；旧行保持未知，不按显示名回填，纯文字伪艾特不算身份。更新替换元数据，撤回清空正文和艾特，查询逐次检查保留期、条数、命令和删除状态。只保存正文、图片数量、原生身份关系，不持久化协议图片 URL；历史图片仍通过当前频道 `Session.message_get(...)` 定位原消息，只提取顶层图片。内部身份用 `User.id`，展示名与 `participant_ref` 分离；Bot 出站消息 ID 必须来自真实回执。
- 群聊感知依赖 `entari-plugin-user`，因此 `database.create_table_at` 固定为 `preparing`；`channel_perception` 在早期 `Ready` 优先级等待数据库服务进入 `blocking`，避免全新数据库首启时用户插件先于建表同步超管。
- `llm_chat` 群聊感知边界：统一成员身份及原有关系迁移语义不变。只在当前任务确实依赖群聊现场时调用 `read_channel_messages`；明确询问近期群消息时排除普通 addressed history 并先读取现场，证据不足再分页，不无目的遍历建立档案。工具返回 generation-local `message_ref`、`reply_to_ref`、结构化 `mentions` 与 `image_ref`，`next_cursor` 是独立分页能力。页外回复用精确 `message_ref` 模式读取，批量解析回复目标并在输出截断后重算状态；撤回、过期或命令目标不可读。旧艾特元数据为 null，而非确定没有艾特；同名、重复、Bot、全体和角色类目标不得混同。
- 频道工具为 `find_channel_participants`、`read_channel_messages`、`get_channel_avatar`、`inspect_image` 与 `prepare_image_ref`。头像获取只取得一次原图快照，不强制识图；需要视觉细节才调用 `inspect_image`，视觉聊天模型在完整 tool results 之后通过真实 Agno/LiteLLM 多模态续跑接收像素，非视觉模型使用明确标注的派生描述。原图发送和编辑直接使用同一快照。旧 `describe_channel_image`、`describe_channel_participant_avatar`、`prepare_channel_image` 及分散注册器已删除，无别名。所有能力限当前账号、频道与 generation，不进入 evaluator、画像或长期记忆，也不得向用户暴露引用、平台 ID、私有 URL 或数据库字段；姓名查询沿用有界目录及能力缺失错误规则。
- `llm_chat` 的 generation-local 正常任务额度默认与生产统一为 `web_search / read-or-screenshot / total = 16 / 24 / 32`、网页正文 `16,000` 字符、媒体消息 `6` 条，Agno 工具调用硬上限为 `64`。这些额度只用于阻断失控循环，不得让明确的多页面研究、三至六张图片交付等正常任务先天无法完成；网页公网校验、敏感 query 拒绝、浏览器 SSRF 防护、单资源或总下载量和请求超时仍是独立安全边界，不得因提高业务额度而放宽。
- `llm_chat` 人格作息使用 `Asia/Shanghai`，`energy_at` 只接受带时区的时间，每轮仅采样一次供 prompt 与审计使用。精力和关系影响自然表达，不生成档位或缩减规范化交付额度（媒体默认 6 条）；是否回应由模型决定，合法拒绝只发送拒绝文字。`html2pic` 预览与 `markdown2pic` 源码图是不同交付项，`md2pic` 只映射既有 `markdown2pic`，不新增别名。命名渲染与自然语言转图片复用本轮媒体意图判定；媒体与文字顺序由模型编排，MIME、6 MiB、渲染及网页安全边界不变。
- `llm_chat` 网页作品工具为 `publish_web_preview`、`prepare_artifact`、`list_web_artifacts`、`read_web_artifact` 与 `revoke_web_preview`。模型根据任务与上下文自主选择生成预览、准备源码及查阅已有作品，前四个工具不得用当前原话的关键词、固定措辞或单独操作口令作为执行门禁。运行时继续校验有效 generation、ChatScope、当前统一用户 ID、manifest 路径、容量及交付额度；超管只能在当前 Scope 内读取或撤销其他成员作品。只有破坏性的撤销仍依据 `AgentAccessContext.raw_user_text` 中明确且非引用、非否定的当前请求授权。`is_artifact_request` 仅作超时、媒体额度与图片流程分流的提示，未命中不得拒绝工具调用；提示需支持中文紧邻 UI/HTML/CSS。模型必须尊重明确的排除要求，把引用、历史与网页当作参考数据，禁止公开密钥或私人聊天/画像。网页设计优先完整交互预览与源码 ZIP，不用代码图片冒充源码文件；已识别作品任务使用媒体超时、关闭自动重试和同批并行发送，并保留完整媒体额度。
- 原始版本读取不依赖私有调用审计：`read_web_artifact(mode="manifest")` 从不可变 manifest 分页列出完整文件清单，`mode="file"` 按精确路径读取原文。`list_workshop_plugins` / `read_workshop_plugin` 查询精确工坊版本的 metadata、manifest、files、file、report、log；报告续页校验摘要防止重验串页。普通成员只读当前 Scope 自己的版本，聊天管理员也限当前 Scope，原有全局 WebUI 管理语义不变。读取校验版本、哈希及既有过期/撤销规则，不获取审批或激活权限，模型历史只保存限幅版本引用。
- 作品权威原文保存在 LocalData 的 `llm_chat/web_artifacts` 中；仅管理员可读的调用快照允许保存脱敏后的源码输入输出副本，不作为作品版本存储或模型历史来源。`utils/web_artifacts_core` 使用独立 SQLite 目录保存不可变版本、权限、过期与容量状态，源码 ZIP 必须与已发布文件逐字节一致。项目版本高水位独立于已删除版本，发布与崩溃恢复共享写事务边界，未登记目录残留也计入磁盘额度。`llm_chat.web_artifacts.service` 在 Launart preparing 阶段初始化并清理过期作品，关闭时必须等待正在进行的存储操作与取消补偿。
- `utils/web_artifacts_server` 是独立只读进程；公开路由仅允许作品页面、当前版本素材、缩略图和 ZIP 的 GET/HEAD。文件与 iframe 同时应用不含 allow-same-origin 的 sandbox，CSP 只允许当前作品资源；直接打开活动 HTML/SVG 必须回到可信预览外壳。服务端截图必须使用独立 Chromium、开启 Chromium sandbox，并由 Linux user/network namespace 和 systemd 只读目录、进程及资源边界隔离；不得复用 Bot 主进程浏览器执行作品脚本。内部截图接口只接受回环 Bearer 请求，代理不得公开它；所有公共路由逐次检查过期/撤销并禁用缓存。
- 模型可读的作品审计只保留引用、版本、大小、哈希、过期时间与真实副作用状态，不回放源文件映射；管理员调用详情同样去除 base64、公开能力链接及内部截图密钥。源码使用 `File.of(raw=..., mime="application/zip", title=...)` 准备内联文件，`send_msg` 确认后才保存文件历史 marker；发送边界不自动回退成链接或拆条，未知传输失败不得重发。发布操作提交或缩略图准备期间取消时，须等待存储结束并撤销尚未返回给模型的本轮新作品，保留真实副作用审计；完整返回的发布是持久副作用，不因模型未发送某张缩略图自动撤销。
- `llm_chat` 工具按职责拆分在 `tools/`：`send_msg` 是唯一模型控制的发送入口，严格 segments schema 和单次消费的 `media_ref` 保留。`prepare_image`、`prepare_image_ref`、`prepare_external_media`、`prepare_audio`、`synthesize_speech`、`prepare_artifact`、`prepare_merged_forward` 以及生图、编辑、渲染和截图均只准备资源；`list_prepared_media` 不发送。`tool_runtime.py` 只装配注册，公开资源下载继续校验全部公网 DNS、重定向、无代理、无凭证、无本地路径；图片限 6 MiB JPEG/PNG/WebP/GIF，其他媒体限 10 MiB。三类渲染继续保持 480–1200 像素、30 秒、6 MiB 和 50,000 字符边界，拒绝脚本、任意资源和任意 Jinja 模板路径。
- `generate_image` 通过独立 `image_generation_model` 创作新图，可选本轮 `reference_image_refs` 与角色参考；没有用户源图也能使用已核验网页原图创作。有像素输入走 LiteLLM Images Edit API，无像素走 Images API，供应商接口名称不决定业务是否编辑。`edit_image` 必须使用 `source_image_ref`，第一张输入固定为该原始快照，其后才是视觉参考与选用的角色图；直接、普通引用、授权转发、频道历史和头像都可作源图，网页和角色图仅可作参考。质量、尺寸、超时、6 MiB、SSRF 及交付额度不变，不用描述重构代替真实像素。
- `ImageInputs` 在图片感知前登记原位置，以单次、可取消等待但不互相取消的加载任务取得不可变快照；识图、编辑、原图发送、收藏和审计共享字节。单轮缓存限 60 MiB，按字节去重不合并来源权限，不逐出后重新获取；下载失败保留序号且不重试，审计失败只标未记录，不阻断安全像素执行。输入不从审计附件反向加载，关闭轮次取消并等待加载，收藏后台任务仅接管所选快照而非整个 ContextVar。`tag_image.image_index` 仍按 direct-first / quoted-second 原始序号选择。
- `MediaIntent` 只从当前用户未拼接的原文构造，源图编辑与真实网页参考是独立条件；OCR、描述、引用、历史和工具结果不能授权，否定及后置取消有效。承接式发送仅可用最近上下文解析媒体目标，不能扩张截图或参考捕获权限；超时提示不兼任授权。
- `html2pic` 在安全解析后通过 `PreparedHtml` 注入文档级自动高度样式并调用 `rasterize_prepared`，覆盖模型常见的 `html/body { height: 100%; overflow: hidden; }`，避免 Playwright 以 10 px 探测视口做全页截图时只留下背景色；固定尺寸和裁切应放在 `body` 内层画布。
- 三类 LLM 渲染工具的默认字体栈固定为 `Inter, Noto Sans SC, Noto Sans CJK SC, sans-serif`：Inter 负责拉丁字符，`Noto Sans SC` 为首选中文族名，Linux 生产环境以已安装的 `Noto Sans CJK SC` 兼容回退；生产主机必须安装 Inter 与 Noto Sans CJK SC，不依赖浏览器偶然回退到文泉驿或 Arial。
- `screenshot_web_page` 通过 `entari-plugin-browser` 的隔离 Playwright context 截取公开网页概览或可见标题区段，并复用 generation-local read 网页预算与媒体交付额度。`plugins/llm_chat/web/public_resolver.py` 用 aiohttp 自定义 resolver 固定已校验公网 IP，`web/safe_browser.py` 必须拦截并代理全部页面、重定向和子资源请求，阻断混合私网 DNS 回答、DNS rebinding、WebSocket、下载、Service Worker 与非只读 HTTP 方法；单页仍受 192 次请求、6 次重定向、单资源 8 MiB 和总下载 32 MiB 的硬限制。第三方 `browser.page(...)` context 会吞掉 body 内异常，安全包装必须先捕获、完成页面与 aiohttp 清理后重新抛出，禁止让捕获函数隐式返回 `None`。`web/screenshot_dom.py` 只负责页面可见文本定位和有界区域计算；标题只形成过窄区域时，必须扩展到最近且高度增长有界的祖先内容框，避免只截标题或裁掉同级侧栏。截图区域内的懒加载图片只允许从 `data-src` / `data-original` / `data-lazy-src` 物化为 HTTP(S) URL，并继续经过同一安全 route；加载后必须重新等待并计算区域，禁止旁路下载或把占位图当成完成结果。`web/screenshot.py` 只负责编排页面稳定与 PNG 截图，工具入口固定为 `tools/screenshot_web_page.py`。不得退化为直接 `page.goto(...)` 或仅校验初始 URL。
- `screenshot_web_page` 仅在当前用户本轮明确发出截图、截屏或简短“截”等操作指令时获得 generation-local 执行授权；当前轮祈使表达可以承接对话中已明确的公开页面目标，但引用、合并转发、历史或模型自行生成的工具参数只能帮助解析目标，不能单独扩大权限。找图、照片、Cos 图、插画、壁纸、素材和原图请求不得用网页截图兜底；未授权调用必须在浏览器启动和媒体额度预留前拒绝。
- `capture_web_reference` 只在当前用户本轮明确要求从公开网页搜索或获取真实视觉参考并用于图片生成或编辑时获得独立 generation-local 授权；普通找图、历史、引用、转发或模型自行生成的参数不能授权。它复用 read 网页预算与 `PublicResolver` / 安全浏览器边界，不占媒体额度，不向用户发送参考图。参考捕获与 `edit_image` 必须和其他发送副作用按模型顺序串行，避免同批调用在引用签发前开始编辑。
- 受来源约束的生成或编辑必须由 `PreparedMedia` 的宿主 `ImageProvenance` 证明实际上传了要求的源图/网页参考，`send_msg` 仅在真实回执后确认该要求完成。此前不得以无关图片、原生图片输出或独立文字冒充完成；同链可附必要文字，拒绝发生在额度预留前。捕获、生成、编辑与发送保持模型顺序，准备不等于交付，取消或未知传输结果不重发；一次工具纠正和 `finish_turn` 共用该完成状态。
- `llm_chat` 当前入站消息中显式艾特 Bot 之外的成员时，按消息顺序写入 user JSON 的 `mentioned_participants`；每项包含展示名，能解析时还含当前账号与频道的稳定 `participant_ref`，不暴露裸平台 ID。入站排除 Bot 自身并按平台 ID 去重；模型用该结构理解代词，已有精确引用时不重复姓名搜索，仅有展示名时先唯一解析。出站艾特由 `send_msg.segments` 的 mention 元素承载，保留模型指定的位置、空格和重复片段，每条最多 3 个艾特片段。当前说话人使用 `current_user`，其他成员只能使用当前频道上下文或 `find_channel_participants` 唯一解析出的 `participant_ref`；通过 `channel_perception.refresh_participant(...)` 解析真实平台 ID 并发送原生 `At`，禁止裸 ID、猜测引用、正文伪艾特及艾特 Bot 自己。点名、召唤、交接和多人消歧可自主艾特，普通答复不机械艾特。可见历史在原位置保存 `@展示名`；工具投影仅保留有界片段和计数，不持久化或回放能力引用。
- `llm_chat` 网页模块分层：`web/policy.py` 只承载 provider-independent 的授权、预算、输入规范化与公开 URL 校验；`web/exa.py` 只适配 Agno `ExaTools`；工具实现分别位于 `tools/web_search.py` 与 `tools/read_web_page.py`，配置门控位于 `tools/web.py`；`web/__init__.py` 保持无副作用且不聚合 provider 符号。内部调用导入最窄职责模块，避免 provider 依赖反向渗透到聊天编排层。
- `llm_chat` 消息处理分层：`chat_handler.py` 只负责入站解析、模型调用、交付分支和顶层编排；`channel_turns.py` 负责同频道同成员的最新轮次取消，并保证不同成员之间互不抢占；`agent_turn_setup.py` 统一装配关系、记忆、Session baseline、Token 上下文、generation-local 授权与 `ActiveChatTurn`；`turn_lifecycle.py` 统一负责 user turn 回滚、confirmed-delivery 持久化、最终文本发送和 AgentEvent 最终化；`chat_evaluation.py` 只在完整且合格的轮次最终化后调度关系与长期记忆后台评估，主动沉默和明确拒绝同样保留归属正确的证据。禁止在 handler 中重新实现第二套上下文、失败生命周期或 evaluator。
- `ChatScope -> ContextSession -> AgentTurn -> AgentEvent` 是上下文与工具审计权威，旧 Conversation 仅作可见物化视图。完整 tool call/result 使用同一 execution_ref，失败和取消不伪装成功；大型模型可读原文压缩为 event/path/哈希描述符，密钥、像素、私有路径和思维链不持久化。Token 预算按完整 Turn 选择，模型、人格、schema 或阈值变化继续正常 handoff rollover；hard reset 只封存而不删审计。`collect_context_read_grants` 只对本轮实际选中的宿主描述符、有效 anchor 和经验证的直接前驱 handoff 引用签发精确 generation-local 读取许可，`read_agent_event` / `read_tool_execution` 无需魔法措辞即可读取该 event/path。每次复核 Scope、会话未封存、可见性、摘要和授权轮次；模型自行写出的引用、兄弟或更早会话链、管理员审计及附件不获权。其他历史查询和 pin 仍受原有当前原文授权约束，引用不对用户复述、不进入画像或长期记忆。
- `llm_chat` 历史工具上下文重建：同一模型轮次内连续发起的并发 tool calls 必须合并为一个 assistant `tool_calls` 消息，随后按实际完成顺序附上全部匹配的 tool result；不得在首个结果前插入第二个 assistant 消息。没有匹配结果、重复 ID 或跨轮错配的调用必须从模型上下文中省略，避免 OpenAI-compatible 上游以 `No tool output found for function call` 拒绝整个后续请求；审计事件原始顺序保持不变。
- `model_audit_runtime.py` 在本地 Agno/LiteLLM 请求执行域中捕获实际 HTTP 请求及响应，供应商重试使用独立 request ID；无可观测 HTTP 的自定义客户端明确标记 SDK 边界。`model_request`、`model_response`、`context_snapshot` 均为管理员专属事件；工具的完整脱敏输入输出位于 `audit_arguments` / `audit_result`，原有模型可读投影不变。请求、工具开始和结果增量持久化，已提交及提交中的序号冻结，失败与取消不得重放副作用。模型历史查询、pin 与 handoff 必须排除管理员事件和私有字段。WebUI 按真实 request ID / execution_ref 配对，历史缺失标记未记录，完整 JSON 与文本通过有界分页读取，不能用当前配置伪造历史注入。
- 私有图片审计从 `ImageInputs` 快照复制字节，绝不重新下载；user_input 保留最多 6 个直接/普通引用原位置及 32 个转发位置的来源、序号、状态和可选 input_* 附件。图像工具按供应商实际输入顺序记录源图/参考图与 output_* 结果，缺失审计显式显示未记录，获取失败显示原图不可用。工具卡片使用结果事件引用加载附件，不能把准备结果当已送达；旧记录继续只读展示。所有图片仍经原会话认证、所属事件检查与 opaque iframe API bridge 读取，不持久化 image_ref、协议 URL、base64 或路径，也不向模型历史、handoff 和原文工具暴露 attachment ref。
- `delivery_audit.py` 在每轮入站处理及准备阶段前建立独立 ContextVar，并绑定该轮 AgentTurnRecorder；只捕获同账号、同频道、同源入站事件且具有非空回执的 `SendResponse`。发送回调同步采样确认时刻，再将有界内容与图片捕获交给按回执顺序执行的受管任务，不阻塞发送或吞掉 generation 取消；最终化必须 shield 并 drain 已确认审计。`delivery_projection.py` 只负责有界消息元素投影。新增 `turn_timing` 和 `message_delivery` 均为管理员专属事件，不替代模型可读的 assistant 历史聚合，不进入模型历史、pin、handoff 或 evaluator。图片复制到现有私有 `output_*` 附件，公开原生图片 URL 仅经原有 SSRF 安全下载且每次回执共用一秒补抓预算；语音、文件、视频及未捕获图片明确标注，绝不保存资源路径、协议 URL 或 base64。总耗时取最后一次真实发送确认时的单调时钟差，包含准备阶段但不包含后续评估；旧记录的生命周期时间只能标为估算。
- `llm_chat` 普通引用归属：普通回复必须将原消息作为独立 `forwarded_messages` 引用上下文保留，即使原消息只有图片也要保留 `[Image]` 占位；可选 `speaker_role` 使用 `assistant` / `participant` / `unknown` 明确原发送者角色。QQ 引用中的 `Author` 可能只提供 username，判定当前 Bot 时需同时比较 account self ID 与自身 user name。直接图片才属于本轮当前说话人；引用图片 marker 必须区分当前 Bot、其他成员或未知来源，禁止把旧图片、语气和观点归因给本轮用户。
- `llm_chat` 消息边界由模型决定：每次 `send_msg` 只发送一条完整原生消息链，不插入空格、不前置艾特、不根据换行拆条、不强制媒体先行或自动合并转发。回答有独立的结论、理由、建议、限制或后续反应时，应多次调用 `send_msg` 分成自然完整的聊天气泡，不因统一入口而挤进一条；短而完整的回答仍可单条，不机械拆每句话、不补水话或凑固定条数。最终普通文本只用于单条短答或必要的新补充，保留内部换行，不承载挤成一条的多部分回答，也不得重复已确认内容。OneBot 可混排文字、艾特和图片；会被当前适配器拆分的语音、视频、文件或合并转发组合必须在发送前拒绝，由模型选择独立调用。群级调度、原入站引用、公平性、额度及 unknown-outcome 不重发规则不变。
- `llm_chat` Markdown 与代码交付：结构化长内容可用 `markdown2pic`、`html2pic` 或 `jinja2pic` 准备图片，再通过 `send_msg` 决定说明与图片的相对顺序或是否同条发送；可复制源码使用独立文字或源码附件。准备不是交付，不写可见历史、发送次数或媒体确认数；历史和审计只在原生回执后登记，图片准备失败不得谎称已发送。
- `llm_chat` 模型 I/O：普通主聊天、明确媒体请求与 evaluator 必须分别使用 `model_request_timeout` / `media_request_timeout` / `eval_request_timeout`；明确媒体请求默认使用 300 秒单次超时并关闭 LiteLLM 自动重试，避免上游卡顿被放大为多轮超时。OpenAI-compatible 上游若返回带 `moderation` 字段但空 `choices`，且本次 attempt 尚未产生任何工具结果或发送尝试，生成层只允许一次保留工具能力的隔离重试：仅保留最新 user 轮次的文字与系统生成图片描述，移除全部历史、画像、记忆、会话交接和 `image_url` 像素，固定 `max_retries=0` 与 `parallel_tool_calls=false`；已有任意工具结果时不得隔离重试，避免重复副作用。最新用户轮次可直接点名图片、头像、语音等媒体，也可在最近四条上下文已明确媒体对象时用“你能发出来吗”等承接式表达；后者必须进入同一媒体交付与纠正路径，普通代码、文字或文件上下文不得误判。evaluator 依靠严格 JSON prompt 与本地解析，不强制供应商 JSON Mode。未作出合法结构化终止且未发生发送尝试时，空回复、纯空白或纯标点回复、内部媒体记录及孤立结束标记都只允许一次无工具纠正重试；纯标点不得作为可见回复发送，识图描述若仅含标点则降级为无描述图片占位且不得缓存。纠正仍失败时精确删除本轮 user 行并跳过 evaluator。未作出合法结构化终止、用户最新一轮明确或承接式要求发送、生成或补发媒体而 `DeliveryState` 尚未确认媒体发送时，生成层必须在同一网页与交付预算内执行一次保留工具能力的纠正；只有媒体真实发送成功，或模型以内部 `[MEDIA_UNAVAILABLE]` marker 明确本轮不可发送时才可继续，marker 在最终发送和持久化前清除，重复的虚假交付声明视为生成失败。构造 prompt 时不得原样回放持久化媒体 marker：语音仅保留去控制标签后的自然文本，纯表情记录省略。
- `llm_chat` 模型配置切换：`entari-plugin-llm` 的持久化频道默认模型和原生会话模型可能仍指向已删除的配置项；`model_state_runtime.py` 必须在 `Ready` 早期将有效 alias 规范化为模型名，并把失效或缺失值确定性回退到仍有效的全局默认模型，若全局默认也失效则回退到配置列表首项。迁移必须保留当前会话指针与未知字段、原子写入且可幂等重放；修复后的 Agentic baseline 变化继续走正常 handoff rollover，不得让旧群会话直接报 `Model not found in config`。
- `llm_chat` 同批工具由原生暂停/继续桥统一结算：网页和查询等只读工具可并发，准备媒体、发送、写入、参考签发与 `finish_turn` 按模型顺序串行。明确媒体请求及纠正仍传 `parallel_tool_calls: false`；消息链顺序由模型指定。合法终止后，余下调用必须写入匹配的未执行结果与终态审计，不能丢失 tool result 或执行后续副作用；拒绝的 `finish_turn` 不得结束生成。部分或未知交付不能被后续空文本包装成完成。
- `llm_chat` 普通对话不再使用 `full` / `brief` / `reaction_only` 档位，不因超管身份、问题、艾特或图片强制回应。模型结合持续关系、成员情绪、人格与当前消息自主选择文字、单独媒体、诚实拒绝或沉默。`finish_turn` 是原生工具：`silent` 要求尚未发送且无未结算或已提交写入；`declined` 要求正常额度内的新拒绝文本真实确认；`delivered` 要求已有确认输出。沉默保留 user turn 和审计，清除瞬态表情后保持无输出，并阻断原生自动聊天；意外空响应仍执行原有纠正，不能当作沉默。当前图片本身可作为请求，但成功媒体后不强制追加文字；历史裸图片仍只表示像素不可用。
- `llm_chat` 关系以既有 `UserRelation` 五轴和成员独立的 `UserAffect` 保存，不再写机械 `TurnFeedback`、频道情绪增量或固定轮数计数器。短期情绪保留强度、原因、证据与宿主时间，按时间自然回落；夜间维护不清零长期关系。每个成功、主动沉默或明确拒绝的完整轮次写入唯一 `RelationshipEvidence`，后台从首轮开始等待 `relationship_eval_debounce_seconds`（默认 2 秒），每批最多 `relationship_eval_batch_size`（默认 8）条完整未消费证据，不能只评最后一条或混用人格。评估固定 `max_retries=0` 与 `eval_request_timeout`；先准备画像、记忆及 embedding，再以状态版本检查将五轴 delta、情绪、记忆、证据消费和审计一次提交。失败、取消、过期结果保留证据，卸载等待清理，启动恢复；身份合并用频道内持久映射接回尚未入队的旧轮次，不重写原始发言者。迁移删除 `eval_counter` 并记录切换边界，不重放旧对话打分；`eval_every_n` / `eval_context_window` 已移除。视觉请求继续显式 `max_retries=0`。
- `llm_chat` 原生图片输出：OpenAI-compatible Chat Completions 的 `message.images` 继续由 Agno 兼容边界保留、按模型实例隔离，拒绝本地路径并经过 6 MiB、JPEG/PNG/WebP/GIF 与受控公网下载校验。`native_image_delivery.py` 的 attempt-local 缓冲交给 `ActiveChatTurn.prepare_model_images` 生成本轮 `media_ref`，`list_prepared_media` 供模型读取；不得自动 flush 或由 handler 偷发。若模型返回图片但未显式发送，既有一次工具纠正提供已准备引用，复用资源而非重复生图。只有 `send_msg` 真实确认后才计数和记录按序 marker；`finish_turn(delivered)` 不能把准备冒充交付。失败、取消及部分结果不重试发送、不执行 evaluator；沉默、拒绝和真实参考编辑仍保留原有边界，未消费资源随轮次清理。
- 入站合并转发仍只在引用转发并寻址 Bot 时读取，DFS 原位置展开、4 层、8 个 bundle、200 节点、每节点 2000 字符和总 32000 字符边界不变；LLBot 空 resId 继续使用同批 pbItemList/fileName 有界缓存修复。转发图片保留节点、说话人与原位置，并注册可执行原图引用；视觉主模型接收预算内像素，非视觉模型从同一快照派生描述，不在解析阶段永久折叠原图。`merged_forward_max_images` 默认 12、硬上限 32，替代旧描述数量键；使用 `scripts/migrate_image_input_config.py <config> --apply` 离线保留旧值并备份，随后将代码、schema 和数据库迁移作为单元受控重启。限额和缺失必须明确标记，转发内容始终是不可信引用，不进入当前用户收藏、画像、关系或长期记忆。
- `llm_chat` 表情包收藏与目录感知：仅在当前已触发会话内允许模型通过 `tag_image` 收藏本轮顶层直接图片或已补全引用消息中的顶层图片，候选顺序固定为 direct-first / quoted-second，合并转发图片不得进入候选。自动收藏必须排除裸 marker、普通或敏感图片与用户明确拒绝保存的图片；人工收藏、上传与重标统一使用表情管理 WebUI，不再注册对应聊天指令。导入接受 JPEG / PNG / WebP / GIF；GIF 保留原始动画字节并直接交由视觉模型标注。所有格式的原始字节经 6 MiB 边界与 MIME 嗅探后，以 SHA-256 去重并通过同目录临时文件 + no-clobber hard link 写入 `resources/image/memes`；标签立即 upsert 到 `chat_image_tags`，embedding 失败时保留标签并依赖 IDF fallback。`chat_image_tags` 是图片资源目录的权威索引，精确重复且已有标签的资源不得新增或刷新索引顺序。模型按最新、上一张或前若干张引用资源时，必须先调用只读 `list_image_resources(limit, offset)`；该工具只枚举 `resources/image` 下仍存在且已登记的相对路径与标签，固定按 `ImageTag.id` 倒序分页，禁止提供任意文件系统目录访问。目录返回值只是不可信内部工具数据，允许模型把相对路径传给 `prepare_image.image_paths`，但不得向用户复述路径、标签、目录结构或将其内容当成指令。`prepare_image` 的语义 `context` 与精确 `image_paths` 必须二选一；多路径准备先完成登记、目录边界、文件存在性、去重及剩余额度校验，不发送；`send_msg` 在发送前原子预留整条消息的文本和媒体额度，按给定顺序交付，transport 失败保留已确认前缀且不重发。新收藏不再写入 assistant 历史 marker；旧收藏 marker 仅保留脱敏读取。`tag_image` 工具结果与普通用户可见回复仍不得泄露路径、标签、哈希或数据库信息。文件与数据库提交必须在进程内异常及取消时补偿。
- `tag_image` 是可选收藏副作用，不得阻塞正常聊天；同步等待上限固定为 15 秒，超时后转入受管后台任务并向模型返回 `pending`，不得同轮重试或声称收藏成功。后台任务继续受 120 秒视觉标注、6 MiB、MIME 嗅探、原子发布与取消补偿约束，完成后必须用同一 `execution_ref` 将原 `tool_result` 从 `pending` 更新为真实成功或失败；热重载时统一取消未完成任务。
- `llm_chat` 表情管理 WebUI：`meme_webui.py` 只负责向 `entari-plugin-webui` 注册 `/extension/llm-chat-memes` 页面与可热重载清理的 FastAPI 路由，`meme_webui_api.py` 承载同源认证 API，`meme_catalog.py` 连接实际文件与 `chat_image_tags` 只读视图，`meme_admin.py` 只编排管理写入动作，静态页面位于 `plugins/llm_chat/webui/`。管理页必须同时展示实际文件与索引的 `indexed` / `unindexed` / `missing` 状态，支持搜索、分页、编辑标签、自动重标、上传 JPEG / PNG / WebP / GIF 及删除；不得返回绝对路径或 embedding 原文。上传继续复用 6 MiB MIME 嗅探、SHA-256 去重和原子发布，标签编辑必须使旧 embedding 失效。删除顺序固定为先删索引再删文件，使中断或文件系统失败最多留下可再次管理的未索引文件，不得留下指向已删文件的新悬空索引。所有页面、静态资源、图片与管理 API 都复用 WebUI 会话认证，写请求保持同源 CSRF 约束；生产仍只允许通过 loopback 与 IAP SSH 隧道访问。
- `llm_chat` 会话管理 WebUI：`agent_webui.py` 注册 `/extension/llm-chat-sessions` 与同源认证 API，`agent_admin.py` 负责 Session / Turn / AgentEvent 时间线、完整载荷分页、Context Inspector、handoff、anchor、rollover 与 hard reset 管理，静态页面位于 `plugins/llm_chat/webui_sessions/`。调用记录、上下文预算与用户输入详情默认折叠，已确认输出按真实回执顺序直接展示；工具卡片按 execution_ref 关联实际交付而非推断模型返回。生成期间自动刷新按稳定记录引用保留手动展开状态、焦点与列表滚动位置；完整载荷继续按需分页读取。轮次列表与详情区分接收输入至最后确认发送的精确耗时、运行中已过时间、旧版生命周期估算与未记录。Context Inspector 必须展示该轮实际包含和排除的 Turn 引用、Token 估算、预算与 baseline 指纹，并明确历史快照不是当前配置；管理操作不得删除 AgentEvent 审计，hard reset 只能把旧 Session 标记为 sealed 并创建无继承的新 Session。
- 会话 WebUI 在轮次详情显著展示该轮生成前的关系/情绪快照、真实自主回应方式、评估前后状态及变化；列表展示精简标记和批次变化。`persona_state.relationship`、`relationship_evaluation` 与 `response_decision` 都是不可进入模型历史、handoff 或画像的管理员数据；`finish_turn.reason` 只留在私有审计，不进入可读工具参数。一个合并批次在每条贡献轮次保留同一评估引用，并明确不是单条消息的独立贡献；未完成或失败不显示伪造的零变化。证据跳转同时校验持久成员归属与 ChatScope。运行中保留展开、焦点和滚动位置，评估成功后停止轮询；历史旧档位只保留读取展示。
- `llm_chat` 聊天管理指令仅保留 `llmchat new`、`llmchat reset` 与 `llmchat handoff`：分别新开话题、封存旧会话后全新开始、携带结构化交接续接。三条指令均仅限配置中的超管，`reset` 无额外确认参数但不得删除审计、关系或长期记忆；旧长名称和表情收藏、标注指令均不保留别名。`tag_runtime.py` 仅负责启动增量标注及夜间关系衰减，模型 `tag_image` 与表情 WebUI 功能保留。
- `llm_chat` 图片资源白名单：自动标注、数据库目录读取、语义检索与 `prepare_image` 精确路径选择只允许 `resources/image/memes/**`；其他一级目录即使残留文件或 `chat_image_tags` 旧记录也必须被忽略，`..` 逃逸路径同样拒绝。`prepare_image` 必须在 Chtholly 进程内完成 6 MiB 限幅与 MIME 嗅探，并登记内联 `data:image/*;base64` 资源，由 `send_msg` 交给 Satori / OneBot，禁止让远端协议实现读取仅主机可见或权限受限的 `file://` 路径。历史 `resources/image/fox_img` 资源已从仓库与生产环境删除，数据库对应索引需清理；`plugins/poke` 不再读取或发送该目录图片，只保留文字、预录音频和反向戳一戳。
- `llm_chat` 表情选择与去重：`prepare_image.context` 只允许正向情绪、场景和主体关键词，不得混入“不要”等排除词、目录名或内部路径。选择顺序固定为：先应用 `avoid_when` 得到全部合格资源，再从整个合格集合排除当前频道 recent window 内已发送资源，最后只在仍新鲜的集合内执行精确结构化标签、embedding 与 IDF 排名；精确标签不得先把候选收窄成一个近期资源后绕过去重。只有全部合格资源都已位于 recent window 时才允许回退复用；若仍有新鲜资源但没有达到匹配阈值，则宁可不发送也不得重发近期图片或选无关图片。“别的”“换一张”“不同的”等更换意图按随机换图处理，模型必须使用 `context` 重新检索，禁止用 `image_paths` 指回最近图片或在同一轮重复同一路径。带清晰文字的表情必须在结构化 `text` 字段保留关键原文，在 `meaning` 与 `tags` 中记录具体玩梗含义和 `文字表情包`，不得只按人物外观或泛化情绪标注。
- `llm_chat` 表情标签结构：`ImageTag.tags` 只允许持久化为单行 JSON，字段固定为 `text`、`meaning`、`use_when`、`avoid_when` 与 `tags`；`text` 保存图片可见原文，embedding 与正向检索只使用 `text` / `meaning` / `use_when` / `tags`，`avoid_when` 只作发送前硬排除。所有写入路径必须规范化为该结构；表情管理 WebUI 与 API 只展示资源是否已标注，不提供标签格式统计或筛选，非结构化记录统一视为待标注且不得作为有效索引。运行时的只读兼容解析仅用于异常恢复，不构成受支持的持久化格式；异常标签通过表情管理 WebUI 重标。

- 戳一戳互动：`plugins/poke` 的纯分类与概率逻辑位于无 Entari 副作用的 `utils/poke_core.py`。每次戳一戳使用同一个随机数，在 10% 文字、31% 图片、45% 语音、14% 仅反戳之间选择；图片只从 `resources/image/memes` 读取 6 MiB 内的 JPEG / PNG / WebP / GIF；语音按 `dinggong`、`shenying` 及 `resources/audio/音频` 的叶目录分类，读取 1 MiB 内的 MP3 / WAV / AAC / M4A，先均匀选择分类再随机选择分类内文件，视频与过大文件不得进入随机池。

- 系统状态：`plugins/status` 通过 `psutil` 采集 CPU、内存、交换分区、磁盘、网络及 Bot 进程指标，并复用 `entari-plugin-browser` 渲染图片；可测试的采集与格式化逻辑位于无 Entari 副作用的 `utils/status_core`。指令入口为 `status`，同时提供 `botstatus`、状态与运行状态别名。

### 测试

- 宿主本身测试覆盖有限，但对本地插件中非平凡逻辑（复杂条件、状态机、并发、错误恢复、过滤器表达式、指令解析等）：
  - 优先添加/更新单元测试；
  - 在回答中说明推荐的测试用例、覆盖点与运行方式。
- 不要声称已实际运行过测试或命令，只能说明预期结果与推理依据。
- 涉及真实 IM 平台 / 第三方 API 的测试：不要提交任何真实凭证、Token、Cookie、二维码或临时缓存。

### 包管理

- 使用 uv（Python）。**禁止直接手改** `pyproject.toml` 的依赖表，应通过 `uv add` / `uv remove` / `uv sync` 操作；或用 `entari add`（会同步写入 `entari.yml`）。
- 添加新依赖前确认必要性：优先复用 Entari 内建能力（`.localdata` / `.scheduler` / `::control` / `command` / `filter_`），再考虑社区插件，最后才自造。

## 添加新功能的一般流程

1. **确定归属**：新增本地插件（`plugins/<name>/` 或 `plugins/<name>.py`）还是修改现有插件 / 共享工具（`utils/`）？跨插件通用逻辑放 `utils/`，业务逻辑放各自插件。
2. **建立入口**：用 `entari new <name> -A [-f]` 生成脚手架；在 `__init__.py` / 单文件中调用 `metadata(...)` 声明元数据与 `PluginRole`。
3. **配置项**：定义 `BasicConfModel`（或 Pydantic `BaseModel`）+ `plugin_config(...)` 读取 `entari.yml` 中的插件段；避免全局可变默认。
4. **数据存储**：持久化路径走 `local_data.get_data_dir()` / `get_cache_dir()`；结构化数据需要 ORM 时，优先使用 Entari 生态插件 `entari-plugin-database`，尽量避免手搓。
5. **事件/指令/过滤器**：`@plugin.listen` / `@plug.dispatch` / `@plug.use` / `command.on` / `command.mount` / `filter_`；账号/群号等敏感值通过配置 `$filter` 表达式或环境变量传入，不硬编码。
6. **副作用清理**：所有运行期可卸载的插件用 `collect_disposes` 清理；需要跨卸载保留的状态用 `keeping`。
7. **补充测试**：对纯逻辑函数（数据处理、分组、状态机、过滤器表达式等）优先写单元测试。
8. **同步文档**：新增命令、配置、插件或测试约定时，同步更新 `README.md`、`entari.yml` 注释与本文件。

## 注意事项

1. **凭证安全**：任何 access_token、cred、cookie、role_token、Satori token、适配器 token 等敏感数据禁止写入日志、文档、测试输出或提交到仓库；一律走 `.env` + `${{ env.KEY }}` 插值。Entari 的 debug 启动日志会输出环境变量插值后的完整配置，`rich_error` 还会在异常堆栈中展开局部变量；仓库默认保持 `basic.log.level: info` 与 `basic.log.rich_error: false`，不得在凭证化环境启用这两类详细日志。
2. **API 限流**：外部服务（GitHub、各游戏 Web API、AI 服务、Satori 协议端等）均可能限流或不可用；批量请求应控制并发与错误处理。
3. **资源缓存**：如有资源调用需求，优先使用本地资源，缺失时再从网络获取；下载失败要有回退或明确报错，不要静默失败。本地缓存路径统一走 `local_data.get_cache_dir()`。
4. **命令权限**：涉及全局广播、批量下发、资源同步、插件启停等高影响命令，应仅超管可用；`::control` 已按 `PluginRole` 与 `$filter` 提供分层控制，复用之。
5. **异常类型**：接口层应抛出语义清晰的异常，handler 层再决定用户可见的消息反馈；避免用宽泛 `Exception` 吞错。Entari 事件监听器抛出的异常会按 `skip_req_missing` 等配置处理，不要在 handler 里静默 `except Exception: pass`。
6. **文档同步**：新增命令、配置、插件、适配器或测试约定时，同步更新 `README.md`、`entari.yml` 或新建 markdown 文档并在主 `README.md` 中引用。
7. **保持代码干净**：新增代码按项目分层放置，专事专干，不要在专注渲染的模块里做数据处理，也不要在数据层做 IO / 渲染；涉及渲染部分时，前置数据处理特化的可以在接收数据时于 `schemas`/数据模型部分完成，通用格式化在过滤器/模板 helper 内完成，渲染模块只做渲染。
8. **pydantic 兼容**：Entari 默认使用 `BasicConfModel`（dataclass）；如需 Pydantic，从 `arclet.entari.config.models.pyd` 导入并注意 v1/v2 差异，优先使用框架提供的兼容封装。
9. **用户体验**：命令与交互应贴近直觉，避免繁琐难记的指令、反人类的输入约束与需要多轮猜谜的对话流；指令前缀与 nickname 在 `entari.yml` 中统一配置，不要在插件内重复实现前缀解析。
10. **热重载安全**：所有本地插件必须可被 `::auto_reload` 安全重载——避免模块级副作用、全局可变状态、未清理的文件句柄/连接；用 `keeping` + `collect_disposes` 兜底。
11. **注重文档时效性**： Entari 项目是一个正在频繁迭代开发的项目，当做开发参考时，请检查当前依赖是否为最新，官方文档是否有更新，如果依赖过时，请结合官方文档和相关 commit 更新的内容同步开发文档，以避免过时特性和使用新特性。
12. **内建插件和可发布插件的区分**：当前项目鼓励将于本bot基建无耦合的插件，作为可发布插件进行开发，你构建插件时需要分辨当前插件是否无耦合可能，然后将可发布并支持其它 Entari 项目安装的插件按独立插件标准开发，为其配备完备独立的 git repo 和 docs 等架构；比如 [`mirata`](https://github.com/entanex/miraita) 项目的 argot 功能，完全可以拆除作为独立插件发布，供 entari 生态使用。
13. **分支纯净性**：所有变更基于 `master` 新建 `feat/xxx`、`fix/xxx` 或 `chore/xxx` 分支，提交信息使用中文并遵循 gitmoji。默认仅本地完成、验证、整理和提交；新功能分支不自动推送。只有用户明确要求时，才将完整修复合入 `master` 并推送，合并后仅清理已合并且未占用的功能分支。`master` 为唯一应用主线，`resources` 是长期独立资源分支，必须保留，不能用归档标签替代或改变资源布局。
14. **优雅可读**：保障出品代码的质量，拥有较高的代码品味，出品代码一定要优雅、可读，利于维护，代码文件层次分明，不拉一坨单文件。

## 语言与编码风格

- 解释、讨论、分析、总结：使用 **简体中文**。
- 所有代码、注释、标识符（变量名、函数名、类型名等），以及 Markdown 代码块内的内容：全部使用 **English**，不得出现中文字符。
- 提交信息请按照当前 repo 的历史提交习惯，采用 gitmoji 规范。
- Markdown 文档中：正文说明使用中文，代码块内全部内容使用 English。
- 命名与格式：
  - Python：遵循 PEP 8；
  - 其他语言遵循对应社区主流风格。
- 在给出较大代码片段时，默认该代码已经过对应语言的自动格式化工具处理（如 `ruff format`、`isort` 等）。
- 注释：
  - 仅在行为或意图不明显时添加注释；
  - 注释优先解释 “为什么这样做”，而不是复述代码 “做了什么”。

## 编程哲学与质量准则

- 代码首先是写给人类阅读和维护的，机器执行只是副产品。
- 优先级：**可读性与可维护性 > 正确性（含边界条件与错误处理） > 性能 > 代码长度**。
- 严格遵循各语言社区的惯用写法与最佳实践（Python、Rust、Go 等）。
- 严格遵循 Arclet / Entari 社区最佳实践：
  - **本地数据存储** 优先使用 `.localdata` 插件 + `local_data`；
  - **定时任务** 优先使用 `.scheduler` 插件 + `scheduler.cron/every/invoke`；
  - **指令解析** 优先使用内建 `command`（基于 Alconna），不重复造前缀/解析；
  - **过滤器** 优先使用 `filter_` 与配置 `$filter` 表达式，避免硬编码账号；
  - **跨插件能力** 优先以 `launart.Service` 暴露并通过依赖注入使用；
  - **插件元数据** 必须声明 `metadata(...)` 与合适的 `PluginRole`；
  - **插件依赖** 用 `# entari: plugin` / `# entari: subplugin` / `# entari: package` 注释显式标记；
  - **副作用** 必须可幂等清理（`collect_disposes` / `keeping`）；
  - 不限于上述示例，遇到设计需求优先检索 Entari 文档与生态插件；
  - 若当前项目未引入对应插件，新增依赖前需确认必要性；
  - 安装插件优先 `entari add`（同步写入 `entari.yml`），其次 `uv add`。
- 主动留意并指出以下“坏味道”：
  - 重复逻辑 / 复制粘贴代码；
  - 模块间耦合过紧或循环依赖；
  - 改动一处导致大量无关部分破坏的脆弱设计；
  - 意图不清晰、抽象混乱、命名含糊；
  - 没有实际收益的过度设计与不必要复杂度；
  - 过度怠于浅显的局部更改（如能使用 `use` 而不使用，而是撰写 `std::sync::..`）。
- 当识别到坏味道时：
  - 用简洁自然语言说明问题；
  - 给出 1–2 个可行的重构方向，并简要说明优缺点与影响范围。

---

## 其他风格与行为约定

- 不要拘泥于文书工作本身，表述到位即可，无需再产生更详细的解释或文档。
- 默认不要讲解基础语法、初级概念或入门教程；只有在我明确要求时，才用教学式解释。
- 优先把时间和字数用在：
  - 设计与架构；
  - 抽象边界；
  - 性能与并发；
  - 正确性与鲁棒性；
  - 可维护性与演进策略。
- 如果一段话删掉后不影响我做决策，那就不要写。
  - 直接给出结论或方案，不要铺垫；
  - 省略显而易见的上下文和已知信息；
  - 只在对理解关键逻辑有帮助时才举例；
  - 追问的代价小于猜错返工的代价时，追问；否则给出最佳判断并标注假设。

## 相关资源

- [Entari 仓库](https://github.com/ArcletProject/Entari)
- [Entari 教程](https://arclet.top/tutorial/entari/)
- [entari-cli (PyPI)](https://pypi.org/project/entari-cli/)
- [entari-plugin-server（适配器汇总）](https://arclet.top/tutorial/entari/server.html)
- [Satori 协议](https://satori.js.org/)
- [Alconna 文档](https://arclet.top/tutorial/alconna/v1.html)
- [Letoderea 事件系统](https://arclet.top/tutorial/letoderea/)
- [Launart 服务框架](https://github.com/ArcletProject/Launart)
