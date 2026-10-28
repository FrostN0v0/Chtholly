"""One native host wake inside the shared companion dialogue owner, never a sender loop."""

from __future__ import annotations

from copy import deepcopy
import asyncio
from datetime import datetime
from collections.abc import Mapping, Callable, Sequence

from arclet.entari.const import ITEM_SESSION
from arclet.entari.logger import log
from arclet.letoderea.context import Contexts
from entari_plugin_llm.config import get_model_config
from entari_plugin_llm.exception import ModelNotFoundError

from utils.companion_core.dialogue import DialogueDecision
from utils.llm_model_core.snapshot import pin_main_model, main_model_scope

from . import store
from ..config import LLMChatConfig
from .contact import Runner, _contact_context
from .context import companion_limits
from .delivery import CompanionLeaseLost, companion_transport_scope
from .internal import settle_owned
from ..identity import ChatIdentity
from ..steering import steering_scope
from .claim_store import claim_contact, claim_wait_contact
from .scope_kinds import bind_scope_channel
from .participants import CompanionParticipantRegistry, companion_participants_scope
from ..image_inputs import ImageInputs, image_inputs_scope
from ..steer_inputs import bind_steer_inputs
from .opportunities import validate_opportunity
from ..delivery_audit import delivery_audit_scope
from ..group_delivery import group_delivery_scope
from .dialogue_control import dialogue_control_scope
from .dialogue_manager import DialogueBatch, DialogueInput, DialogueOutcome
from .dialogue_session import host_dialogue_session
from ..agent_turn_setup import prepare_autonomous_agent_turn
from .dialogue_steering import DialogueSteeringState

_LOGGER = log.wrapper("[llm_chat.companion.dialogue]")
_END = DialogueDecision(action="end")


async def run_host_dialogue(
    config: LLMChatConfig,
    batch: DialogueBatch,
    *,
    tool_schemas: Sequence[Mapping[str, object]],
    clock: Callable[[], datetime],
    runner: Runner | None = None,
) -> DialogueOutcome:
    """Consume one committed opportunity or explicit wait with native capabilities and receipts."""
    if batch.inputs or batch.trigger not in {"wait", "opportunity"}:
        raise ValueError("Host dialogue requires an input-free opportunity or explicit wait")
    if not await batch.valid():
        return DialogueOutcome(decision=_END, status="cancelled")
    await bind_scope_channel(batch.scope, batch.session.channel)
    limits = companion_limits(config)
    if batch.trigger == "opportunity":
        origin, proposal = validate_opportunity(batch.opportunity, scope_id=batch.scope.id)
        if (origin.persona_key, origin.mode_revision, origin.context_revision) != (
            batch.mode.persona_key,
            batch.mode.revision,
            batch.mode.context_revision,
        ):
            return DialogueOutcome(decision=_END, status="cancelled")
        claim = await claim_contact(
            batch.scope.id,
            batch.mode.persona_key,
            proposal.member_ref,
            proposal,
            now=clock(),
            origin=origin,
            limits=limits,
        )
    else:
        reserved = await claim_wait_contact(batch.mode, now=clock(), limits=limits)
        claim, proposal = reserved if reserved is not None else (None, None)
    if claim is None:
        # A blocked unsolicited wake is consumed, never turned into a polling alarm.
        return DialogueOutcome(decision=_END)

    prepared = None
    audit = None
    inputs = ImageInputs(warn=_LOGGER.warning)
    try:
        session = host_dialogue_session(batch.account, batch.scope, batch.session.channel)
        ctx = Contexts({ITEM_SESSION: session, "$event": session.event})
        root = DialogueInput(session, ctx, False, clock())
        steering = DialogueSteeringState(root, (), defer_input=batch.defer_input)
        registry = CompanionParticipantRegistry(config, session, None)
        batch.on_invalidated = steering.invalidate_source
        batch.bind_admitter(steering.admit)
    except BaseException:
        await settle_owned(store.finish_contact(claim, status="cancelled", now=clock(), reason="host_binding_failed"))
        await inputs.aclose()
        raise
    decision = _END
    native_status = "failed"
    settlement = "failed"
    reason = "host_preparation_failed"
    count = 0
    unknown = False
    owner = asyncio.current_task()
    if owner is None:
        raise RuntimeError("Host dialogue requires an owned worker")

    async def guard() -> bool:
        return await batch.valid() and await store.contact_valid(claim, now=clock())

    async def renew() -> None:
        while True:
            await asyncio.sleep(5)
            try:
                current = await batch.valid() and await store.renew_contact(claim, now=clock())
            except Exception as exc:
                _LOGGER.warning(f"host dialogue renewal failed: {type(exc).__name__}")
                current = False
            if not current:
                owner.cancel()
                return

    renewer = asyncio.create_task(renew(), name=f"companion-host-renew-{claim.attempt_ref}")
    try:
        with (
            companion_participants_scope(registry),
            dialogue_control_scope() as control,
            steering_scope(steering),
            group_delivery_scope(session, scheduled_reminder=True),
            delivery_audit_scope(session, _LOGGER.warning, scheduled_reminder=True) as audit,
            main_model_scope(),
            image_inputs_scope(inputs),
            companion_transport_scope(claim, guard, clock=clock),
        ):
            try:
                await session.event.gather(ctx)
                if not await guard():
                    raise CompanionLeaseLost("Host wake changed before native preparation")
                turn_config = deepcopy(config)
                try:
                    model = get_model_config(turn_config.model, batch.scope.channel_id)
                except ModelNotFoundError:
                    model = get_model_config(turn_config.model)
                model_name = pin_main_model(model).name
                state = await store.read_companion_context(batch.scope.id, claim.persona_key, claim.user_id)
                if state is None:
                    raise CompanionLeaseLost("Host companion state is no longer enabled")
                facts = _contact_context(proposal, state, now=clock())
                facts.update(
                    {
                        "trigger": batch.trigger,
                        "target": {"kind": claim.target, "display_name": claim.display_name},
                        "host_origin": True,
                        "new_human_input": False,
                    }
                )
                prepared = await prepare_autonomous_agent_turn(
                    turn_config,
                    session,
                    ChatIdentity(claim.user_id, claim.display_name, ""),
                    model_name=model_name,
                    content=proposal.topic,
                    contact_context=facts,
                    image_inputs=inputs,
                    warn=_LOGGER.warning,
                    tool_schemas=tool_schemas,
                )
                audit.bind(prepared.agent_events)
                if (
                    prepared.agent_access.scope_id != claim.scope_id
                    or prepared.agent_access.user_id != claim.user_id
                    or prepared.agent_access.trigger_kind != "autonomous"
                    or prepared.agent_access.raw_user_text
                    or prepared.persona.key != claim.persona_key
                    or prepared.conversation_mode != "companion"
                    or prepared.companion_revision != claim.mode_revision
                ):
                    raise CompanionLeaseLost("Prepared host turn differs from its durable destination")
                if not await store.bind_contact_turn(claim, prepared.agent_access.turn_id, now=clock()):
                    raise CompanionLeaseLost("Host turn lease changed before native generation")
                bind_steer_inputs(steering, prepared, turn_config, session, model_name=model_name, warn=_LOGGER.warning)
                await steering.prepare_first_request(prepared)
                if not await guard():
                    raise CompanionLeaseLost("Host wake changed before native generation")
                if runner is None:
                    from ..chat_handler import run_prepared_agent_turn

                    active_runner = run_prepared_agent_turn
                else:
                    active_runner = runner
                result = await active_runner(
                    session,
                    ctx,
                    prepared,
                    model_name=model_name,
                    turn_config=turn_config,
                    reaction=None,
                )
                native_status = result if result in {"completed", "silent", "declined", "partial"} else "failed"
                decision = control.decision
                settlement = (
                    "delivered"
                    if prepared.delivery_state.confirmed_deliveries
                    else (result if result in {"silent", "declined"} else "failed")
                )
                reason = "" if native_status in {"completed", "silent", "declined"} else "native_host_failed"
            finally:
                # A confirmed prefix and pending receipt writes outlive cancellation;
                # keep the real capability/audit contexts bound while they drain.
                control.close()
                steering.defer_unprepared_inputs()
                await settle_owned(steering.aclose())
                if audit is not None:
                    await settle_owned(audit.drain())
    except asyncio.CancelledError:
        native_status, settlement, reason = "cancelled", "cancelled", "host_interrupted"
        raise
    except CompanionLeaseLost:
        native_status, settlement, reason = "cancelled", "cancelled", "host_capability_revoked"
    except Exception as exc:
        reason = type(exc).__name__
        _LOGGER.warning(f"host dialogue failed: {reason}")
    finally:
        renewer.cancel()
        await settle_owned(asyncio.gather(renewer, return_exceptions=True))

        async def finish() -> None:
            nonlocal count, unknown
            try:
                await store.finish_contact(claim, status=settlement, now=clock(), reason=reason)
                history = await store.read_contact_history(
                    claim.scope_id,
                    claim.persona_key,
                    member_ref=claim.member_ref,
                    limit=100,
                )
                record = next((item for item in history if item["attempt_ref"] == claim.attempt_ref), None)
                if record is None:
                    raise RuntimeError("Host contact settlement disappeared from durable history")
                count = len(record["receipts"])
                unknown = record["status"] == "unknown"
                if prepared is not None:
                    final_status = (
                        "partial"
                        if count and native_status not in {"completed", "silent", "declined"}
                        else native_status
                    )
                    if final_status not in {"completed", "silent", "declined"}:
                        await prepared.lifecycle.preserve_and_rollback()
                    prepared.agent_events.append(
                        "companion_contact",
                        model_visible=False,
                        status=record["status"],
                        payload={
                            "attempt_ref": claim.attempt_ref,
                            "trigger": batch.trigger,
                            "target": claim.target,
                            "reason": reason,
                            "confirmed_deliveries": count,
                        },
                    )
                    if not await prepared.agent_events.flush():
                        raise RuntimeError("Host dialogue terminal audit could not be persisted")
                    await prepared.lifecycle.finalize_agent_turn(final_status)
            finally:
                await inputs.aclose()

        await settle_owned(finish())
    status = (
        "unknown"
        if unknown
        else (
            "completed"
            if native_status in {"completed", "silent", "declined"}
            else "cancelled"
            if native_status == "cancelled"
            else "failed"
        )
    )
    return DialogueOutcome(
        decision=decision if status == "completed" else _END, status=status, confirmed_deliveries=count
    )
