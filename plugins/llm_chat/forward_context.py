"""Runtime extraction of inbound OneBot merged-forward messages."""

from __future__ import annotations

import asyncio
from secrets import token_hex
from dataclasses import dataclass
from collections.abc import Callable, Sequence

from arclet.entari import Session, MessageChain
from satori.element import Custom

from .config import LLMChatConfig
from .core.forward import (
    MAX_FORWARD_IMAGES,
    ForwardNode,
    ForwardSource,
    ForwardedMessage,
    render_forward_node,
    parse_forward_payload,
    collect_nested_forward_ids,
)
from .image_inputs import ImageInputError, current_image_inputs
from .core.image_source import fetch_image_bytes

_FORWARD_TAG = "onebot:forward"
_MAX_FORWARD_BUNDLES = 8
_MAX_NESTED_DEPTH = 4


@dataclass(frozen=True, slots=True)
class ForwardReference:
    message_id: str
    source: ForwardSource
    depth: int = 0
    parent_node_ref: str = ""


@dataclass(frozen=True, slots=True)
class _ResolvedNode:
    source: ForwardSource
    node: ForwardNode
    node_ref: str
    speaker_ref: str
    node_index: int
    depth: int
    parent_node_ref: str


ResolvedEntry = _ResolvedNode | ForwardedMessage


def _reference(element: Custom, source: ForwardSource) -> ForwardReference | None:
    if element.tag != _FORWARD_TAG:
        return None
    try:
        message_id = str(element["id"]).strip()
    except KeyError:
        return None
    return ForwardReference(message_id, source) if message_id else None


def _select_references(elements: MessageChain, source: ForwardSource) -> list[ForwardReference]:
    return [reference for element in elements.select(Custom) if (reference := _reference(element, source))]


def collect_merged_forward_references(session: Session) -> list[ForwardReference]:
    """Collect merged forwards only from the quoted message."""
    quote = session.quote
    if not quote or not quote.children:
        return []
    return _select_references(MessageChain(quote.children), "quoted")


def _normalized_limit(value: int, *, minimum: int, maximum: int) -> int:
    return min(maximum, max(minimum, value))


async def _fetch_forward_payload(session: Session, message_id: str, timeout: float) -> object:
    return await asyncio.wait_for(
        session.internal("get_forward_msg", message_id=message_id),
        timeout=min(30.0, max(1.0, timeout)),
    )


def _limit_total_chars(
    messages: Sequence[ForwardedMessage],
    max_total_chars: int,
) -> tuple[list[ForwardedMessage], bool]:
    remaining = _normalized_limit(max_total_chars, minimum=128, maximum=128_000)
    bounded: list[ForwardedMessage] = []
    truncated = False
    for message in messages:
        if remaining <= 0:
            truncated = True
            break
        content = message["content"]
        if len(content) > remaining:
            content = f"{content[: max(1, remaining - 1)]}…"
            truncated = True
        bounded.append({**message, "content": content})
        remaining -= len(content)
    return bounded, truncated or len(bounded) < len(messages)


async def resolve_merged_forward_messages(
    config: LLMChatConfig,
    session: Session,
    warn: Callable[[str], None],
) -> list[ForwardedMessage]:
    """Fetch, recursively normalize, and bound merged-forward context for one user turn."""

    initial = collect_merged_forward_references(session)
    if not initial:
        return []

    max_messages = _normalized_limit(config.merged_forward_max_messages, minimum=1, maximum=500)
    max_chars = _normalized_limit(config.merged_forward_max_chars_per_message, minimum=64, maximum=8000)
    seen: set[str] = set()
    resolved: list[ResolvedEntry] = []
    bundles = 0
    omitted_nodes = 0
    omitted_nested = 0
    speakers: dict[str, str] = {}

    async def resolve_reference(reference: ForwardReference) -> None:
        nonlocal bundles, omitted_nodes, omitted_nested

        if reference.message_id in seen:
            omitted_nested += 1
            return
        if bundles >= _MAX_FORWARD_BUNDLES or len(resolved) >= max_messages:
            omitted_nested += 1
            return
        seen.add(reference.message_id)
        bundles += 1
        try:
            payload = await _fetch_forward_payload(
                session,
                reference.message_id,
                config.merged_forward_fetch_timeout,
            )
            nodes = parse_forward_payload(payload)
        except Exception as exc:
            warn(f"merged forward fetch failed: {type(exc).__name__}")
            resolved.append(
                {
                    "speaker": "Merged forward",
                    "content": "[Forwarded content unavailable]",
                    "source": reference.source,
                }
            )
            return
        if not nodes:
            resolved.append(
                {
                    "speaker": "Merged forward",
                    "content": "[Forwarded content unavailable]",
                    "source": reference.source,
                }
            )
            return

        for index, node in enumerate(nodes):
            if len(resolved) >= max_messages:
                omitted_nodes += len(nodes) - index
                return
            node_ref = f"forward_node_{token_hex(8)}"
            speaker_ref = speakers.setdefault(node.speaker_id or node_ref, f"forward_speaker_{token_hex(8)}")
            resolved.append(
                _ResolvedNode(
                    reference.source,
                    node,
                    node_ref,
                    speaker_ref,
                    index + 1,
                    reference.depth,
                    reference.parent_node_ref,
                )
            )
            nested_ids = collect_nested_forward_ids((node,))
            omitted_nested += sum(part.kind == "forward" and not part.source for part in node.parts)
            if not nested_ids:
                continue
            if reference.depth >= _MAX_NESTED_DEPTH:
                omitted_nested += len(nested_ids)
                continue
            for nested_index, nested_id in enumerate(nested_ids):
                if len(resolved) >= max_messages:
                    omitted_nested += len(nested_ids) - nested_index
                    break
                await resolve_reference(
                    ForwardReference(
                        nested_id,
                        reference.source,
                        reference.depth + 1,
                        node_ref,
                    )
                )

    for index, reference in enumerate(initial):
        if len(resolved) >= max_messages:
            omitted_nested += len(initial) - index
            break
        await resolve_reference(reference)

    image_limit = _normalized_limit(config.merged_forward_max_images, minimum=0, maximum=MAX_FORWARD_IMAGES)
    inputs = current_image_inputs()
    registered_images = 0
    omitted_images = 0
    rendered: list[ForwardedMessage] = []
    for entry in resolved:
        if not isinstance(entry, _ResolvedNode):
            rendered.append(entry)
            continue
        message = render_forward_node(
            entry.node,
            source=entry.source,
            image_descriptions={},
            max_chars=max_chars,
        )
        message.update(
            node_ref=entry.node_ref,
            speaker_ref=entry.speaker_ref,
            node_index=entry.node_index,
            depth=entry.depth,
        )
        if entry.parent_node_ref:
            message["parent_node_ref"] = entry.parent_node_ref
        images: list[dict[str, object]] = []
        for image_index, part in enumerate((part for part in entry.node.parts if part.kind == "image"), start=1):
            view: dict[str, object] = {"index": image_index, "source": "forward", "status": "unavailable"}
            if part.source and inputs is not None and registered_images < image_limit:

                async def load(source: str = part.source) -> bytes:
                    data = await fetch_image_bytes(session, source)
                    if data is None:
                        raise ImageInputError("Forwarded image could not be acquired")
                    return data

                try:
                    ref = inputs.register(
                        session,
                        source="forward",
                        key=("download", part.source),
                        load=load,
                        index=image_index,
                    )
                    view = inputs.view(ref)
                    registered_images += 1
                except ImageInputError:
                    omitted_images += 1
            elif part.source:
                view["status"] = "omitted"
                omitted_images += 1
            images.append(view)
        if images:
            message["images"] = images
        rendered.append(message)
    rendered, chars_truncated = _limit_total_chars(rendered, config.merged_forward_max_total_chars)
    if omitted_nested:
        warn("merged forward nested content unavailable or exceeded recursion limits")
    if omitted_nodes or chars_truncated:
        warn("merged forward truncated by configured limits")
    if omitted_nested or omitted_nodes or chars_truncated or omitted_images:
        rendered.append(
            {
                "speaker": "Merged forward",
                "content": "[Additional forwarded content omitted by configured limits]",
                "source": initial[0].source,
            }
        )
    return rendered
