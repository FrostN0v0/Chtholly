"""Bounded readable projection of actual outgoing Entari message elements."""

from __future__ import annotations

from dataclasses import field, dataclass
from collections.abc import Callable, Sequence

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


@dataclass
class _Message:
    speaker: str | None = None
    role: str = "unknown"
    parts: list[str] = field(default_factory=list)
    reply_to: _Message | None = None
    forwarded_messages: list[_Message] = field(default_factory=list)
    image_indexes: list[int] = field(default_factory=list)
    reply_to_sequence: int | None = None
    truncated: bool = False


class _Projection:
    """Snapshot only actual sent elements; resource attributes never enter JSON."""

    def __init__(self, session: Session, reply_sequence: Callable[[str], int | None] | None = None):
        self.session = session
        self.reply_sequence = reply_sequence
        user = getattr(session.account.self_info, "user", None)
        name = (getattr(user, "nick", None) or getattr(user, "name", None)) if user is not None else None
        self.message = _Message(
            speaker=name if isinstance(name, str) and name != session.account.self_id else None,
            role="assistant",
        )
        self.images: list[tuple[int, str]] = []
        self.media: list[dict[str, object]] = []
        self.remaining = _MAX_TEXT_CHARS
        self.nodes = 0
        self.image_count = 0
        self.partial = False
        self.redacted = False
        self._snapshot: dict[str, object] | None = None

    def _incomplete(self, message: _Message) -> None:
        message.truncated = True
        self.partial = True

    def text(self, text: str, message: _Message) -> None:
        if len(text) > self.remaining:
            text = text[: self.remaining]
            self._incomplete(message)
        self.remaining -= len(text)
        message.parts.append(text)

    def walk(
        self, elements: Sequence[Element], depth: int = 0, *, quoted: bool = False, message: _Message | None = None
    ) -> None:
        target = self.message if message is None else message
        if depth > 16:
            self._incomplete(target)
            return
        for element in elements:
            self.nodes += 1
            if self.nodes > _MAX_ELEMENTS:
                self._incomplete(target)
                return
            tag = element.tag
            if isinstance(element, Text):
                self.text(element.text, target)
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
                self.text(f"@{name}" if name else "[提及用户]", target)
            elif tag in {"img", "image"}:
                if quoted:
                    # Quoted resources are not new sends and must not cause downloads.
                    self.text("[引用图片未记录]", target)
                    self._incomplete(target)
                    continue
                self.image_count += 1
                index = self.image_count
                self.text(f"[图片 {index}]", target)
                target.image_indexes.append(index)
                source = getattr(element, "src", None)
                if len(self.images) < MAX_EVENT_ATTACHMENTS and isinstance(source, str):
                    self.images.append((index, source))
                else:
                    self.media.append({"kind": "image", "label": f"图片 {index}", "capture_status": "omitted"})
                    self._incomplete(target)
            elif tag in {"audio", "file", "video"}:
                label = {"audio": "语音", "file": "文件", "video": "视频"}[tag]
                self.text(f"[{label}]", target)
                self.media.append({"kind": tag, "label": label, "capture_status": "unsupported"})
                self._incomplete(target)
            elif tag == "author":
                name = getattr(element, "name", None)
                identifier = getattr(element, "id", None)
                if not isinstance(name, str) or not name:
                    parts: list[str] = []
                    remaining = 161
                    for child in element.children:
                        self.nodes += 1
                        if self.nodes > _MAX_ELEMENTS:
                            self._incomplete(target)
                            break
                        if isinstance(child, Text):
                            part = child.text[:remaining]
                            parts.append(part)
                            remaining -= len(part)
                            if remaining <= 0:
                                break
                    name = "".join(parts)
                target.speaker = name if name and name != identifier else None
                target.role = (
                    "assistant"
                    if identifier == self.session.account.self_id
                    else "participant"
                    if identifier
                    else "unknown"
                )
            elif tag == "quote":
                reply = _Message()
                identifier = getattr(element, "id", None)
                if isinstance(identifier, str) and identifier and self.reply_sequence is not None:
                    reply.reply_to_sequence = self.reply_sequence(identifier)
                if element.children:
                    self.walk(element.children, depth + 1, quoted=True, message=reply)
                elif reply.reply_to_sequence is None:
                    self.text("[引用原消息未记录]", reply)
                    self._incomplete(reply)
                if target.reply_to is None:
                    target.reply_to = reply
                else:
                    self.text("[额外引用未记录]", target)
                    self._incomplete(target)
            elif tag == "message":
                forwarded = _Message()
                self.walk(element.children, depth + 1, quoted=quoted, message=forwarded)
                if not element.children:
                    self.text("[合并转发内容未记录]", forwarded)
                    self._incomplete(forwarded)
                if (
                    depth == 0
                    and getattr(element, "forward", False)
                    and not forwarded.parts
                    and forwarded.speaker is None
                    and forwarded.reply_to is None
                    and forwarded.forwarded_messages
                    and not forwarded.truncated
                ):
                    target.forwarded_messages.extend(forwarded.forwarded_messages)
                else:
                    target.forwarded_messages.append(forwarded)
            elif tag in {"p", "blockquote"}:
                self.text("\n", target)
                self.walk(element.children, depth + 1, quoted=quoted, message=target)
                self.text("\n", target)
            elif tag == "br":
                self.text("\n", target)
            elif tag == "a":
                if element.children:
                    self.walk(element.children, depth + 1, quoted=quoted, message=target)
                else:
                    href = getattr(element, "href", None)
                    self.text(href if isinstance(href, str) else "[链接]", target)
            elif tag in {"b", "i", "u", "s", "spl", "code", "sup", "sub", "button"}:
                self.walk(element.children, depth + 1, quoted=quoted, message=target)
            else:
                self.text("[未支持的消息元素]", target)
                self._incomplete(target)

    def _message_snapshot(self, message: _Message) -> dict[str, object]:
        text, redacted = _safe_text("".join(message.parts).strip())
        self.redacted = self.redacted or redacted
        speaker = None
        if message.speaker:
            limit = min(160, self.remaining)
            speaker, redacted = _safe_text(message.speaker[:limit])
            self.remaining -= min(len(message.speaker), limit)
            self.redacted = self.redacted or redacted
            if len(message.speaker) > limit:
                self._incomplete(message)
        result: dict[str, object] = {
            "speaker": speaker,
            "role": message.role,
            "content": text,
            "reply_to": self._message_snapshot(message.reply_to) if message.reply_to is not None else None,
            "forwarded_messages": [self._message_snapshot(child) for child in message.forwarded_messages],
            "image_indexes": message.image_indexes,
            "truncated": message.truncated,
        }
        if message.reply_to_sequence is not None:
            result["reply_to_sequence"] = message.reply_to_sequence
        return result

    def structure(self) -> dict[str, object]:
        if self._snapshot is None:
            self._snapshot = self._message_snapshot(self.message)
        return self._snapshot

    def content(self) -> str:
        return str(self.structure()["content"])
