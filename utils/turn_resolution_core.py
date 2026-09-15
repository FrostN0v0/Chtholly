"""Generation-local autonomous terminal decision state."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Iterator

_OUTCOMES = {"automatic", "silent", "declined", "delivered"}


@dataclass(slots=True)
class TurnResolution:
    outcome: str = "automatic"
    reason: str = ""
    reply: str = ""
    source: str = "model"

    def audit(self) -> dict[str, str]:
        return {"outcome": self.outcome, "source": self.source, "reason": self.reason[:240]}

    @property
    def explicit(self) -> bool:
        return self.outcome in {"silent", "declined", "delivered"}

    def set(self, outcome: str, *, reason: str = "", reply: str = "", source: str = "model") -> None:
        if outcome not in _OUTCOMES or outcome == "automatic":
            raise ValueError("invalid terminal outcome")
        self.outcome = outcome
        self.reason = reason[:240]
        self.reply = reply[:600]
        self.source = source if source in {"model", "runtime"} else "runtime"


_ACTIVE: ContextVar[TurnResolution | None] = ContextVar("llm_chat_turn_resolution", default=None)


@contextmanager
def turn_resolution_scope(resolution: TurnResolution) -> Iterator[TurnResolution]:
    token = _ACTIVE.set(resolution)
    try:
        yield resolution
    finally:
        _ACTIVE.reset(token)


def current_turn_resolution() -> TurnResolution | None:
    return _ACTIVE.get()
