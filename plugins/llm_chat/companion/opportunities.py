"""JSON-only handoff from committed private review to the shared dialogue owner."""

from __future__ import annotations

from datetime import datetime
from collections.abc import Mapping

from utils.companion_core import ContactProposal

from .types import ReviewClaim
from .persistence import naive

_ORIGIN_FIELDS = frozenset(
    {"scope_id", "persona_key", "mode_revision", "context_revision", "lease_token", "lease_deadline", "life_version"}
)


def build_opportunity(origin: ReviewClaim, proposal: ContactProposal) -> dict[str, object]:
    """Construct host data only; the durable claim revalidates the actual committed review."""
    payload = {
        "version": 1,
        "origin": {
            "scope_id": origin.scope_id,
            "persona_key": origin.persona_key,
            "mode_revision": origin.mode_revision,
            "context_revision": origin.context_revision,
            "lease_token": origin.lease_token,
            "lease_deadline": origin.lease_deadline.isoformat(),
            "life_version": origin.life_version,
        },
        "proposal": proposal.model_dump(mode="json"),
    }
    validate_opportunity(payload)
    return payload


def validate_opportunity(payload: object, *, scope_id: int | None = None) -> tuple[ReviewClaim, ContactProposal]:
    if (
        not isinstance(payload, Mapping)
        or set(payload) != {"version", "origin", "proposal"}
        or type(payload["version"]) is not int
        or payload["version"] != 1
    ):
        raise ValueError("Invalid committed companion opportunity envelope")
    origin = payload["origin"]
    if not isinstance(origin, Mapping) or set(origin) != _ORIGIN_FIELDS:
        raise ValueError("Invalid companion opportunity origin")
    for key in ("scope_id", "mode_revision", "context_revision", "life_version"):
        if type(origin[key]) is not int or origin[key] < (1 if key == "scope_id" else 0):
            raise ValueError("Invalid companion opportunity revision")
    for key in ("persona_key", "lease_token"):
        if not isinstance(origin[key], str) or not origin[key].strip() or len(origin[key]) > 200:
            raise ValueError("Invalid companion opportunity capability")
    if not isinstance(origin["lease_deadline"], str):
        raise ValueError("Invalid companion opportunity lease time")
    deadline = datetime.fromisoformat(origin["lease_deadline"])
    naive(deadline, "lease_deadline")
    if scope_id is not None and origin["scope_id"] != scope_id:
        raise ValueError("Companion opportunity belongs to a different scope")
    claim = ReviewClaim(
        origin["scope_id"],
        origin["persona_key"],
        origin["mode_revision"],
        origin["context_revision"],
        origin["lease_token"],
        deadline,
        origin["life_version"],
    )
    proposal = ContactProposal.model_validate(payload["proposal"])
    return claim, proposal
