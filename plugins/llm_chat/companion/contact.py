"""Exact-target fresh native companion generation and receipt-based settlement."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
import asyncio
from datetime import datetime, timezone
from collections.abc import Mapping, Callable, Sequence, Awaitable

from satori import User, Event, Guild, Channel, ChannelType
from arclet.entari import Session
from arclet.entari.const import ITEM_SESSION
from arclet.entari.logger import log
from arclet.entari.event.base import InternalEvent
from arclet.letoderea.context import Contexts
from entari_plugin_llm.config import get_model_config
from entari_plugin_llm.exception import ModelNotFoundError

from utils.companion_core import ContactProposal
from utils.llm_model_core.snapshot import pin_main_model, main_model_scope

from . import store
from .types import ContactClaim
from ..config import LLMChatConfig
from ..models import ChatScope
from .delivery import CompanionLeaseLost, companion_transport_scope
from .internal import settle_owned
from ..identity import ChatIdentity
from ..image_inputs import ImageInputs, image_inputs_scope
from ..delivery_audit import delivery_audit_scope
from ..group_delivery import group_delivery_scope
from ..agent_turn_setup import PreparedAgentTurn, prepare_autonomous_agent_turn

_LOGGER = log.wrapper("[llm_chat.companion]")
Runner = Callable[..., Awaitable[str]]
Guard = Callable[[], Awaitable[bool]]


def build_contact_session(account: Any, scope: ChatScope, claim: ContactClaim, channel: Channel) -> Session:
    """Bind a host-origin Session to the existing scope and immutable member target."""
    if channel.id != scope.channel_id or not claim.platform_user_id or claim.scope_id != scope.id:
        raise ValueError("Companion contact does not match its existing scope and target")
    if (str(account.platform), str(account.self_id)) != (scope.platform, scope.account_id):
        raise ValueError("Companion contact account does not own its existing scope")
    if channel.type not in {ChannelType.TEXT, ChannelType.DIRECT}:
        raise ValueError("Companion contact requires a native text or direct channel")
    event = InternalEvent(
        account,
        Event(
            type="internal",
            timestamp=datetime.now(timezone.utc),
            login=account.self_info,
            channel=channel,
            guild=Guild(id=scope.guild_id) if scope.guild_id and channel.type != ChannelType.DIRECT else None,
            user=User(id=claim.platform_user_id, name=claim.display_name or None),
            message=None,
        ),
    )
    return Session(account, event)


def _contact_context(proposal: ContactProposal, state: Mapping[str, object], *, now: datetime) -> dict[str, object]:
    attention = state.get("attention")
    own_attention = []
    if isinstance(attention, (tuple, list)):
        for item in attention:
            if not isinstance(item, Mapping):
                raise ValueError("Companion contact attention must be structured scoped state")
            own_attention.append(
                {key: item[key] for key in ("key", "kind", "summary", "status", "evidence_turn_ids") if key in item}
            )
    return {
        "purpose": proposal.purpose,
        "topic": proposal.topic,
        "reason": proposal.reason,
        "evidence_turn_ids": list(proposal.evidence_turn_ids),
        "now": now.isoformat(),
        "life": state["life"],
        "attention": own_attention,
    }


async def run_contact(
    *,
    config: LLMChatConfig,
    scope: ChatScope,
    account: Any,
    claim: ContactClaim,
    proposal: ContactProposal,
    guard: Guard,
    clock: Callable[[], datetime],
    tool_schemas: Sequence[Mapping[str, object]] = (),
    runner: Runner | None = None,
    lease_seconds: int = 300,
) -> str:
    """Own preparation, generation, renewable capability, audit and final settlement."""
    owner = asyncio.current_task()
    if owner is None:
        raise RuntimeError("Companion contact requires an owned async worker")
    prepared: PreparedAgentTurn | None = None
    inputs = ImageInputs(warn=_LOGGER.warning)
    audit = None
    status = "failed"
    reason = "preparation_failed"
    terminal_status = "failed"
    generation_started = False

    async def renew() -> None:
        while True:
            await asyncio.sleep(min(5.0, max(1.0, lease_seconds / 3)))
            try:
                current = await guard() and await store.renew_contact(
                    claim,
                    now=clock(),
                    lease_seconds=lease_seconds,
                )
            except Exception as exc:
                _LOGGER.warning(f"companion contact lease renewal failed: {type(exc).__name__}")
                current = False
            if not current:
                owner.cancel()
                return

    renewer = asyncio.create_task(renew(), name=f"companion-renew-{claim.attempt_ref}")
    try:
        if not await guard():
            raise CompanionLeaseLost("Companion contact was revoked before preparation")
        channel = await account.protocol.channel_get(scope.channel_id)
        if not isinstance(channel, Channel):
            raise ValueError("Companion native channel lookup returned no channel")
        if not await guard():
            raise CompanionLeaseLost("Companion contact changed during native channel lookup")
        session = build_contact_session(account, scope, claim, channel)
        ctx = Contexts({ITEM_SESSION: session, "$event": session.event})
        await session.event.gather(ctx)
        turn_config = deepcopy(config)
        state = await store.read_companion_context(scope.id, claim.persona_key, claim.user_id)
        if state is None:
            raise CompanionLeaseLost("Companion contact state is no longer enabled")
        facts = _contact_context(proposal, state, now=clock())
        with (
            group_delivery_scope(session, scheduled_reminder=True),
            delivery_audit_scope(session, _LOGGER.warning, scheduled_reminder=True) as audit,
            main_model_scope(),
            image_inputs_scope(inputs),
            companion_transport_scope(claim, guard, clock=clock),
        ):
            try:
                try:
                    model = get_model_config(turn_config.model, scope.channel_id)
                except ModelNotFoundError:
                    model = get_model_config(turn_config.model)
                model_name = pin_main_model(model).name
                if not await guard():
                    raise CompanionLeaseLost("Companion contact was revoked before native turn")
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
                    or prepared.persona.key != claim.persona_key
                    or prepared.conversation_mode != "companion"
                    or prepared.companion_revision != claim.mode_revision
                ):
                    raise CompanionLeaseLost("Prepared native turn does not match the reserved companion target")
                if not await store.bind_contact_turn(claim, prepared.agent_access.turn_id, now=clock()):
                    raise CompanionLeaseLost("Companion contact lease changed before native generation")
                if not await guard():
                    raise CompanionLeaseLost("Companion contact was revoked before native generation")
                if runner is None:
                    from ..chat_handler import run_prepared_agent_turn

                    active_runner = run_prepared_agent_turn
                else:
                    active_runner = runner
                generation_started = True
                result = await active_runner(
                    session,
                    ctx,
                    prepared,
                    model_name=model_name,
                    turn_config=turn_config,
                    reaction=None,
                )
                terminal_status = result if result in {"completed", "silent", "declined", "partial"} else "failed"
                if result == "completed":
                    status = "delivered" if prepared.delivery_state.confirmed_deliveries else "failed"
                elif result in {"silent", "declined"}:
                    status = result
                else:
                    status = "failed"
                reason = "" if status in {"delivered", "silent", "declined"} else "native_turn_failed"
            finally:
                # Keep transport and audit contexts alive while confirmed prefix work drains.
                if audit is not None:
                    await settle_owned(audit.drain())
    except asyncio.CancelledError:
        status, reason = "cancelled", "contact_interrupted"
        terminal_status = (
            "partial" if prepared is not None and prepared.delivery_state.confirmed_deliveries else "cancelled"
        )
        raise
    except CompanionLeaseLost:
        status, reason = "cancelled", "contact_capability_revoked"
        terminal_status = (
            "partial" if prepared is not None and prepared.delivery_state.confirmed_deliveries else "cancelled"
        )
    except Exception as exc:
        reason = type(exc).__name__
        _LOGGER.warning(f"companion contact failed: {reason}")
    finally:
        renewer.cancel()
        await settle_owned(asyncio.gather(renewer, return_exceptions=True))

        async def finish() -> None:
            nonlocal status
            try:
                await store.finish_contact(claim, status=status, now=clock(), reason=reason)
                history = await store.read_contact_history(
                    claim.scope_id,
                    claim.persona_key,
                    member_ref=claim.member_ref,
                    limit=20,
                )
                outcome = next((item for item in history if item["attempt_ref"] == claim.attempt_ref), None)
                if outcome is None:
                    raise RuntimeError("Companion contact settlement disappeared from durable history")
                status = str(outcome["status"])
                if prepared is not None:
                    if terminal_status not in {"completed", "silent", "declined"}:
                        await prepared.lifecycle.preserve_and_rollback()
                    # Native finalization is idempotent; injected runners must not leave running turns.
                    prepared.agent_events.append(
                        "companion_contact",
                        model_visible=False,
                        status=status,
                        payload={
                            "attempt_ref": claim.attempt_ref,
                            "reason": reason,
                            "native_generation": generation_started,
                        },
                    )
                    await prepared.agent_events.flush()
                    await prepared.lifecycle.finalize_agent_turn(terminal_status)
            finally:
                await inputs.aclose()

        await settle_owned(finish())
    return status
