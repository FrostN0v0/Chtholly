"""Bounded committed contact facts shared by the single native dialogue executor."""

from __future__ import annotations

from datetime import datetime
from collections.abc import Mapping, Callable, Awaitable

from utils.companion_core import ContactProposal

Runner = Callable[..., Awaitable[str]]


def _contact_context(proposal: ContactProposal, state: Mapping[str, object], *, now: datetime) -> dict[str, object]:
    attention = state.get("attention")
    own_attention = []
    if isinstance(attention, (tuple, list)):
        for item in attention:
            if not isinstance(item, Mapping):
                raise ValueError("Companion contact attention must be structured scoped state")
            own_attention.append(
                {key: item[key] for key in ("key", "kind", "summary", "status", "evidence_turn_ids") if key in item}
            )
    return {
        "purpose": proposal.purpose,
        "topic": proposal.topic,
        "reason": proposal.reason,
        "evidence_turn_ids": list(proposal.evidence_turn_ids),
        "now": now.isoformat(),
        "life": state["life"],
        "attention": own_attention,
    }
