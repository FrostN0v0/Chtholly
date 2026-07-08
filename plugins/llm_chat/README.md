# llm_chat

`llm_chat` 是面向群聊的 Entari 本地插件：只响应被明确叫到的消息（引用 bot、前置/中间/末尾 `@bot`），把用户文本、入站图片描述、长期画像、相关记忆和近期关系状态一起组织成对话上下文，再交给 `entari-plugin-llm` 生成回复。纯图或图文消息会先用 `image_tag_model` 做视觉描述，注入为 `[图片: ...]` / `[引用图片: ...]`，因此即使当前会话模型切到无视觉模型也能理解图片；失败时降级为 `[图片]` 占位，不阻断回复。

## 功能

- 群聊人格：`persona` 只描述角色，系统规则由 `SYSTEM_SCAFFOLD` 注入。
- 触发条件：引用 bot 消息、消息任意位置 `@bot`，或 Entari 原生 `is_notice_me`。
- 图片理解：直发图和引用图都会被描述，最多处理 `image_describe_max_per_message` 张，超出只插入占位。
- 长期记忆：按用户和频道维护画像、关系数值、近期印象、语义检索记忆。
- 关系评估：每轮或按 `eval_every_n` 用 `eval_model` 评估关系变化和记忆更新。
- 本地媒体工具：模型可调用 `send_image`、`send_audio`、`speak`、`call_plugin`；发送媒体后会写入会话历史，下一轮模型知道自己发过什么。

tts_service defaults to GPT-SoVITS and can switch to Fish Audio with provider: fish-audio plus Fish API credentials.

- 图片素材标注：启动或命令触发时用视觉模型给本地图片打标签，用于表情包检索。

## 模型分工

- 会话模型：跟随 `/llm model` 的频道默认；`model` 为空时不固定。
- 评估模型：`eval_model`，当前配置为 `doubao`，用于稳定 JSON 输出。
- 视觉模型：`image_tag_model`，同时用于本地图片打标和用户入站图片描述。
- 记忆 embedding：`memory_embedding_model`，当前使用 Ark 多模态 embedding endpoint。

## 常用命令

```text
/llmchat tag-images
/llmchat retag-images
/llmchat retag-images-all
```

这些命令仅超管可用；打标任务异步执行，进度会分批回报。

## 关键配置

```yaml
plugins:
  llm_chat:
    $filter: channel.type is public
    persona: Your character prompt
    allowed_commands: ["echo", "help"]
    eval_model: doubao
    image_tag_model: doubao
    image_understanding_enabled: true
    image_describe_max_per_message: 3
    tag_batch_size: 50
    memory_enabled: true
    memory_embedding_model: "volcengine/doubao-embedding-vision-251215"
    memory_embedding_api_key: ${{ env.DOUBAO_API_KEY }}
    memory_embedding_base_url: "https://ark.cn-beijing.volces.com/api/v3"
```

## 日志与安全

`entari.yml` 已通过 `basic.log.ignores: ["openai*"]` 屏蔽 OpenAI SDK 的 DEBUG 请求体 dump，避免视觉请求把 base64 图片写入日志；`server.options.access_log: false` 关闭 uvicorn access log，避免 `/api/health` 轮询刷屏。

## 验收点

- 纯图 `@bot` 会回复，且内容对得上图。
- 图文混合消息会把图片内容纳入回复。
- 引用图片并询问时，会使用 `[引用图片: ...]` 描述。
- A 群和 B 群不同图片不会串描述；缓存 key 使用完整图片 URL。
- 切换无视觉会话模型后，图片仍能被理解，因为视觉预处理独立于会话模型。
