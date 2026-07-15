"""Runtime extraction of inbound OneBot merged-forward messages."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from collections.abc import Callable, Sequence

from arclet.entari import Session, MessageChain
from satori.element import Custom

from .config import LLMChatConfig
from .vision import describe_image
from .core.forward import (
    ForwardNode,
    ForwardSource,
    ForwardedMessage,
    render_forward_node,
    parse_forward_payload,
    collect_nested_forward_ids,
    collect_forward_image_sources,
)

_FORWARD_TAG = "onebot:forward"
_MAX_FORWARD_BUNDLES = 8
_MAX_NESTED_DEPTH = 4


@dataclass(frozen=True, slots=True)
class ForwardReference:
    message_id: str
    source: ForwardSource
    depth: int = 0


ResolvedEntry = tuple[ForwardSource, ForwardNode] | ForwardedMessage


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
    """Collect direct forwards first, then forwards from a quoted message."""
    references = _select_references(session.elements, "direct")
    quote = session.quote
    if quote and quote.children:
        references.extend(_select_references(MessageChain(quote.children), "quoted"))
    return references


def has_direct_merged_forward(session: Session) -> bool:
    """Return whether the current outer message contains a merged forward."""
    return bool(_select_references(session.elements, "direct"))


def _normalized_limit(value: int, *, minimum: int, maximum: int) -> int:
    return min(maximum, max(minimum, value))


async def _fetch_forward_payload(session: Session, message_id: str, timeout: float) -> object:
    return await asyncio.wait_for(
        session.internal("get_forward_msg", message_id=message_id),
        timeout=min(30.0, max(1.0, timeout)),
    )


async def _describe_sources(
    config: LLMChatConfig,
    session: Session,
    sources: Sequence[str],
    warn: Callable[[str], None],
) -> dict[str, str]:
    async def describe(source: str) -> tuple[str, str]:
        try:
            return source, await describe_image(config, session, source)
        except Exception as exc:
            warn(f"forwarded image describe failed: {type(exc).__name__}")
            return source, ""

    pairs = await asyncio.gather(*(describe(source) for source in sources))
    return dict(pairs)


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
    """Fetch, normalize, and bound merged-forward context for one user turn."""
    initial = collect_merged_forward_references(session)
    if not initial:
        return []

    max_messages = _normalized_limit(config.merged_forward_max_messages, minimum=1, maximum=500)
    max_chars = _normalized_limit(config.merged_forward_max_chars_per_message, minimum=64, maximum=8000)
    pending = deque(initial)
    seen: set[str] = set()
    resolved: list[ResolvedEntry] = []
    bundles = 0
    omitted_nodes = 0

    while pending and bundles < _MAX_FORWARD_BUNDLES and len(resolved) < max_messages:
        reference = pending.popleft()
        if reference.message_id in seen:
            continue
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
            continue
        if not nodes:
            resolved.append(
                {
                    "speaker": "Merged forward",
                    "content": "[Forwarded content unavailable]",
                    "source": reference.source,
                }
            )
            continue

        remaining = max_messages - len(resolved)
        selected = nodes[:remaining]
        omitted_nodes += len(nodes) - len(selected)
        resolved.extend((reference.source, node) for node in selected)
        if reference.depth < _MAX_NESTED_DEPTH:
            for nested_id in collect_nested_forward_ids(selected):
                pending.append(ForwardReference(nested_id, reference.source, reference.depth + 1))

    normalized_nodes = [entry[1] for entry in resolved if isinstance(entry, tuple)]
    image_sources = collect_forward_image_sources(normalized_nodes)
    image_limit = _normalized_limit(config.merged_forward_max_described_images, minimum=0, maximum=32)
    descriptions = (
        await _describe_sources(config, session, image_sources[:image_limit], warn)
        if config.image_understanding_enabled and image_limit
        else {}
    )
    rendered: list[ForwardedMessage] = []
    for entry in resolved:
        if isinstance(entry, tuple):
            source, node = entry
            rendered.append(
                render_forward_node(
                    node,
                    source=source,
                    image_descriptions=descriptions,
                    max_chars=max_chars,
                )
            )
        else:
            rendered.append(entry)
    rendered, chars_truncated = _limit_total_chars(rendered, config.merged_forward_max_total_chars)
    if pending or omitted_nodes or chars_truncated:
        warn("merged forward truncated by configured limits")
        rendered.append(
            {
                "speaker": "Merged forward",
                "content": "[Additional forwarded content omitted by configured limits]",
                "source": initial[0].source,
            }
        )
    return rendered
