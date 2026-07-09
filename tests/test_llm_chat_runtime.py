"""Runtime regression tests for llm_chat review fixes."""

from __future__ import annotations

import sys
import json
from types import ModuleType, SimpleNamespace
import base64
from typing import Any, cast
from pathlib import Path

import pytest
from arclet.entari import Image, Session
from arclet.entari.config import EntariConfig

import plugins as _PLUGINS

_PACKAGE = ModuleType("plugins.llm_chat")
setattr(_PACKAGE, "__path__", [str(Path(__file__).resolve().parents[1] / "plugins" / "llm_chat")])
sys.modules.setdefault("plugins.llm_chat", _PACKAGE)
setattr(_PLUGINS, "llm_chat", _PACKAGE)
if not hasattr(EntariConfig, "instance"):
    setattr(EntariConfig, "instance", EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.yml"))

from plugins.llm_chat import vision as vision_module, chat_context as chat_context_module
from plugins.llm_chat.tools import is_command_allowed
from plugins.llm_chat.config import LLMChatConfig
from plugins.llm_chat.vision import (
    VISION_TAG_TIMEOUT,
    IMAGE_FETCH_MAX_BYTES,
    VISION_DESCRIBE_TIMEOUT,
    vision_completion,
    raw_to_image_data_url,
    image_file_to_data_url,
)
from plugins.llm_chat.persona import embedding as embedding_module
from plugins.llm_chat.chat_context import (
    build_chat_messages,
    collect_message_images,
    model_supports_image_input,
    build_multimodal_user_content,
)
from plugins.llm_chat.persona.embedding import embed_text
from plugins.llm_chat.persona.memory_update import resolve_fact_embedding_update

sys.modules.pop("plugins.llm_chat", None)
if getattr(_PLUGINS, "llm_chat", None) is _PACKAGE:
    delattr(_PLUGINS, "llm_chat")

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)
_WEBP_BYTES = base64.b64decode("UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA")


class _EmbeddingConfig:
    memory_enabled = True
    memory_embedding_model = "volcengine/doubao-embedding-vision-251215"
    memory_embedding_api_key: str | None = None
    memory_embedding_base_url = "https://ark.cn-beijing.volces.com/api/v3"
    memory_top_profile_facts = 8
    memory_top_memories = 5
    memory_min_similarity = 0.25
    memory_dedup_similarity = 0.92
    profile_value_similarity = 0.9
    profile_fact_min_confidence = 0.55
    memory_max_records_per_user = 200


class _Elements:
    def __init__(self, images: list[Image]) -> None:
        self._images = images

    def select(self, element_type: type[Any]) -> list[Image]:
        return self._images if element_type is Image else []


class _ImageSession:
    def __init__(self, direct: Image, quoted: Image, downloads: dict[str, bytes] | None = None) -> None:
        self.elements = _Elements([direct])
        self.quote = SimpleNamespace(children=[quoted])
        self._downloads = downloads or {}

    async def download(self, src: str) -> bytes:
        return self._downloads[src]


@pytest.mark.asyncio
async def test_missing_multimodal_embedding_key_skips_http(monkeypatch: pytest.MonkeyPatch):
    created = False
    embedding_module._missing_embedding_key_warned = False

    class SentinelAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            nonlocal created
            created = True
            raise AssertionError("HTTP client must not be created")

    monkeypatch.setattr(embedding_module.httpx, "AsyncClient", SentinelAsyncClient)

    assert await embed_text(_EmbeddingConfig(), "hello") is None
    assert not created


def test_raw_to_image_data_url_sniffs_png_and_rejects_invalid_bytes():
    data_url = raw_to_image_data_url(_PNG_BYTES)

    assert data_url is not None
    assert data_url.startswith("data:image/png")
    assert raw_to_image_data_url(b"not image") is None


def test_image_file_to_data_url_rejects_files_over_limit(tmp_path):
    oversized = tmp_path / "large.png"
    oversized.write_bytes(_PNG_BYTES + b"0" * (IMAGE_FETCH_MAX_BYTES + 1 - len(_PNG_BYTES)))

    assert image_file_to_data_url(oversized) is None


def test_image_file_to_data_url_sniffs_webp_without_suffix_guessing(tmp_path):
    image_path = tmp_path / "sample.webp"
    image_path.write_bytes(_WEBP_BYTES)

    data_url = image_file_to_data_url(image_path)

    assert data_url is not None
    assert data_url.startswith("data:image/webp")
    assert not data_url.startswith("data:image/jpeg")


def test_resolve_fact_embedding_update_clears_stale_embedding_on_replacement():
    embedding_json, should_update = resolve_fact_embedding_update("coffee", "coffee", None, [1.0], None)

    assert embedding_json == ""
    assert should_update


def test_resolve_fact_embedding_update_keeps_existing_embedding_for_retained_value():
    embedding_json, should_update = resolve_fact_embedding_update("tea", "coffee", [0.0], [1.0], None)

    assert embedding_json == ""
    assert not should_update


def test_resolve_fact_embedding_update_backfills_missing_existing_embedding():
    embedding_json, should_update = resolve_fact_embedding_update("tea", "coffee", [0.0], None, [1.0, 0.0])

    assert json.loads(embedding_json) == [1.0, 0.0]
    assert should_update


@pytest.mark.asyncio
async def test_vision_completion_forwards_timeout(monkeypatch: pytest.MonkeyPatch):
    seen: list[float] = []

    async def fake_acompletion(*args: object, **kwargs: object) -> object:
        seen.append(cast(float, kwargs["timeout"]))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setattr(
        vision_module,
        "get_model_config",
        lambda name: SimpleNamespace(name="vision-model", base_url="https://example.test", api_key="key", extra={}),
    )
    monkeypatch.setattr(vision_module.litellm, "acompletion", fake_acompletion)
    config = LLMChatConfig()

    await vision_completion(config, "data:image/png;base64,AA==", "system", "describe", timeout=VISION_DESCRIBE_TIMEOUT)
    await vision_completion(config, "data:image/png;base64,AA==", "system", "tag", timeout=VISION_TAG_TIMEOUT)

    assert seen == [VISION_DESCRIBE_TIMEOUT, VISION_TAG_TIMEOUT]


def test_collect_message_images_returns_direct_then_quoted():
    direct = Image.of(url="local://direct")
    quoted = Image.of(url="local://quoted")
    session = cast(Session, _ImageSession(direct, quoted))

    images = collect_message_images(session)

    assert images == [(direct, False), (quoted, True)]


def test_model_supports_image_input_uses_litellm(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(chat_context_module.litellm, "supports_vision", lambda model: model == "vision-model")

    assert model_supports_image_input("vision-model")
    assert not model_supports_image_input("text-model")
    assert not model_supports_image_input(None)


@pytest.mark.asyncio
async def test_build_multimodal_user_content_attaches_images_without_description():
    direct = Image.of(url="local://direct")
    quoted = Image.of(url="local://quoted")
    session = cast(
        Session,
        _ImageSession(
            direct,
            quoted,
            {"local://direct": _PNG_BYTES, "local://quoted": _WEBP_BYTES},
        ),
    )
    config = LLMChatConfig()
    config.image_describe_max_per_message = 2

    warnings: list[str] = []
    current_content, stored_text = await build_multimodal_user_content(
        config, session, "Alice", "看这个", warnings.append
    )

    assert "[图片" in stored_text
    assert "[引用图片" in stored_text
    assert isinstance(current_content, list)
    image_urls = [part["image_url"]["url"] for part in current_content if part.get("type") == "image_url"]
    assert len(image_urls) == 2
    assert image_urls[0].startswith("data:image/png")
    assert image_urls[1].startswith("data:image/webp")
    assert warnings == []

    messages = build_chat_messages([], "Alice", stored_text, current_content)
    assert messages == [{"role": "user", "content": current_content}]


@pytest.mark.asyncio
async def test_build_multimodal_user_content_falls_back_to_text_when_image_unavailable():
    direct = Image.of(url="local://missing")
    quoted = Image.of(url="local://quoted")
    session = cast(Session, _ImageSession(direct, quoted))
    warnings: list[str] = []

    current_content, stored_text = await build_multimodal_user_content(
        LLMChatConfig(), session, "Alice", "", warnings.append
    )

    assert isinstance(current_content, str)
    assert current_content.startswith("[Alice]:")
    assert "[图片" in stored_text
    assert warnings == [
        "image passthrough skipped: image data unavailable",
        "image passthrough skipped: image data unavailable",
    ]


def test_allowed_commands_default_closed():
    assert LLMChatConfig().allowed_commands == []


@pytest.mark.parametrize(
    ("command_line", "allowed_commands", "expected"),
    [
        ("", ["echo"], (False, "")),
        ("echo hi", ["echo"], (True, "echo")),
        ("ban user", ["echo"], (False, "ban")),
    ],
)
def test_is_command_allowed(command_line: str, allowed_commands: list[str], expected: tuple[bool, str]):
    assert is_command_allowed(command_line, allowed_commands) == expected
