"""Private package publication and genuine Entari lifecycle operations.

Only human-approved native code reaches this driver. Its checks protect ownership
and lifecycle integrity; native plugins have the full privileges of the process.
"""

from __future__ import annotations

import os
import sys
import stat
import shutil
from typing import cast
from pathlib import Path
import tempfile
import importlib
from collections.abc import Mapping
import importlib.machinery

from tarina import ContextModel
from arclet.entari import Plugin
from arclet.alconna import Alconna
from arclet.entari.plugin import find_plugin, load_plugin, reload_plugin, unload_plugin_async
from arclet.entari.plugin.model import current_plugin
from arclet.entari.plugin.service import plugin_service

from utils.plugin_workshop_core.models import VersionRecord, WorkshopError
from utils.plugin_workshop_core.policy import module_name, normalize_submission


def _is_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _safe_tree(path: Path) -> None:
    for ancestor in (path, *path.parents):
        if _is_link(ancestor):
            raise WorkshopError("Workshop storage cannot contain links", code="unsafe_storage")
    if path.is_dir():
        for directory, directories, files in os.walk(path, followlinks=False):
            for name in (*directories, *files):
                item = Path(directory) / name
                if _is_link(item):
                    raise WorkshopError("Workshop runtime cannot contain links", code="unsafe_storage")


async def _load_native(record: VersionRecord, *, replace: bool) -> bool:
    # The host manages this plugin; it must never become its import dependent.
    token = cast(ContextModel[Plugin | None], current_plugin).set(None)
    try:
        package = module_name(record.plugin_name)
        configuration = dict(record.manifest.configuration)
        if replace:
            return await reload_plugin(package, configuration)
        return load_plugin(package, configuration) is not None
    finally:
        current_plugin.reset(token)


async def _unload_native(name: str) -> bool:
    token = cast(ContextModel[Plugin | None], current_plugin).set(None)
    try:
        return await unload_plugin_async(module_name(name))
    finally:
        current_plugin.reset(token)


class _WorkshopFinder(importlib.machinery.FileFinder):
    """Normal import path entry, restricted to host-generated package names."""

    def find_spec(self, fullname: str, target=None):
        if "." in fullname or not fullname.startswith("workshop_"):
            return None
        try:
            if module_name(fullname[len("workshop_") :]) != fullname:
                return None
        except WorkshopError:
            return None
        return super().find_spec(fullname, target)


class NativePluginDriver:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).absolute()
        self.runtime = self.root / "runtime"
        self.staging = self.root / "staging"
        self._path = str(self.runtime)
        self._installed = False
        self._loaded: dict[str, VersionRecord] = {}

        def path_hook(path: str):
            if path != self._path:
                raise ImportError
            return _WorkshopFinder(path, (importlib.machinery.SourceFileLoader, [".py"]))

        self._path_hook = path_hook

    def prepare(self) -> None:
        _safe_tree(self.root)
        watcher = find_plugin("::auto_reload")
        if watcher is not None:
            for value in watcher.config.get("watch_dirs", ["."]):
                watched = Path(value).resolve()
                if self.runtime.resolve().is_relative_to(watched):
                    raise WorkshopError(
                        "Workshop runtime must be outside auto_reload watch_dirs", code="unsafe_watch_scope"
                    )
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        _safe_tree(self.runtime)
        _safe_tree(self.staging)
        if not self._installed:
            if self._path in sys.path:
                raise WorkshopError("Workshop runtime import directory is already owned", code="runtime_owned")
            sys.path_hooks.insert(0, self._path_hook)
            sys.path_importer_cache.pop(self._path, None)
            sys.path.append(self._path)
            self._installed = True
            importlib.invalidate_caches()

    def reconcile(self) -> None:
        """Discard uncommitted publication remnants; immutable store is authoritative."""
        for parent in (self.runtime, self.staging):
            _safe_tree(parent)
            for path in parent.iterdir():
                if parent == self.runtime:
                    if not path.name.startswith("workshop_"):
                        raise WorkshopError("Unexpected file in workshop runtime", code="unsafe_storage")
                    module_name(path.name[len("workshop_") :])
                    if find_plugin(path.name) is not None:
                        raise WorkshopError("Workshop namespace is already loaded", code="runtime_owned")
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        importlib.invalidate_caches()

    def _plugin(self, name: str):
        package = module_name(name)
        plugin = find_plugin(package)
        if plugin is not None:
            expected = self.runtime / package / "__init__.py"
            actual = getattr(plugin.module, "__file__", None)
            if plugin.id != package or actual is None or Path(actual).resolve() != expected.resolve():
                raise WorkshopError("Refusing to target a non-workshop plugin", code="namespace_conflict")
        elif package in sys.modules:
            raise WorkshopError("Workshop namespace is occupied by a non-plugin module", code="namespace_conflict")
        return plugin

    def is_loaded(self, name: str, version: int | None = None) -> bool:
        record = self._loaded.get(name)
        return bool(record and self._plugin(name) is not None and (version is None or record.version == version))

    @staticmethod
    def _owned_commands(package: str) -> dict[int, Alconna]:
        from arclet.entari.command.provider import AlconnaSuppiler

        owned: dict[int, Alconna] = {}
        for key, plugin in tuple(plugin_service.plugins.items()):
            if key != package and not key.startswith(package + "."):
                continue
            for slot in tuple(plugin._scope.subscribers):
                if not slot.subscriber.available:
                    continue
                try:
                    command = slot.subscriber.get_propagator(AlconnaSuppiler).cmd
                except ValueError:
                    continue
                owned[id(command)] = command
        return owned

    @classmethod
    def _command_inventory(cls, exclude: str | None = None) -> tuple[str, ...]:
        from arclet.alconna import command_manager

        excluded = cls._owned_commands(exclude) if exclude is not None else {}
        heads: set[str] = set()
        for command in command_manager.get_commands():
            if id(command) not in excluded:
                text = str(command.command).strip()
                if text:
                    heads.add(text.split()[0])
        return tuple(sorted(heads))

    def occupied_commands(self, name: str | None = None) -> tuple[str, ...]:
        exclude = None
        if name is not None and self._plugin(name) is not None:
            exclude = module_name(name)
        return self._command_inventory(exclude)

    def _check_inventory(self, record: VersionRecord) -> None:
        from arclet.entari.plugin import get_plugin_commands

        package = module_name(record.plugin_name)
        plugin = self._plugin(record.plugin_name)
        if plugin is None or plugin.is_static or not plugin.is_available:
            raise WorkshopError("Generated plugin is missing, static, or disabled", code="native_load_failed")
        declared: set[str] = set()
        for key, item in tuple(plugin_service.plugins.items()):
            if key == package or key.startswith(package + "."):
                for prefixes, head in get_plugin_commands(item):
                    if prefixes and any(prefix for prefix in prefixes):
                        raise WorkshopError("Generated commands must use bare command heads", code="command_mismatch")
                    declared.add(str(head).strip().split()[0])
        actual = {
            str(command.command).strip().split()[0]
            for command in self._owned_commands(package).values()
            if str(command.command).strip()
        }
        if declared != actual:
            raise WorkshopError("Declared native commands lack owned active handlers", code="command_mismatch")
        if actual != set(record.manifest.commands):
            raise WorkshopError("Native command inventory differs from the approved manifest", code="command_mismatch")
        conflicts = actual.intersection(self.occupied_commands(record.plugin_name))
        if conflicts:
            raise WorkshopError("Native commands conflict: " + ", ".join(sorted(conflicts)), code="command_conflict")

    async def apply(self, record: VersionRecord, files: Mapping[str, str]) -> None:
        report = record.report
        if (
            not record.approved_by
            or not record.approved_at
            or report is None
            or not report.passed
            or report.source_hash != record.source_hash
        ):
            raise WorkshopError("Native publication requires exact approved acceptance", code="approval_required")
        name, validated, digest = normalize_submission(record.plugin_name, files, record.manifest)
        if digest != record.source_hash:
            raise WorkshopError("Runtime source digest changed", code="source_mismatch")
        old = self._plugin(name)
        old_record = self._loaded.get(name)
        if old is not None and old_record is None:
            raise WorkshopError("Workshop namespace has another owner", code="namespace_conflict")
        occupied = set(self.occupied_commands(name))
        if conflicts := occupied.intersection(record.manifest.commands):
            raise WorkshopError(
                "Command heads already occupied: " + ", ".join(sorted(conflicts)), code="command_conflict"
            )
        _safe_tree(self.runtime)
        _safe_tree(self.staging)
        stage = Path(tempfile.mkdtemp(prefix="candidate-", dir=self.staging))
        backup = self.staging / (stage.name + "-previous")
        target = self.runtime / module_name(name)
        swapped = False
        changed = False
        keep_backup = False
        try:
            for relative, text in validated.items():
                path = stage / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(text.encode("utf-8"))
            if target.exists():
                target.replace(backup)
            try:
                stage.replace(target)
                swapped = True
            except BaseException:
                if backup.exists():
                    backup.replace(target)
                raise
            importlib.invalidate_caches()
            spec = importlib.machinery.PathFinder.find_spec(module_name(name))
            if spec is None or spec.origin is None or Path(spec.origin).resolve() != (target / "__init__.py").resolve():
                raise WorkshopError("Workshop import path is shadowed by another namespace", code="namespace_conflict")
            changed = await _load_native(record, replace=old is not None)
            if not changed:
                raise WorkshopError(
                    "Entari rejected the candidate import; previous owner retained", code="native_load_failed"
                )
            self._check_inventory(record)
            self._loaded[name] = record
        except BaseException as error:
            recovery_error = None
            if swapped:
                try:
                    current = self._plugin(name)
                    if current is not None and current is not old:
                        try:
                            await _unload_native(name)
                        except Exception:
                            # Always restore source bytes even if native cleanup fails.
                            pass
                    if target.exists():
                        _safe_tree(target)
                        shutil.rmtree(target)
                    if backup.exists():
                        backup.replace(target)
                    importlib.invalidate_caches()
                    current = self._plugin(name)
                    if old_record is not None and current is not old:
                        restored = await _load_native(old_record, replace=current is not None)
                        if not restored:
                            raise RuntimeError("Previous approved plugin could not be restored")
                        self._check_inventory(old_record)
                    elif old_record is None and current is not None:
                        raise RuntimeError("Candidate cleanup left a native owner behind")
                except BaseException as recovery:
                    recovery_error = recovery
                    keep_backup = True
                    if find_plugin(module_name(name)) is not None:
                        self._loaded[name] = record
                    else:
                        self._loaded.pop(name, None)
            if recovery_error is not None:
                raise WorkshopError(
                    "Candidate failed and previous runtime restoration failed; committed version is retained. "
                    "External data or messages are not rolled back.",
                    code="native_recovery_failed",
                ) from error
            raise
        finally:
            for path in (stage,) if keep_backup else (stage, backup):
                if path.exists():
                    _safe_tree(path)
                    shutil.rmtree(path)

    async def disable(self, name: str) -> None:
        if self._plugin(name) is not None:
            if name not in self._loaded:
                raise WorkshopError("Refusing to unload another runtime owner", code="namespace_conflict")
            if not await _unload_native(name):
                raise WorkshopError("Entari did not unload the generated plugin", code="native_unload_failed")
        self._loaded.pop(name, None)
        target = self.runtime / module_name(name)
        if target.exists():
            _safe_tree(target)
            shutil.rmtree(target)
        importlib.invalidate_caches()

    async def aclose(self) -> None:
        errors: list[Exception] = []
        for name in tuple(reversed(self._loaded)):
            try:
                await self.disable(name)
            except Exception as error:
                errors.append(error)
        if self._installed:
            if self._path_hook in sys.path_hooks:
                sys.path_hooks.remove(self._path_hook)
            if self._path in sys.path:
                sys.path.remove(self._path)
            sys.path_importer_cache.pop(self._path, None)
            importlib.invalidate_caches()
            self._installed = False
        if errors:
            raise WorkshopError("Some workshop plugins failed to unload", code="native_cleanup_failed") from errors[0]
