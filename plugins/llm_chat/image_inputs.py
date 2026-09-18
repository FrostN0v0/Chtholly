"""Bounded, generation-local authoritative image acquisition and authorization."""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal
import asyncio
from hashlib import sha256
from pathlib import Path
from secrets import token_hex
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Mapping, Callable, Hashable, Iterator, Awaitable

from arclet.entari import Session

from .agent_attachments import MAX_INPUT_ATTACHMENTS, store_agent_attachment, remove_agent_attachments
from .core.image_source import IMAGE_FETCH_MAX_BYTES, raw_to_image_data_url
from .core.self_reference import load_self_reference_image

ImageSource = Literal["direct", "quoted", "forward", "channel", "avatar", "web", "persona"]
ImagePurpose = Literal["inspect", "send", "edit", "reference", "collect"]
MAX_IMAGE_INPUT_BYTES = 60 * 1024 * 1024
MAX_IMAGE_INPUT_REFERENCES = 256
_ALLOWED_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
_PERMISSIONS: dict[str, frozenset[str]] = {
    "direct": frozenset({"inspect", "send", "edit", "reference", "collect"}),
    "quoted": frozenset({"inspect", "send", "edit", "reference", "collect"}),
    "forward": frozenset({"inspect", "send", "edit", "reference"}),
    "channel": frozenset({"inspect", "send", "edit", "reference"}),
    "avatar": frozenset({"inspect", "send", "edit", "reference"}),
    "web": frozenset({"reference"}),
    "persona": frozenset({"reference"}),
}


class ImageInputError(RuntimeError):
    """An image reference is unauthorized, unavailable, or exceeds its bound."""


@dataclass(frozen=True, slots=True)
class ImageSnapshot:
    data: bytes
    mime: str
    source: str
    image_ref: str
    digest: str
    attachment: Mapping[str, object] | None = None
    audit_status: str = "unrecorded"


@dataclass(frozen=True, slots=True)
class _Pixels:
    data: bytes
    mime: str
    digest: str


@dataclass(slots=True)
class _Acquisition:
    load: Callable[[], Awaitable[bytes]] | None
    task: asyncio.Task[_Pixels] | None = None
    pixels: _Pixels | None = None
    error: str = ""


@dataclass(slots=True)
class _Input:
    source: ImageSource
    index: int
    acquisition: _Acquisition
    snapshot: ImageSnapshot | None = None


def _session_owner(session: Session) -> tuple[str, ...]:
    account = session.account
    event = getattr(session, "event", None)
    message = getattr(event, "message", None)
    user = getattr(session, "user", None)
    return (
        str(getattr(account, "platform", "")),
        str(account.self_id),
        str(session.channel.id),
        str(getattr(user, "id", "")),
        str(getattr(message, "id", "") or id(session)),
    )


class ImageInputs:
    """Keep original positions, independent permissions, and shared immutable pixels."""

    def __init__(
        self,
        *,
        attachment_root: Path | None = None,
        warn: Callable[[str], object] | None = None,
        max_bytes: int = MAX_IMAGE_INPUT_BYTES,
    ) -> None:
        self.attachment_root = attachment_root
        self.warn = warn
        self.max_bytes = min(MAX_IMAGE_INPUT_BYTES, max(0, max_bytes))
        self.persona_reference_path: str | None = None
        self.supports_image_input = False
        self._owner: tuple[str, ...] | None = None
        self._closed = False
        self._entries: dict[str, _Input] = {}
        self._positions: dict[int, str] = {}
        self._references: dict[tuple[str, Hashable, int], str] = {}
        self._acquisitions: dict[Hashable, _Acquisition] = {}
        self._pixels: dict[str, _Pixels] = {}
        self._input_audit_committed = False
        self._byte_count = 0
        self._inspections: dict[str, ImageSnapshot] = {}

    def _check(self, session: Session) -> None:
        if self._closed:
            raise ImageInputError("Image inputs are no longer available for this turn")
        owner = _session_owner(session)
        if self._owner is None:
            self._owner = owner
        elif owner != self._owner:
            raise ImageInputError("Image reference belongs to another account, channel, or turn")

    def register(
        self,
        session: Session,
        *,
        source: ImageSource,
        key: Hashable,
        load: Callable[[], Awaitable[bytes]],
        index: int = 0,
    ) -> str:
        self._check(session)
        if source not in _PERMISSIONS:
            raise ImageInputError("Unsupported image source")
        identity = (source, key, index)
        existing = self._references.get(identity)
        if existing is not None:
            return existing
        if source in {"channel", "avatar"} and self.source_count("channel") + self.source_count("avatar") >= 32:
            raise ImageInputError("The channel image reference capacity for this turn is exhausted")
        if len(self._entries) >= MAX_IMAGE_INPUT_REFERENCES:
            raise ImageInputError("The image reference capacity for this turn is exhausted")
        if source in {"direct", "quoted"} and index in self._positions:
            raise ImageInputError("The original image position is already registered")
        acquisition = self._acquisitions.setdefault(key, _Acquisition(load))
        ref = f"image_{token_hex(16)}"
        self._entries[ref] = _Input(source, index, acquisition)
        self._references[identity] = ref
        if source in {"direct", "quoted"} and index > 0:
            self._positions[index] = ref
        return ref

    def _retain(self, data: bytes) -> _Pixels:
        if not isinstance(data, bytes) or not data or len(data) > IMAGE_FETCH_MAX_BYTES:
            raise ImageInputError("Image bytes are unavailable or exceed the single-image limit")
        data_url = raw_to_image_data_url(data[:256])
        mime = data_url[5:].partition(";")[0].casefold() if data_url else ""
        if mime not in _ALLOWED_MIMES:
            raise ImageInputError("Image format must be PNG, JPEG, WebP, or GIF")
        digest = sha256(data).hexdigest()
        existing = self._pixels.get(digest)
        if existing is not None:
            return existing
        if self._byte_count + len(data) > self.max_bytes:
            raise ImageInputError("The original-image byte capacity for this turn is exhausted")
        pixels = _Pixels(data, mime, digest)
        self._pixels[digest] = pixels
        self._byte_count += len(data)
        return pixels

    def register_bytes(
        self, session: Session, *, source: ImageSource, key: Hashable, data: bytes, index: int = 0
    ) -> str:
        self._check(session)

        async def supplied() -> bytes:
            return data

        ref = self.register(session, source=source, key=key, load=supplied, index=index)
        acquisition = self._entries[ref].acquisition
        if acquisition.task is None and acquisition.pixels is None and not acquisition.error:
            try:
                acquisition.pixels = self._retain(data)
            except ImageInputError as exc:
                acquisition.error = str(exc)
                raise
            finally:
                acquisition.load = None
        return ref

    async def _acquire(self, acquisition: _Acquisition) -> _Pixels:
        try:
            if acquisition.load is None:
                raise ImageInputError("Image source is unavailable")
            data = await acquisition.load()
            if self._closed:
                raise ImageInputError("Image inputs were closed during acquisition")
            acquisition.pixels = self._retain(data)
            return acquisition.pixels
        except asyncio.CancelledError:
            acquisition.error = "Image acquisition was cancelled"
            raise
        except Exception as exc:
            acquisition.error = str(exc) if isinstance(exc, ImageInputError) else "Image acquisition failed"
            raise ImageInputError(acquisition.error) from exc
        finally:
            acquisition.load = None

    async def resolve(self, session: Session, image_ref: str, *, purpose: ImagePurpose) -> ImageSnapshot:
        self._check(session)
        entry = self._entries.get(image_ref)
        if entry is None:
            raise ImageInputError("Unknown image reference for this turn")
        if purpose not in _PERMISSIONS[entry.source]:
            raise ImageInputError("This image source is not authorized for the requested use")
        if entry.snapshot is not None:
            return entry.snapshot
        acquisition = entry.acquisition
        if acquisition.error:
            raise ImageInputError(acquisition.error)
        pixels = acquisition.pixels
        if pixels is None:
            if acquisition.task is None:
                acquisition.task = asyncio.create_task(self._acquire(acquisition), name="llm-chat-image-input")
                acquisition.task.add_done_callback(lambda task: _finish_acquisition(acquisition, task))
            pixels = await asyncio.shield(acquisition.task)
        self._check(session)
        if entry.snapshot is None:
            attachment = None
            try:
                if entry.source not in {"direct", "quoted"} or entry.index <= MAX_INPUT_ATTACHMENTS:
                    attachment = MappingProxyType(
                        store_agent_attachment(
                            pixels.data,
                            kind="input" if entry.source in {"direct", "quoted", "forward"} else "reference",
                            source=entry.source,
                            index=entry.index,
                            root=self.attachment_root,
                        )
                    )
            except Exception as exc:
                if self.warn is not None:
                    self.warn(f"image snapshot audit unrecorded: {type(exc).__name__}")
            entry.snapshot = ImageSnapshot(
                pixels.data,
                pixels.mime,
                entry.source,
                image_ref,
                pixels.digest,
                attachment,
                "recorded" if attachment is not None else "unrecorded",
            )
        return entry.snapshot

    async def resolve_persona(self, session: Session) -> ImageSnapshot:
        self._check(session)
        path = self.persona_reference_path

        async def load() -> bytes:
            result = load_self_reference_image(path)
            if result is None:
                raise ImageInputError("The configured persona reference is unavailable")
            return result[0]

        ref = self.register(session, source="persona", key=("persona", path), load=load)
        return await self.resolve(session, ref, purpose="reference")

    def input_ref(self, index: int) -> str:
        if self._closed or type(index) is not int or index not in self._positions:
            raise ImageInputError("Image index does not identify an original input in this turn")
        return self._positions[index]

    def input_views(self) -> list[dict[str, object]]:
        return [self.view(ref) for _, ref in sorted(self._positions.items())] if not self._closed else []

    def source_count(self, source: ImageSource) -> int:
        return sum(entry.source == source for entry in self._entries.values())

    def commit_input_audit(self) -> None:
        """Transfer initial attachment ownership after the user-input event is stored."""
        self._input_audit_committed = True

    def audit_view(self, image_ref: str) -> dict[str, object]:
        view = self.view(image_ref)
        view.pop("image_ref", None)
        snapshot = self._entries[image_ref].snapshot
        if snapshot is not None and snapshot.attachment is not None:
            view.update(snapshot.attachment)
            view["audit_status"] = "recorded"
        else:
            view["audit_status"] = "unrecorded"
        return view

    def input_audit_views(self) -> list[dict[str, object]]:
        return [
            self.audit_view(ref)
            for ref, entry in self._entries.items()
            if entry.source == "forward"
            or (entry.source in {"direct", "quoted"} and entry.index <= MAX_INPUT_ATTACHMENTS)
        ]

    def view(self, image_ref: str) -> dict[str, object]:
        if self._closed or image_ref not in self._entries:
            raise ImageInputError("Unknown image reference for this turn")
        entry = self._entries[image_ref]
        acquisition = entry.acquisition
        status = (
            "unavailable"
            if acquisition.error
            else ("ready" if acquisition.pixels is not None else "loading" if acquisition.task else "unloaded")
        )
        return {"index": entry.index, "image_ref": image_ref, "source": entry.source, "status": status}

    def queue_inspection(self, snapshot: ImageSnapshot) -> None:
        entry = self._entries.get(snapshot.image_ref)
        if self._closed or entry is None or entry.snapshot is not snapshot:
            raise ImageInputError("Inspection image does not belong to this turn")
        if "inspect" not in _PERMISSIONS[entry.source]:
            raise ImageInputError("This image is not authorized for inspection")
        if len(self._inspections) >= 6 and snapshot.image_ref not in self._inspections:
            raise ImageInputError("The pending inspection image limit is exhausted")
        self._inspections[snapshot.image_ref] = snapshot

    def drain_inspections(self) -> tuple[ImageSnapshot, ...]:
        snapshots = tuple(self._inspections.values())
        self._inspections.clear()
        return snapshots

    async def aclose(self) -> None:
        self._closed = True
        tasks = [item.task for item in self._acquisitions.values() if item.task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if not self._input_audit_committed:
            remove_agent_attachments(
                [
                    dict(entry.snapshot.attachment)
                    for entry in self._entries.values()
                    if entry.snapshot is not None and entry.snapshot.attachment is not None
                ],
                root=self.attachment_root,
            )
        self._entries.clear()
        self._positions.clear()
        self._references.clear()
        self._acquisitions.clear()
        self._pixels.clear()
        self._inspections.clear()
        self._byte_count = 0


def _finish_acquisition(acquisition: _Acquisition, task: asyncio.Task[_Pixels]) -> None:
    if not task.cancelled():
        task.exception()
    acquisition.task = None


_ACTIVE_IMAGE_INPUTS: ContextVar[ImageInputs | None] = ContextVar("llm_chat_image_inputs", default=None)


@contextmanager
def image_inputs_scope(inputs: ImageInputs) -> Iterator[None]:
    token = _ACTIVE_IMAGE_INPUTS.set(inputs)
    try:
        yield
    finally:
        _ACTIVE_IMAGE_INPUTS.reset(token)


def current_image_inputs() -> ImageInputs | None:
    return _ACTIVE_IMAGE_INPUTS.get()
