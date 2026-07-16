<!-- markdownlint-disable MD033 MD036 MD041 -->
<div align="center">
<p>
  <a href="" alt="珂朵莉是世界上最幸福的女孩"><img src="./docs/ChthollyBot.png" width="420"  alt="NoneBotPluginLogo"></a>
</p>
  <p>✨ 基于<a href="https://nonebot.dev/">NoneBot2</a>的QQ机器人 ✨</p>
</div>
<p align="center">
  <a href="https://raw.githubusercontent.com/FrostN0v0/Chtholly/main/LICENSE">
    <img src="https://img.shields.io/github/license/FrostN0v0/Chtholly" alt="license">
  </a>
    <img src="https://img.shields.io/badge/python-3.10+-blue?logo=python&logoColor=edb641" alt="python">
  <a href="https://nonebot.dev/">
    <img src="https://img.shields.io/badge/nonebot-v2.4.0-EA5252" alt="Nonebot2">
  </a>

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

珂朵莉是世界上最幸福的女孩，一款QQ娱乐机器人。基于[Nonebot2](https://kiramibot.dev/)开发。

~~现在跟原生NoneBot2没什么区别喵~~

Welcome To [💬 斯卡布罗集市](http://qm.qq.com/cgi-bin/qm/qr?_wv=1027&k=M75YeO2zj9f5ziuS2ijcDzbjkAfcMHVA&authKey=ilcGvEnqWjHOJKa3f1cpOMQPVAeA0RZyv%2BD9lE9aV1WfwFZ8ig%2BUynUCSM4AXZOB&noverify=0&group_code=326466216)

## 🪧 功能列表

前身使用[Kirami](https://kiramibot.dev/)开发，现使用[NoneBot2](https://v2.nonebot.dev/)，计划迁移原有多数功能。

~~功能列表呢？没有的喵，兼容[nonebot2插件](https://nonebot.dev/store/plugins)，想用什么自己装喵，还要写功能列表？没有这样的道理的喵。~~

## 🛠️ 部署

**须知：项目与框架均处于开发阶段，不稳定，部署中如遇到问题请通过交流群 [斯卡布罗集市](http://qm.qq.com/cgi-bin/qm/qr?_wv=1027&k=M75YeO2zj9f5ziuS2ijcDzbjkAfcMHVA&authKey=ilcGvEnqWjHOJKa3f1cpOMQPVAeA0RZyv%2BD9lE9aV1WfwFZ8ig%2BUynUCSM4AXZOB&noverify=0&group_code=326466216) 联系我**

**协议端的使用具有时效性，~~比如寄了的gocq~~，所以这里不做推荐**

如果有疑问推荐访问[社区文档](https://x.none.bot/before/QA)

### 🦯 安装脚手架

推荐使用 [entari-cli](https://pypi.org/project/entari-cli/) 管理项目。先安装 uv，再安装 entari-cli：

```shell
pipx install uv
uv tool install entari-cli
```

### 🔗 克隆源码

```shell
git clone https://github.com/FrostN0v0/Chtholly.git
cd Chtholly
```

### ➕ 安装依赖

```shell
uv sync --all-extras
```

### ⚙️ 配置

编辑 `entari.yml` 调整网络、日志、插件加载等。敏感值（Token、API Key、密码等）放入 `.env.local`，通过 `${{ env.KEY }}` 插值，不要提交真实凭证。

启用 `llm_chat` 的网页搜索与正文提取需在 `.env` 或 `.env.local` 中配置 `TAVILY_API_KEY`；未配置时两个工具都不会注册，新增或更换密钥后需完整重启 Bot。

`llm_chat` 每轮生成的网页调用预算由 `web_search_max_calls_per_generation`、`web_page_max_calls_per_generation` 与 `web_total_max_calls_per_generation` 配置，默认分别为 `2 / 2 / 4`；预算按生成上下文隔离，实际总额不会超过两个单项之和。若 LLM 工具循环耗尽，插件会基于本轮已积累的工具结果执行一次不携带任何工具的最终化；最终化仍失败时保持静默，不回退到原生自动对话。

`llm_chat` 会在单轮生成内向模型提供 `send_text` 与 `send_merged_forward`。自然闲聊需要多个独立节拍时可按顺序发送最多 5 条普通文本；相邻发送由 `delivery_min_interval_seconds`、`delivery_default_interval_seconds`、`delivery_max_interval_seconds` 控制，默认 `1.1 / 1.2 / 5.0` 秒，配置只能收紧安全上限。普通文本单条、合并转发节点、整轮文本与媒体数量分别由对应的 `delivery_max_*` 配置限制；所有成功文本最终聚合为一条 assistant 历史，避免一次回复占用多个历史窗口行。

预计超过普通文本条数或各部分较长时，模型可改用一次合并转发。OneBot V11 使用公开 Satori `Message(forward=True)` 发送；其他平台或 OneBot 发送失败时，插件会按原顺序和同一安全节拍回退为普通文本。媒体必须先于本轮文本或合并转发；生成、最终发送或取消中断时，仅尽力保存已确认送达的文本前缀，不执行关系评估或关系更新。

群聊中的 OneBot V11 合并转发本身不会触发 `llm_chat`，也不会立即调用 `get_forward_msg`。只有用户引用该合并转发消息并同时 `@` Bot 时，插件才读取各节点、保留原发送者归属，并对限额内图片沿用独立视觉描述链路。默认单轮可读取 200 个节点、每节点 2000 字符、总计 32000 字符，并描述最多 12 张转发图片；对应配置为 `merged_forward_max_messages`、`merged_forward_max_chars_per_message`、`merged_forward_max_total_chars` 与 `merged_forward_max_described_images`。达到显式安全限额时会向模型附加遗漏标记并写 warning，不再静默伪装成完整内容。转发内容只作为引用上下文，不写成当前发送者的画像或记忆事实。

主聊天与关系 evaluator 的单次模型请求分别由 `model_request_timeout` 与 `eval_request_timeout` 限时，默认 `90 / 60` 秒。关系 evaluator 只使用严格 JSON 提示与本地解析，不强制供应商 JSON Mode；主模型若在没有任何发送尝试时返回空内容、内部媒体记录或孤立的 `[END_OF_RESPONSE]`，会执行一次无工具纠正重试。纠正仍失败时删除本轮尚未开始交付的 user 记录，不启动 evaluator；历史中的语音只以自然文本提供给模型，纯表情记录不进入提示历史。

部署默认使用 `info` 日志级别并关闭 `rich_error`，避免第三方 `debug` 日志或异常局部变量展开密钥、搜索参数和工具实参。

### 🚀 运行

```shell
entari run
```

> 运行前需确保 OneBot V11 协议端（如 Lagrange.OneBot）已启动，并在 `.env.local` 中填好 `ONEBOT_TOKEN`；反向 WebSocket 路径需与 `entari.yml` 的适配器配置一致。

详细的框架使用见 [Entari 文档](https://arclet.top/tutorial/entari/)。

## 💖 感谢

- [Entari](https://github.com/ArcletProject/Entari)：基于 Satori 协议的 IM 框架
- [Satori 协议](https://satori.js.org/)：跨平台即时消息协议
- [Entari 社区插件](https://pypi.org/search/?q=entari-plugin)：开发者们贡献的插件生态

## 📢 声明

此项目仅用于学习交流，请勿用于非法用途。

## 📄 许可证

本项目使用 [GNU AGPLv3](https://choosealicense.com/licenses/agpl-3.0/) 作为开源许可证。

这意味着你可以运行本项目，并向你的用户提供服务，如后续有对本项目源码的修改，你需要向用户公开修改后的此项目的源码。
