"""Generation-local source and web-reference images for image editing tools."""

from __future__ import annotations

import re
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import field, dataclass
from collections.abc import Mapping, Iterator, Sequence

from .agent_attachments import is_agent_attachment, resolve_agent_attachment
from .core.image_source import IMAGE_FETCH_MAX_BYTES, raw_to_image_data_url

MAX_EDIT_REFERENCES = 4
_WEB_REFERENCE_REF = re.compile(r"web_ref_[0-9a-f]{24}\Z")


@dataclass(frozen=True, slots=True)
class EditableImage:
    data: bytes
    mime: str
    attachment: Mapping[str, object] | None = None
    description: str = ""


@dataclass(slots=True)
class ImageEditReferences:
    """Private per-generation image inputs that model arguments cannot forge."""

    input_attachments: tuple[Mapping[str, object], ...]
    requires_web_reference: bool = False
    requires_image_edit: bool = False
    attachment_root: Path | None = None
    edit_confirmed: bool = False
    _web_references: dict[str, EditableImage] = field(default_factory=dict)

    @classmethod
    def from_input_attachments(
        cls,
        attachments: Sequence[Mapping[str, object]],
        *,
        requires_web_reference: bool,
        requires_image_edit: bool = False,
        attachment_root: Path | None = None,
    ) -> ImageEditReferences:
        return cls(
            input_attachments=tuple(dict(item) for item in attachments),
            requires_web_reference=requires_web_reference,
            requires_image_edit=requires_image_edit or requires_web_reference,
            attachment_root=attachment_root,
        )

    @property
    def source_image_count(self) -> int:
        return len(self.input_attachments)

    @property
    def web_reference_count(self) -> int:
        return len(self._web_references)

    def resolve_source_image(self, index: int) -> EditableImage:
        if type(index) is not int or index < 1 or index > len(self.input_attachments):
            raise ValueError("source image index is unavailable in the current user turn")
        metadata = self.input_attachments[index - 1]
        attachment_ref = metadata.get("attachment_ref")
        if not isinstance(attachment_ref, str) or not attachment_ref.startswith("input_"):
            raise ValueError("source image attachment is unavailable")
        return self._read_attachment(metadata)

    def add_web_reference(
        self,
        reference_ref: str,
        data: bytes,
        *,
        mime: str,
        description: str,
        attachment: Mapping[str, object],
    ) -> None:
        if not _WEB_REFERENCE_REF.fullmatch(reference_ref):
            raise ValueError("invalid web reference")
        if len(self._web_references) >= MAX_EDIT_REFERENCES:
            raise ValueError("web reference limit reached")
        normalized_mime = mime.strip().casefold()
        data_url = raw_to_image_data_url(data)
        detected_mime = data_url[5:].partition(";")[0].casefold() if data_url is not None else ""
        if not data or len(data) > IMAGE_FETCH_MAX_BYTES or detected_mime != normalized_mime:
            raise ValueError("invalid web reference image")
        if attachment.get("mime") != detected_mime or not str(attachment.get("attachment_ref", "")).startswith(
            "reference_"
        ):
            raise ValueError("invalid web reference attachment")
        self._web_references[reference_ref] = EditableImage(
            data=data,
            mime=detected_mime,
            description=" ".join(description.split())[:500],
            attachment=dict(attachment),
        )

    def resolve_web_references(self, reference_refs: Sequence[str]) -> tuple[EditableImage, ...]:
        if len(reference_refs) > MAX_EDIT_REFERENCES:
            raise ValueError("too many web references")
        resolved: list[EditableImage] = []
        seen: set[str] = set()
        for raw in reference_refs:
            reference_ref = raw.strip()
            if reference_ref in seen or not _WEB_REFERENCE_REF.fullmatch(reference_ref):
                raise ValueError("invalid or duplicate web reference")
            image = self._web_references.get(reference_ref)
            if image is None:
                raise ValueError("web reference was not captured in the current generation")
            seen.add(reference_ref)
            resolved.append(image)
        return tuple(resolved)

    def _read_attachment(self, metadata: Mapping[str, object]) -> EditableImage:
        attachment_ref = metadata.get("attachment_ref")
        mime = metadata.get("mime")
        if (
            not is_agent_attachment(attachment_ref, mime)
            or not isinstance(attachment_ref, str)
            or not isinstance(mime, str)
        ):
            raise ValueError("invalid source image attachment")
        path = resolve_agent_attachment(attachment_ref, mime, root=self.attachment_root)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ValueError("source image attachment is unavailable") from exc
        data_url = raw_to_image_data_url(data)
        detected_mime = data_url[5:].partition(";")[0].casefold() if data_url is not None else ""
        if not data or len(data) > IMAGE_FETCH_MAX_BYTES or detected_mime != mime.casefold():
            raise ValueError("source image attachment is invalid")
        return EditableImage(data=data, mime=detected_mime, attachment=dict(metadata))


_ACTIVE_IMAGE_EDIT_REFERENCES: ContextVar[ImageEditReferences | None] = ContextVar(
    "llm_chat_image_edit_references",
    default=None,
)


@contextmanager
def llm_chat_image_edit_scope(references: ImageEditReferences) -> Iterator[None]:
    token = _ACTIVE_IMAGE_EDIT_REFERENCES.set(references)
    try:
        yield
    finally:
        _ACTIVE_IMAGE_EDIT_REFERENCES.reset(token)


def current_image_edit_references() -> ImageEditReferences | None:
    return _ACTIVE_IMAGE_EDIT_REFERENCES.get()


__all__ = [
    "EditableImage",
    "ImageEditReferences",
    "MAX_EDIT_REFERENCES",
    "current_image_edit_references",
    "llm_chat_image_edit_scope",
]
