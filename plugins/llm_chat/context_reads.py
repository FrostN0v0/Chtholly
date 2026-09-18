"""Shared model-readable payload boundary for context selection and exact reads."""

from __future__ import annotations

import json
from hashlib import sha256
from collections.abc import Mapping

from .models import AgentEvent
from .agent_events import load_event_payload, select_payload_path
from .core.model_audit import _SECRET_KEY, _PRIVATE_KEY, ADMIN_ONLY_EVENT_TYPES

_PUBLIC_ROOTS = frozenset({"arguments", "result", "content"})
_AUDIT_FIELDS = frozenset({"attachments", "audit_arguments", "audit_result"})


def is_private_context_field(name: str) -> bool:
    return name.casefold() in _AUDIT_FIELDS or bool(_PRIVATE_KEY.search(name) or _SECRET_KEY.search(name))


def _public_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _public_value(item) for key, item in value.items() if not is_private_context_field(str(key))}
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    return value


def model_readable_payload(event: AgentEvent, *, compact: bool = False) -> dict[str, object]:
    """Never expose private siblings, including through a parent-object read."""
    if not event.model_visible or event.event_type in ADMIN_ONLY_EVENT_TYPES:
        return {}
    payload = load_event_payload(event)
    readable: dict[str, object] = {}
    for key, value in payload.items():
        if key not in _PUBLIC_ROOTS:
            continue
        public_value = _public_value(value)
        if compact:
            context_key = f"context_{key}"
            public_value = (
                _public_value(payload[context_key])
                if context_key in payload
                else {
                    "stored": True,
                    "event_ref": event.event_ref,
                    "path": key,
                    "sha256": payload_digest(public_value),
                }
            )
        readable[key] = public_value
    return readable


def public_payload_path(payload: Mapping[str, object], path: str) -> object:
    parts = path.split(".")
    if parts[0] not in _PUBLIC_ROOTS or any(not part or is_private_context_field(part) for part in parts):
        raise KeyError(path)
    return select_payload_path(payload, path)


def payload_digest(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
