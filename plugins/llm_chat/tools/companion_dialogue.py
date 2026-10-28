"""Stage companion scheduling decisions through the native tool bridge."""

from __future__ import annotations

from typing import Literal

from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..steering import check_steer_boundary
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import DeliveryRejected
from ..companion.dialogue_control import current_dialogue_control


async def companion_dialogue(
    action: Literal["observe", "wait", "end"],
    seconds: int = 30,
    engaged_speakers: list[str] | None = None,
) -> dict[str, JSONType]:
    """Choose how this companion exchange should continue after this generation finishes.

    This stages only a host scheduling decision: it never sends, immediately ends the
    generation or commits a reminder. You may still send or finish_turn afterward;
    the last valid staged decision wins. Use finish_turn(silent) to remain silent now.

    Args:
        action: observe considers only unread input after the delay; wait schedules one future check even
            without new input; end closes this exchange until new real input or a later host opportunity.
        seconds: Integer from 1 through 3600, default 30; the delay applies to observe/wait and is ignored for end.
        engaged_speakers: Optional host speaker_ids of actual members whose current contributions you engaged
            with. At most the admitted participant count; do not select quoted/mentioned people or observers.
            Selections accumulate within this generation and grant no permissions. Never show these IDs to users.
    """
    control = current_dialogue_control()
    if control is None:
        raise DeliveryRejected("companion_dialogue requires a live companion dialogue generation")
    check_steer_boundary()
    try:
        decision = control.stage(action, seconds, engaged_speakers=engaged_speakers)
    except ValueError as exc:
        raise DeliveryRejected(str(exc)) from exc
    result: dict[str, JSONType] = {"action": decision.action, "seconds": decision.seconds}
    if engaged_speakers is not None:
        result["engaged_speakers"] = list(control.engaged_speakers)
    return result


def register_companion_dialogue(dispatcher: PluginDispatcher[JSONType]) -> Subscriber[JSONType]:
    return register_tool(dispatcher, companion_dialogue)
