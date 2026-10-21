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
    monkeypatch.setattr(module, "CGROUP_ROOT", tmp_path / "cgroups")
    cgroup = module.CGROUP_ROOT / "controller-test.service"
    cgroup.mkdir(parents=True)
    (cgroup / "cgroup.events").write_text("populated 1\nfrozen 0\n")

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
        now=0.0,
        cgroup=cgroup,
        unit={
            "ActiveState": "active",
            "SubState": "running",
            "MainPID": "100",
            "ControlPID": "0",
            "Job": "",
            "ControlGroup": "/controller-test.service",
            "TimeoutStopUSec": "1min 30s",
        },
        stop_duration=0,
        stop_returncode=0,
        stop_overrides={},
        stop_populated=False,
        after_stop=None,
        start_results=[],
        config_writes=[],
        show_returncode=0,
        show_timeout=False,
    )

    def atomic_write(path, data, **kwargs):
        if path == module.LAST_GOOD_PATH:
            state.promotions.append(data)
        path.write_bytes(data)

    def systemctl(command, *, timeout, **kwargs):
        assert command[0] == "/usr/bin/systemctl"
        assert command[-1] == module.SERVICE_NAME
        operation = command[1]
        if operation == "show":
            if state.show_timeout:
                state.now += timeout
                raise module.subprocess.TimeoutExpired(command, timeout)
            return module.subprocess.CompletedProcess(
                command,
                state.show_returncode,
                stdout="\n".join(f"{key}={value}" for key, value in state.unit.items()),
            )
        state.operations.append(operation)
        if operation == "is-active":
            return module.subprocess.CompletedProcess(command, 0 if state.active else 3)
        if operation == "stop":
            state.unit.update(ActiveState="deactivating", SubState="stop-sigterm", Job="17")
            if state.stop_duration > timeout:
                state.now += timeout
                raise module.subprocess.TimeoutExpired(command, timeout)
            state.now += state.stop_duration
            state.active = False
            state.unit.update(ActiveState="inactive", SubState="dead", MainPID="0", ControlPID="0", Job="")
            state.unit.update(state.stop_overrides)
            (state.cgroup / "cgroup.events").write_text(
                f"populated {int(state.stop_populated)}\nfrozen 0\n"
            )
            if state.after_stop is not None:
                state.after_stop()
            return module.subprocess.CompletedProcess(command, state.stop_returncode)
        assert operation == "start"
        # A start must never race the old stop job or surviving descendant tasks.
        assert module.service_quiescent(state.unit)
        started = state.start_results.pop(0) if state.start_results else True
        if started:
            state.active = True
            state.health = "ok"
            state.running = module.digest(module.CONFIG_PATH.read_bytes())
            state.unit.update(ActiveState="active", SubState="running", MainPID="101")
            state.cgroup.mkdir(parents=True, exist_ok=True)
            (state.cgroup / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        return module.subprocess.CompletedProcess(command, 0 if started else 1)

    def sleep(seconds):
        state.sleeps.append(seconds)
        state.now += seconds

    write_config = module.write_config

    def checked_write_config(data):
        assert module.service_quiescent(state.unit)
        state.config_writes.append(data)
        write_config(data)

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
    state.real_request_json = module.request_json
    monkeypatch.setattr(module.subprocess, "run", systemctl)
    monkeypatch.setattr(module, "write_config", checked_write_config)
    monkeypatch.setattr(module, "request_json", request_json)
    monkeypatch.setattr(module.time, "sleep", sleep)
    monkeypatch.setattr(module.time, "monotonic", lambda: state.now)
    return state


@pytest.mark.parametrize("duration", [70, 90])
def test_long_graceful_stop_finishes_before_staged_config_is_written(controller, duration):
    state = controller
    module = state.module
    module.CONFIG_PATH.write_bytes(state.old)
    state.running = module.digest(state.old)
    state.stop_duration = duration

    assert module.apply_candidate(state.candidate, state.old, "d" * 32, staged=True)

    assert state.now == duration + 4
    assert state.operations[:2] == ["stop", "start"]
    assert state.config_writes == [state.candidate]
    assert state.running == module.digest(state.candidate)
    assert module.CONFIG_PATH.read_bytes() == state.candidate
    assert module.LAST_GOOD_PATH.read_bytes() == state.candidate
    assert json.loads(module.STATUS_PATH.read_bytes())["result"] == "applied"


def test_systemd_timeout_kill_can_start_only_after_failed_unit_is_empty(controller):
    state = controller
    module = state.module
    state.stop_duration = 90
    state.stop_returncode = 1
    state.stop_overrides = {"ActiveState": "failed", "SubState": "failed"}

    assert module.apply_candidate(state.candidate, state.old, "e" * 32, staged=False)

    assert state.now == 94
    assert state.operations[:2] == ["stop", "start"]
    assert state.running == module.digest(state.candidate)
    assert module.LAST_GOOD_PATH.read_bytes() == state.candidate
    assert json.loads(module.STATUS_PATH.read_bytes())["result"] == "applied"


def test_unfinished_stop_fails_closed_without_replacing_config_or_starting(controller):
    state = controller
    module = state.module
    module.CONFIG_PATH.write_bytes(state.old)
    state.stop_duration = 150

    assert not module.apply_candidate(state.candidate, state.old, "f" * 32, staged=True)

    assert state.now == 105
    assert state.unit["ActiveState"] == "deactivating"
    assert state.unit["MainPID"] == "100"
    assert state.unit["Job"] == "17"
    assert (state.cgroup / "cgroup.events").read_text().startswith("populated 1\n")
    assert state.operations == ["stop"]
    assert state.config_writes == []
    assert module.CONFIG_PATH.read_bytes() == state.old
    assert module.LAST_GOOD_PATH.read_bytes() == state.old
    assert not (module.STATE_DIR / "pending.yml").exists()
    assert json.loads(module.STATUS_PATH.read_bytes())["result"] == "rollback_failed"


@pytest.mark.parametrize("returncode", [0, 1])
@pytest.mark.parametrize(
    ("overrides", "populated"),
    [
        ({"ActiveState": "deactivating", "SubState": "stop-sigterm"}, False),
        ({"ActiveState": "active", "SubState": "running"}, False),
        ({"MainPID": "100"}, False),
        ({"ControlPID": "101"}, False),
        ({"Job": "18"}, False),
        ({}, True),
    ],
)
def test_stop_command_result_never_substitutes_for_quiescence(controller, returncode, overrides, populated):
    state = controller
    module = state.module
    module.CONFIG_PATH.write_bytes(state.old)
    state.stop_returncode = returncode
    state.stop_overrides = overrides
    state.stop_populated = populated

    assert not module.apply_candidate(state.candidate, state.old, "1" * 32, staged=True)

    assert state.operations == ["stop"]
    assert state.config_writes == []
    assert module.CONFIG_PATH.read_bytes() == state.old
    assert module.LAST_GOOD_PATH.read_bytes() == state.old
    assert state.promotions == []
    assert json.loads(module.STATUS_PATH.read_bytes())["result"] == "rollback_failed"


def test_recovery_cannot_replace_config_while_a_descendant_survives(controller):
    state = controller
    module = state.module
    state.stop_returncode = 1
    state.stop_overrides = {"ActiveState": "failed", "SubState": "failed"}
    state.stop_populated = True

    assert not module.restore_last_good(
        state.candidate, state.old, None, reason="Rejected candidate", rejected=True
    )

    assert state.operations == ["stop"]
    assert state.config_writes == []
    assert module.CONFIG_PATH.read_bytes() == state.candidate
    assert module.LAST_GOOD_PATH.read_bytes() == state.old
    assert json.loads(module.STATUS_PATH.read_bytes())["result"] == "rollback_failed"


@pytest.mark.parametrize("removed", [False, True])
def test_missing_cgroup_events_is_safe_only_when_the_entire_group_is_gone(controller, removed):
    state = controller
    module = state.module

    def remove_events():
        (state.cgroup / "cgroup.events").unlink()
        if removed:
            state.cgroup.rmdir()

    state.after_stop = remove_events
    assert module.apply_candidate(state.candidate, state.old, "2" * 32, staged=False) is removed
    assert ("start" in state.operations) is removed
    assert module.LAST_GOOD_PATH.read_bytes() == (state.candidate if removed else state.old)


@pytest.mark.parametrize("defect", ["failed_probe", "timed_out_probe", "missing_property", "final_probe"])
def test_uninspectable_unit_fails_closed(controller, defect):
    state = controller
    module = state.module
    module.CONFIG_PATH.write_bytes(state.old)
    if defect == "failed_probe":
        state.show_returncode = 1
    elif defect == "timed_out_probe":
        state.show_timeout = True
    elif defect == "missing_property":
        state.unit.pop("ControlPID")
    else:
        state.after_stop = lambda: setattr(state, "show_returncode", 1)

    assert not module.apply_candidate(state.candidate, state.old, "3" * 32, staged=True)

    assert "start" not in state.operations
    assert state.config_writes == []
    assert module.CONFIG_PATH.read_bytes() == state.old
    assert module.LAST_GOOD_PATH.read_bytes() == state.old


@pytest.mark.parametrize("timeout", ["infinity", "0", "0s", "121s", "3min", "invalid"])
def test_unbounded_or_unsupported_stop_timeout_does_not_begin_restart(controller, timeout):
    state = controller
    module = state.module
    state.unit["TimeoutStopUSec"] = timeout

    assert not module.apply_candidate(state.candidate, state.old, "4" * 32, staged=False)

    assert state.operations == []
    assert state.config_writes == []
    assert module.CONFIG_PATH.read_bytes() == state.candidate
    assert module.LAST_GOOD_PATH.read_bytes() == state.old


def test_failed_candidate_and_recovery_starts_remain_failure(controller):
    state = controller
    module = state.module
    state.start_results = [False, False]

    assert not module.apply_candidate(state.candidate, state.old, "5" * 32, staged=False)

    assert state.operations == ["stop", "start", "stop", "start"]
    assert not state.active
    assert state.config_writes == [state.old]
    assert module.CONFIG_PATH.read_bytes() == state.old
    assert module.LAST_GOOD_PATH.read_bytes() == state.old
    assert state.promotions == []
    assert json.loads(module.STATUS_PATH.read_bytes())["result"] == "rollback_failed"


def test_runtime_failure_after_long_stop_still_restores_and_verifies_last_good(controller):
    state = controller
    module = state.module
    state.stop_duration = 90

    def missing_plugin(status):
        if state.running == module.digest(state.candidate):
            status["loaded_config_keys"] = ["llm"]

    state.status_filter = missing_plugin
    assert module.apply_candidate(state.candidate, state.old, "6" * 32, staged=False)

    assert state.now == 274
    assert state.operations.count("stop") == 2
    assert state.operations.count("start") == 2
    assert state.config_writes == [state.old]
    assert state.running == module.digest(state.old)
    assert module.CONFIG_PATH.read_bytes() == state.old
    assert module.LAST_GOOD_PATH.read_bytes() == state.old
    assert json.loads(module.STATUS_PATH.read_bytes())["result"] == "rolled_back"


def test_save_during_stop_replaces_the_superseded_candidate(controller):
    state = controller
    module = state.module
    state.after_stop = lambda: module.CONFIG_PATH.write_bytes(state.newer)

    assert module.apply_candidate(state.candidate, state.old, "7" * 32, staged=False)

    assert state.running == module.digest(state.newer)
    assert state.config_writes == []
    assert module.CONFIG_PATH.read_bytes() == state.newer
    assert module.LAST_GOOD_PATH.read_bytes() == state.newer
    assert json.loads(module.STATUS_PATH.read_bytes())["candidate_sha256"] == module.digest(state.newer)


def test_recovery_preserves_a_newer_save_that_arrives_during_stop(controller):
    state = controller
    module = state.module
    state.start_results = [False, True]

    def save_during_recovery():
        if state.operations.count("stop") == 2:
            module.CONFIG_PATH.write_bytes(state.newer)

    state.after_stop = save_during_recovery
    assert module.apply_candidate(state.candidate, state.old, "8" * 32, staged=False)

    assert module.NEXT_PATH.read_bytes() == state.newer
    assert module.CONFIG_PATH.read_bytes() == state.old
    assert module.LAST_GOOD_PATH.read_bytes() == state.old
    assert state.running == module.digest(state.old)
    assert json.loads(module.STATUS_PATH.read_bytes())["result"] == "rolled_back"


def test_healthy_last_good_without_explicit_request_does_not_restart(controller):
    state = controller
    module = state.module
    module.LAST_GOOD_PATH.write_bytes(state.candidate)

    assert module.apply_candidate(state.candidate, state.candidate, None, staged=False)

    assert state.operations == ["is-active"]
    assert state.config_writes == []
    assert state.promotions == []
    assert not module.STATUS_PATH.exists()


def test_native_builtin_group_uses_runtime_owner_for_readiness(controller):
    state = controller
    module = state.module
    source = json.loads(state.candidate)
    source["plugins"][".localdata"] = {"app_name": "controller-test"}
    source["plugins"][".scheduler"] = {}
    state.candidate = json.dumps(source).encode()
    module.CONFIG_PATH.write_bytes(state.candidate)
    module.LAST_GOOD_PATH.write_bytes(state.candidate)
    state.running = module.digest(state.candidate)

    def loaded_builtin_owner(status):
        status["loaded_config_keys"].append("$")

    state.status_filter = loaded_builtin_owner
    assert module.apply_candidate(state.candidate, state.candidate, None, staged=False)
    assert "stop" not in state.operations
    assert module.CONFIG_PATH.read_bytes() == state.candidate


def test_unchanged_required_plugin_missing_after_restart_is_not_accepted(controller):
    state = controller
    module = state.module
    old = json.loads(state.old)
    candidate = json.loads(state.candidate)
    old["plugins"]["llm_chat"] = {"companion_daily_contacts": 96}
    candidate["plugins"]["llm_chat"] = {"companion_daily_contacts": 96}
    state.old = json.dumps(old).encode()
    state.candidate = json.dumps(candidate).encode()
    module.CONFIG_PATH.write_bytes(state.candidate)
    module.LAST_GOOD_PATH.write_bytes(state.old)

    def plugin_startup(status):
        if state.running == module.digest(state.old):
            status["loaded_config_keys"].append("llm_chat")

    state.status_filter = plugin_startup
    assert module.apply_candidate(state.candidate, state.old, "a" * 32, staged=False)
    assert state.promotions == []
    assert state.operations.count("start") == 2
    assert module.CONFIG_PATH.read_bytes() == state.old
    assert json.loads(module.STATUS_PATH.read_bytes())["result"] == "rolled_back"


def test_same_config_still_requires_chat_plugin_before_skipping_restart(controller):
    state = controller
    module = state.module
    candidate = json.loads(state.candidate)
    candidate["plugins"]["llm_chat"] = {"companion_daily_contacts": 96}
    state.candidate = json.dumps(candidate).encode()
    module.CONFIG_PATH.write_bytes(state.candidate)
    module.LAST_GOOD_PATH.write_bytes(state.candidate)
    state.running = module.digest(state.candidate)

    def plugin_recovered(status):
        if "start" in state.operations:
            status["loaded_config_keys"].append("llm_chat")

    state.status_filter = plugin_recovered
    assert module.apply_candidate(state.candidate, state.candidate, None, staged=False)
    assert state.operations.count("start") == 1
    assert json.loads(module.STATUS_PATH.read_bytes())["result"] == "applied"


def test_newer_live_save_takes_precedence_over_staged_recovery_candidate(controller):
    state = controller
    module = state.module
    module.CONFIG_PATH.write_bytes(state.newer)
    module.NEXT_PATH.write_bytes(state.candidate)

    assert module.apply_candidate(state.candidate, state.old, "9" * 32, staged=True)

    assert state.running == module.digest(state.newer)
    assert state.config_writes == []
    assert module.CONFIG_PATH.read_bytes() == state.newer
    assert module.LAST_GOOD_PATH.read_bytes() == state.newer
    assert not module.NEXT_PATH.exists()


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


def test_controller_authenticates_secure_loopback_cookie_and_renews_after_restart(controller, monkeypatch):
    from threading import Thread
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from urllib.error import URLError

    module = controller.module
    monkeypatch.setenv("CONTROLLER_TEST_PASSWORD", "controller-test-password")
    config = json.dumps(
        {"plugins": {"entari_plugin_webui": {"password": "${{ env.CONTROLLER_TEST_PASSWORD }}"}}}
    ).encode()
    module.CONFIG_PATH.write_bytes(config)
    state = {"session": "a" * 32, "logins": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_arguments):
            pass

        def reply(self, status, data, **headers):
            encoded = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            for name, value in headers.items():
                self.send_header(name.replace("_", "-"), value)
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if self.path == "/redirect":
                self.reply(302, {}, Location="http://untrusted.invalid/status")
            elif self.headers.get("Cookie") != "webui_sid=" + state["session"]:
                self.reply(401, {})
            else:
                self.reply(200, {"running_sha256": "verified-runtime"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if (
                self.path != "/api/auth/login"
                or body != {"password": "controller-test-password"}
                or self.headers.get("Origin") != module.API_URL
            ):
                self.reply(401, {})
                return
            state["logins"] += 1
            self.reply(200, {"success": True}, Set_Cookie=f"webui_sid={state['session']}; Secure; HttpOnly; Path=/")

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    monkeypatch.setattr(module, "API_URL", f"http://127.0.0.1:{server.server_port}")
    thread.start()
    try:
        request = controller.real_request_json
        assert request("/api/config-apply/status")["running_sha256"] == "verified-runtime"
        assert request("/api/config-apply/status")["running_sha256"] == "verified-runtime"
        assert state["logins"] == 1
        state["session"] = "b" * 32
        assert request("/api/config-apply/status")["running_sha256"] == "verified-runtime"
        assert state["logins"] == 2
        with pytest.raises(URLError, match="redirects are not allowed"):
            request("/redirect")
        assert module.CONFIG_PATH.read_bytes() == config
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
