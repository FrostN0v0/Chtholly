"""Chat commands for persona selection and observable context-session lifecycle."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from arclet.entari import Session, command, plugin_config
from arclet.alconna import Args
from arclet.letoderea import STOP
from arclet.entari.filter import superusers
from entari_plugin_database import get_session
from entari_plugin_llm.config import get_model_config

from .config import LLMChatConfig
from .models import ChatScope, ContextSession, ScopePersonaSelection, SessionPersonaSnapshot
from .personality import session_persona, persona_scope_lock, resolve_scope_persona, remember_session_persona
from .tool_runtime import registered_tool_schemas
from .context_builder import build_baseline_fingerprint
from .session_handoff import generate_session_handoff
from .session_manager import (
    create_session,
    scope_identity,
    rollover_session,
    get_active_session,
    get_or_create_scope,
    seal_scope_sessions,
    resolve_scope_identity,
)
from .core.personality import ResolvedPersona, resolve_persona
from .session_inspection import session_usage, session_context_summary
from .core.self_reference import resolve_self_reference_image

config = plugin_config(LLMChatConfig)
_superuser_check = superusers().check


async def _is_superuser(session: Session) -> bool:
    return await _superuser_check(session) is not STOP


def _baseline(channel_id: str, persona: ResolvedPersona):
    return build_baseline_fingerprint(
        model_name=get_model_config(config.model, channel_id).name,
        persona=persona.baseline_text,
        tool_schemas=registered_tool_schemas,
    )


async def _existing_scope(session: Session) -> ChatScope | None:
    identity = scope_identity(session)
    async with get_session() as db:
        return (
            await db.execute(
                select(ChatScope).where(
                    ChatScope.platform == identity.platform,
                    ChatScope.account_id == identity.account_id,
                    ChatScope.channel_id == identity.channel_id,
                )
            )
        ).scalar_one_or_none()


async def _clean_session(scope: ChatScope, channel_id: str, persona: ResolvedPersona, reason: str):
    baseline = _baseline(channel_id, persona)
    current = await get_active_session(scope.id)
    if current is None:
        created = await create_session(scope.id, baseline, start_reason=reason)
    else:
        created = await rollover_session(
            scope, current, baseline, reason=reason, handoff_json="{}", carry_handoff=False
        )
    await remember_session_persona(created.id, persona)
    return created


@command.on("llmchat new")
async def new_session_command(session: Session) -> str:
    """Start a clean topic while preserving relationship, profile, and long-term memory."""
    if not await _is_superuser(session):
        return "权限不足：仅配置的超级用户可切换群聊会话。"
    scope = await get_or_create_scope(await resolve_scope_identity(session))
    async with persona_scope_lock(scope.id):
        persona = await resolve_scope_persona(config, scope.id)
        created = await _clean_session(scope, session.channel.id, persona, "manual_new")
    return f"已创建新会话（#{created.sequence}，{persona.name}），不会继承上一话题。"


@command.on("llmchat handoff")
async def rollover_session_command(session: Session) -> str:
    """Carry a structured handoff only if the captured session and persona remain current."""
    if not await _is_superuser(session):
        return "权限不足：仅配置的超级用户可切换群聊会话。"
    scope = await get_or_create_scope(await resolve_scope_identity(session))
    async with persona_scope_lock(scope.id):
        persona = await resolve_scope_persona(config, scope.id)
        baseline = _baseline(session.channel.id, persona)
        current = await get_active_session(scope.id)
        if current is None or current.persona_hash != baseline.persona_hash:
            created = await _clean_session(scope, session.channel.id, persona, "manual_new")
            return f"当前角色没有可续接的话题，已创建新会话（#{created.sequence}，{persona.name}）。"
    handoff = await generate_session_handoff(
        current,
        model_name=config.model,
        channel_id=session.channel.id,
        timeout=config.session_handoff_timeout,
        source_max_chars=config.session_handoff_source_max_chars,
        output_max_chars=config.session_handoff_max_chars,
    )
    async with persona_scope_lock(scope.id):
        selected = await resolve_scope_persona(config, scope.id)
        active = await get_active_session(scope.id)
        if active is None or active.id != current.id or selected != persona:
            return "交接期间会话或角色已切换，未覆盖当前会话；请按需重新续接。"
        created = await rollover_session(scope, current, baseline, reason="manual_rollover", handoff_json=handoff)
        await remember_session_persona(created.id, persona)
    return f"已续接新会话（#{created.sequence}，{persona.name}），结构化交接已保留。"


@command.on("llmchat reset")
async def hard_reset_session_command(session: Session) -> str:
    """Seal prior sessions without deleting their audit events."""
    if not await _is_superuser(session):
        return "权限不足：仅配置的超级用户可重置群聊会话。"
    scope = await get_or_create_scope(await resolve_scope_identity(session))
    async with persona_scope_lock(scope.id):
        persona = await resolve_scope_persona(config, scope.id)
        baseline = _baseline(session.channel.id, persona)
        await seal_scope_sessions(scope.id)
        created = await create_session(scope.id, baseline, start_reason="hard_reset")
        await remember_session_persona(created.id, persona)
    return f"已封存旧会话并创建全新会话（#{created.sequence}，{persona.name}）；审计事件未删除。"


@command.on("llmchat persona {key}", args={"key": Args["key?", str]})
async def persona_command(session: Session, key: str = "") -> str:
    """List configured personas or atomically select a persona and start a clean topic."""
    if not key:
        scope = await _existing_scope(session)
        selected = await resolve_scope_persona(config, scope.id) if scope else resolve_persona(config)
        lines = [f"当前角色：{selected.name}（{selected.key}）", "可用角色："]
        for persona_key, definition in config.personas.items():
            markers = []
            if persona_key == selected.key:
                markers.append("当前")
            if persona_key == config.default_persona:
                markers.append("默认")
            suffix = f"［{'、'.join(markers)}］" if markers else ""
            lines.append(f"{persona_key}：{definition.name}{suffix}")
        lines.append("切换：/llmchat persona <角色键>（仅超级用户）")
        return "\n".join(lines)
    if not await _is_superuser(session):
        return "权限不足：仅配置的超级用户可切换群聊角色。"
    try:
        persona = resolve_persona(config, key)
        if persona.reference_image and resolve_self_reference_image(persona.reference_image) is None:
            return "角色参考图当前不可用，未切换角色。请检查配置。"
        baseline = _baseline(session.channel.id, persona)
    except ValueError:
        return "角色键或配置无效，未切换角色。使用 /llmchat persona 查看可用角色。"
    scope = await _existing_scope(session)
    if scope is None and persona.key == config.default_persona:
        return f"当前已使用角色 {persona.name}，无需创建新会话。"
    if scope is None:
        scope = await get_or_create_scope(await resolve_scope_identity(session))
    async with persona_scope_lock(scope.id):
        selected = await resolve_scope_persona(config, scope.id)
        current = await get_active_session(scope.id)
        if selected == persona and (current is None or current.persona_hash == baseline.persona_hash):
            return f"当前已使用角色 {persona.name}，无需创建新会话。"
        async with get_session() as db:
            now = datetime.utcnow()
            if current is not None:
                previous = await db.get(ContextSession, current.id)
                if previous is not None:
                    previous.status = "closed"
                    previous.close_reason = "persona_change"
                    previous.closed_at = now
            sequence = (
                int(
                    await db.scalar(
                        select(func.max(ContextSession.sequence)).where(ContextSession.scope_id == scope.id)
                    )
                    or 0
                )
                + 1
            )
            created = ContextSession(
                scope_id=scope.id,
                sequence=sequence,
                previous_session_id=current.id if current else None,
                start_reason="persona_change",
                model_name=baseline.model_name,
                persona_hash=baseline.persona_hash,
                system_version=baseline.system_version,
                tool_schema_hash=baseline.tool_schema_hash,
                policy_version=baseline.policy_version,
                handoff_json="{}",
            )
            db.add(created)
            await db.flush()
            db.add(SessionPersonaSnapshot(session_id=created.id, snapshot_json=persona.baseline_text))
            selection = await db.get(ScopePersonaSelection, scope.id)
            if selection is None:
                db.add(ScopePersonaSelection(scope_id=scope.id, persona_key=persona.key))
            else:
                selection.persona_key = persona.key
                selection.updated_at = now
            await db.commit()
    return f"已切换为 {persona.name}，创建新会话（#{sequence}）；不继承上一话题，关系与长期记忆未清除。"


def _count(value: object) -> str:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else "未知"


@command.on("llmchat session")
async def session_command(session: Session) -> str:
    """Show recorded scope state without creating a scope, session, or model request."""
    scope = await _existing_scope(session)
    if scope is None:
        return f"当前没有活动会话\n当前角色：{resolve_persona(config).name}"
    async with persona_scope_lock(scope.id):
        selected = await resolve_scope_persona(config, scope.id)
        current = await get_active_session(scope.id)
        if current is None:
            return f"当前没有活动会话\n当前角色：{selected.name}"
        historic = await session_persona(current.id)
        usage = await session_usage(current.id)
        context = await session_context_summary(current.id)
    statuses = {"active": "活动中", "closed": "已关闭", "sealed": "已封存"}
    lines = [
        f"会话：#{current.sequence}",
        f"当前选择角色：{selected.name}",
        f"会话角色：{historic['name'] if historic else '未记录（历史会话）'}",
        f"使用模型：{current.model_name or '未记录'}",
        f"状态：{statuses.get(current.status, '未知')}",
        f"对话轮数：{current.turn_count}",
        f"创建时间：{current.created_at.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"实际 Token：输入 {_count(usage.get('input_tokens'))} / 输出 {_count(usage.get('output_tokens'))}"
        f" / 总计 {_count(usage.get('total_tokens'))}",
        f"其中缓存输入 {_count(usage.get('cached_input_tokens'))} / 推理输出 {_count(usage.get('reasoning_tokens'))}"
        "（子项不另加总）",
        f"请求计量：已记录 {_count(usage.get('measured_requests'))} / 未知 {_count(usage.get('unknown_requests'))}",
        f"最近上下文估计：{_count(context.get('estimated_tokens'))} Token；"
        f"输入上限 {_count(context.get('max_input_tokens'))} / 输出预留 {_count(context.get('output_reserve_tokens'))}",
        f"最近上下文轮次：纳入 {_count(context.get('included_count'))} / 排除 {_count(context.get('excluded_count'))}",
    ]
    recorded_coverage = usage.get("coverage")
    coverage = recorded_coverage if isinstance(recorded_coverage, dict) else {}
    if coverage.get("legacy_request_count_unknown"):
        lines.append("部分数据仅有旧版生成总量，实际请求次数未知。")
    if not coverage.get("complete", False):
        lines.append("Token 记录不完整；已知值仅为已记录部分，不代表完整用量。")
    return "\n".join(lines)
