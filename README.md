<!-- markdownlint-disable MD033 MD036 MD041 -->
<div align="center">
<p>
  <img src="./docs/ChthollyBot.png" width="420" alt="Chtholly">
</p>
  <p>✨ 基于 <a href="https://github.com/ArcletProject/Entari">Entari</a> 与 <a href="https://satori.js.org/">Satori</a> 的 QQ 娱乐机器人 ✨</p>
</div>
<p align="center">
  <a href="https://raw.githubusercontent.com/FrostN0v0/Chtholly/main/LICENSE">
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
uv sync --all-extras
uv run entari run
```

更多框架用法见 [Entari 文档](https://arclet.top/tutorial/entari/)。

## LLM 会话管理

`llm_chat` 使用按聊天范围隔离的 Session、Turn 与 AgentEvent 保存对话、工具调用和确认交付，并按 Token 预算自动续接会话。登录 Entari WebUI 后可在“LLM 会话”页面查看时间线、上下文选择、结构化交接与固定事件。

超管可显式控制当前群会话：

```text
llmchat new-session
llmchat rollover-session
llmchat hard-reset-session CONFIRM
```

`new-session` 保留关系、画像和长期记忆，但不继承上一话题；`rollover-session` 携带结构化交接；硬重置只封存模型访问路径，不删除审计事件。

人格作息按上海时区计算；低精力会让文字回复简短，但不会减少明确请求的媒体交付额度（默认每轮最多 6 条）。静态报告继续使用图片渲染，网页与交互原型使用下面的作品交付流程。

## 网页作品与源码

可以直接请求“设计一个带弹窗和主题切换的网页，给我预览和源码 ZIP”。启用作品服务后，Bot 会保存完整 HTML、CSS、JavaScript 与同包素材，提供可交互预览、真实预览图和原始源码 ZIP；源码不再用代码图片代替。协议明确不支持文件上传时回退为下载链接，不将链接发送伪装成文件上传成功。

每次修改产生独立版本，支持查找、读取、修改和撤销当前聊天范围内自己的作品。链接默认有效 24 小时；持有链接的人都可以预览和下载，过期或撤销后页面、素材和 ZIP 同时失效。不要在作品中放入密钥、真实登录信息或私人对话。

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
