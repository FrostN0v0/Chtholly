"""Embedding API boundary for llm_chat persona memory."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import httpx
import litellm
from arclet.entari.logger import log

from ..core.errors import summarize_exception
from .config_types import LLMChatConfigLike

_LOGGER = log.wrapper("[llm_chat]")
_MULTIMODAL_MARKER = "-vision-"

_missing_embedding_key_warned = False


def _numbers(values: object) -> list[float]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    result: list[float] = []
    for value in values:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return []
        result.append(float(value))
    return result


def extract_embedding(response: object) -> list[float]:
    """Extract an embedding from dict-like or attribute-like LiteLLM responses."""
    data: object
    if isinstance(response, Mapping):
        data = response.get("data")
    else:
        data = getattr(response, "data", None)
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes, bytearray)) or not data:
        return []
    item = data[0]
    if isinstance(item, Mapping):
        embedding = item.get("embedding")
    else:
        embedding = getattr(item, "embedding", None)
    return _numbers(embedding)


async def _embed_multimodal(config: LLMChatConfigLike, text: str) -> list[float]:
    """Call Ark's multimodal endpoint; vision models reject plain /embeddings."""
    model = config.memory_embedding_model.split("/", 1)[-1]
    url = config.memory_embedding_base_url.rstrip("/") + "/embeddings/multimodal"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {config.memory_embedding_api_key}"},
            json={"model": model, "input": [{"type": "text", "text": text}]},
        )
        response.raise_for_status()
        payload: object = response.json()
    if not isinstance(payload, Mapping):
        return []
    data = payload.get("data")
    item = data[0] if isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)) and data else data
    if not isinstance(item, Mapping):
        return []
    return _numbers(item.get("embedding"))


async def embed_text(config: LLMChatConfigLike, text: str) -> list[float] | None:
    """Return an embedding or None; embedding failures never block replies."""
    global _missing_embedding_key_warned

    if not config.memory_enabled or not text.strip():
        return None
    if _MULTIMODAL_MARKER in config.memory_embedding_model and not (config.memory_embedding_api_key or "").strip():
        if not _missing_embedding_key_warned:
            _LOGGER.warning("embedding skipped: memory_embedding_api_key is required for multimodal embedding model")
            _missing_embedding_key_warned = True
        return None
    try:
        if _MULTIMODAL_MARKER in config.memory_embedding_model:
            embedding = await _embed_multimodal(config, text)
        else:
            response = await litellm.aembedding(
                model=config.memory_embedding_model,
                input=[text],
                api_key=config.memory_embedding_api_key,
                api_base=config.memory_embedding_base_url,
                encoding_format="float",
            )
            embedding = extract_embedding(response)
        return embedding or None
    except Exception as exc:
        _LOGGER.warning(f"embedding failed: {summarize_exception(exc)}")
        return None
