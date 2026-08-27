"""Localized WebUI schema for llm_chat configuration."""

from pydantic import GetJsonSchemaHandler
from pydantic_core import CoreSchema
from arclet.entari.config import config_model_schema
from pydantic.json_schema import JsonSchemaValue
from arclet.entari.config.models.pyd import BaseModel

from .config import LLMChatConfig

_CONFIG_SCHEMA_TEXT = {
    "persona": ("人格设定", "仅填写角色人格文本；框架规则由系统提示词脚手架维护。"),
    "context_window": ("对话上下文条数", "每次回复加载的历史消息条数。"),
    "tool_context_max_events": ("工具上下文事件数", "每次生成最多注入的近期工具执行事件数。"),
    "tool_context_max_chars": ("工具上下文字符数", "近期工具活动序列化后最多注入的字符数。"),
    "tool_history_max_records_per_channel": ("频道工具历史保留数", "每个频道最多保留的工具执行历史记录数。"),
    "merged_forward_fetch_timeout": ("合并转发读取超时", "单次调用 OneBot get_forward_msg 的超时时间（秒）。"),
    "merged_forward_max_messages": ("合并转发节点上限", "单次生成最多读取的合并转发消息节点数。"),
    "merged_forward_max_chars_per_message": ("单节点字符上限", "每个合并转发节点规范化后最多保留的字符数。"),
    "merged_forward_max_total_chars": ("合并转发总字符上限", "单次生成中合并转发内容的总字符上限。"),
    "merged_forward_max_described_images": (
        "合并转发图片理解上限",
        "单次生成最多交给视觉模型描述的合并转发图片数。",
    ),
    "channel_message_max_images": ("频道历史图片上限", "每页频道历史中最多作为本轮引用暴露的图片数。"),
    "self_reference_image": ("自身参考图片", "用于生成自身主题原生图片的 resources/image 下相对路径。"),
    "model": ("主聊天模型", "会话使用的模型别名；留空时使用 LLM 插件默认模型。"),
    "eval_model": ("关系评估模型", "关系评估使用的模型别名；留空时使用主聊天模型。"),
    "model_request_timeout": ("主模型请求超时", "普通聊天模型单次请求的超时时间（秒）。"),
    "media_request_timeout": ("媒体请求超时", "用户明确请求媒体时，单次模型请求的超时时间（秒）。"),
    "image_generation_model": (
        "图片生成模型",
        "原创图片生成使用的独立模型别名；留空时不注册 generate_image 工具。",
    ),
    "image_generation_timeout": ("图片生成超时", "单次原创图片生成请求的最长等待时间（秒）。"),
    "image_generation_quality": ("图片生成质量", "原创图片生成请求使用的固定质量等级。"),
    "image_generation_output_format": ("图片输出格式", "原创图片发送前要求供应商返回的图片格式。"),
    "image_generation_output_compression": (
        "图片输出压缩率",
        "JPEG 与 WebP 原创图片输出使用的压缩百分比。",
    ),
    "eval_request_timeout": ("评估请求超时", "关系评估模型单次请求的超时时间（秒）。"),
    "eval_every_n": ("关系评估间隔", "每位用户累计多少次机器人回复后运行一次关系评估。"),
    "eval_context_window": ("评估上下文条数", "关系评估时包含的近期历史消息条数。"),
    "memory_enabled": ("启用长期记忆", "是否启用用户画像与语义记忆的检索和更新。"),
    "memory_embedding_model": (
        "记忆嵌入模型",
        "用于画像和记忆向量化的嵌入模型；名称包含 -vision- 时使用 Ark 多模态接口。",
    ),
    "memory_embedding_api_key": (
        "记忆嵌入 API 密钥",
        "嵌入服务 API 密钥，建议通过 entari.yml 的环境变量注入。",
    ),
    "memory_embedding_base_url": ("记忆嵌入 API 地址", "嵌入服务的基础 URL。"),
    "memory_top_profile_facts": ("画像事实注入上限", "主聊天提示词最多注入的稳定画像事实数。"),
    "memory_top_memories": ("相关记忆注入上限", "主聊天提示词最多注入的语义相关记忆数。"),
    "memory_min_importance": ("记忆最低重要度", "持久化并允许进入提示词的情景记忆最低重要度。"),
    "memory_min_similarity": ("记忆最低相似度", "检索相关情景记忆时要求的最低余弦相似度。"),
    "memory_dedup_similarity": ("记忆去重相似度", "将新记忆视为已存记忆重复项的余弦相似度阈值。"),
    "memory_prompt_dedup_similarity": (
        "提示词记忆去重相似度",
        "仅在提示词读取视图中折叠相似记忆的余弦相似度阈值。",
    ),
    "profile_alias_similarity": ("画像别名相似度", "读取时将同类别画像值归为别名的余弦相似度阈值。"),
    "memory_eval_profile_fact_limit": ("评估画像事实上限", "关系评估器最多接收的分组画像事实数。"),
    "profile_value_similarity": (
        "画像值相同阈值",
        "将画像补丁值视为同一事实并增强置信度的余弦相似度阈值。",
    ),
    "profile_fact_min_confidence": ("画像事实最低置信度", "存储并注入稳定画像事实所需的最低置信度。"),
    "memory_max_records_per_user": ("每用户记忆保留上限", "每个用户和频道最多保留的情景记忆记录数。"),
    "web_search_enabled": ("启用网页工具", "是否为 llm_chat 注册 Exa 搜索和网页正文读取工具。"),
    "web_search_max_calls_per_generation": ("单轮搜索调用上限", "单次生成允许调用 web_search 的最大次数。"),
    "web_page_max_calls_per_generation": (
        "单轮网页读取上限",
        "单次生成允许调用网页读取和截图工具的最大次数。",
    ),
    "web_total_max_calls_per_generation": ("单轮网页工具总上限", "单次生成允许调用全部网页工具的总次数上限。"),
    "exa_api_key": ("Exa API 密钥", "Exa API 密钥，建议通过 entari.yml 的环境变量注入。"),
    "exa_search_type": ("Exa 搜索模式", "web_search 使用的 Exa 搜索算法。"),
    "exa_search_category": ("Exa 搜索类别", "应用于每次搜索的可选 Exa 数据类别。"),
    "exa_include_domains": ("Exa 包含域名", "Exa 搜索允许包含的域名列表；留空表示不限制。"),
    "exa_exclude_domains": ("Exa 排除域名", "Exa 搜索需要排除的域名列表。"),
    "exa_start_published_date": ("发布日期下限", "搜索结果发布日期的可选起始时间，使用 ISO 8601 格式。"),
    "exa_end_published_date": ("发布日期上限", "搜索结果发布日期的可选结束时间，使用 ISO 8601 格式。"),
    "web_search_max_results": ("搜索结果数量", "每次搜索最多返回给模型的结果数。"),
    "web_search_timeout": (
        "网页请求超时",
        "单次 Exa 请求的超时时间（秒），运行时会限制在供应商允许范围内。",
    ),
    "web_page_max_chars": ("网页正文字符上限", "单个网页最多返回给模型的正文字符数。"),
    "delivery_min_interval_seconds": ("发送最小间隔", "两次消息交付尝试之间允许的最小节拍间隔（秒）。"),
    "delivery_default_interval_seconds": ("发送默认间隔", "模型未指定节拍时使用的默认消息间隔（秒）。"),
    "delivery_max_interval_seconds": ("发送最大间隔", "模型可请求的最大消息节拍间隔（秒）。"),
    "delivery_max_text_messages_per_generation": ("单轮文本消息上限", "单次生成最多允许发送的普通文本消息数。"),
    "delivery_max_text_chars_per_message": ("单条文本字符上限", "每条普通文本消息规范化后的最大字符数。"),
    "delivery_max_forward_nodes": ("合并转发节点发送上限", "单次合并转发最多包含的规范化节点数。"),
    "delivery_max_forward_chars_per_node": ("转发节点字符上限", "每个合并转发节点规范化后的最大字符数。"),
    "delivery_max_total_text_chars_per_generation": (
        "单轮文本总字符上限",
        "单次生成通过工具交付的文本总字符上限。",
    ),
    "delivery_max_media_messages_per_generation": (
        "单轮媒体消息上限",
        "单次生成在文字交付前最多允许发送的媒体消息数。",
    ),
    "tts_enabled": ("启用语音合成", "安装 tts_service 插件时，是否启用语音合成工具。"),
    "tts_max_chars": ("语音合成字符上限", "speak 工具单次合成最多保留的字符数，优先在句子边界截断。"),
    "image_tags_enabled": ("启用图片标注", "启动时是否使用视觉模型为本地图片生成检索标签。"),
    "image_tag_model": ("图片标注模型", "图片标注使用的模型别名或名称；留空时使用 LLM 插件默认模型。"),
    "image_tag_prompt": ("图片标注提示词", "视觉模型提取图片标签时使用的系统提示词。"),
    "image_understanding_enabled": (
        "启用图片理解",
        "是否启用入站图片理解；支持视觉的聊天模型会直接接收图片。",
    ),
    "image_describe_prompt": ("图片描述提示词", "聊天模型无法直接接收图片时，视觉描述使用的备用提示词。"),
    "image_describe_max_per_message": (
        "单条消息图片理解上限",
        "每条入站消息最多附加或描述的图片数，超出部分仅保留占位符。",
    ),
    "tag_batch_size": ("图片标注批次上限", "每次启动标注流程最多处理的图片数。"),
    "tag_concurrency": ("图片标注并发数", "图片标注和嵌入请求的最大并发数。"),
    "image_match_min_similarity": ("图片匹配最低相似度", "语义检索本地图片时要求的最低余弦相似度。"),
    "image_top_candidates": ("图片候选池大小", "语义检索后用于随机选择的高相关候选数量。"),
    "allowed_commands": ("工具命令白名单", "call_plugin 工具允许调用的插件命令列表。"),
}


class LLMChatWebUIConfig(BaseModel):
    """Localized schema adapter used only by Entari WebUI."""

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        del core_schema, handler
        schema = config_model_schema(LLMChatConfig, ref_root="/")
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            raise TypeError("LLMChatConfig schema properties must be an object")
        missing = set(properties) - _CONFIG_SCHEMA_TEXT.keys()
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(f"Missing WebUI translations for llm_chat config fields: {names}")
        for name, property_schema in properties.items():
            if not isinstance(property_schema, dict):
                raise TypeError(f"Schema for llm_chat config field {name!r} must be an object")
            title, description = _CONFIG_SCHEMA_TEXT[name]
            property_schema["title"] = title
            property_schema["description"] = description
        schema["title"] = "LLM 聊天配置"
        schema["description"] = "群聊会话、记忆、网页工具、交付、语音与图片能力配置。"
        return schema
