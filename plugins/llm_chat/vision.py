"""Vision-model boundary for inbound images and local image tagging."""

from __future__ import annotations

from typing import Protocol, cast

import litellm
from arclet.entari import Session
from entari_plugin_llm.config import get_model_config

from .config import LLMChatConfig
from .core.media import normalize_image_tags, normalize_image_description
from .core.image_source import fetch_image_data_url


class _MessageLike(Protocol):
    content: str | None


class _ChoiceLike(Protocol):
    message: _MessageLike


class _CompletionLike(Protocol):
    choices: list[_ChoiceLike]


VISION_DESCRIBE_TIMEOUT = 60.0
VISION_TAG_TIMEOUT = 120.0
_IMAGE_DESC_CACHE_MAX = 128
_image_desc_cache: dict[str, str] = {}


async def vision_completion(
    config: LLMChatConfig,
    data_url: str,
    system_prompt: str,
    user_text: str,
    *,
    timeout: float,
) -> str:
    """Call the configured vision model and return stripped text content."""
    model = get_model_config(config.image_tag_model)
    extra = {key: value for key, value in model.extra.items() if key not in {"timeout", "max_retries"}}
    response = await litellm.acompletion(
        model=model.name,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        base_url=model.base_url,
        api_key=model.api_key,
        timeout=timeout,
        max_retries=0,
        **extra,
    )
    completion = cast(_CompletionLike, response)
    content = completion.choices[0].message.content
    return (content or "").strip()


async def describe_image(config: LLMChatConfig, session: Session, src: str) -> str:
    """Describe one inbound image; empty string means bare placeholder."""
    cached = _image_desc_cache.get(src)
    if cached is not None:
        return cached
    data_url = await fetch_image_data_url(session, src)
    if data_url is None:
        return ""
    raw = await vision_completion(
        config,
        data_url,
        config.image_describe_prompt,
        "Describe this chat image for conversation context.",
        timeout=VISION_DESCRIBE_TIMEOUT,
    )
    description = normalize_image_description(raw)
    if description:
        if len(_image_desc_cache) >= _IMAGE_DESC_CACHE_MAX:
            _image_desc_cache.pop(next(iter(_image_desc_cache)))
        _image_desc_cache[src] = description
    return description


async def generate_image_tags(config: LLMChatConfig, data_url: str) -> str:
    """Generate normalized local-image tags."""
    raw = await vision_completion(
        config,
        data_url,
        config.image_tag_prompt,
        "Tag this image for chat reaction retrieval.",
        timeout=VISION_TAG_TIMEOUT,
    )
    return normalize_image_tags(raw)
