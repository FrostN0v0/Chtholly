"""Normalize real participant supplements into one existing logical AgentTurn."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import TYPE_CHECKING, cast
from datetime import datetime
from dataclasses import replace
from collections.abc import Callable

from arclet.entari import Session

from utils.request_text import request_clauses

from .config import LLMChatConfig
from .identity import resolve_chat_identity, resolve_mentioned_participants
from .steering import SteerInput, SteeringState
from .core.types import ChatMessage
from .web.policy import update_llm_chat_web_reference_access
from .chat_context import (
    build_image_notes,
    render_user_storage,
    serialize_user_turn,
    collect_message_images,
    reply_with_image_views,
    build_multimodal_user_content,
    register_message_image_inputs,
)
from .image_inputs import image_inputs_scope
from .reply_record import bind_reply_record
from .reply_context import collect_reply_context
from .context_builder import (
    estimate_tokens,
    requests_context_pin,
    requests_tool_payload,
    requests_archived_context,
)
from .core.user_turns import update_user_payload, attribute_user_payload
from .forward_context import resolve_merged_forward_messages
from .agent_attachments import capture_user_input_images
from .core.media_delivery import (
    MediaIntent,
    build_media_intent,
    current_media_requirements,
)
from .core.artifact_access import is_artifact_request
from .core.tool_trace_safety import compact_text

if TYPE_CHECKING:
    from .agent_turn_setup import PreparedAgentTurn

_CANCEL = re.compile(
    r"^(?:\u7b97\u4e86|\u53d6\u6d88(?:\u5427|\u4e86)?|\u4e0d\u5fc5\u4e86|\u4e0d\u7528\u4e86|"
    r"\u5148\u522b(?:\u505a|\u5f04)?\u4e86?|\u522b\u505a\u4e86|\u505c\u6b62(?:\u5427)?)$"
    r"|^(?:never mind|nevermind|cancel(?: that)?|stop|do not do (?:it|that))$",
    re.IGNORECASE,
)
_NEGATIVE = re.compile(
    r"\u522b(?!\u53ea)|\u4e0d\u8981(?!\u53ea)|\u4e0d\u7528|\u65e0\u9700|\u7981\u6b62|"
    r"\u4e0d\u9700\u8981|\u4e0d\u8bb8|\u4e0d\u51c6|"
    r"\b(?:never|do not|don't|dont|no need to|stop)\b",
    re.IGNORECASE,
)
_MAX_RECEIPT_TEXT = 50_000


class SteerInputRejected(RuntimeError):
    """A supplement was audited but cannot safely become executable input."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Participant supplement refused: {reason}")


def _ordered_flag(clauses: list[str], request: Callable[[str], bool]) -> bool:
    allowed = False
    for clause in clauses:
        if _CANCEL.fullmatch(clause):
            allowed = False
        elif request(clause):
            allowed = not bool(_NEGATIVE.search(clause))
    return allowed


def bind_steer_inputs(
    state: SteeringState,
    prepared: PreparedAgentTurn,
    config: LLMChatConfig,
    root_session: Session,
    *,
    model_name: str | None,
    warn: Callable[[str], None],
) -> None:
    """Bind normalization, receipt audit, and policy refresh without another turn/session."""
    if state.processor is not None:
        raise RuntimeError("Participant input processor is already bound")
    if not state.owns(root_session) or state.inputs[0].session.event is not root_session.event:
        raise RuntimeError("Participant input processor requires the original root event")
    captured = deepcopy(config)
    state.request_input_budget = max(1024, int(captured.max_input_tokens) - max(0, int(captured.output_reserve_tokens)))
    recorder = prepared.agent_events
    original_access = prepared.agent_access
    inputs = prepared.image_inputs
    inputs.admit_input(root_session, state=state)
    original_messages = list(prepared.chat_messages)
    applied_messages: dict[int, ChatMessage] = {}
    received: set[int] = set()
    terminal: set[int] = set()
    state.request_model_name = model_name
    state.media_request_timeout = captured.media_request_timeout
    own_clauses = request_clauses(original_access.raw_user_text)

    def refresh_policy(new_text: str = "") -> None:
        trusted_text = "\n;\n".join(own_clauses)
        access = replace(
            original_access,
            raw_user_text=trusted_text,
            allow_archived_sessions=_ordered_flag(own_clauses, requests_archived_context),
            allow_payload_delivery=_ordered_flag(own_clauses, requests_tool_payload),
            allow_context_pin=_ordered_flag(own_clauses, requests_context_pin),
        )
        prepared.agent_access = access
        state.access = access
        has_images = bool(inputs.input_views())
        intent = build_media_intent(trusted_text, has_image_inputs=has_images)
        artifact_requested = is_artifact_request(trusted_text)
        if artifact_requested:
            intent = MediaIntent(media_requested=intent.media_requested)
        prepared.media_intent = intent
        prepared.media_requested = (
            intent.media_requested or artifact_requested or is_artifact_request(trusted_text, "send")
        )
        state.media_requested = prepared.media_requested
        requirements = current_media_requirements()
        if requirements is not None:
            requirements.intent = intent
            if intent.media_requested and build_media_intent(new_text, has_image_inputs=has_images).media_requested:
                requirements.confirmed = False
                state.media_confirmation_baseline = prepared.lifecycle.delivery_state.confirmed_media_deliveries
        update_llm_chat_web_reference_access(intent.requires_web_reference)

    def on_receive(item: SteerInput) -> None:
        if item.sequence < 2 or item.sequence in received:
            return
        received.add(item.sequence)
        text = item.session.elements.extract_plain_text().strip()
        safe_text = compact_text(text, _MAX_RECEIPT_TEXT)
        event_message = getattr(item.session.event, "message", None)
        recorder.append(
            "steer_received",
            payload={
                "steer_sequence": item.sequence,
                "received_at": item.received_at.isoformat(),
                "content": safe_text,
                "text_chars": len(text),
                "text_truncated": len(text) > _MAX_RECEIPT_TEXT,
                "source_message_id": compact_text(getattr(event_message, "id", ""), 256),
            },
            model_visible=False,
            status="received",
            effect="none",
            created_at=item.received_at,
        )

    async def reject(item: SteerInput, reason: str, *, status: str = "refused") -> None:
        if item.sequence not in terminal:
            terminal.add(item.sequence)
            recorder.append(
                "steer_checkpoint",
                payload={
                    "steer_sequence": item.sequence,
                    "received_at": item.received_at.isoformat(),
                    "reason": reason,
                },
                model_visible=False,
                status=status,
                effect="none",
            )
            await recorder.flush()

    def check_budget(message: ChatMessage) -> None:
        messages: list[ChatMessage] = [
            {"role": "system", "content": prepared.system},
            *original_messages,
            *applied_messages.values(),
            message,
        ]
        if estimate_tokens(model_name, messages) > cast(int, state.request_input_budget):
            raise SteerInputRejected("input_budget_exceeded")

    async def process(item: SteerInput) -> ChatMessage:
        existing = applied_messages.get(item.sequence)
        if existing is not None:
            return existing
        if item.sequence < 2 or not state.owns(item.session):
            raise SteerInputRejected("input_not_admitted")
        if item.sequence in terminal:
            raise SteerInputRejected("input_already_refused")
        on_receive(item)
        session = item.session
        raw_text = session.elements.extract_plain_text().strip()
        try:
            check_budget({"role": "user", "content": serialize_user_turn("", raw_text)})
            inputs.admit_input(session, state=state)
            with image_inputs_scope(inputs):
                reply_to = await bind_reply_record(session, collect_reply_context(session), warn=warn)
                register_message_image_inputs(session, inputs, reply_to=reply_to)
                try:
                    forwarded = await resolve_merged_forward_messages(captured, session, warn)
                except Exception as exc:
                    warn(f"steered merged forward normalization failed: {type(exc).__name__}")
                    forwarded = []
                if not raw_text and not collect_message_images(session) and not forwarded and reply_to is None:
                    raise SteerInputRejected("empty_input")
                identity = await resolve_chat_identity(session)
                if identity.user_id != original_access.user_id:
                    raise SteerInputRejected("participant_identity_changed")
                mentioned = await resolve_mentioned_participants(session)
                model_text = raw_text
                if inputs.supports_image_input:
                    current_content, content = await build_multimodal_user_content(
                        captured,
                        session,
                        identity.display_name,
                        raw_text,
                        warn,
                        forwarded_messages=forwarded,
                        mentioned_participants=mentioned,
                        reply_to=reply_to,
                    )
                else:
                    notes = await build_image_notes(captured, session, warn, forwarded)
                    model_text = " ".join(part for part in [raw_text, *notes] if part)
                    views = inputs.input_views(session=session)
                    reply_to = reply_with_image_views(reply_to, views)
                    content = render_user_storage(model_text, forwarded, reply_to=reply_to, input_images=views)
                    current_content = serialize_user_turn(
                        identity.display_name, model_text, forwarded, mentioned, views, reply_to=reply_to
                    )
                message = update_user_payload(
                    {"role": "user", "content": current_content},
                    lambda payload: attribute_user_payload(
                        payload, speaker_id=prepared.speaker_id, fallback_speaker=identity.display_name
                    ),
                )
                check_budget(message)
                attachments = await capture_user_input_images(session, inputs, warn=warn)
            message_id = await prepared.lifecycle.append_history(
                root_session.channel.id, original_access.user_id, identity.display_name, "user", content
            )
            prepared.lifecycle.add_user_message(cast(int, message_id))
            applied_at = datetime.utcnow()
            recorder.append(
                "user_input",
                role="user",
                payload={
                    "content": serialize_user_turn(
                        identity.display_name,
                        model_text,
                        forwarded,
                        mentioned,
                        inputs.input_views(session=session),
                        reply_to=reply_with_image_views(reply_to, inputs.input_views(session=session)),
                        durable=True,
                    ),
                    "speaker": identity.display_name,
                    "input_format": "structured_user_turn",
                    "fresh_context": False,
                    "attachments": attachments,
                    "steer_sequence": item.sequence,
                    "received_at": item.received_at.isoformat(),
                    "applied_at": applied_at.isoformat(),
                    "raw_user_text": raw_text,
                },
                status="applied",
                model_visible=True,
                created_at=applied_at,
            )
            inputs.commit_input_audit(session=session)
            if not await recorder.flush():
                raise SteerInputRejected("input_audit_persistence_failed")
            applied_messages[item.sequence] = message
            own_clauses.extend(request_clauses(raw_text))
            refresh_policy("\n;\n".join(request_clauses(raw_text)))
            return message
        except SteerInputRejected as exc:
            await reject(item, exc.reason)
            raise
        except Exception as exc:
            warn(f"steered input normalization failed: {type(exc).__name__}")
            await reject(item, "input_normalization_failed", status="failed")
            raise SteerInputRejected("input_normalization_failed") from None

    async def on_close() -> None:
        for item in state.inputs:
            if item.sequence > state.applied_revision and item.sequence not in terminal:
                await reject(item, "task_closed_before_application", status="unapplied")
        await recorder.flush()

    state.on_receive = on_receive
    state.on_flush = recorder.flush
    state.on_close = on_close
    state.processor = process
    refresh_policy()
    for item in state.inputs:
        on_receive(item)
