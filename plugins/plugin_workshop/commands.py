"""Operator-only native commands sharing the workshop review backend."""

from __future__ import annotations

import json
import hashlib
from collections.abc import Callable, Awaitable

from arclet.entari import Text, Plugin, Session, MessageChain, command
from arclet.letoderea import STOP
from arclet.entari.filter import superusers
from arclet.entari.plugin.model import current_plugin

from utils.plugin_workshop_core.models import Actor, WorkshopAPI, WorkshopError

_NATIVE_WARNING = "原生插件激活后拥有机器人进程的完整权限；声明权限不是限制。停用或回滚不会撤回外部消息或数据修改。"
_REVIEW = "请在已登录的 WebUI「插件工坊」审阅完整源码、差异与报告；群聊不展示源码。"
_owner = Plugin.current()


def register_commands(service: WorkshopAPI) -> None:
    if _owner is None:
        raise RuntimeError("Workshop commands require their native plugin owner")
    token = current_plugin.set(_owner)
    try:
        _register_commands(service)
    finally:
        current_plugin.reset(token)


def _register_commands(service: WorkshopAPI) -> None:
    check_operator = superusers().check

    async def actor_for(session: Session) -> Actor:
        if await check_operator(session) is STOP:
            raise WorkshopError("仅配置的超级用户可以管理插件工坊", code="operator_required", status=403)
        identity = json.dumps([session.account.platform, session.account.self_id, session.user.id], ensure_ascii=False)
        return Actor(key="operator:" + hashlib.sha256(identity.encode()).hexdigest(), is_admin=True)

    async def invoke(session: Session, action: Callable[[Actor], Awaitable[str]]) -> MessageChain:
        try:
            result = await action(await actor_for(session))
        except WorkshopError as exc:
            result = (
                "权限不足：仅配置的超级用户可以管理插件工坊。"
                if exc.code == "operator_required"
                else f"操作未完成（{exc.code}）。请在已登录的 WebUI 查看详情。"
            )
        except Exception:
            result = "插件工坊请求失败；未确认操作成功。请在已登录的 WebUI 查看状态。"
        return MessageChain([Text(result[:5000])])

    @command.on("workshop help")
    async def workshop_help(session: Session) -> MessageChain:
        """Native plugin management; approval grants full process privileges."""

        async def help_text(actor: Actor) -> str:
            return (
                "workshop list\nworkshop show <name> <version>\nworkshop approve <name> <version>\n"
                "workshop activate <name> <version>\nworkshop rollback <name> <version>\nworkshop disable <name>\n"
                "approve 是对指定不可变版本的人工授权，即明确接受完整原生权限。\n" + _NATIVE_WARNING + "\n" + _REVIEW
            )

        return await invoke(session, help_text)

    @command.on("workshop list")
    async def workshop_list(session: Session) -> MessageChain:
        """List managed projects without disclosing candidate source."""

        async def list_projects(actor: Actor) -> str:
            projects = await service.list_projects(actor, limit=25)
            if not projects:
                return "暂无插件工坊项目。\n" + _REVIEW
            lines = ["插件工坊（最多 25 项，完整列表见 WebUI）："]
            for project in projects:
                lines.append(
                    f"{project['plugin_name']} · enabled={bool(project.get('enabled'))} "
                    f"· active={project.get('active_version')}"
                )
            return "\n".join(lines) + "\n" + _REVIEW

        return await invoke(session, list_projects)

    @command.on("workshop show {name} {version}")
    async def workshop_show(session: Session, name: str, version: int) -> MessageChain:
        """Show immutable identity and acceptance outcome, never source or logs."""

        async def show(actor: Actor) -> str:
            record = await service.detail(name, version, actor)
            checks = record.report.checks if record.report else ()
            return (
                f"{record.plugin_name} v{record.version}\nSHA-256: {record.source_hash}\n"
                f"验收: {record.validation_status}（{sum(check.passed for check in checks)}/{len(checks)}）\n"
                f"审批: {'已批准' if record.approved_by else '未批准'} · 当前活动: {'是' if record.active else '否'}\n"
                f"声明命令: {', '.join(record.manifest.commands)}\n"
                f"Permissions: {len(record.manifest.permissions)} · Config: {len(record.manifest.configuration)}\n"
                + _NATIVE_WARNING
                + "\n"
                + _REVIEW
            )

        return await invoke(session, show)

    @command.on("workshop approve {name} {version}")
    async def workshop_approve(session: Session, name: str, version: int) -> MessageChain:
        """Explicitly approve this immutable version with full native process privileges."""

        async def approve(actor: Actor) -> str:
            record = await service.detail(name, version, actor)
            approved = await service.approve(name, version, record.source_hash, actor)
            return (
                f"已人工批准 {approved.plugin_name} v{approved.version}\nSHA-256: {approved.source_hash}\n尚未激活。"
                + _NATIVE_WARNING
            )

        return await invoke(session, approve)

    @command.on("workshop activate {name} {version}")
    async def workshop_activate(session: Session, name: str, version: int) -> MessageChain:
        """Activate an approved immutable version with full native privileges."""

        async def activate(actor: Actor) -> str:
            record = await service.detail(name, version, actor)
            state = await service.activate(name, version, record.source_hash, actor)
            return (
                f"Activated: {state.plugin_name} v{state.active_version}\nSHA-256: {record.source_hash}\n"
                + _NATIVE_WARNING
            )

        return await invoke(session, activate)

    @command.on("workshop rollback {name} {version}")
    async def workshop_rollback(session: Session, name: str, version: int) -> MessageChain:
        """Restore a previously active approved version, not its external effects."""

        async def rollback(actor: Actor) -> str:
            record = await service.detail(name, version, actor)
            state = await service.rollback(name, version, record.source_hash, actor)
            return (
                f"Code restored: {state.plugin_name} v{state.active_version}\nSHA-256: {record.source_hash}\n"
                + _NATIVE_WARNING
            )

        return await invoke(session, rollback)

    @command.on("workshop disable {name}")
    async def workshop_disable(session: Session, name: str) -> MessageChain:
        """Unload a managed native plugin without deleting history or external data."""

        async def disable(actor: Actor) -> str:
            state = await service.disable(name, actor)
            return (
                f"停用已完成：{state.plugin_name} · {'已启用' if state.enabled else '未启用'}；保留版本与数据。\n"
                + _NATIVE_WARNING
            )

        return await invoke(session, disable)
