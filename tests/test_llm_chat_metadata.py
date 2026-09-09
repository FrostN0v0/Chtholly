"""Operator-visible configuration schema and nested persona validation contracts."""

from __future__ import annotations

import pytest
from arclet.entari.config import config_model_validate
from arclet.entari.plugin.model import PluginMetadata

from plugins.llm_chat.config import LLMChatConfig
from plugins.llm_chat.config_schema import LLMChatWebUIConfig


def test_llm_chat_metadata_exposes_localized_nested_persona_schema() -> None:
    schema = PluginMetadata(name="llm_chat", config=LLMChatWebUIConfig).get_config_schema()
    assert schema is not None
    properties = schema["properties"]
    assert {"default_persona", "personas", "allowed_commands"} <= properties.keys()
    assert not {"persona", "self_reference_image"} & properties.keys()
    persona_schema = properties["personas"]["additionalProperties"]
    assert {"name", "prompt", "reference_image", "appearance"} == persona_schema["properties"].keys()
    for item in [*properties.values(), *persona_schema["properties"].values()]:
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
