"""Generation-local, single-use media prepared for model-controlled message delivery."""

from __future__ import annotations

import asyncio
import logging
from secrets import token_hex
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import field, dataclass
from collections.abc import Mapping, Callable, Sequence, Awaitable, AsyncIterator

from satori import Element
from arclet.entari import Session

from .core.types import JSONType
from .core.delivery import DeliveryRejected, require_llm_chat_delivery
from .core.tool_trace import record_tool_evidence, current_tool_execution_ref
from .core.media_delivery import ImageProvenance, current_media_requirements

_Callback = Callable[[], Awaitable[None]]
_MAX_ITEM_BYTES = 10 * 1024 * 1024
_MAX_PREPARED_BYTES = 60 * 1024 * 1024
_KINDS = {"img": "image", "image": "image", "audio": "audio", "video": "video", "file": "file", "message": "message"}
_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PreparedMedia:
    ref: str
    element: Element
    byte_count: int
    tool_name: str
    history_marker: str
    provenance: ImageProvenance | None
    execution_ref: str
    metadata: dict[str, object]
    on_confirm: _Callback | None = None
    consumed: bool = False
    confirmed: bool = False

    @property
    def kind(self) -> str:
        return _KINDS[self.element.tag]

    def describe(self) -> dict[str, JSONType]:
        return {
            "status": "prepared",
            "media_ref": self.ref,
            "kind": self.kind,
            "bytes": self.byte_count,
            "source_tool": self.tool_name,
            "guidance": "Not sent. Place this media_ref in a send_msg media segment to deliver it once this turn.",
        }


@dataclass(slots=True)
class _PreparedMediaState:
    owner: tuple[str, str, str, str] | None = None
    entries: dict[str, PreparedMedia] = field(default_factory=dict)
    byte_count: int = 0
    closed: bool = False


_ACTIVE: ContextVar[_PreparedMediaState | None] = ContextVar("llm_chat_prepared_media", default=None)


def _state(session: Session | None = None) -> _PreparedMediaState:
    require_llm_chat_delivery()
    state = _ACTIVE.get()
    if state is None or state.closed:
        raise DeliveryRejected("Prepared media is unavailable outside the active generation")
    if session is not None:
        owner = (session.account.platform, session.account.self_id, session.channel.id, session.user.id)
        if state.owner is None:
            state.owner = owner
        elif state.owner != owner:
            raise DeliveryRejected("Prepared media belongs to another conversation")
    return state


@asynccontextmanager
async def prepared_media_scope() -> AsyncIterator[None]:
    state = _PreparedMediaState()
    token = _ACTIVE.set(state)
    try:
        yield
    finally:
        state.closed = True
        _ACTIVE.reset(token)
        state.entries.clear()
        state.byte_count = 0


def prepare_media(
    session: Session,
    element: Element,
    *,
    byte_count: int,
    tool_name: str,
    history_marker: str = "",
    on_confirm: _Callback | None = None,
    provenance: ImageProvenance | None = None,
    metadata: Mapping[str, object] | None = None,
) -> dict[str, JSONType]:
    """Store validated bytes without sending, reserving delivery, or publishing history."""
    state = _state(session)
    if element.tag not in _KINDS:
        raise DeliveryRejected("Unsupported prepared media element")
    if type(byte_count) is not int or byte_count < 0 or byte_count > _MAX_ITEM_BYTES:
        raise DeliveryRejected("Prepared media exceeds the size limit")
    if element.tag != "message" and byte_count == 0:
        raise DeliveryRejected("Prepared media is empty")
    ensure_media_capacity(1, byte_count=byte_count)
    ref = f"media_{token_hex(16)}"
    item = PreparedMedia(
        ref,
        element,
        byte_count,
        tool_name,
        history_marker,
        provenance,
        current_tool_execution_ref(),
        dict(metadata or {}),
        on_confirm,
    )
    state.entries[ref] = item
    state.byte_count += byte_count
    return item.describe()


def ensure_media_capacity(count: int, *, byte_count: int) -> None:
    state = _state()
    delivery = require_llm_chat_delivery()
    if type(count) is not int or count < 1 or type(byte_count) is not int or byte_count < 0:
        raise DeliveryRejected("Invalid prepared media batch")
    if len(state.entries) + delivery.media_messages + count > delivery.limits.max_media_messages:
        raise DeliveryRejected("Prepared media exceeds the remaining media budget")
    if state.byte_count + byte_count > _MAX_PREPARED_BYTES:
        raise DeliveryRejected("Prepared media exceeds the generation memory limit")


def prepared_media_metadata() -> tuple[Mapping[str, object], ...]:
    return tuple(item.metadata for item in _state().entries.values())


def resolve_media(session: Session, refs: Sequence[str]) -> tuple[PreparedMedia, ...]:
    state = _state(session)
    resolved = []
    for ref in refs:
        item = state.entries.get(ref) if isinstance(ref, str) else None
        if item is None or item.consumed:
            raise DeliveryRejected("Media reference is unavailable, expired, or already used")
        resolved.append(item)
    return tuple(resolved)


def consume_media(items: Sequence[PreparedMedia]) -> None:
    state = _state()
    unique = {item.ref: item for item in items}
    if any(item.consumed or state.entries.get(ref) is not item for ref, item in unique.items()):
        raise DeliveryRejected("Media reference is unavailable, expired, or already used")
    for ref, item in unique.items():
        item.consumed = True
        del state.entries[ref]
        state.byte_count -= item.byte_count


async def confirm_media(items: Sequence[PreparedMedia]) -> None:
    for item in {item.ref: item for item in items}.values():
        if item.confirmed:
            continue
        if not item.consumed:
            raise DeliveryRejected("Cannot confirm media before its send attempt")
        item.confirmed = True
        requirements = current_media_requirements()
        if requirements is not None and requirements.accepts(item.provenance):
            requirements.confirmed = True
        record_tool_evidence(
            {
                "prepared_media": [
                    {
                        "source_tool": item.tool_name,
                        "source_execution_ref": item.execution_ref,
                        "kind": item.kind,
                        "bytes": item.byte_count,
                        "confirmed": True,
                        **item.metadata,
                    }
                ]
            }
        )
        if item.on_confirm is not None:
            try:
                await item.on_confirm()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.warning("Confirmed media bookkeeping failed (%s)", type(exc).__name__)


def list_prepared_media() -> list[dict[str, JSONType]]:
    return [item.describe() for item in _state().entries.values()]
