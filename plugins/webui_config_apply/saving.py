"""Protect native configuration edits while retaining model snapshots and restart boundaries."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from hashlib import sha256
from collections.abc import Mapping, Callable

from arclet.letoderea import post
from arclet.entari.config.file import EntariConfig
from arclet.entari.config.model import ApplyError, ApplyReport
from arclet.entari.event.config import ConfigModify

from utils.webui_config_core import ConfigValidationError, validate_candidate
from utils.webui_config_core.validation import same_config

from .staging import stage_config
from .serializer import canonical_plugins
from .persistence import persist_candidate
from .model_reload import prepare_model_reload

_RESTART_PLUGINS = frozenset(
    {
        "llm",
        "entari_plugin_llm",
        "server",
        "entari_plugin_server",
        "webui",
        "entari_plugin_webui",
        "database",
        "entari_plugin_database",
        "webui_config_apply",
        "webui_sso",
    }
)


class SaveError(ValueError):
    def __init__(self, message: str, *, code: str, status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(message)


def requires_managed_restart(key: str) -> bool:
    return key.lstrip("~?").rsplit(".", 1)[-1] in _RESTART_PLUGINS


def _body(source: Mapping[str, object]) -> Mapping[str, Any]:
    body = source.get("entari", source)
    if not isinstance(body, Mapping):
        raise ConfigValidationError("Entari configuration must be an object")
    return body


def _source(source: Mapping[str, object]) -> dict[str, Any]:
    body = dict(_body(source))
    body["plugins"] = canonical_plugins(body.get("plugins", {}))
    return body


def _changed_plugins(before: Mapping[str, object], after: Mapping[str, object]) -> set[str]:
    old, new = _body(before).get("plugins", {}), _body(after).get("plugins", {})
    return {key for key in old.keys() | new.keys() if not same_config(old.get(key), new.get(key))}


def _native_allowed(before: Mapping[str, object] | None, after: Mapping[str, object]) -> bool:
    if before is None:
        return False
    old, new = _body(before), _body(after)
    if not same_config(
        {key: value for key, value in old.items() if key != "plugins"},
        {key: value for key, value in new.items() if key != "plugins"},
    ):
        return False
    changed = _changed_plugins(before, after)
    return not any(key.startswith("$") or requires_managed_restart(key) for key in changed)


def _sanitize_report(report: ApplyReport) -> ApplyReport:
    report.errors = [
        ApplyError(
            error.plugin,
            "Configuration application failed"
            + (f": {type(error.exception).__name__}" if error.exception is not None else ""),
        )
        for error in report.errors
    ]
    return report


class ConfigSaver:
    def __init__(self, running_sha256: str | None, restart_available: Callable[[], bool]) -> None:
        from entari_plugin_webui.services import config_service

        self.running_sha256 = running_sha256
        self.restart_available = restart_available
        self.lock = config_service._save_lock
        self.application_mode = "restart"
        self.last_application: dict[str, object] = {"result": "applied" if running_sha256 else "unverified"}
        live = EntariConfig.instance
        content = live.path.read_bytes()
        self._running_source = (
            _source(validate_candidate(content, live.env_vars))
            if sha256(content).hexdigest() == running_sha256
            else None
        )

    async def save_section(
        self,
        section: str,
        value: object | Callable[[Any], object],
        *,
        if_match: str | None = None,
        check_etag: bool = True,
        mutate: bool = False,
        create: bool = False,
        source: str = "webui",
    ) -> ApplyReport:
        from entari_plugin_webui.services import config_service
        from entari_plugin_webui.core.error import ConfigConflict, ConfigPreconditionRequired

        async with self.lock:
            live = EntariConfig.instance
            current_etag = config_service.compute_etag(live)
            if check_etag and if_match is None:
                raise ConfigPreconditionRequired(message="If-Match header is required", etag=current_etag)
            if check_etag and if_match != current_etag:
                raise ConfigConflict(
                    message="Configuration changed since it was loaded",
                    etag=current_etag,
                    data=config_service.get_section(section),
                )
            staged = stage_config(live, section, value, mutate=mutate)
            digest = sha256(staged.content).hexdigest()
            candidate = _source(staged.source)
            original = _source(validate_candidate(staged.snapshots[live.path][0], live.env_vars))
            before = self._running_source
            if any(key.lstrip("~?").startswith("workshop_") for key in _changed_plugins(original, candidate)):
                raise SaveError(
                    "Workshop plugins must be managed through workshop tools", code="workshop_managed", status=403
                )
            unchanged = digest == self.running_sha256 or same_config(candidate, before)
            model_reload = None
            if not unchanged and before is not None and not staged.config.plugin_extra_files:
                model_reload = prepare_model_reload(before, candidate, live.env_vars)
            native = not unchanged and model_reload is None and _native_allowed(before, candidate)
            mode = "unchanged" if unchanged else "hot_reload" if native or model_reload is not None else "restart"
            if mode == "restart":
                if mutate:
                    raise SaveError(
                        "This core plugin requires a managed configuration restart",
                        code="managed_restart_required",
                        status=409,
                    )
                if not self.restart_available():
                    raise SaveError(
                        "This configuration change requires the managed restart helper",
                        code="helper_unavailable",
                        status=503,
                    )
            if not staged.changed and not create and unchanged:
                return ApplyReport(ok=True, applied=False)
            self.application_mode = mode
            self.last_application = {"result": "applying", "candidate_sha256": digest, "mode": mode}
            report = ApplyReport(ok=True, applied=mode != "restart" and not unchanged)
            if mode == "restart":
                report.ignored = "Configuration saved; managed process restart is required"
            try:
                with persist_candidate(live, staged.config, staged.snapshots, staged.changed, publish=not native):
                    if model_reload is not None:
                        model_reload.apply()
                        report.changed_plugins = [model_reload.config_key]
                    elif native:
                        applied = await post(ConfigModify(live.path, source=source), validate=True)
                        if applied is None:
                            raise SaveError(
                                "The native configuration applier is unavailable",
                                code="config_handler_missing",
                                status=503,
                            )
                        report = _sanitize_report(applied.value)
            except BaseException as exc:
                recovery_failed = native or (
                    isinstance(exc, ConfigValidationError) and exc.code == "config_rollback_failed"
                )
                self.last_application = {
                    "result": "rollback_failed" if recovery_failed else "rejected",
                    "candidate_sha256": digest,
                    "mode": mode,
                }
                if recovery_failed:
                    self.running_sha256 = None
                    self._running_source = None
                if not isinstance(exc, Exception) or isinstance(exc, (ConfigValidationError, SaveError)):
                    raise
                raise SaveError(
                    "Configuration application failed; inspect the managed application status",
                    code="config_apply_failed",
                    status=503,
                ) from None
            if mode != "restart" and report.ok:
                self.running_sha256 = digest
                self._running_source = deepcopy(candidate)
            if not report.ok:
                failed = {error.plugin for error in report.errors}
                partial = report.loaded or report.unloaded or report.reloaded or set(report.changed_plugins) - failed
                if partial:
                    # A mixed runtime no longer has a truthful whole-file baseline.
                    self.running_sha256 = None
                    self._running_source = None
            self.last_application = {
                "result": "pending" if mode == "restart" else "applied" if report.ok else "failed",
                "candidate_sha256": digest,
                "mode": mode,
            }
            return report
