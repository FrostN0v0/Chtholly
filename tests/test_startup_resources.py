from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path
from dataclasses import replace
from importlib.metadata import distribution

import pytest

from utils.startup import resources


@pytest.fixture(autouse=True)
def isolated_resource_environment(monkeypatch):
    for name in (
        "TIKTOKEN_CACHE_DIR",
        "CUSTOM_TIKTOKEN_CACHE_DIR",
        "DATA_GYM_CACHE_DIR",
        "PLAYWRIGHT_BROWSERS_PATH",
        "INIT_CWD",
        "LITELLM_LOCAL_MODEL_COST_MAP",
    ):
        # Record absent keys too: configure_resources sets them directly.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)

    def deny_network(*args, **kwargs):
        raise AssertionError("Resource checks must not access the network")

    monkeypatch.setattr(resources, "urlopen", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)


def _executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"browser fixture: only executable presence is checked, never launched")
    path.chmod(0o755)


def _bundled_vocabulary():
    bundled = Path(str(distribution("litellm").locate_file("litellm/litellm_core_utils/tokenizers")))
    vocabulary = next(item for item in resources._vocabularies() if item.name == "cl100k_base")
    source = bundled / vocabulary.cache_key
    assert resources._valid_vocabulary(source, vocabulary), "Locked LiteLLM must ship its cl100k_base vocabulary"
    return vocabulary, source


def test_minimal_configuration_needs_no_resources_or_imports(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Disabled resources must not be inspected")

    monkeypatch.setattr(resources, "_driver", forbidden)
    monkeypatch.setattr(resources, "_vocabularies", forbidden)
    plugins = {".localdata": {"app_name": "chtholly"}, "database": {}, "echo": {}}
    resources.configure_resources(plugins, tmp_path)
    assert resources.resource_issues(plugins, tmp_path) == []
    resources.prepare_resources(plugins, tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert "TIKTOKEN_CACHE_DIR" not in os.environ


def test_renderer_cache_does_not_follow_legacy_browser_cache(tmp_path, monkeypatch):
    inherited = tmp_path / "legacy"
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(inherited))
    plugins = {
        ".localdata": {"app_name": "Chtholly", "base_dir": "state"},
        "browser": {},
        "htmlrender": {"provider": "playwright"},
    }
    legacy, renderer = resources._browsers(plugins, tmp_path)
    legacy_executable, legacy_cache, _ = resources._browser_location(legacy, tmp_path)
    renderer_executable, renderer_cache, _ = resources._browser_location(renderer, tmp_path)
    assert legacy_cache == inherited
    assert renderer_cache == tmp_path / "state" / "cache" / "htmlrender" / "playwright"
    assert legacy_executable is not None
    assert renderer_executable is not None
    _executable(legacy_executable)
    issues = resources.resource_issues(plugins, tmp_path)
    assert len(issues) == 1
    assert "htmlrender" in issues[0]
    _executable(renderer_executable)
    assert resources.resource_issues(plugins, tmp_path) == []
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(inherited)


def test_renderer_explicit_storage_and_channel_select_actual_executable(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "unrelated"))
    config = {"provider": "playwright", "provider_config": {"storage_path": "render-cache", "channel": "chromium"}}
    plugins = {"htmlrender": config}
    browser = resources._browsers(plugins, tmp_path)[0]
    executable, cache, _ = resources._browser_location(browser, tmp_path)
    headless, _, _ = resources._browser_location(replace(browser, channel=None), tmp_path)
    assert cache == tmp_path / "render-cache"
    assert executable is not None
    assert headless is not None
    assert executable != headless
    _executable(headless)
    assert resources.resource_issues(plugins, tmp_path)
    _executable(executable)
    assert resources.resource_issues(plugins, tmp_path) == []


def test_global_localdata_cache_uses_framework_path(tmp_path):
    from nonestorage import user_cache_dir

    plugins = {".localdata": {"app_name": "MyBot", "use_global": True, "base_dir": "ignored"}}
    assert resources._local_cache(plugins, tmp_path) == user_cache_dir("Mybot").resolve()


def test_remote_browser_modes_need_no_local_executables(tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError("Remote modes must not start the local driver")

    monkeypatch.setattr(resources, "_driver", forbidden)
    plugins = {
        "browser": {"connect_endpoint": "ws://example.invalid/browser"},
        "htmlrender": {
            "provider": "playwright",
            "provider_config": {"connect_cdp": {"endpoint": "http://example.invalid"}},
        },
    }
    assert resources.resource_issues(plugins, tmp_path) == []
    resources.prepare_resources(plugins, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_explicit_browser_executable_is_checked_without_driver(tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError("Explicit executable needs no registry lookup")

    monkeypatch.setattr(resources, "_driver", forbidden)
    plugins = {"browser": {"executable_path": "custom/browser", "channel": "chrome"}}
    target = tmp_path / "custom" / "browser"
    assert resources.resource_issues(plugins, tmp_path)
    _executable(target)
    assert resources.resource_issues(plugins, tmp_path) == []
    target.write_bytes(b"")
    assert resources.resource_issues(plugins, tmp_path)


def test_prepare_deduplicates_selected_browser_stores_and_is_repeatable(tmp_path, monkeypatch):
    cache = tmp_path / "shared"
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    plugins = {
        "browser": {},
        "htmlrender": {"provider": "playwright", "provider_config": {"storage_path": str(cache)}},
    }
    installs = []

    def install(arguments, env, root):
        installs.append((arguments, env["PLAYWRIGHT_BROWSERS_PATH"]))
        for browser in resources._browsers(plugins, root):
            executable, _, _ = resources._browser_location(browser, root)
            assert executable is not None
            _executable(executable)

    monkeypatch.setattr(resources, "_run_installer", install)
    resources.prepare_resources(plugins, tmp_path)
    assert installs == [(["install", "chromium"], str(cache))]
    assert resources.resource_issues(plugins, tmp_path) == []
    resources.prepare_resources(plugins, tmp_path)
    assert len(installs) == 1


@pytest.mark.parametrize("override", ["TIKTOKEN_CACHE_DIR", "CUSTOM_TIKTOKEN_CACHE_DIR", "DATA_GYM_CACHE_DIR"])
def test_tokenizer_override_remains_effective_for_litellm_and_agno(tmp_path, monkeypatch, override):
    monkeypatch.setenv(override, "token-cache")
    resources.configure_resources({"llm": {}}, tmp_path)
    selected = str(tmp_path / "token-cache")
    assert os.environ["TIKTOKEN_CACHE_DIR"] == selected
    assert os.environ["CUSTOM_TIKTOKEN_CACHE_DIR"] == selected
    assert not (tmp_path / "token-cache").exists()


def test_conflicting_or_disabled_tokenizer_cache_is_rejected_without_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("CUSTOM_TIKTOKEN_CACHE_DIR", "first")
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "second")
    with pytest.raises(ValueError, match="disagree"):
        resources.configure_resources({"llm": {}}, tmp_path)
    assert os.environ["TIKTOKEN_CACHE_DIR"] == "second"
    monkeypatch.delenv("CUSTOM_TIKTOKEN_CACHE_DIR")
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "")
    assert resources.resource_issues({"llm": {}}, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_damaged_tokenizer_is_reported_without_deletion_or_download(tmp_path):
    plugins = {"llm": {}, ".localdata": {"app_name": "Chtholly"}}
    resources.configure_resources(plugins, tmp_path)
    cache = tmp_path / ".chtholly" / "cache" / "tiktoken"
    cache.mkdir(parents=True)
    vocabulary, source = _bundled_vocabulary()
    target = cache / vocabulary.cache_key
    shutil.copyfile(source, target)
    healthy_issues = resources.resource_issues(plugins, tmp_path)
    assert not any("cl100k_base" in item for item in healthy_issues)
    assert any("o200k_base" in item for item in healthy_issues)
    target.write_bytes(b"truncated cache")
    damaged_issues = resources.resource_issues(plugins, tmp_path)
    assert any("cl100k_base" in item for item in damaged_issues)
    assert target.read_bytes() == b"truncated cache"
    assert list(cache.iterdir()) == [target]


def test_prepare_reuses_native_bundled_vocabulary_and_preserves_valid_cache(tmp_path):
    vocabulary, source = _bundled_vocabulary()
    destination = tmp_path / vocabulary.cache_key
    destination.write_bytes(b"damaged")
    resources._prepare_vocabulary(tmp_path, vocabulary)
    assert destination.read_bytes() == source.read_bytes()
    before = destination.stat().st_mtime_ns
    resources._prepare_vocabulary(tmp_path, vocabulary)
    assert destination.stat().st_mtime_ns == before
    assert list(tmp_path.iterdir()) == [destination]


def test_download_failure_does_not_expose_proxy_credentials(tmp_path, monkeypatch):
    def fail_download(*args, **kwargs):
        raise ValueError("proxy http://user:secret@example.invalid refused the request")

    monkeypatch.setattr(resources, "_existing_tokenizer_caches", lambda: [])
    monkeypatch.setattr(resources, "urlopen", fail_download)
    with pytest.raises(RuntimeError) as caught:
        resources.prepare_resources({"llm": {}}, tmp_path)
    assert "secret" not in str(caught.value)
    assert "--prepare" in str(caught.value)
    assert list((tmp_path / ".entari" / "cache" / "tiktoken").iterdir()) == []
