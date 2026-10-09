"""Native self-service reminder controls for public chat and the user's own DM."""

from __future__ import annotations

import re
from uuid import uuid4
from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dataclasses import dataclass

from satori import ChannelType
from arclet.entari import Session, MessageCreatedEvent, command
from arclet.alconna import Args
from arclet.entari.filter.parse import FilterPropagator
from arclet.entari.plugin.model import Plugin

from .identity import resolve_chat_identity
from .reminders import store
from .session_manager import scope_identity

# A separate command owner lets the same user's DM bypass the root's public-only
# chat filter. This owner registers commands only, never an automatic DM handler.
_owner = Plugin.current()
if _owner is not None and _owner.module.__name__ == __name__:
    _scope = getattr(_owner, "_scope", None)
    if _scope is not None:
        _scope.propagators[:] = [item for item in _scope.propagators if not isinstance(item, FilterPropagator)]

_TASK_REF = re.compile(r"reminder_[A-Za-z0-9_-]{8,64}\Z")
_STATES = {
    "active": "待提醒",
    "paused": "已暂停",
    "completed": "已完成",
    "cancelled": "已取消",
}
_ACTIONS = {
    "pause": (store.pause_task, "已暂停"),
    "resume": (store.resume_task, "已恢复"),
    "cancel": (store.cancel_task, "已取消"),
}
_INVALID_USER = "无法确认当前发送者，请从本人账号发送指令。"
_UNKNOWN_RESULT = "操作结果未确认，请先用 llmchat reminder list 查看，不要立即重试。"


@dataclass(frozen=True, slots=True)
class _Owner:
    platform: str
    account_id: str
    unified_user_id: str
    platform_user_id: str

    def task_scope(self) -> dict[str, str]:
        return {
            "platform": self.platform,
            "account_id": self.account_id,
            "unified_user_id": self.unified_user_id,
            "platform_user_id": self.platform_user_id,
        }


async def _current_owner(session: Session) -> _Owner | None:
    event = getattr(session, "event", None)
    if (
        not isinstance(event, MessageCreatedEvent)
        or getattr(event, "message", None) is None
        or getattr(event, "user", None) is None
        or getattr(event, "channel", None) is None
    ):
        return None
    user = getattr(session, "user", None)
    channel = getattr(session, "channel", None)
    if (
        user is None
        or channel is None
        or channel.type not in (ChannelType.TEXT, ChannelType.DIRECT)
        or not str(getattr(channel, "id", "") or "").strip()
        or str(channel.id) != str(event.channel.id)
        or str(getattr(user, "id", "") or "").strip()
        != str(getattr(getattr(event, "user", None), "id", "") or "").strip()
    ):
        return None
    scoped = scope_identity(session)
    if not scoped.platform.strip() or not scoped.account_id.strip():
        return None
    identity = await resolve_chat_identity(session)
    if not identity.user_id.strip():
        return None
    return _Owner(
        platform=scoped.platform,
        account_id=scoped.account_id,
        unified_user_id=identity.user_id,
        platform_user_id=str(user.id),
    )


def _short(value: object, limit: int = 96) -> str:
    cleaned = " ".join("".join(character if character.isprintable() else " " for character in str(value or "")).split())
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")


def _when(task: object) -> str:
    due = task.due_at.replace(tzinfo=timezone.utc) if task.due_at.tzinfo is None else task.due_at
    try:
        zone = ZoneInfo(str(task.timezone))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        zone = timezone.utc
    local = due.astimezone(zone)
    return f"{local:%Y-%m-%d %H:%M} {local:%z}"


def _line(task: object) -> str:
    return f"{task.task_ref} · {_STATES.get(task.status, '状态未知')} · {_when(task)} · {_short(task.goal)}"


def _operation_id() -> str:
    return f"reminder_command_{uuid4().hex}"


async def _confirmed(operation_id: str, action: str, task_ref: str | None = None) -> bool:
    """Resolve an uncertain store reply by reading its committed mutation, never replaying it."""
    try:
        mutation = await store.get_mutation(operation_id)
    except Exception:
        return False
    return mutation is not None and mutation.action == action and mutation.task_ref == task_ref


async def _change(session: Session, task_ref: str, action: str) -> str:
    owner = await _current_owner(session)
    if owner is None:
        return _INVALID_USER
    if not _TASK_REF.fullmatch(task_ref.strip()):
        return "提醒编号格式无效；请从 llmchat reminder list 复制编号。"
    task_ref = task_ref.strip()
    task = await store.get_task(task_ref, **owner.task_scope())
    if task is None:
        return "未找到属于你的提醒；请用 llmchat reminder list 查看。"
    if action == "resume" and task.status != "paused":
        return "此提醒不在暂停状态；请先查看最新列表。"
    if action == "pause" and task.status != "active":
        return "此提醒不在待提醒状态；请先查看最新列表。"
    if action == "cancel" and task.status not in ("active", "paused"):
        return "此提醒已经结束；请先查看最新列表。"
    mutate, label = _ACTIONS[action]
    operation_id = _operation_id()
    try:
        updated = await mutate(
            task_ref,
            operation_id=operation_id,
            expected_revision=task.revision,
            **owner.task_scope(),
        )
    except store.ReminderNotFound:
        return "未找到属于你的提醒；请用 llmchat reminder list 查看。"
    except store.ReminderConflict:
        return "提醒状态已变化；请刷新列表后再操作。"
    except Exception:
        if await _confirmed(operation_id, action, task_ref):
            return f"{label}；请用 llmchat reminder list 查看当前状态。"
        return _UNKNOWN_RESULT
    return f"{label}：{_line(updated)}"


async def _pause_autonomous(owner: _Owner) -> tuple[int, bool]:
    try:
        tasks = await store.list_tasks(**owner.task_scope(), statuses=("active",), limit=100)
    except Exception:
        return 0, True
    paused = 0
    uncertain = False
    for task in tasks:
        if task.origin == "user":
            continue
        operation_id = _operation_id()
        try:
            await store.pause_task(
                task.task_ref,
                operation_id=operation_id,
                expected_revision=task.revision,
                **owner.task_scope(),
            )
        except (store.ReminderNotFound, store.ReminderConflict):
            uncertain = True
        except Exception:
            if await _confirmed(operation_id, "pause", task.task_ref):
                paused += 1
            else:
                uncertain = True
        else:
            paused += 1
    return paused, uncertain


async def _preference(session: Session, enabled: bool) -> str:
    owner = await _current_owner(session)
    if owner is None:
        return _INVALID_USER
    operation_id = _operation_id()
    try:
        await store.set_preference(
            operation_id=operation_id,
            account_id=owner.account_id,
            unified_user_id=owner.unified_user_id,
            enabled=enabled,
        )
    except store.ReminderConflict:
        return "偏好状态已变化；请重新查询后再操作。"
    except Exception:
        if not await _confirmed(operation_id, "preference"):
            return _UNKNOWN_RESULT
    if enabled:
        return "已允许未来自主联系；已暂停的任务不会自动恢复。"
    paused, uncertain = await _pause_autonomous(owner)
    result = "已关闭自主联系；明确委托的提醒保持原状态。"
    if paused:
        result += f"已暂停 {paused} 项自主提醒。"
    if uncertain:
        result += "部分自主提醒状态未确认，请用 llmchat reminder list 查看。"
    return result


@command.on("llmchat reminder list {page}", args={"page": Args["page?", int]})
async def reminder_list(session: Session, page: int = 1) -> str:
    owner = await _current_owner(session)
    if owner is None:
        return _INVALID_USER
    if not 1 <= page <= 50:
        return "页码应在 1 到 50 之间。"
    tasks = await store.list_tasks(**owner.task_scope(), limit=11, offset=(page - 1) * 10)
    preference = await store.get_preference(account_id=owner.account_id, unified_user_id=owner.unified_user_id)
    header = "自主联系：" + ("已关闭" if preference is not None and not preference.enabled else "已开启")
    if not tasks:
        return header + ("\n本页暂无提醒。" if page > 1 else "\n暂无提醒。")
    lines = [header, f"提醒第 {page} 页：", *(_line(task) for task in tasks[:10])]
    if len(tasks) > 10:
        lines.append(f"更多提醒：llmchat reminder list {page + 1}")
    return "\n".join(lines)


@command.on("llmchat reminder pause {task_ref}", args={"task_ref": Args["task_ref", str]})
async def reminder_pause(session: Session, task_ref: str) -> str:
    return await _change(session, task_ref, "pause")


@command.on("llmchat reminder resume {task_ref}", args={"task_ref": Args["task_ref", str]})
async def reminder_resume(session: Session, task_ref: str) -> str:
    return await _change(session, task_ref, "resume")


@command.on("llmchat reminder cancel {task_ref}", args={"task_ref": Args["task_ref", str]})
async def reminder_cancel(session: Session, task_ref: str) -> str:
    return await _change(session, task_ref, "cancel")


@command.on("llmchat reminder off")
async def reminder_off(session: Session) -> str:
    return await _preference(session, False)


@command.on("llmchat reminder on")
async def reminder_on(session: Session) -> str:
    return await _preference(session, True)


__all__ = [
    "reminder_list",
    "reminder_pause",
    "reminder_resume",
    "reminder_cancel",
    "reminder_off",
    "reminder_on",
]
