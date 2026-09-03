"""Regression tests for persisted LLM model selection repair."""

from __future__ import annotations

import json

import pytest

from plugins.llm_chat.core.model_state import ConfiguredModel, repair_model_state
from plugins.llm_chat.core.model_state_store import repair_model_state_file

_MODELS = [
    ConfiguredModel("gpt-5.6-sol", "gpt"),
    ConfiguredModel("openai/gemini-3.8-flash-high", "gemini"),
]


def _scope(data: dict[str, object], key: str) -> dict[str, object]:
    state = data[key]
    assert isinstance(state, dict)
    return state


def test_repairs_scoped_defaults_and_session_models_without_touching_sessions(tmp_path):
    current_sessions = {"onebot11:user": {"session_id": "session-a", "created_at": 42}}
    raw = {
        "$default": {
            "default_model": "gpt",
            "current_sessions": current_sessions,
            "session_models": {
                "session-a": "openai/gemini-3.7-flash-high",
                "session-b": "gemini",
            },
            "future_field": {"keep": True},
        },
        "channel-valid": {
            "default_model": "gemini",
            "current_sessions": {},
            "session_models": {},
        },
        "channel-stale": {
            "default_model": "openai/gemini-3.7-flash-high",
            "current_sessions": {},
            "session_models": {},
        },
        "channel-without-default": {
            "current_sessions": {},
            "session_models": {},
        },
    }

    repaired, report = repair_model_state(raw, _MODELS)

    assert report.fallback_model == "gpt-5.6-sol"
    assert report.default_model_updates == 4
    assert report.session_model_updates == 2
    global_state = _scope(repaired, "$default")
    assert global_state["default_model"] == "gpt-5.6-sol"
    assert _scope(repaired, "channel-valid")["default_model"] == "openai/gemini-3.8-flash-high"
    assert _scope(repaired, "channel-stale")["default_model"] == "gpt-5.6-sol"
    assert _scope(repaired, "channel-without-default")["default_model"] == "gpt-5.6-sol"
    assert global_state["session_models"] == {
        "session-a": "gpt-5.6-sol",
        "session-b": "openai/gemini-3.8-flash-high",
    }
    assert global_state["current_sessions"] == current_sessions
    assert global_state["future_field"] == {"keep": True}
    assert raw["$default"]["default_model"] == "gpt"


def test_uses_first_configured_model_when_global_default_was_removed():
    repaired, report = repair_model_state(
        {
            "$default": {
                "default_model": "removed-model",
                "current_sessions": {},
                "session_models": {},
            },
            "channel": {
                "default_model": "also-removed",
                "current_sessions": {},
                "session_models": {},
            },
        },
        _MODELS,
    )

    assert report.fallback_model == "gpt-5.6-sol"
    assert _scope(repaired, "$default")["default_model"] == "gpt-5.6-sol"
    assert _scope(repaired, "channel")["default_model"] == "gpt-5.6-sol"


def test_repairs_legacy_state_without_changing_its_shape():
    repaired, report = repair_model_state(
        {
            "default_model": "gemini",
            "current_sessions": {},
            "session_models": {"session-a": "removed-model"},
        },
        _MODELS,
    )

    assert "$default" not in repaired
    assert report.fallback_model == "openai/gemini-3.8-flash-high"
    assert repaired["default_model"] == "openai/gemini-3.8-flash-high"
    assert repaired["session_models"] == {"session-a": "openai/gemini-3.8-flash-high"}


def test_creates_global_state_for_an_empty_state_file():
    repaired, report = repair_model_state({}, _MODELS)

    assert report.default_model_updates == 1
    assert repaired == {
        "$default": {
            "default_model": "gpt-5.6-sol",
            "current_sessions": {},
            "session_models": {},
        }
    }


def test_no_configured_models_leave_state_untouched():
    raw = {"$default": {"default_model": "removed-model"}}

    repaired, report = repair_model_state(raw, [])

    assert repaired == raw
    assert report.fallback_model is None
    assert report.changed is False


def test_file_repair_is_atomic_and_idempotent(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "$default": {
                    "default_model": "gpt",
                    "current_sessions": {},
                    "session_models": {"session-a": "removed-model"},
                }
            }
        ),
        encoding="utf-8",
    )

    first = repair_model_state_file(path, _MODELS)
    first_bytes = path.read_bytes()
    second = repair_model_state_file(path, _MODELS)

    assert first.changed is True
    assert second.changed is False
    assert path.read_bytes() == first_bytes
    assert list(tmp_path.glob("*.tmp")) == []


def test_file_repair_refuses_to_overwrite_invalid_json(tmp_path):
    path = tmp_path / "state.json"
    original = b"{invalid"
    path.write_bytes(original)

    with pytest.raises(ValueError, match="invalid state JSON"):
        repair_model_state_file(path, _MODELS)

    assert path.read_bytes() == original
