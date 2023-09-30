<div align="center">
<p>
  <a href="https://kiramibot.dev/"><img src="https://raw.githubusercontent.com/FrostN0v0/Chtholly/main/logo.png" width="200" height="200" alt="珂朵莉是世界上最幸福的女孩"></a>
</p>
  <p>✨ 基于<a href="https://kiramibot.dev/">Kirami</a>的QQ机器人 ✨</p>
</div>
<p align="center">
  <a href="https://raw.githubusercontent.com/FrostN0v0/Chtholly/main/LICENSE">
    <img src="https://img.shields.io/github/license/FrostN0v0/Chtholly" alt="license">
  </a>
    <img src="https://img.shields.io/badge/python-3.10+-blue?logo=python&logoColor=edb641" alt="python">
  <a href="https://github.com/A-kirami/KiramiBot">
    <img src="https://img.shields.io/badge/KiramiBot-0.3.2-green" alt="KiramiBot">
  </a>
    
</p>

# Chtholly

## 简介

珂朵莉是世界上最幸福的女孩，一款QQ娱乐机器人。基于[Kirami](https://kiramibot.dev/)开发。

Welcome To [斯卡布罗集市](http://qm.qq.com/cgi-bin/qm/qr?_wv=1027&k=M75YeO2zj9f5ziuS2ijcDzbjkAfcMHVA&authKey=ilcGvEnqWjHOJKa3f1cpOMQPVAeA0RZyv%2BD9lE9aV1WfwFZ8ig%2BUynUCSM4AXZOB&noverify=0&group_code=326466216)

## 功能列表


前身使用[NoneBot2](https://v2.nonebot.dev/)开发，现使用[Kirami](https://kiramibot.dev/)，计划迁移原有多数功能。

先开坑，慢慢迁,先列功能，命令及配置参数之后再补

<details ><summary>每日60s推送</summary>

|    命令    |  @  |    功能说明     | 示例  |
|:--------:|:---:|:-----------:|:---:|
| 每日60s/日报 |  是  | 发送今日每日60s图片 |  无  |
| 日报(取消)订阅 |  否  | 为本群（取消）订阅推送 |  无  |
</details>

<details ><summary>摸鱼日历推送</summary>

|    命令    |  @  |    功能说明     | 示例  |
|:--------:|:---:|:-----------:|:---:|
| 摸鱼/摸鱼日历  |  是  | 发送今日摸鱼日历图片  |  无  |
| 摸鱼订阅（取消） |  否  | 为本群（取消）订阅推送 |  无  |
</details>

<details ><summary>狗屁不通文章生成器</summary>

|    命令     |  @  | 功能说明 |       示例        |
|:---------:|:---:|:----:|:---------------:|
| 狗屁不通/gpbt |  否  |  无   | 狗屁不通 <关键字> <字数> |
</details>

<details ><summary>狐娘图片</summary>

|   命令    |  @  | 功能说明 | 示例  |
|:-------:|:---:|:----:|:---:|
| 嘤/狐娘表情包 |  否  | 返回图片 |  无  |
</details>

<details ><summary>随机可爱</summary>

|  命令  |  @  | 功能说明 | 示例  |
|:----:|:---:|:----:|:---:|
| 来个猫猫 |  否  | 返回图片 |  无  |
| 来个狐狸 |  否  | 返回图片 |  无  |
| 来个二哈 |  否  | 返回图片 |  无  |
</details>

<details ><summary>公共图库</summary>

|       命令       |  @  | 功能说明 |  示例  |
|:--------------:|:---:|:----:|:----:|
|      上传图片      |  是  |  无   | 配置说明 |
|     查看公开图库     |  是  |  无   | 配置说明 |
| <图库名> <id(可选)> |  否  | 返回图片 | 美图 1 |
</details>

<details ><summary>钉宫语音</summary>

|  命令   |  @  | 功能说明 |   示例   |
|:-----:|:---:|:----:|:------:|
| 骂+任何话 |  是  |  返回语音   | @珂朵莉骂我 |
</details>

<details ><summary>点歌</summary>

| 命令  |  @  | 功能说明 |  示例  |
|:---:|:---:|:----:|:----:|
| 点歌/qq点歌/网易点歌/酷我点歌/酷狗点歌/咪咕点歌/b站点歌 + 关键词  |  否  | 返回音乐 | 配置说明 |

</details>

<details ><summary>mockingbird合成语音</summary>

|  命令   |  @  | 功能说明 |  示例  |
|:-----:|:---:|:----:|:----:|
| 说+任何话 |  是  | 返回语音 | 说我爱你 |
</details>

<details ><summary>ChatGPT对话</summary>

|    命令    |                         说明                         |        示例        |
|:--------:|:--------------------------------------------------:|:----------------:|
|  /talk   |                  开始对话，默认群里@机器人也可以                  | /talk 先有鸡还是先有蛋？  |
|   /hi    |                    沉浸式对话（仅限私聊）                     |                  |
|  /speak  |                      gpt转语音对话                      | /speak 先有鸡还是先有蛋？ |
|  /reset  |                    重置对话（不会重置预设）                    |                  |
| /prompt  |                 设置预设（人格），设置后会重置对话                  |                  |
| /chatgpt | 如果talk_with_chatgpt_all_group_enable为false，则用该命令启用 |                  |
|   修改角色   |                   设置预设vits转语音角色                    |    修改角色 <派蒙>     |
</details>

<details ><summary>被动技能</summary>

- 戳一戳回复语
- 戳回去
- 骂回去
- 发送图片
</details>

## 部署

### 安装脚手架

可参考[Kirami用户手册](https://kiramibot.dev/docs/guide/start/installation)进行部署,安装好脚手架[Kirami-CLI](https://github.com/A-kirami/KiramiCLI)，以及[MongoDB](https://www.mongodb.com/try/download/community)

###  克隆源码

```shell
git clone https://github.com/FrostN0v0/Chtholly.git
```

### 安装依赖

#### 使用pdm

```shell
pdm install
```

#### 或使用pip

```shell
pip install -r requirements.txt
```

#### 安装ffmpeg

[FFmpeg官网](https://ffmpeg.org/)，找到对应的系统版本下载。

将压缩包解压到指定的目录。

将安装安装目录下的bin文件夹添加到系统的Path环境变量中。

### 运行

修改 `kirami.config.toml` 配置文件，自定义运行配置，详见[Kirami配置](https://kiramibot.dev/docs/guide/tutorial/config)

使用 `kirami run` 命令运行机器人

See [Docs](https://kiramibot.dev/)

## 感谢

- [Nonebot2](https://github.com/nonebot/nonebot2)：跨平台 PYTHON 异步机器人框架
- [Kirami](https://kiramibot.dev/)：基于 Nonebot2 二次开发的机器人框架
- [go-cqhttp](https://github.com/Mrs4s/go-cqhttp)：提供稳定接口
- [Nonebot2商店](https://v2.nonebot.dev/store)：插件灵感来源以及参考
- [绪山真寻Bot](https://github.com/HibiKier/zhenxun_bot): 插件灵感来源以及参考

## 声明

此项目仅用于学习交流，请勿用于非法用途。

## 许可证

本项目使用 [GNU AGPLv3](https://choosealicense.com/licenses/agpl-3.0/) 作为开源许可证。

这意味着你可以运行本项目，并向你的用户提供服务，如后续有对本项目源码的修改，你需要向用户公开修改后的此项目的源码。
