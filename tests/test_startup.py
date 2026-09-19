"""Regression coverage for write-free validation and portable initialization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from utils.startup.cli import main
from utils.startup.validation import initialize_directories, validate_configuration
from utils.startup.configuration import load_configuration


@pytest.fixture
def startup_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ENTARI_CONFIG_FILE", raising=False)
    monkeypatch.delenv("ENTARI_CONFIG_EXTENSION", raising=False)
    from arclet.entari.config import EntariConfig

    previous = getattr(EntariConfig, "instance", None)
    inited = EntariConfig._inited
    yield tmp_path
    EntariConfig._inited = inited
    if previous is None:
        if hasattr(EntariConfig, "instance"):
            del EntariConfig.instance
    else:
        EntariConfig.instance = previous


def write_config(root: Path, plugins: dict, name: str = "entari.yml") -> Path:
    path = root / name
    path.write_text(json.dumps({"basic": {}, "plugins": plugins}), encoding="utf-8")
    return path


def test_missing_database_parent_is_checked_without_writes_then_prepared(startup_root, capsys):
    source = write_config(startup_root, {"database": {"name": "new/nested/chtholly.db"}})
    original = source.read_bytes()

    assert main(["--check"]) == 0
    assert not (startup_root / "new").exists()
    assert "initialized" in capsys.readouterr().out
    assert main(["--prepare"]) == 0
    assert (startup_root / "new/nested").is_dir()
    assert not (startup_root / "new/nested/chtholly.db").exists()
    assert source.read_bytes() == original


def test_invalid_enabled_features_are_aggregated_before_any_writes(startup_root, capsys):
    source = write_config(
        startup_root,
        {
            "database": {"name": "must-not-exist/state.db"},
            "server": {"host": "0.0.0.0", "adapters": [{"$path": "@qq:QQBotWebsocketAdapter"}]},
            "plugin_workshop": {},
            "llm": {"models": []},
            "tts_service": {"provider": "fish-audio", "fish_api_key": ""},
        },
    )
    original = source.read_bytes()
    assert main(["--prepare"]) == 1
    error = capsys.readouterr().err
    for missing in ("webui", "password", "app_id", "secret", "loopback", "llm.models", "fish_api_key"):
        assert missing in error
    assert not (startup_root / "must-not-exist").exists()
    assert source.read_bytes() == original


def test_native_disabled_features_do_not_require_credentials(startup_root, monkeypatch):
    monkeypatch.setenv("DISABLE_STARTUP_TEST_TTS", "yes")
    write_config(
        startup_root,
        {
            "~llm": {"models": []},
            "~plugin_workshop": {},
            "tts_service": {
                "$disable": "env.DISABLE_STARTUP_TEST_TTS == 'yes'",
                "provider": "fish-audio",
                "fish_api_key": "",
            },
            "~server": {"adapters": [{"$path": "@qq:QQBotWebsocketAdapter"}]},
        },
    )
    assert main(["--check"]) == 0


def test_source_precedence_uses_native_dotenv_without_disclosing_values(startup_root, monkeypatch, capsys):
    for name in ("entari.yml", "entari.local.yml", "environment.yml", "explicit.yml"):
        write_config(startup_root, {"~llm": {"api_key": "${{ env.STARTUP_TEST_SECRET }}"}}, name)
    (startup_root / ".env").write_text(
        "ENTARI_CONFIG_FILE=environment.yml\nSTARTUP_TEST_SECRET=never-print-this-secret\n", encoding="utf-8"
    )
    assert load_configuration(Path("explicit.yml")).native.path == startup_root / "explicit.yml"
    assert load_configuration().native.path == startup_root / "environment.yml"
    monkeypatch.setenv("ENTARI_CONFIG_FILE", "")
    assert load_configuration().native.path == startup_root / "entari.local.yml"
    (startup_root / "entari.local.yml").unlink()
    assert load_configuration().native.path == startup_root / "entari.yml"
    assert main(["--check"]) == 0
    output = capsys.readouterr()
    assert "never-print-this-secret" not in output.out + output.err

    (startup_root / "invalid.yml").write_text("plugins: [never-print-this-secret", encoding="utf-8")
    assert main(["--config", "invalid.yml", "--prepare"]) == 1
    output = capsys.readouterr()
    assert "never-print-this-secret" not in output.out + output.err


def test_database_binds_preserve_existing_files_and_skip_memory(startup_root):
    existing = startup_root / "existing.db"
    existing.write_bytes(b"existing database bytes are not startup's to rewrite")
    write_config(
        startup_root,
        {
            "database": {
                "name": str(existing),
                "binds": {
                    "archive": {"name": "archives/new/archive.db"},
                    "temporary": {"name": ":memory:"},
                },
            }
        },
    )
    config = load_configuration()
    result = validate_configuration(config)
    assert not result.issues
    assert not (startup_root / "archives").exists()
    initialize_directories(result)
    assert (startup_root / "archives/new").is_dir()
    assert not (startup_root / "archives/new/archive.db").exists()
    assert existing.read_bytes() == b"existing database bytes are not startup's to rewrite"


def test_selected_model_can_inherit_an_empty_endpoint_override(startup_root):
    write_config(
        startup_root,
        {
            "llm": {
                "base_url": "http://127.0.0.1:11434/v1",
                "models": [{"name": "openai/local", "base_url": ""}],
            }
        },
    )
    assert validate_configuration(load_configuration()).issues == ()


def test_enabled_onebot_without_optional_package_is_reported(startup_root, monkeypatch):
    from importlib.metadata import PackageNotFoundError

    from utils.startup import validation

    installed_version = validation.version

    def version(name):
        if name == "satori-python-adapter-onebot11":
            raise PackageNotFoundError(name)
        return installed_version(name)

    monkeypatch.setattr(validation, "version", version)
    write_config(
        startup_root,
        {"server": {"adapters": [{"$path": "@onebot11.reverse", "access_token": "synthetic-smoke-token"}]}},
    )
    assert any("--all-extras" in issue for issue in validate_configuration(load_configuration()).issues)
