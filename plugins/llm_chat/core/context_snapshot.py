"""Capture the actual composed prompt and its named runtime inputs for operators."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from .model_audit import sanitize_model_request


def build_context_snapshot(
    *,
    system: str,
    messages: Sequence[Mapping[str, object]],
    model: str,
    persona: Mapping[str, object],
    selection: Mapping[str, object],
    budgets: Mapping[str, object],
) -> dict[str, object]:
    """Keep recorded context distinct from later provider-level request transformations."""
    marker = "<runtime_context>\n"
    start = system.rfind(marker)
    end = system.find("\n</runtime_context>", start + len(marker)) if start >= 0 else -1
    runtime: object = None
    if start >= 0 and end >= 0:
        try:
            runtime = json.loads(system[start + len(marker) : end])
        except ValueError:
            pass
    blocks = (
        [
            {
                "name": str(name),
                "content": value,
                "chars": len(json.dumps(value, ensure_ascii=False, separators=(",", ":"))),
            }
            for name, value in runtime.items()
        ]
        if isinstance(runtime, dict)
        else []
    )
    request = sanitize_model_request(
        [{"role": "system", "content": system}, *messages],
        parameters={
            "persona": dict(persona),
            "selection": dict(selection),
            "budgets": dict(budgets),
            "blocks": blocks,
        },
        model=model,
    )
    captured_messages = request.get("messages")
    captured_parameters = request.get("parameters")
    parameters = captured_parameters if isinstance(captured_parameters, dict) else {}
    recorded = captured_messages if isinstance(captured_messages, list) else []
    first = recorded[0] if recorded and isinstance(recorded[0], dict) else {}
    return {
        "system": first.get("content", ""),
        "messages": recorded[1:],
        "persona": parameters.get("persona", {}),
        "selection": parameters.get("selection", {}),
        "budgets": parameters.get("budgets", {}),
        "blocks": parameters.get("blocks", []),
        "blocks_status": "captured" if isinstance(runtime, dict) else "unavailable",
        "capture_status": request.get("capture_status", "not_recorded"),
        "redactions": request.get("redactions", []),
    }
