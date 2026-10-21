"""Operator-visible configuration schema and nested persona validation contracts."""

from __future__ import annotations

from uuid import uuid4
import asyncio
from datetime import datetime, timezone, timedelta

import pytest
from jsonschema import ValidationError as SchemaValidationError, Draft202012Validator
import pytest_asyncio
from arclet.entari.config import config_model_validate
from arclet.entari.plugin import niche
from arclet.entari.plugin.model import PluginMetadata

from utils.companion_core import CompanionLimits, contact_ready
from plugins.llm_chat.config import LLMChatConfig
from plugins.llm_chat.config_schema import localize_llm_chat_schema


@pytest_asyncio.fixture
async def schema_owner():
    owner = niche(f"llm-chat-schema-{uuid4().hex}", config={})(lambda _plugin: None)
    owner.exec_apply()
    owner.metadata = PluginMetadata(name="llm_chat", config=LLMChatConfig)
    owner.register_schema(fragment=localize_llm_chat_schema, replace=True)
    try:
        yield owner
    finally:
        pending = owner.dispose()
        if pending:
            await asyncio.gather(*pending)


def _persona_schema(schema):
    ref = schema["properties"]["personas"]["additionalProperties"]["$ref"]
    name = ref.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
    return schema["$defs"][name]


def test_llm_chat_metadata_exposes_localized_nested_persona_schema(schema_owner) -> None:
    assert schema_owner.metadata.config is LLMChatConfig
    schema = schema_owner.config_schema()
    properties = schema["properties"]
    assert {"default_persona", "personas", "allowed_commands"} <= properties.keys()
    assert not {"persona", "self_reference_image"} & properties.keys()
    persona_schema = _persona_schema(schema)
    assert {"name", "prompt", "reference_image", "appearance"} == persona_schema["properties"].keys()
    config_fields = [item for name, item in properties.items() if not name.startswith(("$", "."))]
    for item in [*config_fields, *persona_schema["properties"].values()]:
        for field in ("title", "description"):
            assert any("\u4e00" <= char <= "\u9fff" for char in item[field])


def test_nested_persona_configuration_resolves_the_selected_profile() -> None:
    configured = config_model_validate(
        LLMChatConfig,
        {
            "default_persona": "second",
            "personas": {
                "first": {"name": "First", "prompt": "First personality"},
                "second": {"name": "Second", "prompt": "Second personality", "appearance": "Silver hair"},
            },
        },
    )
    selected = configured.personas[configured.default_persona]
    assert selected.name == "Second"
    assert selected.appearance == "Silver hair"


@pytest.mark.parametrize(
    "invalid",
    [{"default_persona": "missing"}, {"personas": {}}],
)
def test_unresolvable_persona_configuration_is_rejected(invalid: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError), match="persona|Persona|reference_image"):
        config_model_validate(LLMChatConfig, invalid)


@pytest.mark.parametrize("cap", [0, 24, 48, 49, 96, 1000])
def test_contact_caps_are_accepted_by_native_schema_and_runtime(cap: int, schema_owner) -> None:
    schema = schema_owner.config_schema()
    validator = Draft202012Validator(schema["properties"]["companion_daily_contacts"])
    validator.validate(cap)
    configured = config_model_validate(LLMChatConfig, {"companion_daily_contacts": cap})
    now = datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc)
    facts = {
        "last_contact": None,
        "last_user_at": now - timedelta(minutes=20),
        "limits": CompanionLimits(daily_contacts=configured.companion_daily_contacts),
    }
    assert contact_ready(now, contacts_today=max(0, cap - 1), **facts) is (cap > 0)
    assert not contact_ready(now, contacts_today=cap, **facts)


@pytest.mark.parametrize("cap", [-1, True, 1.5, "96"])
def test_unsupported_contact_caps_are_rejected_by_native_schema_and_runtime(cap: object, schema_owner) -> None:
    schema = schema_owner.config_schema()
    validator = Draft202012Validator(schema["properties"]["companion_daily_contacts"])
    with pytest.raises(SchemaValidationError):
        validator.validate(cap)
    with pytest.raises((TypeError, ValueError)):
        config_model_validate(LLMChatConfig, {"companion_daily_contacts": cap})


@pytest.mark.parametrize("limit", [1, 10, 50])
def test_channel_context_bounds_are_accepted_by_native_schema_and_runtime(limit, schema_owner) -> None:
    Draft202012Validator(schema_owner.config_schema()).validate({"channel_context_messages": limit})
    assert config_model_validate(LLMChatConfig, {"channel_context_messages": limit}).channel_context_messages == limit


@pytest.mark.parametrize("limit", [0, 51, True])
def test_channel_context_bounds_are_rejected_by_native_schema_and_runtime(limit, schema_owner) -> None:
    with pytest.raises(SchemaValidationError):
        Draft202012Validator(schema_owner.config_schema()).validate({"channel_context_messages": limit})
    with pytest.raises((TypeError, ValueError)):
        config_model_validate(LLMChatConfig, {"channel_context_messages": limit})


def test_native_nested_schema_references_validate_in_embedded_consumer(schema_owner) -> None:
    embedded = schema_owner.config_schema(ref_root="/properties/llm_chat/")
    schema = {"type": "object", "properties": {"llm_chat": embedded}}
    configured = {
        "default_persona": "custom",
        "personas": {"custom": {"name": "Custom", "prompt": "Independent persona", "appearance": "Silver hair"}},
    }
    Draft202012Validator(schema).validate({"llm_chat": configured})
    assert config_model_validate(LLMChatConfig, configured).personas["custom"].appearance == "Silver hair"


@pytest.mark.parametrize(
    "persona",
    [
        {"name": "Custom"},
        {"prompt": "Independent persona"},
        {"name": "Custom", "prompt": "Independent persona", "appearance": 7},
    ],
)
def test_native_nested_schema_and_runtime_reject_invalid_persona(persona, schema_owner) -> None:
    configured = {"default_persona": "custom", "personas": {"custom": persona}}
    with pytest.raises(SchemaValidationError):
        Draft202012Validator(schema_owner.config_schema()).validate(configured)
    with pytest.raises((TypeError, ValueError)):
        config_model_validate(LLMChatConfig, configured)


def test_native_nested_schema_rejects_unknown_persona_fields(schema_owner) -> None:
    configured = {
        "default_persona": "custom",
        "personas": {"custom": {"name": "Custom", "prompt": "Independent persona", "unknown": True}},
    }
    with pytest.raises(SchemaValidationError):
        Draft202012Validator(schema_owner.config_schema()).validate(configured)
