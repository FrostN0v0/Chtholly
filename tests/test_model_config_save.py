"""Defend native YAML saves and rollback at the model hot-update boundary."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from hashlib import sha256
from pathlib import Path
from functools import partial
from contextlib import contextmanager
from dataclasses import field

import pytest
from arclet.entari import BasicConfModel
from arclet.entari.config import EntariConfig, config_model_validate

from utils.webui_config_core import ConfigValidationError
from plugins.webui_config_apply import saving
from plugins.webui_config_apply.model_reload import ModelReloadBindings, prepare_model_reload


class Model(BasicConfModel):
    name: str
    alias: str | None = None
    api_key: str | None = None
    base_url: str = "https://api.openai.com/v1"
    prompt: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class Config(BasicConfModel):
    api_key: str | None = None
    base_url: str = "https://api.openai.com/v1"
    prompt: str = ""
    models: list[Model] = field(default_factory=list)
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)


@pytest.fixture
def yaml_save(tmp_path, monkeypatch):
    path = tmp_path / "entari.yml"
    path.write_text(
        'basic:\n  log:\n    level: "info"\nplugins:\n'
        '  llm:\n    models:\n      - name: "old-model"\n'
        '  other:\n    title: "quoted"\n    notes: |\n      first line\n      second line\n    ratio: 0.5\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(EntariConfig, "instance", getattr(EntariConfig, "instance", None), raising=False)
    config = EntariConfig(path, env_vars={})
    runtime = config_model_validate(Config, {"models": [{"name": "old-model"}]})
    plug = SimpleNamespace(config=config.plugin["llm"], _is_disposed=False)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"$default": {"default_model": "old-model"}}), encoding="utf-8")
    bindings = ModelReloadBindings("llm", plug, runtime, Config, config_model_validate, state_path)
    monkeypatch.setattr(saving, "prepare_model_reload", partial(prepare_model_reload, bindings=bindings))
    saver = saving.ConfigSaver(sha256(path.read_bytes()).hexdigest(), restart_available=lambda: False)
    return SimpleNamespace(saver=saver, path=path, state_path=state_path, runtime=runtime)


def test_quoted_block_and_numeric_yaml_do_not_force_model_save_to_restart(yaml_save):
    env = yaml_save
    result = env.saver.save_section("plugins:llm", {"models": [{"name": "new-model"}]})
    assert result["applied"] is True
    assert result["application_mode"] == "hot_reload"
    assert result["restart_required"] is False
    assert json.loads(env.state_path.read_text())["$default"]["default_model"] == "new-model"
    assert env.runtime.models[0].name == "new-model"
    repeated = env.saver.save_section("plugins:llm", {"models": [{"name": "new-model"}]})
    assert repeated["application_mode"] == "unchanged"


def test_failed_state_repair_restores_exact_yaml_and_keeps_old_runtime(yaml_save):
    env = yaml_save
    original = env.path.read_bytes()
    inode = env.path.stat().st_ino
    env.state_path.write_bytes(b"{unreadable-state")
    with pytest.raises(ConfigValidationError) as error:
        env.saver.save_section("plugins:llm", {"models": [{"name": "new-model"}]})
    assert error.value.code == "model_state_failed"
    assert "unreadable-state" not in str(error.value)
    assert env.path.read_bytes() == original
    assert env.path.stat().st_ino == inode
    assert env.runtime.models[0].name == "old-model"
    assert env.state_path.read_bytes() == b"{unreadable-state"


def test_file_close_failure_cannot_publish_new_model_state(yaml_save, monkeypatch):
    env = yaml_save
    original = env.path.read_bytes()
    state = env.state_path.read_bytes()
    original_open = Path.open
    fail_close = True

    @contextmanager
    def closing_failure(stream):
        with stream:
            yield stream
        raise OSError("fixture close failure")

    def open_file(path, mode="r", *args, **kwargs):
        nonlocal fail_close
        stream = original_open(path, mode, *args, **kwargs)
        if path == env.path and mode == "r+b" and fail_close:
            fail_close = False
            return closing_failure(stream)
        return stream

    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(saving.SaveError):
        env.saver.save_section("plugins:llm", {"models": [{"name": "new-model"}]})
    assert env.path.read_bytes() == original
    assert env.state_path.read_bytes() == state
    assert env.runtime.models[0].name == "old-model"
