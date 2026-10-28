"""Audited, state-explicit internal companion reflections."""

from __future__ import annotations

import json
from typing import TypeVar
import asyncio
from datetime import datetime
from collections.abc import Mapping, Sequence, Awaitable

from arclet.entari.logger import log

from utils.companion_core import (
    CompanionLimits,
    ReflectionProposal,
    build_reflection_prompt,
    build_reflection_system,
)
from utils.llm_model_core.snapshot import pin_main_model, main_model_scope

from . import provider
from .types import ReviewClaim, CompanionConflict, ReflectionSnapshot
from ..config import LLMChatConfig
from ..models import ChatScope
from .context import mode_baseline
from .persistence import session
from ..personality import persona_scope_lock, resolve_scope_persona, remember_session_persona
from .observations import reflection_input
from ..agent_events import persist_agent_events
from ..context_builder import build_baseline_fingerprint
from ..session_manager import start_turn, finish_turn, rollover_session, ensure_active_session
from ..core.agent_trace import AgentTurnRecorder
from .scope_kind_models import CompanionScopeKind
from ..core.context_snapshot import build_context_snapshot

_LOGGER = log.wrapper("[llm_chat.companion]")
_T = TypeVar("_T")


async def settle_owned(operation: Awaitable[_T]) -> _T:
    """Drain a finite persistence/cleanup operation even after repeated cancellation."""
    task = asyncio.ensure_future(operation)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


def _bounded_prompt(
    system: str,
    payload: dict[str, object],
    *,
    now: datetime,
    limit: int,
) -> str:
    """Trim whole optional observations only; required state is never truncated."""
    while True:
        prompt = build_reflection_prompt(payload, now=now)
        if len(system) + len(prompt) <= limit:
            return prompt
        contacts = payload.get("recent_contacts")
        if isinstance(contacts, list) and contacts:
            contacts.pop()
            continue
        members = payload.get("members")
        trimmed = False
        if isinstance(members, list):
            for member in reversed(members):
                if not isinstance(member, dict):
                    continue
                episodes = member.get("episodes")
                if isinstance(episodes, list) and episodes:
                    episodes.pop(0)
                    trimmed = True
                    break
        if not trimmed:
            raise ValueError("Complete companion state, persona and schema exceed the reflection input budget")


async def run_reflection(
    *,
    config: LLMChatConfig,
    scope: ChatScope,
    claim: ReviewClaim,
    snapshot: ReflectionSnapshot,
    limits: CompanionLimits,
    now: datetime,
    tool_schemas: Sequence[Mapping[str, object]] = (),
    is_private: bool | None = None,
) -> ReflectionProposal:
    """Make exactly one real provider request in a private, native internal turn."""
    turn_id: int | None = None
    status = "failed"
    audit_failures: list[str] = []

    def warn(message: str) -> None:
        audit_failures.append(message)
        _LOGGER.warning(message)

    recorder = AgentTurnRecorder(warn=warn)
    system = ""
    prompt = ""
    model_name = config.companion_model or config.model or "default"
    with main_model_scope():
        model_error: Exception | None = None
        try:
            model = pin_main_model(provider.reflection_model(config, scope.channel_id))
            model_name = model.name
        except Exception as exc:
            model_error = exc
        async with persona_scope_lock(scope.id):
            persona = await resolve_scope_persona(config, scope.id)
            if persona.key != claim.persona_key:
                raise CompanionConflict("Reflection persona is no longer selected")
            baseline = mode_baseline(
                build_baseline_fingerprint(
                    model_name=model_name,
                    persona=persona.baseline_text,
                    tool_schemas=(),
                ),
                "companion",
            )
            context_session, reason = await ensure_active_session(
                scope,
                baseline,
                idle_minutes=config.session_idle_minutes,
                max_turns=config.session_max_turns,
                now=now.replace(tzinfo=None),
                purpose="companion_reflection",
            )
            if reason is not None:
                context_session = await rollover_session(
                    scope,
                    context_session,
                    baseline,
                    reason=reason,
                    handoff_json="{}",
                    carry_handoff=False,
                    purpose="companion_reflection",
                )
            await remember_session_persona(context_session.id, persona)
            native_turn = await start_turn(
                context_session,
                trigger_message_id="",
                user_id="",
                user_name="",
                conversation_user_id=None,
                fresh_context=True,
                trigger_kind="reflection",
            )
            turn_id = native_turn.id

        async def sink(events):
            await persist_agent_events(native_turn.id, events)

        recorder.sink = sink
        recorder.append(
            "companion_reflection",
            model_visible=False,
            status="started",
            payload={
                "lease_token": claim.lease_token,
                "mode_revision": claim.mode_revision,
                "context_revision": claim.context_revision,
                "life_version": claim.life_version,
            },
        )
        recorder.append(
            "conversation_mode",
            model_visible=False,
            payload={
                "mode": "companion",
                "revision": claim.mode_revision,
                "context_revision": claim.context_revision,
                "persona_key": claim.persona_key,
            },
        )
        try:
            if not await recorder.flush():
                raise RuntimeError("Companion internal-turn start audit could not be persisted")
            if model_error is not None:
                raise model_error
            persona_input = {
                "name": persona.name,
                "prompt": persona.prompt,
                "appearance": persona.appearance,
                "has_reference_image": bool(persona.reference_image),
            }
            async with session() as db:
                channel_kind = await db.get(CompanionScopeKind, scope.id)
            allow_channel = channel_kind is not None and channel_kind.kind == "text" and is_private is not True
            system = build_reflection_system(
                json.dumps(persona_input, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                is_private=(channel_kind.kind == "direct") if channel_kind is not None else None,
            )
            envelope = len(build_reflection_prompt({}, now=now)) - 2
            available = limits.input_chars - len(system) - envelope
            payload, source_ids = await reflection_input(
                snapshot,
                scope_id=scope.id,
                channel_id=scope.channel_id,
                limits=limits,
                available_chars=available,
                now=now,
            )
            prompt = _bounded_prompt(system, payload, now=now, limit=limits.input_chars)
            recorder.append("companion_state", payload=payload, model_visible=False)
            recorder.append(
                "context_snapshot",
                model_visible=False,
                payload=build_context_snapshot(
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                    model=model_name,
                    persona=persona_input,
                    selection={"purpose": "companion_reflection", "fresh_context": True},
                    budgets={"input_chars": limits.input_chars, "output_tokens": limits.output_tokens},
                ),
            )
            if not await recorder.flush():
                raise RuntimeError("Companion reflection context audit could not be persisted")
            proposal = await provider.request_reflection(
                model,
                system=system,
                prompt=prompt,
                limits=limits,
                member_turn_ids=source_ids,
                recorder=recorder,
                allow_channel=allow_channel,
            )
            if audit_failures or any(
                event.event_type in {"model_request", "model_response"}
                and (event.payload.get("capture_status") == "failed")
                for event in recorder.events
            ):
                raise RuntimeError("Companion reflection model audit capture failed")
            if not await recorder.flush():
                raise RuntimeError("Companion reflection model audit could not be persisted")
            recorder.append(
                "companion_reflection",
                model_visible=False,
                status="succeeded",
                payload={"proposal": proposal.model_dump(mode="json")},
            )
            status = "completed"
            return proposal
        except asyncio.CancelledError:
            status = "cancelled"
            recorder.append("companion_reflection", model_visible=False, status="cancelled", payload={})
            raise
        except Exception as exc:
            recorder.append(
                "companion_reflection",
                model_visible=False,
                status="failed",
                payload={"error": type(exc).__name__},
            )
            raise
        finally:
            if turn_id is not None:

                async def finalize() -> None:
                    persisted = await recorder.flush()
                    await finish_turn(
                        turn_id,
                        status=status if persisted else "event_persistence_failed",
                    )
                    if not persisted:
                        raise RuntimeError("Companion reflection terminal audit could not be persisted")

                await settle_owned(finalize())
