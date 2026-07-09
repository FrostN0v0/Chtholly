# AGENTS.md

本文档为 AI 编程代理（如 Codex、Claude Code 等）提供本仓库的项目上下文与开发指南。内容应以当前源码为准；修改实现后请同步更新本文档。

> 本仓库正在 `etr` 分支上将 Bot 从 NoneBot2 迁移到 **Entari**（Arclet Project，基于 Satori 协议）。本文件已按目标架构编写；尚未迁移完成的旧 NoneBot2 代码以 `plugins/` 下的现存文件为准，迁移时遵循本文规范重写。

## 项目概述

**Chtholly** 是一款 QQ 娱乐机器人工程，基于 [Entari](https://github.com/ArcletProject/Entari)（Satori 协议）运行。它本身不实现单一业务，而是通过 `entari.yml` 声明加载的社区插件与 `plugins/` 目录下的本地插件组合出功能。

- 协议层：Satori 协议；通过 `entari-plugin-server` + 适配器对接 OneBot V11 / Milky / QQ / Lagrange / Console / 纯 Satori 等协议端。
- 主要职责：提供运行时环境（`entari.yml`）、共享工具（`utils/`）、静态资源（`resources/`）、本地扩展插件（`plugins/`）。
- 使用者可按需在 `entari.yml` 的 `plugins` 段裁剪加载列表，或通过 `external_dirs` 引入额外插件目录。

## 技术栈与关键依赖

- **Python**: >= 3.14
- **Bot 框架**: [arclet-entari](https://pypi.org/project/arclet-entari/)（完整安装 `arclet-entari[full]`，含 CLI、YAML、文件监听）
- **CLI 工具**: [entari-cli](https://pypi.org/project/entari-cli/) —— `entari init / run / new / add / remove / config / gen_main`
- **事件总线**: arclet-letoderea（Entari 内建依赖）
- **命令系统**: arclet-alconna（Entari 内建 `command` 模块）
- **服务管理**: launart（`Service` 基类用于跨插件依赖注入）
- **协议适配器**: `satori-python-adapter-onebot11`（默认）；`entari-plugin-server` 在 Python 3.14 下有 dataclass 兼容问题，暂用外部 Satori server + `basic.network` 连接，已降级到 Python 3.10，待修复后升回 3.14
- **配置模型**: `BasicConfModel`（默认，dataclass 风格）/ Pydantic `BaseModel`（`arclet.entari.config.models.pyd`）/ msgspec `Struct`
- **HTTP 客户端**: httpx
- **日志与终端**: rich（Entari 内建 log 使用 loguru，可启用 `rich_error`）
- **包管理**: uv（`uv sync` / `uv add` / `uv remove`）
- **代码质量**: Ruff、Pyright（`typeCheckingMode = "standard"`）

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
- 测试直接导入 import-safe core 包，不通过 `sys.path.insert(...)`、synthetic package alias 或文件级 Pyright suppress 绕过插件副作用；pytest 的 import 根通过 `pyproject.toml` 配置。
- 新增 provider / client 类必须支持显式依赖注入测试 seam（例如可传入 HTTP client/transport），测试不得改写私有属性。
- 对会被 `::auto_reload` 重复加载的运行时副作用，注册时同步考虑清理：长任务用 `collect_disposes(...)` 取消，跨卸载状态用 `keeping(...)` 或明确的持久化存储。
- 结构质量门槛：入口文件原则上保持在 120 行以内；本地插件生产文件原则上保持在 250 行以内。超过不是硬错误，但必须能用单一职责解释其存在；否则优先按 `config` / `schemas` / `data_source` / `client` / `render` / `listener` / `command` / `runtime` / `utils` 等自然边界拆分。

### 基础建设

- 项目引进了 `entari-plugin-browser`、`entari-plugin-llm`、`entari-plugin-database`、`entari-plugin-permission` 作为项目的基础建设工具，当你需要使用 playwright、jinja2 模板渲染，AI会话调用、数据库及ORM、权限管理等方面时，优先考虑现有基建。
- 当前项目拟构造一个供其它插件或服务调用的 TTS 服务，目前拟兼容 gpt-sovits 的接入，参考 `nonebot-plugin-deepseek` 中的 TTS 服务对接，并优化实现，使其符合当前项目的基建要求。
- 帮助菜单，当前项目拟参考 [`nonebot-plugin-picmenu-next`](https://github.com/lgc-NB2Dev/nonebot-plugin-picmenu-next) 的菜单功能，结合 entari 基建，实现一个自动生成、界面美观、自定义程度高，开发简单的图片帮助基建插件。
- 会话互动系统，当前项目拟构造一个基于 llm 插件支持群聊场景需求，在复杂的多人对话中保有人物认知、长期用户画像和语义检索记忆能力的聊天交互系统，支持对本地图片通过 llm 视觉识别打上标签，并在合适不突兀的语境下发送图片和语音（语音文件以内容命名，根据文件名判断语境发送），并支持调用插件功能。

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

1. **凭证安全**：任何 access_token、cred、cookie、role_token、Satori token、适配器 token 等敏感数据禁止写入日志、文档、测试输出或提交到仓库；一律走 `.env` + `${{ env.KEY }}` 插值。
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
13. **分支纯净性**：所有变更请基于当前 `etr` 分支新建分支，命名结构参考 `etr/feat-xxx`，并在该分支按原子性提交规范落成 `commit` , `commit message` 使用中文，遵循 `gitmoji` 规范，主 `etr` 分支不要随便 commit。
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
