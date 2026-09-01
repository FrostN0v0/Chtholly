"""Administrator commands for explicit context-session lifecycle control."""

from __future__ import annotations

from arclet.entari import Session, command, plugin_config
from arclet.letoderea import STOP
from arclet.entari.filter import superusers
from arclet.entari.command import Match
from entari_plugin_llm.config import get_model_config

from .config import LLMChatConfig
from .tool_runtime import registered_tool_schemas
from .context_builder import build_baseline_fingerprint
from .session_handoff import generate_session_handoff
from .session_manager import (
    create_session,
    rollover_session,
    get_or_create_scope,
    seal_scope_sessions,
    ensure_active_session,
    resolve_scope_identity,
)

config = plugin_config(LLMChatConfig)
_superuser_check = superusers().check


async def _is_superuser(session: Session) -> bool:
    return await _superuser_check(session) is not STOP


def _baseline(channel_id: str):
    model_name = get_model_config(config.model, channel_id).name
    return build_baseline_fingerprint(
        model_name=model_name,
        persona=config.persona,
        tool_schemas=registered_tool_schemas,
    )


async def _current_session(session: Session):
    scope = await get_or_create_scope(await resolve_scope_identity(session))
    context_session, _reason = await ensure_active_session(
        scope,
        _baseline(session.channel.id),
        idle_minutes=0,
        max_turns=0,
    )
    return scope, context_session


@command.on("llmchat new-session")
async def new_session_command(session: Session) -> str:
    """Start a clean topic session while preserving relationship, profile, and long-term memory."""

    if not await _is_superuser(session):
        return "Permission denied: only configured superusers may switch the group session."
    scope, current = await _current_session(session)
    created = await rollover_session(
        scope,
        current,
        _baseline(session.channel.id),
        reason="manual_new",
        handoff_json="{}",
        carry_handoff=False,
    )
    return f"已创建新会话（#{created.sequence}），不会继承上一话题。"


@command.on("llmchat rollover-session")
async def rollover_session_command(session: Session) -> str:
    """Close the current session and carry a structured handoff into its continuation."""

    if not await _is_superuser(session):
        return "Permission denied: only configured superusers may switch the group session."
    scope, current = await _current_session(session)
    handoff = await generate_session_handoff(
        current,
        model_name=config.model,
        channel_id=session.channel.id,
        timeout=config.session_handoff_timeout,
        source_max_chars=config.session_handoff_source_max_chars,
        output_max_chars=config.session_handoff_max_chars,
    )
    created = await rollover_session(
        scope,
        current,
        _baseline(session.channel.id),
        reason="manual_rollover",
        handoff_json=handoff,
    )
    return f"已续接新会话（#{created.sequence}），结构化交接已保留。"


@command.on("llmchat hard-reset-session {confirmation}")
async def hard_reset_session_command(session: Session, confirmation: Match[str]) -> str:
    """Seal all prior sessions after an explicit confirmation token."""

    if not await _is_superuser(session):
        return "Permission denied: only configured superusers may reset the group session."
    value = confirmation.result.strip() if confirmation.available else ""
    if value != "CONFIRM":
        return "高风险操作未执行。请使用：llmchat hard-reset-session CONFIRM"
    scope = await get_or_create_scope(await resolve_scope_identity(session))
    await seal_scope_sessions(scope.id)
    created = await create_session(scope.id, _baseline(session.channel.id), start_reason="hard_reset")
    return f"已封存旧会话并创建全新会话（#{created.sequence}）；审计事件未删除。"
