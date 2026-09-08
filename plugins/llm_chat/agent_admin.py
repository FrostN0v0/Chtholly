"""Authenticated administration service for AgentEvent sessions and context inspection."""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass

from sqlalchemy import select
from entari_plugin_database import get_session
from entari_plugin_llm.config import get_model_config

from .config import LLMChatConfig
from .models import AgentTurn, ChatScope, AgentEvent, ContextSession
from .agent_events import load_event_payload, select_payload_path
from .context_builder import build_baseline_fingerprint
from .session_handoff import generate_session_handoff
from .session_manager import (
    pin_event,
    list_scopes,
    unpin_event,
    create_session,
    get_scope_by_ref,
    rollover_session,
    clean_channel_name,
    get_session_by_ref,
    load_scope_anchors,
    list_scope_sessions,
    seal_scope_sessions,
)
from .agent_event_view import serialize_event_view
from .agent_attachments import (
    is_agent_attachment,
    resolve_agent_attachment,
    event_attachment_metadata,
)


class AgentAdminError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_request", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class AgentAttachment:
    path: Path
    mime: str


@dataclass(slots=True)
class AgentAdminService:
    config: LLMChatConfig
    tool_schemas: list[dict[str, str]]
    attachment_root: Path | None = None

    def _baseline(self, channel_id: str):
        model_name = get_model_config(self.config.model, channel_id).name
        return build_baseline_fingerprint(
            model_name=model_name,
            persona=self.config.persona,
            tool_schemas=self.tool_schemas,
        )

    async def list_scopes(self, limit: int = 100) -> list[dict[str, object]]:
        scopes = await list_scopes(limit=max(1, min(500, limit)))
        return [
            {
                "scope_ref": scope.scope_ref,
                "platform": scope.platform,
                "account_id": scope.account_id,
                "guild_id": scope.guild_id,
                "channel_id": scope.channel_id,
                "display_name": scope.display_name,
                "channel_name": self._channel_name(scope),
                "created_at": scope.created_at.isoformat(),
                "updated_at": scope.updated_at.isoformat(),
            }
            for scope in scopes
        ]

    async def list_sessions(self, scope_ref: str, limit: int = 100) -> list[dict[str, object]]:
        scope = await self._scope(scope_ref)
        sessions = await list_scope_sessions(scope.id, limit=max(1, min(500, limit)))
        return [self._serialize_session(item) for item in sessions]

    async def session_detail(self, session_ref: str) -> dict[str, object]:
        context_session = await self._session(session_ref)
        scope = await self._scope_id(context_session.scope_id)
        anchors = await load_scope_anchors(scope.id)
        return {
            **self._serialize_session(context_session),
            "scope": {
                "scope_ref": scope.scope_ref,
                "platform": scope.platform,
                "channel_id": scope.channel_id,
                "display_name": scope.display_name,
                "channel_name": self._channel_name(scope),
            },
            "handoff": self._json_object(context_session.handoff_json),
            "anchors": [
                {
                    "event_ref": event.event_ref,
                    "label": anchor.label,
                    "created_by_user_id": anchor.created_by_user_id,
                    "created_at": anchor.created_at.isoformat(),
                }
                for anchor, event in anchors
            ],
        }

    async def list_turns(self, session_ref: str, limit: int = 200) -> list[dict[str, object]]:
        context_session = await self._session(session_ref)
        async with get_session() as db:
            turns = list(
                (
                    await db.execute(
                        select(AgentTurn)
                        .where(AgentTurn.session_id == context_session.id)
                        .order_by(AgentTurn.sequence.desc())
                        .limit(max(1, min(1000, limit)))
                    )
                )
                .scalars()
                .all()
            )
        turns.reverse()
        return [
            {
                "turn_ref": turn.turn_ref,
                "sequence": turn.sequence,
                "user_id": turn.user_id,
                "user_name": turn.user_name,
                "status": turn.status,
                "fresh_context": turn.fresh_context,
                "final_text": turn.final_text,
                "created_at": turn.created_at.isoformat(),
                "finished_at": turn.finished_at.isoformat() if turn.finished_at else None,
            }
            for turn in turns
        ]

    async def list_events(self, turn_ref: str) -> list[dict[str, object]]:
        turn = await self._turn(turn_ref)
        async with get_session() as db:
            events = list(
                (
                    await db.execute(
                        select(AgentEvent).where(AgentEvent.turn_id == turn.id).order_by(AgentEvent.sequence.asc())
                    )
                )
                .scalars()
                .all()
            )
        return [self._serialize_event(event) for event in events]

    async def read_event_payload(
        self,
        event_ref: str,
        *,
        path: str = "",
        offset: int = 0,
        limit: int = 16000,
    ) -> dict[str, object]:
        event = await self._event(event_ref)
        payload: object = load_event_payload(event)
        if path:
            try:
                payload = select_payload_path(payload, path)
            except KeyError as exc:
                raise AgentAdminError("Unknown event payload path", code="unknown_path", status=404) from exc
        maximum = max(256, min(100_000, limit))
        if isinstance(payload, str):
            start = max(0, offset)
            end = min(len(payload), start + maximum)
            return {
                "event_ref": event.event_ref,
                "path": path,
                "data": payload[start:end],
                "offset": start,
                "next_offset": end if end < len(payload) else None,
                "total_chars": len(payload),
            }
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) > maximum:
            return {
                "event_ref": event.event_ref,
                "path": path,
                "stored": True,
                "chars": len(serialized),
                "message": "Select a narrower payload path",
            }
        return {
            "event_ref": event.event_ref,
            "path": path,
            "data": payload,
            "offset": 0,
            "next_offset": None,
            "total_chars": len(serialized),
        }

    async def read_event_attachment(self, event_ref: str, attachment_ref: str) -> AgentAttachment:
        """Resolve one private image only when the event payload authorizes it."""

        event = await self._event(event_ref)
        payload = load_event_payload(event)
        for raw in event_attachment_metadata(payload):
            if raw.get("attachment_ref") != attachment_ref:
                continue
            mime = raw.get("mime")
            if not is_agent_attachment(attachment_ref, mime):
                break
            try:
                path = resolve_agent_attachment(
                    attachment_ref,
                    str(mime),
                    root=self.attachment_root,
                )
            except ValueError:
                break
            if path.is_file():
                return AgentAttachment(path=path, mime=str(mime))
            break
        raise AgentAdminError("Event image attachment is unavailable", code="attachment_not_found", status=404)

    async def context_inspector(self, turn_ref: str) -> dict[str, object]:
        turn = await self._turn(turn_ref)
        async with get_session() as db:
            selection = (
                await db.execute(
                    select(AgentEvent).where(
                        AgentEvent.turn_id == turn.id,
                        AgentEvent.event_type == "context_selection",
                    )
                )
            ).scalar_one_or_none()
        context_session = await self._session_id(turn.session_id)
        return {
            "turn_ref": turn.turn_ref,
            "session_ref": context_session.session_ref,
            "baseline": {
                "model": context_session.model_name,
                "persona_hash": context_session.persona_hash,
                "system_version": context_session.system_version,
                "tool_schema_hash": context_session.tool_schema_hash,
                "policy_version": context_session.policy_version,
            },
            "selection": load_event_payload(selection) if selection is not None else {},
            "budgets": {
                "max_input_tokens": self.config.max_input_tokens,
                "output_reserve_tokens": self.config.output_reserve_tokens,
                "rollover_ratio": self.config.context_rollover_ratio,
                "minimum_recent_turns": self.config.context_min_recent_turns,
                "inline_event_chars": self.config.context_inline_event_chars,
            },
        }

    async def rollover(self, scope_ref: str, session_ref: str, *, carry_handoff: bool) -> dict[str, object]:
        scope = await self._scope(scope_ref)
        context_session = await self._session(session_ref)
        if context_session.scope_id != scope.id:
            raise AgentAdminError("Session does not belong to scope", code="scope_mismatch", status=409)
        if context_session.status != "active":
            raise AgentAdminError("Only the active session can be rolled over", code="not_active", status=409)
        handoff = "{}"
        reason = "webui_new"
        if carry_handoff:
            reason = "webui_rollover"
            handoff = await generate_session_handoff(
                context_session,
                model_name=self.config.model,
                channel_id=scope.channel_id,
                timeout=self.config.session_handoff_timeout,
                source_max_chars=self.config.session_handoff_source_max_chars,
                output_max_chars=self.config.session_handoff_max_chars,
            )
        created = await rollover_session(
            scope,
            context_session,
            self._baseline(scope.channel_id),
            reason=reason,
            handoff_json=handoff,
            carry_handoff=carry_handoff,
        )
        return self._serialize_session(created)

    async def hard_reset(self, scope_ref: str) -> dict[str, object]:
        scope = await self._scope(scope_ref)
        await seal_scope_sessions(scope.id)
        created = await create_session(scope.id, self._baseline(scope.channel_id), start_reason="webui_hard_reset")
        return self._serialize_session(created)

    async def pin(self, scope_ref: str, event_ref: str, label: str) -> dict[str, object]:
        scope = await self._scope(scope_ref)
        event = await self._event(event_ref)
        event_scope = await self._event_scope(event.id)
        if event_scope != scope.id:
            raise AgentAdminError("Event does not belong to scope", code="scope_mismatch", status=409)
        anchor = await pin_event(scope.id, event.id, label=label, created_by_user_id="webui")
        return {"event_ref": event.event_ref, "label": anchor.label, "active": True}

    async def unpin(self, scope_ref: str, event_ref: str) -> dict[str, object]:
        scope = await self._scope(scope_ref)
        event = await self._event(event_ref)
        changed = await unpin_event(scope.id, event.id)
        return {"event_ref": event.event_ref, "active": not changed}

    @staticmethod
    def _channel_name(scope: ChatScope) -> str:
        """Return the sanitized channel name, never the raw platform ID."""

        name = clean_channel_name(scope.display_name)
        return "" if name == scope.channel_id.strip() else name

    @staticmethod
    def _serialize_session(item: ContextSession) -> dict[str, object]:
        return {
            "session_ref": item.session_ref,
            "sequence": item.sequence,
            "status": item.status,
            "start_reason": item.start_reason,
            "close_reason": item.close_reason,
            "model": item.model_name,
            "turn_count": item.turn_count,
            "created_at": item.created_at.isoformat(),
            "last_turn_at": item.last_turn_at.isoformat() if item.last_turn_at else None,
            "closed_at": item.closed_at.isoformat() if item.closed_at else None,
        }

    @staticmethod
    def _serialize_event(event: AgentEvent) -> dict[str, object]:
        payload = load_event_payload(event)
        return {
            "event_ref": event.event_ref,
            "sequence": event.sequence,
            "attempt": event.attempt,
            "event_type": event.event_type,
            "role": event.role,
            "execution_ref": event.execution_ref,
            "tool": event.tool_name,
            "status": event.status,
            "effect": event.effect,
            "duration_ms": event.duration_ms,
            "model_visible": event.model_visible,
            "payload_keys": list(payload),
            "created_at": event.created_at.isoformat(),
            **serialize_event_view(event, payload),
        }

    @staticmethod
    def _json_object(value: str) -> dict[str, object]:
        try:
            payload = json.loads(value)
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    async def _scope(scope_ref: str) -> ChatScope:
        scope = await get_scope_by_ref(scope_ref.strip())
        if scope is None:
            raise AgentAdminError("Unknown scope_ref", code="scope_not_found", status=404)
        return scope

    @staticmethod
    async def _scope_id(scope_id: int) -> ChatScope:
        async with get_session() as db:
            scope = await db.get(ChatScope, scope_id)
        if scope is None:
            raise AgentAdminError("Unknown scope", code="scope_not_found", status=404)
        return scope

    @staticmethod
    async def _session(session_ref: str) -> ContextSession:
        context_session = await get_session_by_ref(session_ref.strip())
        if context_session is None:
            raise AgentAdminError("Unknown session_ref", code="session_not_found", status=404)
        return context_session

    @staticmethod
    async def _session_id(session_id: int) -> ContextSession:
        async with get_session() as db:
            context_session = await db.get(ContextSession, session_id)
        if context_session is None:
            raise AgentAdminError("Unknown session", code="session_not_found", status=404)
        return context_session

    @staticmethod
    async def _turn(turn_ref: str) -> AgentTurn:
        async with get_session() as db:
            turn = (
                await db.execute(select(AgentTurn).where(AgentTurn.turn_ref == turn_ref.strip()))
            ).scalar_one_or_none()
        if turn is None:
            raise AgentAdminError("Unknown turn_ref", code="turn_not_found", status=404)
        return turn

    @staticmethod
    async def _event(event_ref: str) -> AgentEvent:
        async with get_session() as db:
            event = (
                await db.execute(select(AgentEvent).where(AgentEvent.event_ref == event_ref.strip()))
            ).scalar_one_or_none()
        if event is None:
            raise AgentAdminError("Unknown event_ref", code="event_not_found", status=404)
        return event

    @staticmethod
    async def _event_scope(event_id: int) -> int | None:
        async with get_session() as db:
            return await db.scalar(
                select(ContextSession.scope_id)
                .join(AgentTurn, AgentTurn.session_id == ContextSession.id)
                .join(AgentEvent, AgentEvent.turn_id == AgentTurn.id)
                .where(AgentEvent.id == event_id)
            )
