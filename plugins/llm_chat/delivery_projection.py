"""Bounded readable projection of actual outgoing Entari message elements."""

from __future__ import annotations

from collections.abc import Sequence

from satori import At, Text, Element
from arclet.entari import Session

from .core.model_audit import sanitize_audit_value
from .agent_attachments import MAX_EVENT_ATTACHMENTS

_MAX_TEXT_CHARS = 50_000
_MAX_ELEMENTS = 1024


def _safe_text(value: str) -> tuple[str, bool]:
    snapshot = sanitize_audit_value(value)
    text = snapshot["data"]
    if not isinstance(text, str):
        return "[内容未能捕获]", True
    return text, snapshot["capture_status"] != "complete"


class _Projection:
    """Walk sent elements, never serialize resource attributes into the audit."""

    def __init__(self, session: Session):
        self.session = session
        self.parts: list[str] = []
        self.images: list[tuple[int, str]] = []
        self.media: list[dict[str, object]] = []
        self.remaining = _MAX_TEXT_CHARS
        self.nodes = 0
        self.image_count = 0
        self.partial = False
        self.redacted = False

    def text(self, text: str) -> None:
        if len(text) > self.remaining:
            text = text[: self.remaining]
            self.partial = True
        self.remaining -= len(text)
        self.parts.append(text)

    def walk(self, elements: Sequence[Element], depth: int = 0, *, quoted: bool = False) -> None:
        if depth > 16:
            self.partial = True
            return
        for element in elements:
            self.nodes += 1
            if self.nodes > _MAX_ELEMENTS:
                self.partial = True
                return
            tag = element.tag
            if isinstance(element, Text):
                self.text(element.text)
            elif isinstance(element, At):
                name = element.name
                user = self.session.event.user
                if not name and user is not None and element.id == user.id:
                    member = self.session.event.member
                    name = (member.nick if member is not None else None) or user.nick or user.name
                if name == element.id:
                    name = None
                if element.type in {"all", "here"}:
                    name = "全体成员" if element.type == "all" else "在线成员"
                self.text(f"@{name}" if name else "[提及用户]")
            elif tag in {"img", "image"}:
                if quoted:
                    self.text("[引用图片]")
                    continue
                self.image_count += 1
                index = self.image_count
                self.text(f"[图片 {index}]")
                source = getattr(element, "src", None)
                if len(self.images) < MAX_EVENT_ATTACHMENTS and isinstance(source, str):
                    self.images.append((index, source))
                else:
                    self.media.append({"kind": "image", "label": f"图片 {index}", "capture_status": "omitted"})
                    self.partial = True
            elif tag in {"audio", "file", "video"}:
                label = {"audio": "语音", "file": "文件", "video": "视频"}[tag]
                self.text(f"[{label}]")
                self.media.append({"kind": tag, "label": label, "capture_status": "unsupported"})
                self.partial = True
            elif tag == "author":
                name = getattr(element, "name", None)
                if isinstance(name, str) and name:
                    self.text(f"{name}: ")
                elif element.children:
                    self.walk(element.children, depth + 1, quoted=quoted)
                    self.text(": ")
            elif tag in {"message", "quote", "p", "blockquote"}:
                self.text("\n")
                if tag == "quote":
                    self.text("[引用] ")
                self.walk(element.children, depth + 1, quoted=quoted or tag == "quote")
                self.text("\n")
            elif tag == "br":
                self.text("\n")
            elif tag == "a":
                if element.children:
                    self.walk(element.children, depth + 1, quoted=quoted)
                else:
                    href = getattr(element, "href", None)
                    self.text(href if isinstance(href, str) else "[链接]")
            elif tag in {"b", "i", "u", "s", "spl", "code", "sup", "sub", "button"}:
                self.walk(element.children, depth + 1, quoted=quoted)
            else:
                # Raw/custom elements may contain executable markup or source URLs.
                self.text("[未支持的消息元素]")
                self.partial = True

    def content(self) -> str:
        text, self.redacted = _safe_text("".join(self.parts).strip())
        return text
