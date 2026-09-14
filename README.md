<!-- markdownlint-disable MD033 MD036 MD041 -->
<div align="center">
<p>
  <img src="./docs/ChthollyBot.png" width="420" alt="Chtholly">
</p>
  <p>✨ 基于 <a href="https://github.com/ArcletProject/Entari">Entari</a> 与 <a href="https://satori.js.org/">Satori</a> 的 QQ 娱乐机器人 ✨</p>
</div>
<p align="center">
  <a href="https://raw.githubusercontent.com/FrostN0v0/Chtholly/master/LICENSE">
    <img src="https://img.shields.io/github/license/FrostN0v0/Chtholly" alt="license">
  </a>
    <img src="https://img.shields.io/badge/python-3.10+-blue?logo=python&logoColor=edb641" alt="python">

</p>

# 𝓒𝓱𝓽𝓱𝓸𝓵𝓵𝔂

>_我曾经发誓要永远和他在一起，能够如此发誓，让我无比幸福。_  
>_——我曾经发誓要永远和她在一起，能够如此发誓，让我心获安详。_  
>_我曾经认为自己喜欢这个人。_  
>_我曾经觉得自己非常珍视她。_  
>_能有如此感受，让我无比幸福_  
>_——能有如此感受，让我无比喜悦。_  
>_他曾经对我说，我一定会让你幸福。_  
>_——我曾经对她说，我一定会让你幸福。_  
>_能够听到他那样说，让我无比幸福。_  
>_——能够对她那么说，让我心获满足。_  
>_那个人，分了这么多的幸福给我。_  
>_——我从她那，得到了这么多的东西，可是我却……_  
>_所以，我敢肯定，现在的我，不管别人怎么说，都一定是世界上最幸福的女孩。_

## 📖 简介

珂朵莉是世界上最幸福的女孩，也是一款基于 Entari 与 Satori 协议构建的 QQ 娱乐机器人。

Welcome To [💬 斯卡布罗集市](http://qm.qq.com/cgi-bin/qm/qr?_wv=1027&k=M75YeO2zj9f5ziuS2ijcDzbjkAfcMHVA&authKey=ilcGvEnqWjHOJKa3f1cpOMQPVAeA0RZyv%2BD9lE9aV1WfwFZ8ig%2BUynUCSM4AXZOB&noverify=0&group_code=326466216)

## 🛠️ 快速开始

需要 Python 3.10+ 与 [uv](https://docs.astral.sh/uv/)。

```shell
git clone https://github.com/FrostN0v0/Chtholly.git
cd Chtholly
uv sync --locked --all-extras
uv run --locked entari run
```

`uv.lock` 固定当前兼容组合，包含 Entari、WebUI 补丁包与 LLM Git 版本；请按锁文件安装。Entari `0.19.0rc2+chtholly.2` 修复热更新回滚、监听器清理和命令目录残留；WebUI `1.0.3+chtholly.1` 将登录会话与聊天连接分离，支持同一账号多个标签页，并允许失败的认证初始化重试。wheel 随仓库提供，可用 `python scripts/build_patched_wheel.py --package entari` 或 `--package webui` 从固定官方 wheel 和仓库补丁重建，不手改 `.venv`。

更多框架用法见 [Entari 文档](https://arclet.top/tutorial/entari/)。

## WebUI 配置应用

启用 `webui_config_apply` 后，已加载 LLM 的模型目录、全局及分模型凭证引用、接口地址、提示词与模型参数可通过原生“保存”直接热生效，不重启 Bot。正在执行的主聊天保持原模型直到本轮纠正与最终化结束，新轮次使用新配置；删除模型时同步修复默认选择，保留会话指针和历史。不显示常驻浮窗或独立重启菜单。

工具集合、其他插件、基础配置及适配器等变更仍交给 Linux 后台控制器执行完整 Bot 重启、健康检查和失败回滚，只重启 Bot、不重启 LLBot。控制器使用既有环境引用密码登录受保护的健康状态接口，Bot 重启或会话失效后重新认证；不会为探测放开匿名管理权限。已热生效的配置会跳过重复重启；未安装控制器时，不支持的变更会在写入前明确拒绝。模型热更新失败会回滚原配置文件，无法确认恢复时明确报错，不把失败显示为保存成功。

分模型的 `api_key`、`base_url` 留空或删除即可继承全局设置。凭证使用环境变量引用，不在表单中填写新密钥明文；新增或轮换进程环境变量仍需先重启使其进入运行环境。Bot 管理端口保持回环监听；可通过 SSH 隧道或下述独立 SSO 网关访问。

## 公网管理登录

公网入口使用独立 HTTPS 域名，经 Caddy 和 OAuth2 Proxy 接入 Casdoor OIDC。启用独立 `webui_sso` 后，Casdoor 认证会自动换取原生 WebUI 管理会话，不再输入第二次 WebUI 密码；SSH 隧道仍保留原密码登录。该 Casdoor 应用允许认证的账号均获得 WebUI 管理权限，不额外限制邮箱或组；网关使用签名 `sub` 作为身份，不要求账号填写或验证邮箱，但继续校验 issuer、audience、签名、nonce 和 PKCE。SSO 不写入 `llm_chat`，也不把后端切成全局免密模式。

部署配置位于 `scripts/webui.Caddyfile`、`scripts/webui-oauth2.cfg` 和 `scripts/chtholly-webui-oidc.service`。Caddy 的 `WEBUI_HOST` 指向管理域名，`ACME_EMAIL` 提供证书联系邮箱；该域名优先使用 Let's Encrypt，签发失败时使用 ZeroSSL 备用签发，不影响其他站点。Casdoor 应用回调须为该域名的 `/oauth2/callback`；OIDC client ID、secret、issuer、redirect URL 与随机 cookie secret 只保存在 root-owned `0600` 环境文件，通过 `OAUTH2_PROXY_*` 注入，不放到前端、Bot 配置或仓库。网关不可用时公网入口失败关闭，SSH 隧道仍可应急访问。

会话使用独立回环 Redis（`scripts/chtholly-webui-sessions.service`、`scripts/webui-redis.conf`）：刷新令牌只保留在加密的服务端会话中，浏览器仅持有签名票据。网关请求 `offline_access`，每 5 分钟按需刷新，Cookie 窗口为 12 小时；Casdoor 刷新令牌到期、撤销或账号权限失效时仍须重新登录。Redis 密码通过同一受限环境文件的 `OAUTH2_PROXY_REDIS_PASSWORD` 注入，ACL 仅保存密码哈希；默认 Redis 服务不启用，也不开放公网端口。旧 Cookie 会话切换到服务端存储时须重新登录一次。

在既有空前缀插件加载列表中加入 `webui_sso`，并设置它的 `public_origin` 为管理域名的 HTTPS origin；`auth_url` 默认固定指向 `http://127.0.0.1:4180/oauth2/auth`。插件只向该回环网关验证本轮请求的 SSO Cookie，然后复用原生 SessionStore；不接受伪造身份头，不修改第三方安装文件。退出先通过回环网关删除服务端会话，再清除两层 Cookie 并停在退出页，旧票据不可重放。后台标签页失效时不抢先打开登录页，回到前台后才重新访问当前管理页面，并优先复用其他标签页已续期的会话；浏览器返回缓存也会重新检查。登录流程的 CSRF Cookie 有效期为一小时；Casdoor 自身登录态不随此处退出连带清除。

连接恢复区分登录失效与临时网络故障：网关或身份服务不可用时返回 503，不删除会话、不重放写请求，也不自动发起登录。聊天与日志继续由原生 WebSocket 管理重连，HTTP 会话恢复会合并并退避；原生离线提示仍由真实健康检查控制。维护者通过 `scripts/build_oauth2_proxy.py` 构建固定源码与工具链的网关补丁，包含受限刷新超时、覆盖认证窗口的 Redis 锁租期与刷新令牌验证；构建默认执行回归并输出来源记录。

Caddy 只代理明确的管理页面、API 与两个管理 WebSocket；Satori/OneBot、自动 API 文档和未知路径一律拒绝。API、静态脚本和图标未认证时统一返回 401，不分别启动 OAuth 流程。跨站 Origin 和不带正确 Origin 的写请求在入口拒绝，管理内容禁用共享缓存。DNS 建议保持仅 DNS，以保留工坊最长 330 秒的请求窗口；如启用 CDN 代理，需另行确认超时与真实客户端 IP 配置。

## LLM 会话管理

`llm_chat` 按聊天范围保存会话、轮次、工具调用和确认交付，并按 Token 预算自动续接。登录 Entari WebUI 后，“LLM 会话”页面提供调用时间线、上下文注入、输入与输出三个视图；详细内容默认折叠，点击记录展开，支持全部折叠。生成期间自动刷新保留手动展开状态，较长内容按需分段加载与复制。

「输入与输出」直接按真实发送顺序展示确认送达的文字和图片，渲染、生图等工具卡片也可查看关联的交付图片并点击放大。语音、文件和视频保留交付类型提示，不提供媒体播放或文件内容预览。新轮次的总耗时从接收输入开始，到最后一条消息确认发送为止；旧轮次仅显示明确标注的生命周期估算，未留存的历史图片不能恢复，也不会重新渲染后冒充原图。

关系与情绪按成员持续保存，普通对话不再套用固定回应档位：模型可以自然聊天、只发表情、明确拒绝或主动沉默。后台评估合并完整的未处理互动，失败保留证据；短期情绪随时间回落，不每天清空长期关系。`relationship_eval_batch_size`（默认 8）是单批上限，`relationship_eval_debounce_seconds`（默认 2 秒）是合并窗口，不是每隔若干条只评最后一次。

聊天默认保留自然多条回复：短答可以一条，多要点优先分成几个普通聊天气泡，事实问答也不强行合并成长消息；最终普通文本的自然换行继续分条发送，不自动改成合并转发。不切碎句子、不凑条数，也不因关系亲近就机械追问。

多人同时提问时，模型处理仍并行，只有已经准备好的回复进入群级发送调度；每组最多连续 3 条，满 4 秒后让位给其他就绪轮次，不强制中断已开始的传输；后续内容尚未准备好就让其他人先回复。组首、被插话打断或隔一段时间后恢复时，Bot 自动引用对应的原问题，不额外艾特。文字和图片直接使用原生引用；语音、文件、视频和合并转发受 OneBot 能力限制，若没有后续可引用消息，会在媒体全部完成后补一条带引用的归属提示。发送超时或取消不会盲目重发，同一成员的新请求仍会取代其未完成的旧请求。

会话页同时展示生成时使用的关系与情绪、实际回应方式、评估前后变化和关联证据。合并评估会明确标注贡献轮次，失败与等待不显示成零变化；自动刷新保留展开状态、焦点与滚动位置，评估完成后停止轮询。

会话页通过原生 WebUI 的已认证父页面读取 API 和图片附件，兼容隔离 iframe 与密码登录；源码脚本和样式以内联 nonce 加载。切换轮次会停止旧的分页读取并释放临时图片地址，不关闭认证，也不放宽 iframe 的同源隔离。

在 `llm_chat.personas` 下按角色键配置 `name`、`prompt`、`appearance` 和可选的 `reference_image`，用 `default_persona` 指定默认角色。人格和口吻由所填 `prompt` 定义，不额外附加情景对白或固定说话示例；参考图使用 `resources/image` 下已有文件的安全相对路径。旧 `persona` 移至对应角色的 `prompt`，旧 `self_reference_image` 移至该角色的 `reference_image`。

普通成员可查看当前角色列表、会话与 Token 信息：

```text
llmchat persona
llmchat session
```

Token 信息区分供应商实际用量与上下文估算；缺失数据明确标为未知。历史注入快照保留当轮记录，不会随当前人格配置变化；修改人格后请查看新轮次。凭证、图片像素及内部思考不进入新增调用快照。

超管可显式控制当前群会话：

```text
llmchat persona chtholly
llmchat new
llmchat reset
llmchat handoff
```

`persona <key>` 只切换当前聊天范围，并创建不继承旧话题的会话；已有轮次和后台评估继续使用各自启动时的人格快照。`new` 保留关系、画像和长期记忆，但不继承上一话题；`reset` 封存旧会话并新建，不删除审计事件；`handoff` 携带结构化交接继续当前任务。以上变更指令仅限超管，关系与长期记忆不会因换角色清空。

表情收藏与标注不再提供聊天指令；人工管理统一使用 WebUI“表情库管理”。页面的脚本与样式以内联 nonce 加载，列表、图片、上传与修改都通过已认证父页面的 API bridge 工作，兼容隔离 iframe。图片使用受限 Blob URL，翻页或关闭时释放；上传保留原始 multipart 边界，不开放匿名文件接口或放宽沙箱。模型按需收藏与启动增量标注仍保留。

人格作息按上海时区计算；低精力会让文字回复简短，但不会减少明确请求的媒体交付额度（默认每轮最多 6 条）。静态报告继续使用图片渲染，网页与交互原型使用下面的作品交付流程。

## 网页作品与源码

直接描述任务即可，例如“帮我设计并优化一版ui”；无需特定关键词、空格或额外的发布、读取、打包口令。模型会按任务自主选择生成预览、查阅已有作品和交付源码。启用作品服务后，Bot 保存完整 HTML、CSS、JavaScript 与同包素材，提供可交互预览、真实预览图和原始源码 ZIP；源码不再用代码图片代替。协议明确不支持文件上传时回退为下载链接，不将链接发送伪装成文件上传成功。

每次修改产生独立版本，支持查找、读取、修改当前聊天范围内自己的作品；撤销链接仍需要当前用户明确提出。链接默认有效 24 小时；持有链接的人都可以预览和下载，过期或撤销后页面、素材和 ZIP 同时失效。不要在作品中放入密钥、真实登录信息或私人对话。

预览仅支持静态前端交互，不运行服务端代码、不安装项目依赖，也不连接真实支付、登录或 Bot API。作品的截图与源码文件先于说明文字发送，并计入同一轮媒体额度。

配置 `llm_chat.web_artifacts_public_url` 为独立 HTTPS 域名后启用；留空则不注册作品工具。`web_artifacts_capture_url` 必须指向回环截图接口，`web_artifacts_capture_token` 通过环境变量注入，`web_artifacts_ttl_hours` 控制有效期（最多 168 小时）。独立服务与仅公开作品路由的代理示例位于 `scripts/chtholly-web-artifacts.service` 和 `scripts/web-artifacts.Caddyfile`；管理面板端口不得对公网开放。

Bot 与独立预览进程共享项目依赖环境；同步依赖或浏览器后，必须同时重启 `chtholly.service` 与 `chtholly-web-artifacts.service`。仅健康接口成功不能证明截图可用，需用一次真实作品验证隔离截图返回 PNG；不要关闭 Chromium sandbox 或网络隔离来绕过失败。

## 插件工坊

启用 `plugin_workshop` 与 `llm_chat.plugin_workshop_enabled` 后，可让模型编写标准 Entari 插件，使用 `submit_plugin` 保存完整源码、命令、配置、权限与数据声明，并在无生产凭证及数据的 Linux Docker 容器中验收。容器没有网络接口出口；仅可经逐次受限的 Unix Socket 代理调用 Open-Meteo 的地名和天气 GET 接口。模型可承接上下文直接提交、修订或重新提交，不要求当前消息再次说“提交”。失败报告返回模型修正，每次修订产生新的不可变版本；提交或验收通过均不会自动激活。

验收宿主需 Linux 与可用的 Linux Docker Engine；Windows 可在 WSL2 的 Linux 环境运行宿主。维护者需显式构建与当前锁文件一致的验收镜像：

```shell
uv run --locked python scripts/build_workshop_sandbox.py --tag chtholly-workshop:local
```

构建只打包固定依赖、vendored wheel 和可信验收器，不发送项目配置、资源或 `.env`；模型提交不会构建或拉取镜像。镜像预装 Chromium、Inter/Noto CJK 字体和图像解码器，先验证可信模板的真实 PNG，再执行候选命令、重载及清理。模板访问限于候选源码目录，单命令 30 秒、总验收默认 180 秒；图片须完整解码并满足大小、像素和动画限制。Docker、镜像或版本前提不满足时明确显示不可用，不回退到宿主验收。

容器验收与激活后的宿主渲染使用各自的 HTMLRender 配置。宿主 `htmlrender.resources.local_access.allowed_paths` 必须同时保留原有模板目录和工坊专用 `runtime` 目录；本仓库默认 `.localdata.app_name: chtholly` 对应 `.chtholly/data/plugin_workshop/runtime`，改变 LocalData 位置时需同步调整。该目录只发布已批准插件，停用时删除对应包；不得授权整个 `.chtholly`、`versions` 或 `staging`。仅批准源码不会自动扩大渲染器文件访问权限，验收需同时覆盖激活后的实际命令出图。

生产推荐由 Bot 用户运行 rootless Docker，并通过 `DOCKER_HOST` 指向受限 Unix Socket；不要为方便调用而将 Bot 加入可控制宿主 root 的 Docker 组。rootless daemon 必须支持 cgroup v2 的内存、CPU 与 PID 限制，不能因安装模式改变而放宽验收边界。

在 Entari WebUI「插件工坊」审阅功能、源码差异、不可变配置、数据影响、验收报告与 SHA-256，确认后批准精确版本，再激活。工坊要求预先配置 WebUI 密码，并在回环监听时同样启用真实密码认证；若热加载前已有免密会话，必须完整重启以使旧 Cookie 失效，不把免密 Cookie 升格为管理员会话。管理端口继续保持回环监听及 SSH 隧道。页面复用原生隔离 iframe 的 API 消息桥，不放宽 iframe sandbox、同源写入或会话认证。

超管也可使用同一审批契约的原生命令：

```text
workshop help
workshop list
workshop show counter_demo 1
workshop approve counter_demo 1
workshop activate counter_demo 1
workshop rollback counter_demo 1
workshop disable counter_demo
```

`activate_plugin` 与 `rollback_plugin` 仅在当前超管明确请求且指定版本已获批准时执行，模型不能自行批准。失败更新保留旧版；重启恢复最后批准且启用的版本，停用状态也会保留。源码与版本存储由 LocalData 管理，配置变更同样需要新版本、重新验收和批准。

**批准原生代码是人工信任决定，不是安全认证。** 激活后插件拥有 Bot 进程的全部权限，权限清单只是声明；验收器与候选同进程，功能报告可能被恶意代码伪造。代码回滚与停用不会撤回消息、外部请求或数据修改。不要批准未经审阅的源码，也不要把 Docker Socket、宿主凭证或生产数据挂入验收容器。

## 💖 感谢

- [Entari](https://github.com/ArcletProject/Entari)：基于 Satori 协议的 IM 框架
- [Satori 协议](https://satori.js.org/)：跨平台即时消息协议
- [Entari 社区插件](https://pypi.org/search/?q=entari-plugin)：开发者们贡献的插件生态

## 📢 声明

此项目仅用于学习交流，请勿用于非法用途。

## 📄 许可证

本项目使用 [GNU AGPLv3](https://choosealicense.com/licenses/agpl-3.0/) 作为开源许可证。

这意味着你可以运行本项目，并向你的用户提供服务，如后续有对本项目源码的修改，你需要向用户公开修改后的此项目的源码。
