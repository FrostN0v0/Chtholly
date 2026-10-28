"""Live generation-local staging for the companion conversation owner."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import field, dataclass
from collections.abc import Iterator

from utils.companion_core.dialogue import DialogueAction, DialogueDecision


@dataclass(slots=True)
class DialogueControl:
    """Hold a decision without sending, scheduling or resolving the current turn."""

    _decision: DialogueDecision = field(default_factory=DialogueDecision, init=False)
    _engaged_speakers: tuple[str, ...] = field(default=(), init=False)
    _live: bool = field(default=True, init=False)

    @property
    def decision(self) -> DialogueDecision:
        return self._decision

    @property
    def engaged_speakers(self) -> tuple[str, ...]:
        return self._engaged_speakers

    @property
    def live(self) -> bool:
        return self._live

    def stage(
        self,
        action: DialogueAction,
        seconds: int = 30,
        *,
        engaged_speakers: list[str] | None = None,
    ) -> DialogueDecision:
        if not self._live or _ACTIVE_DIALOGUE_CONTROL.get() is not self:
            raise ValueError("Companion dialogue control is no longer active in this generation")
        decision = DialogueDecision(action=action, seconds=seconds)
        if engaged_speakers is not None:
            from .participants import current_companion_participants

            registry = current_companion_participants()
            if registry is None:
                raise ValueError("Companion engagement requires a live participant registry")
            registry.engage(engaged_speakers)
            self._engaged_speakers = tuple(dict.fromkeys((*self._engaged_speakers, *engaged_speakers)))
        self._decision = decision
        return decision

    def close(self) -> None:
        """Fence copied child contexts before the owner commits the staged decision."""
        self._live = False


_ACTIVE_DIALOGUE_CONTROL: ContextVar[DialogueControl | None] = ContextVar(
    "llm_chat_companion_dialogue_control", default=None
)


@contextmanager
def dialogue_control_scope() -> Iterator[DialogueControl]:
    control = DialogueControl()
    token = _ACTIVE_DIALOGUE_CONTROL.set(control)
    try:
        yield control
    finally:
        control.close()
        _ACTIVE_DIALOGUE_CONTROL.reset(token)


def current_dialogue_control() -> DialogueControl | None:
    """Return only a live generation capability, including in inherited tasks."""
    control = _ACTIVE_DIALOGUE_CONTROL.get()
    return control if control is not None and control.live else None
