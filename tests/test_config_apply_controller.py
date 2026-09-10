"""Controller contracts for acknowledging hot saves without losing newer writes."""

import sys
import json
from types import ModuleType, SimpleNamespace
from pathlib import Path
from importlib.util import module_from_spec, spec_from_file_location

import pytest


@pytest.fixture
def controller(tmp_path, monkeypatch):
    if sys.platform == "win32":
        # These Linux-only imports are unused by the isolated apply transaction.
        monkeypatch.setitem(sys.modules, "grp", ModuleType("grp"))
        monkeypatch.setitem(sys.modules, "fcntl", ModuleType("fcntl"))
    path = Path(__file__).resolve().parents[1] / "scripts" / "chtholly_config_apply.py"
    spec = spec_from_file_location("_config_apply_controller_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, filename in {
        "CONFIG_PATH": "entari.yml",
        "LAST_GOOD_PATH": "last-good.yml",
        "STATUS_PATH": "status.json",
        "PUBLIC_STATUS_PATH": "public-status.json",
        "NEXT_PATH": "next.yml",
    }.items():
        monkeypatch.setattr(module, name, tmp_path / filename)
    monkeypatch.setattr(module, "STATE_DIR", tmp_path)
    monkeypatch.setattr(module, "grp", SimpleNamespace(getgrnam=lambda name: SimpleNamespace(gr_gid=0)))

    def config(model):
        return json.dumps({"plugins": {"llm": {"models": [{"name": model}]}, "webui_config_apply": {}}}).encode()

    old, candidate, newer = (config(name) for name in ("old", "candidate", "newer"))
    module.LAST_GOOD_PATH.write_bytes(old)
    module.CONFIG_PATH.write_bytes(candidate)
    state = SimpleNamespace(
        module=module,
        old=old,
        candidate=candidate,
        newer=newer,
        running=module.digest(candidate),
        active=True,
        health="ok",
        status_calls=0,
        operations=[],
        sleeps=[],
        promotions=[],
        status_filter=None,
    )

    def atomic_write(path, data, **kwargs):
        if path == module.LAST_GOOD_PATH:
            state.promotions.append(data)
        path.write_bytes(data)

    def systemctl(operation, *arguments):
        state.operations.append(operation)
        if operation == "is-active":
            return state.active
        if operation == "stop":
            state.active = False
        elif operation == "start":
            state.active = True
            state.health = "ok"
            state.running = module.digest(module.CONFIG_PATH.read_bytes())
        return True

    def request_json(path):
        if path == "/api/health":
            return {"status": state.health}
        assert path == "/api/config-apply/status"
        state.status_calls += 1
        saved = module.digest(module.CONFIG_PATH.read_bytes())
        status = {
            "success": True,
            "running_sha256": state.running,
            "saved_sha256": saved,
            "in_sync": state.running == saved,
            "loaded_config_keys": ["llm", "webui_config_apply"],
        }
        if state.status_filter is not None:
            state.status_filter(status)
        return status

    monkeypatch.setattr(module, "atomic_write", atomic_write)
    monkeypatch.setattr(module, "run_systemctl", systemctl)
    monkeypatch.setattr(module, "request_json", request_json)
    monkeypatch.setattr(module.time, "sleep", state.sleeps.append)
    return state


def test_hot_save_requires_five_checks_and_promotes_without_restart(controller):
    state = controller
    module = state.module
    assert module.apply_candidate(state.candidate, state.old, None, staged=False)
    assert state.status_calls == 5
    assert state.sleeps == [1, 1, 1, 1]
    assert "stop" not in state.operations
    assert "start" not in state.operations
    assert module.LAST_GOOD_PATH.read_bytes() == state.candidate
    status = json.loads(module.STATUS_PATH.read_bytes())
    assert status["result"] == "applied"
    assert status["application_mode"] == "hot_reload"
    assert status["candidate_sha256"] == module.digest(state.candidate)


def test_explicit_restart_is_not_skipped_even_for_last_good(controller):
    state = controller
    module = state.module
    module.LAST_GOOD_PATH.write_bytes(state.candidate)
    request_id = "a" * 32
    assert module.apply_candidate(state.candidate, state.candidate, request_id, staged=False)
    assert state.operations[:2] == ["stop", "start"]
    assert state.status_calls == 5
    status = json.loads(module.STATUS_PATH.read_bytes())
    assert status["result"] == "applied"
    assert status["application_mode"] == "restart"
    assert status["request_id"] == request_id


@pytest.mark.parametrize(
    "defect",
    [
        "stale_runtime",
        "missing_saved",
        "unsynced",
        "nonboolean_sync",
        "missing_success",
        "corrupt_plugins",
        "unhealthy",
    ],
)
def test_untrusted_runtime_status_falls_back_without_hot_wait(controller, defect):
    state = controller
    module = state.module

    def damage(status):
        if "stop" in state.operations:
            return
        if defect == "stale_runtime":
            status["running_sha256"] = module.digest(state.old)
        elif defect == "missing_saved":
            status.pop("saved_sha256")
        elif defect == "unsynced":
            status["in_sync"] = False
        elif defect == "nonboolean_sync":
            status["in_sync"] = 1
        elif defect == "missing_success":
            status.pop("success")
        elif defect == "corrupt_plugins":
            status["loaded_config_keys"] = [{"llm": True}]
        elif defect == "unhealthy":
            raise ValueError("Malformed response")

    state.status_filter = damage
    assert module.apply_candidate(state.candidate, state.old, None, staged=False)
    assert state.operations[:3] == ["is-active", "stop", "start"]
    assert state.sleeps == [1, 1, 1, 1]
    assert json.loads(module.STATUS_PATH.read_bytes())["application_mode"] == "restart"
    assert module.LAST_GOOD_PATH.read_bytes() == state.candidate


def test_health_loss_during_hot_verification_requires_restart(controller):
    state = controller
    module = state.module

    def lose_health(status):
        if state.status_calls == 3:
            state.health = "failed"

    state.status_filter = lose_health
    assert module.apply_candidate(state.candidate, state.old, None, staged=False)
    assert "stop" in state.operations
    assert "start" in state.operations
    assert json.loads(module.STATUS_PATH.read_bytes())["application_mode"] == "restart"


def test_superseded_hot_candidate_is_not_promoted(controller):
    state = controller
    module = state.module

    def save_newer(status):
        if state.status_calls == 5:
            module.CONFIG_PATH.write_bytes(state.newer)

    state.status_filter = save_newer
    assert module.apply_candidate(state.candidate, state.old, None, staged=False)
    assert state.promotions == [state.newer]
    assert module.CONFIG_PATH.read_bytes() == state.newer
    assert "stop" in state.operations
    assert "start" in state.operations
    status = json.loads(module.STATUS_PATH.read_bytes())
    assert status["candidate_sha256"] == module.digest(state.newer)
    assert status["application_mode"] == "restart"


def test_restart_verification_does_not_promote_a_superseded_candidate(controller):
    state = controller
    module = state.module

    def save_newer(status):
        if state.status_calls == 5:
            module.CONFIG_PATH.write_bytes(state.newer)

    state.status_filter = save_newer
    assert module.apply_candidate(state.candidate, state.old, "b" * 32, staged=False)
    assert state.promotions == []
    assert module.LAST_GOOD_PATH.read_bytes() == state.old
    assert module.CONFIG_PATH.read_bytes() == state.newer
    assert json.loads(module.STATUS_PATH.read_bytes())["result"] == "verifying"


def test_hot_save_during_restart_verification_never_rolls_back_newer_runtime(controller):
    state = controller
    module = state.module

    def hot_save(status):
        if state.status_calls == 1:
            module.CONFIG_PATH.write_bytes(state.newer)
            state.running = module.digest(state.newer)

    state.status_filter = hot_save
    assert module.apply_candidate(state.candidate, state.old, "c" * 32, staged=False)
    assert module.CONFIG_PATH.read_bytes() == state.newer
    assert module.LAST_GOOD_PATH.read_bytes() == state.old
    assert state.operations.count("stop") == 1
    assert state.operations.count("start") == 1
    assert json.loads(module.STATUS_PATH.read_bytes())["result"] == "verifying"

    state.status_filter = None
    assert module.apply_candidate(state.newer, state.old, None, staged=False)
    assert module.LAST_GOOD_PATH.read_bytes() == state.newer
    assert state.operations.count("stop") == 1
    assert json.loads(module.STATUS_PATH.read_bytes())["application_mode"] == "hot_reload"
