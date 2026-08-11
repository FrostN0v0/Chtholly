from __future__ import annotations

from pathlib import Path
from dataclasses import make_dataclass

import yaml
from arclet.entari.config import BasicConfModel, config_model_validate
from satori.adapters.qq.websocket import QQBotWebsocketConfig


def test_qq_sandbox_group_message_intent_is_nested_and_enabled() -> None:
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "entari.yml").read_text(encoding="utf-8"))
    adapter = next(item for item in config["plugins"]["server"]["adapters"] if item.get("$path") == "@qq.websocket")

    validated_type = make_dataclass("ValidatedQQConfig", [], bases=(QQBotWebsocketConfig, BasicConfModel))
    validated = config_model_validate(validated_type, adapter)

    assert validated.is_sandbox is True
    assert validated.intent.c2c_group_at_messages is True
    assert "c2c_group_at_messages" not in adapter
    assert not adapter.get("token")
