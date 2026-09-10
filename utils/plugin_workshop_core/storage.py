"""Private, no-link file storage primitives for immutable versions."""

from __future__ import annotations

import os
import stat
import shutil
from pathlib import Path

from .models import WorkshopError


def checked_stat(path: Path) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise WorkshopError(
            "Workshop storage may not contain links or reparse points", code="unsafe_storage", status=409
        )
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        raise WorkshopError("Workshop source may not contain hard links", code="unsafe_storage", status=409)
    if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
        raise WorkshopError("Workshop storage contains a nonregular entry", code="unsafe_storage", status=409)
    return info


def check_ancestors(path: Path) -> None:
    for current in reversed((path, *path.parents)):
        try:
            info = checked_stat(current)
        except FileNotFoundError:
            continue
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise WorkshopError("Workshop storage ancestor is not a directory", code="unsafe_storage", status=409)


def ensure_directory(path: Path) -> None:
    check_ancestors(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_ancestors(path)
    if not stat.S_ISDIR(checked_stat(path).st_mode):
        raise WorkshopError("Workshop storage is not a directory", code="unsafe_storage", status=409)


def sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_exclusive(path: Path, data: bytes) -> None:
    ensure_directory(path.parent)
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def read_bounded(path: Path, maximum: int) -> bytes:
    try:
        check_ancestors(path)
        info = checked_stat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise WorkshopError("Immutable source size or type changed", code="source_tampered", status=409)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink > 1:
                raise WorkshopError(
                    "Immutable source is not a private regular file", code="source_tampered", status=409
                )
            data = handle.read(maximum + 1)
        if len(data) > maximum:
            raise WorkshopError("Immutable source grew beyond its limit", code="source_tampered", status=409)
        return data
    except FileNotFoundError as exc:
        raise WorkshopError("Immutable workshop source is missing", code="source_missing", status=409) from exc
    except OSError as exc:
        raise WorkshopError("Immutable workshop source cannot be read", code="source_unavailable", status=503) from exc


def tree_files(root: Path, *, max_entries: int = 100000) -> dict[str, int]:
    check_ancestors(root)
    if not stat.S_ISDIR(checked_stat(root).st_mode):
        raise WorkshopError("Workshop source root is not a directory", code="unsafe_storage", status=409)
    pending = [root]
    result: dict[str, int] = {}
    inspected = 0
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                inspected += 1
                if inspected > max_entries:
                    raise WorkshopError("Workshop storage contains too many entries", code="limit_exceeded", status=413)
                path = Path(entry.path)
                info = checked_stat(path)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                else:
                    result[path.relative_to(root).as_posix()] = info.st_size
    return result


def compensate_directory(path: Path) -> None:
    """Remove only a validated private tree; retained debris is quota-accounted."""
    try:
        if not path.exists():
            return
        tree_files(path)
        shutil.rmtree(path)
        sync_directory(path.parent)
    except (OSError, WorkshopError):
        # A failed compensation is safe: future publication counts all residue.
        pass
