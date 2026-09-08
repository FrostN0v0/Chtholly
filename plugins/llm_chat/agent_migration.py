"""One-time import of legacy conversation and tool rows into AgentEvent sessions."""

from __future__ import annotations

import json
from datetime import datetime
from collections import defaultdict

from sqlalchemy import select
from entari_plugin_database import get_session

from .models import (
    AgentTurn,
    ChatScope,
    AgentEvent,
    Conversation,
    ToolExecution,
    ContextSession,
    MigrationState,
)

_MIGRATION_KEY = "legacy_agent_events_v1"


async def migrate_legacy_agent_events() -> int:
    """Import historical rows once, preserving legacy tables as read-only evidence."""

    async with get_session() as db:
        state = await db.get(MigrationState, _MIGRATION_KEY)
        if state is not None and state.status == "completed":
            return 0

        conversations = list((await db.execute(select(Conversation).order_by(Conversation.id.asc()))).scalars().all())
        tools = list((await db.execute(select(ToolExecution).order_by(ToolExecution.id.asc()))).scalars().all())
        tools_by_turn: dict[tuple[str, int], list[ToolExecution]] = defaultdict(list)
        for tool in tools:
            tools_by_turn[(tool.channel_id, tool.turn_id)].append(tool)

        conversations_by_channel: dict[str, list[Conversation]] = defaultdict(list)
        for row in conversations:
            conversations_by_channel[row.channel_id].append(row)

        imported = 0
        for channel_id, rows in conversations_by_channel.items():
            scope = ChatScope(
                platform="legacy",
                account_id="",
                guild_id="",
                channel_id=channel_id,
                display_name=channel_id,
            )
            db.add(scope)
            await db.flush()
            context_session = ContextSession(
                scope_id=scope.id,
                sequence=1,
                status="closed",
                start_reason="legacy_import",
                close_reason="legacy_import",
                model_name="legacy",
                persona_hash="legacy",
                system_version="legacy",
                tool_schema_hash="legacy",
                policy_version="legacy",
                created_at=rows[0].created_at,
                closed_at=rows[-1].created_at,
                last_turn_at=rows[-1].created_at,
            )
            db.add(context_session)
            await db.flush()

            turn: AgentTurn | None = None
            event_sequence = 0
            turn_sequence = 0
            for row in rows:
                if row.role == "user" or turn is None:
                    turn_sequence += 1
                    turn = AgentTurn(
                        session_id=context_session.id,
                        sequence=turn_sequence,
                        conversation_user_id=row.id if row.role == "user" else None,
                        user_id=row.user_id,
                        user_name=row.user_name,
                        status="completed",
                        created_at=row.created_at,
                        finished_at=row.created_at,
                    )
                    db.add(turn)
                    await db.flush()
                    event_sequence = 0
                event_sequence += 1
                db.add(
                    AgentEvent(
                        turn_id=turn.id,
                        sequence=event_sequence,
                        event_type="user_input" if row.role == "user" else "assistant_output",
                        role=row.role,
                        payload_json=json.dumps({"content": row.content}, ensure_ascii=False, separators=(",", ":")),
                        status="confirmed",
                        effect="confirmed",
                        model_visible=True,
                        created_at=row.created_at,
                    )
                )
                imported += 1
                if row.role != "user":
                    continue
                for tool in tools_by_turn.get((channel_id, row.id), []):
                    try:
                        arguments = json.loads(tool.arguments_json)
                    except ValueError:
                        arguments = {}
                    try:
                        outcome = json.loads(tool.outcome_json)
                    except ValueError:
                        outcome = {}
                    execution_ref = f"legacy_exec_{tool.id}"
                    event_sequence += 1
                    db.add(
                        AgentEvent(
                            turn_id=turn.id,
                            sequence=event_sequence,
                            event_type="assistant_tool_call",
                            role="assistant",
                            tool_call_id=execution_ref,
                            execution_ref=execution_ref,
                            tool_name=tool.tool_name,
                            payload_json=json.dumps(
                                {"arguments": arguments, "context_arguments": arguments},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            status="requested",
                            effect="none",
                            model_visible=True,
                            created_at=tool.started_at,
                        )
                    )
                    event_sequence += 1
                    db.add(
                        AgentEvent(
                            turn_id=turn.id,
                            sequence=event_sequence,
                            event_type="tool_result",
                            role="tool",
                            tool_call_id=execution_ref,
                            execution_ref=execution_ref,
                            tool_name=tool.tool_name,
                            payload_json=json.dumps(
                                {"result": outcome, "context_result": outcome},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            status=tool.status,
                            effect=tool.effect,
                            duration_ms=tool.duration_ms,
                            model_visible=True,
                            created_at=tool.started_at,
                        )
                    )
            context_session.turn_count = turn_sequence

        if state is None:
            state = MigrationState(key=_MIGRATION_KEY)
            db.add(state)
        state.status = "completed"
        state.details_json = json.dumps(
            {"conversations": imported, "tools": len(tools)},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        state.updated_at = datetime.utcnow()
        await db.commit()
        return imported
