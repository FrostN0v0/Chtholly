"""Rollback-safe, inode-preserving publication of detached Entari configuration."""

from __future__ import annotations

import os
from typing import Any, BinaryIO
from pathlib import Path
from contextlib import ExitStack, contextmanager
from collections.abc import Mapping, Iterator

from arclet.entari.config.file import EntariConfig

from utils.webui_config_core import ConfigValidationError

_PERSISTENCE_FIELDS = (
    "_origin_data",
    "plugin",
    "_records",
    "_plugin_names",
    "plugin_prefixes",
    "prelude_plugin",
    "plugin_extra_files",
)


def _write(stream: BinaryIO, content: bytes) -> None:
    stream.seek(0)
    if stream.write(content) != len(content):
        raise OSError("Incomplete configuration write")
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


@contextmanager
def persist_candidate(
    live: EntariConfig,
    candidate: EntariConfig,
    snapshots: Mapping[Path, tuple[bytes, dict[str, Any]]],
    changed: Mapping[Path, bytes],
) -> Iterator[None]:
    """Publish persistence only after the caller's runtime application succeeds."""
    with ExitStack() as stack:
        streams = {path: stack.enter_context(path.open("r+b")) for path in changed}
        for path, (previous, _) in snapshots.items():
            if path.read_bytes() != previous:
                raise ConfigValidationError("Configuration changed on disk; reload the page", code="source_changed")
        attempted: list[Path] = []
        try:
            if changed:
                live.save_flag = True
            for path, stream in streams.items():
                attempted.append(path)
                _write(stream, changed[path])
            candidate.save_flag = False
            candidate.reload()
            # Closing a file can fail too; finish it before publishing model state.
            stack.close()
            yield
        except BaseException:
            recovery_failed = False
            for path in reversed(attempted):
                previous = snapshots[path][0]
                try:
                    current = path.read_bytes()
                    if current == previous:
                        continue
                    # A failed truncate can leave the old suffix. Anything else
                    # may belong to an external writer and must not be clobbered.
                    written = changed[path]
                    if current not in (written, written + previous[len(written) :]):
                        recovery_failed = True
                        continue
                    with path.open("r+b") as stream:
                        _write(stream, previous)
                except OSError:
                    recovery_failed = True
            if attempted:
                live.save_flag = True
            if recovery_failed:
                raise ConfigValidationError(
                    "Configuration recovery could not be confirmed; check the managed controller",
                    code="config_rollback_failed",
                ) from None
            raise
        else:
            # Never mutate running BasicConfig or existing plugin-owned objects.
            # Entari's shutdown save must still flush the newly persisted source.
            for field in _PERSISTENCE_FIELDS:
                setattr(live, field, getattr(candidate, field))
