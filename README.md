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

编辑 `entari.yml` 调整网络、日志、插件加载等。敏感值（Token、超管 QQ、适配器端点等）放入 `.env.local`，通过 `${{ env.KEY }}` 插值，不要提交真实凭证。

### 🚀 运行

```shell
entari run
```

> 运行前需确保 OneBot V11 协议端（如 Lagrange.OneBot）已启动，并在 `.env.local` 中填好 `ONEBOT11_ENDPOINT` / `ONEBOT11_ACCESS_TOKEN`。

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
