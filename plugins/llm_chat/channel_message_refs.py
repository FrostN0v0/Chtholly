"""Opaque, bounded message and pagination capabilities for one generation."""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import field, dataclass
from collections.abc import Iterator

from arclet.entari import Session

MAX_MESSAGE_REFERENCES = 2048
MAX_PAGE_REFERENCES = 256


class ChannelMessageReferenceError(ValueError):
    """An unknown, expired, or differently scoped history capability was used."""


@dataclass(slots=True)
class ChannelMessageReferences:
    _owner: tuple[str, str, str] | None = None
    _messages: dict[str, str] = field(default_factory=dict)
    _message_refs: dict[str, str] = field(default_factory=dict)
    _pages: dict[str, tuple[str, str]] = field(default_factory=dict)
    _page_refs: dict[tuple[str, str], str] = field(default_factory=dict)
    _closed: bool = False

    def _check(self, session: Session) -> None:
        owner = (str(session.account.platform), str(session.account.self_id), str(session.channel.id))
        if self._closed or (self._owner is not None and self._owner != owner):
            raise ChannelMessageReferenceError("Channel history reference is unavailable in this generation or scope")
        self._owner = owner

    def register(self, session: Session, cursor: str) -> str:
        self._check(session)
        if not cursor:
            raise ChannelMessageReferenceError("A private message locator is required")
        if cursor not in self._message_refs:
            if len(self._messages) >= MAX_MESSAGE_REFERENCES:
                raise ChannelMessageReferenceError("Channel history reference capacity reached")
            reference = f"message_{secrets.token_hex(16)}"
            self._messages[reference] = cursor
            self._message_refs[cursor] = reference
        return self._message_refs[cursor]

    def resolve(self, session: Session, reference: str) -> str:
        self._check(session)
        try:
            return self._messages[reference]
        except KeyError as exc:
            raise ChannelMessageReferenceError("A message_ref issued in this generation is required") from exc

    def page(self, session: Session, cursor: str, participant_ref: str = "") -> str:
        self._check(session)
        if not cursor:
            return ""
        target = (cursor, participant_ref)
        if target not in self._page_refs:
            if len(self._pages) >= MAX_PAGE_REFERENCES:
                raise ChannelMessageReferenceError("Channel history page capacity reached")
            reference = f"page_{secrets.token_hex(16)}"
            self._pages[reference] = target
            self._page_refs[target] = reference
        return self._page_refs[target]

    def resolve_page(self, session: Session, reference: str, participant_ref: str = "") -> str:
        self._check(session)
        target = self._pages.get(reference)
        if target is None or target[1] != participant_ref:
            raise ChannelMessageReferenceError(
                "A next_cursor issued for this generation and participant filter is required"
            )
        return target[0]

    def close(self) -> None:
        self._closed = True
        self._messages.clear()
        self._message_refs.clear()
        self._pages.clear()
        self._page_refs.clear()


_ACTIVE: ContextVar[ChannelMessageReferences | None] = ContextVar("channel_message_references", default=None)


@contextmanager
def channel_message_scope(references: ChannelMessageReferences) -> Iterator[None]:
    if references._closed:
        raise ChannelMessageReferenceError("Channel history generation has already ended")
    token = _ACTIVE.set(references)
    try:
        yield
    finally:
        references.close()
        _ACTIVE.reset(token)


def current_channel_message_references() -> ChannelMessageReferences | None:
    return _ACTIVE.get()
