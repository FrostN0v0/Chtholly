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

`uv.lock` 固定当前兼容组合，包含 Entari RC 与 LLM Git 版本；请按锁文件安装，升级前核验框架、LLM 与渲染插件的兼容性。

更多框架用法见 [Entari 文档](https://arclet.top/tutorial/entari/)。

## WebUI 配置应用

Linux 托管部署启用 `webui_config_apply` 后，配置保存会自动触发完整 Bot 重启，页面右下角显示待应用、重连、成功或失败状态。侧栏“配置应用与重启”也提供手动重启入口；只重启 Bot，不重启 LLBot，重复保存未变配置不会重复重启。

分模型的 `api_key`、`base_url` 留空或删除即可继承全局设置。新凭证先写入环境文件，再在表单中使用环境变量引用；非法配置会在写入前明确报错，不再把“保存成功”当作“已经生效”。管理面板继续仅通过回环地址和 SSH 隧道访问。

## LLM 会话管理

`llm_chat` 按聊天范围保存会话、轮次、工具调用和确认交付，并按 Token 预算自动续接。登录 Entari WebUI 后，“LLM 会话”页面提供调用时间线、上下文注入、输入与输出三个视图；每次模型请求和工具参数、返回结果均可展开，较长内容支持分段加载与复制。

在 `llm_chat.personas` 下按角色键配置 `name`、`prompt`、`appearance` 和可选的 `reference_image`，用 `default_persona` 指定默认角色。人格、口吻和外观与通用系统规则分离；参考图使用 `resources/image` 下已有文件的安全相对路径。旧 `persona` 移至对应角色的 `prompt`，旧 `self_reference_image` 移至该角色的 `reference_image`。

普通成员可查看当前角色列表、会话与 Token 信息：

```text
llmchat persona
llmchat session
```

Token 信息区分供应商实际用量与上下文估算；缺失数据明确标为未知。历史输入不会根据当前配置重建，凭证、图片像素及内部思考不进入新增调用快照。

超管可显式控制当前群会话：

```text
llmchat persona chtholly
llmchat new
llmchat reset
llmchat handoff
```

`persona <key>` 只切换当前聊天范围，并创建不继承旧话题的会话；已有轮次和后台评估继续使用各自启动时的人格快照。`new` 保留关系、画像和长期记忆，但不继承上一话题；`reset` 封存旧会话并新建，不删除审计事件；`handoff` 携带结构化交接继续当前任务。以上变更指令仅限超管，关系与长期记忆不会因换角色清空。

表情收藏与标注不再提供聊天指令；人工管理统一使用 WebUI“表情库管理”。模型按需收藏与启动增量标注仍保留。

人格作息按上海时区计算；低精力会让文字回复简短，但不会减少明确请求的媒体交付额度（默认每轮最多 6 条）。静态报告继续使用图片渲染，网页与交互原型使用下面的作品交付流程。

## 网页作品与源码

直接描述任务即可，例如“帮我设计并优化一版ui”；无需特定关键词、空格或额外的发布、读取、打包口令。模型会按任务自主选择生成预览、查阅已有作品和交付源码。启用作品服务后，Bot 保存完整 HTML、CSS、JavaScript 与同包素材，提供可交互预览、真实预览图和原始源码 ZIP；源码不再用代码图片代替。协议明确不支持文件上传时回退为下载链接，不将链接发送伪装成文件上传成功。

每次修改产生独立版本，支持查找、读取、修改当前聊天范围内自己的作品；撤销链接仍需要当前用户明确提出。链接默认有效 24 小时；持有链接的人都可以预览和下载，过期或撤销后页面、素材和 ZIP 同时失效。不要在作品中放入密钥、真实登录信息或私人对话。

预览仅支持静态前端交互，不运行服务端代码、不安装项目依赖，也不连接真实支付、登录或 Bot API。作品的截图与源码文件先于说明文字发送，并计入同一轮媒体额度。

配置 `llm_chat.web_artifacts_public_url` 为独立 HTTPS 域名后启用；留空则不注册作品工具。`web_artifacts_capture_url` 必须指向回环截图接口，`web_artifacts_capture_token` 通过环境变量注入，`web_artifacts_ttl_hours` 控制有效期（最多 168 小时）。独立服务与仅公开作品路由的代理示例位于 `scripts/chtholly-web-artifacts.service` 和 `scripts/web-artifacts.Caddyfile`；管理面板端口不得对公网开放。

## 💖 感谢

- [Entari](https://github.com/ArcletProject/Entari)：基于 Satori 协议的 IM 框架
- [Satori 协议](https://satori.js.org/)：跨平台即时消息协议
- [Entari 社区插件](https://pypi.org/search/?q=entari-plugin)：开发者们贡献的插件生态

## 📢 声明

此项目仅用于学习交流，请勿用于非法用途。

## 📄 许可证

本项目使用 [GNU AGPLv3](https://choosealicense.com/licenses/agpl-3.0/) 作为开源许可证。

这意味着你可以运行本项目，并向你的用户提供服务，如后续有对本项目源码的修改，你需要向用户公开修改后的此项目的源码。
