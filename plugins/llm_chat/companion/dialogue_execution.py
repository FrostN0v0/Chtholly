"""Execute companion batches through the existing native chat and tool lifecycle."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from collections.abc import Mapping, Callable, Sequence

from arclet.entari.const import ITEM_SESSION
from arclet.letoderea.context import Contexts

from ..config import LLMChatConfig
from .internal import settle_owned
from ..identity import resolve_chat_identity
from ..steering import steering_scope
from .participants import CompanionParticipantRegistry, companion_participants_scope
from .dialogue_control import dialogue_control_scope
from .dialogue_manager import DialogueBatch, DialogueOutcome
from .dialogue_steering import DialogueSteeringState
from .dialogue_transport import dialogue_transport_scope


async def run_dialogue_batch(
    config: LLMChatConfig,
    batch: DialogueBatch,
    *,
    tool_schemas: Sequence[Mapping[str, object]],
    clock: Callable[[], datetime],
    runner=None,
) -> DialogueOutcome:
    if not await batch.valid():
        return DialogueOutcome(status="cancelled")
    captured = deepcopy(config)
    if not batch.inputs:
        from .host_dialogue import run_host_dialogue

        return await run_host_dialogue(captured, batch, tool_schemas=tool_schemas, clock=clock, runner=runner)

    root = next((item for item in batch.inputs if item.directed), batch.inputs[0])
    state = DialogueSteeringState(root, batch.inputs, defer_input=batch.defer_input)
    for item in batch.inputs:
        if item is not root and not state.admit(item):
            raise RuntimeError("Initial companion input was not admitted")
    batch.on_invalidated = state.invalidate_source
    # Bind before identity/profile IO: newly arriving inputs immediately fence
    # any stale unsent candidate through the existing steering boundary.
    batch.bind_admitter(state.admit)
    try:
        identity = await resolve_chat_identity(root.session)
        registry = CompanionParticipantRegistry(captured, root.session, identity)
        with (
            steering_scope(state),
            companion_participants_scope(registry),
            dialogue_control_scope() as control,
            dialogue_transport_scope(batch.valid),
        ):
            # Supplements carry their own attributed profile context through
            # normalization, rather than bloating the root system before the
            # per-input budget can accept or defer their real source.
            await registry.register_input(
                root.session,
                identity,
                own_text=root.session.elements.extract_plain_text().strip(),
                directed=root.directed,
                received_at=root.received_at,
            )
            if not await batch.valid():
                return DialogueOutcome(status="cancelled")
            ctx = Contexts(root.ctx.copy()) if root.ctx is not None else Contexts()
            ctx[ITEM_SESSION] = root.session
            ctx["$event"] = root.session.event
            if root.ctx is None:
                await root.session.event.gather(ctx)
            from ..chat_handler import handle_chat_turn

            await handle_chat_turn(root.session, ctx, quiet=not root.directed, turn_config=captured)
            prepared = state.prepared
            confirmed = prepared.delivery_state.confirmed_deliveries if prepared is not None else 0
            if prepared is not None and prepared.delivery_state.delivery_attempts > confirmed:
                return DialogueOutcome(status="unknown", confirmed_deliveries=confirmed)
            if state.status in {"completed", "silent", "declined"}:
                return DialogueOutcome(control.decision, confirmed_deliveries=confirmed)
            return DialogueOutcome(
                status="unknown" if state.status == "partial" else "failed",
                confirmed_deliveries=confirmed,
            )
    finally:
        state.defer_unprepared_inputs()
        await settle_owned(state.aclose())
