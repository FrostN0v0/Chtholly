"""Native quote decoration without splitting OneBot special media into empty sends."""

from __future__ import annotations

from collections.abc import Sequence

from satori import Quote, Element
from arclet.entari import MessageChain


def supports_reply(payload: str | MessageChain, platform: str) -> bool:
    if platform != "onebot" or isinstance(payload, str):
        return True

    def composable(elements: Sequence[Element]) -> bool:
        return all(
            element.tag not in {"audio", "video", "file", "message"} and composable(element.children)
            for element in elements
        )

    return composable(payload)


def quote_request(payload: str | MessageChain, message_id: str) -> MessageChain:
    content = MessageChain(payload) if isinstance(payload, str) else payload
    return MessageChain([Quote(message_id), *(element for element in content if not isinstance(element, Quote))])
