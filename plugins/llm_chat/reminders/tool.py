"""Strict native reminder tool and host-bound reminder identity policy."""

from __future__ import annotations

import re
import json
from typing import Any, Literal, Annotated, cast
from datetime import datetime, timezone
from dataclasses import dataclass
from collections.abc import Mapping, Iterable

from pydantic import Field, RootModel, ConfigDict, TypeAdapter, ValidationError, model_validator
from arclet.entari import Session
from pydantic_core import CoreSchema
from arclet.letoderea import Subscriber
from pydantic.json_schema import JsonSchemaValue, GetJsonSchemaHandler
from arclet.entari.plugin.model import PluginDispatcher
from arclet.entari.config.models.pyd import BaseModel

from utils.reminder_core import (
    AcceptedSchedule,
    parse_schedule,
    validate_task_text,
    schedule_autonomous,
)

from . import store
from .runtime import ReminderDecision, propose_reminder_decision
from ..core.types import JSONType
from ..agent_context import AgentAccessContext, current_agent_access
from ..core.delivery import DeliveryRejected
from ..core.tool_trace import current_tool_execution_ref

UTC = timezone.utc


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateReminder(_StrictModel):
    action: Literal["create"]
    goal: str = Field(min_length=1, max_length=1000)
    schedule: str = Field(min_length=1, max_length=128)
    timezone: str = Field(min_length=1, max_length=64)
    mode: Literal["once", "adaptive"] = "once"
    background: str = Field(default="", max_length=3000)
    continuation_state: str = Field(default="", max_length=2000)
    delivery: Literal["channel", "dm"] = "channel"
    expires_at: str | None = Field(default=None, max_length=128)


class ListReminders(_StrictModel):
    action: Literal["list"]
    statuses: list[Literal["active", "paused", "completed", "cancelled"]] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=10_000)


class UpdateReminder(_StrictModel):
    action: Literal["update"]
    task_ref: str = Field(min_length=1, max_length=300)
    goal: str | None = Field(default=None, max_length=1000)
    schedule: str | None = Field(default=None, max_length=128)
    timezone: str | None = Field(default=None, max_length=64)
    background: str | None = Field(default=None, max_length=3000)
    continuation_state: str | None = Field(default=None, max_length=2000)
    status: Literal["active", "paused"] | None = None
    expires_at: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def has_change(self) -> UpdateReminder:
        if not any(
            value is not None
            for value in (
                self.goal,
                self.schedule,
                self.timezone,
                self.background,
                self.continuation_state,
                self.status,
                self.expires_at,
            )
        ):
            raise ValueError("update requires at least one change")
        return self


class CancelReminder(_StrictModel):
    action: Literal["cancel"]
    task_ref: str = Field(min_length=1, max_length=300)
    reason: str = Field(default="cancelled_by_user", max_length=1000)


class ReminderPreferences(_StrictModel):
    action: Literal["preferences"]
    enabled: bool


class ResolveReminder(_StrictModel):
    action: Literal["resolve"]
    task_ref: str | None = Field(default=None, max_length=300)
    decision: Literal["completed", "continue", "paused"]
    schedule: str | None = Field(default=None, max_length=128)
    timezone: str | None = Field(default=None, max_length=64)
    continuation_state: str = Field(default="", max_length=2000)
    reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def schedule_matches_decision(self) -> ResolveReminder:
        if self.decision == "continue" and (self.schedule is None or self.timezone is None):
            raise ValueError("continue requires schedule and timezone")
        if self.decision != "continue" and (self.schedule is not None or self.timezone is not None):
            raise ValueError("schedule is only valid for continue")
        return self


ReminderRequest = Annotated[
    CreateReminder | ListReminders | UpdateReminder | CancelReminder | ReminderPreferences | ResolveReminder,
    Field(discriminator="action"),
]
REMINDER_REQUEST_ADAPTER = TypeAdapter(ReminderRequest)


def _inline_reminder_refs(value: Any, definitions: Mapping[str, Any], seen: frozenset[str] = frozenset()) -> Any:
    if isinstance(value, list):
        return [_inline_reminder_refs(item, definitions, seen) for item in value]
    if not isinstance(value, dict):
        return value
    ref = value.get("$ref")
    if ref is not None:
        if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
            raise ValueError("Reminder schema contains an unsupported reference")
        name = ref.removeprefix("#/$defs/")
        if name in seen or name not in definitions:
            raise ValueError("Reminder schema contains a recursive or missing reference")
        return _inline_reminder_refs(
            {**definitions[name], **{key: item for key, item in value.items() if key != "$ref"}},
            definitions,
            seen | {name},
        )
    return {key: _inline_reminder_refs(item, definitions, seen) for key, item in value.items()}


class ReminderRequestBody(RootModel[ReminderRequest]):
    """Pydantic root wrapper so Agno advertises the actual action schema."""

    model_config = ConfigDict(strict=True)

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        # Agno removes $defs but does not resolve oneOf references. Advertise
        # the same Pydantic union as an inline schema on the native Function.
        del core_schema, handler
        schema = REMINDER_REQUEST_ADAPTER.json_schema()
        definitions = schema.pop("$defs", {})
        discriminator = schema.get("discriminator")
        if isinstance(discriminator, dict):
            discriminator.pop("mapping", None)
        schema["title"] = cls.__name__
        return cast(JsonSchemaValue, _inline_reminder_refs(schema, definitions))


@dataclass(frozen=True, slots=True)
class BoundReminderOwner:
    """Current transport identity; model input never supplies these fields."""

    access: AgentAccessContext
    platform: str
    account_id: str
    scope_id: int
    channel_id: str
    unified_user_id: str
    platform_user_id: str
    source_turn_ref: str
    source_event_ref: str
    direct: bool


_DM_WORDS = re.compile(r"(?i)(?:\bdm\b|\bprivate\b|\bdirect\b|私聊|私信|悄悄话)")


def validate_reminder_request(value: object) -> object:
    """Validate one strict discriminated request before any durable effect."""

    try:
        return REMINDER_REQUEST_ADAPTER.validate_python(
            value.root if isinstance(value, ReminderRequestBody) else value, strict=True
        )
    except ValidationError as exc:
        raise DeliveryRejected("reminder request must be a strict action-specific object") from exc


def reminder_request_json_schema() -> dict[str, object]:
    """Return the exact native request schema used by tests and diagnostics."""

    return cast(dict[str, object], REMINDER_REQUEST_ADAPTER.json_schema())


def _text(value: object, *, field: str, required: bool = False) -> str:
    try:
        if field == "goal":
            return validate_task_text(value, background="", state="").goal
        if field == "background":
            return validate_task_text("placeholder", background=value, state="").background
        if field in {"continuation_state", "state"}:
            return validate_task_text("placeholder", background="", state=value).state
        if not isinstance(value, str):
            raise ValueError(f"{field} must be plain text")
        if required and not value.strip():
            raise ValueError(f"{field} must not be empty")
        if len(value) > 1_000:
            raise ValueError(f"{field} exceeds its character limit")
        return validate_task_text("placeholder", background="", state=value).state
    except Exception as exc:
        raise DeliveryRejected(str(exc)) from None


def _now() -> datetime:
    return datetime.now(UTC)


def _clean_id(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _event(session: Session) -> Any:
    return getattr(session, "event", None)


def _session_user(session: Session) -> Any:
    event = _event(session)
    return getattr(event, "user", None) or getattr(session, "user", None)


def _session_channel(session: Session) -> Any:
    channel = getattr(session, "channel", None)
    if channel is not None:
        return channel
    event = _event(session)
    return getattr(event, "channel", None)


def _is_direct(session: Session) -> bool:
    channel = _session_channel(session)
    if bool(getattr(channel, "direct", False)):
        return True
    value = getattr(channel, "type", "")
    value = getattr(value, "value", value)
    return str(value).casefold() in {"direct", "private", "dm", "1"}


def _bind_owner(session: Session) -> BoundReminderOwner:
    access = current_agent_access()
    if access is None or access.scope_id <= 0 or access.turn_id <= 0 or not access.user_id.strip():
        raise DeliveryRejected("reminder requires a verified active agent access context")
    account = getattr(session, "account", None)
    channel = _session_channel(session)
    if account is None or channel is None:
        raise DeliveryRejected("reminder requires the current verified Session")
    platform = _clean_id(getattr(account, "platform", ""))
    account_id = _clean_id(getattr(account, "self_id", ""))
    if not account_id:
        self_info = getattr(account, "self_info", None)
        account_id = _clean_id(getattr(getattr(self_info, "user", None), "id", ""))
    channel_id = _clean_id(getattr(channel, "id", ""))
    if not platform or not account_id or not channel_id:
        raise DeliveryRejected("current account and channel identity are unavailable")
    user = _session_user(session)
    platform_user_id = _clean_id(getattr(user, "id", ""))
    event = _event(session)
    message = getattr(event, "message", None)
    event_ref = _clean_id(getattr(message, "id", ""))
    return BoundReminderOwner(
        access=access,
        platform=platform,
        account_id=account_id,
        scope_id=access.scope_id,
        channel_id=channel_id,
        unified_user_id=access.user_id,
        platform_user_id=platform_user_id,
        source_turn_ref=str(access.turn_id),
        source_event_ref=event_ref,
        direct=_is_direct(session),
    )


def _require_capability(access: AgentAccessContext, action: str) -> None:
    capabilities = access.capability_names
    if not ("reminder" in capabilities or "reminder.manage" in capabilities or f"reminder.{action}" in capabilities):
        raise DeliveryRejected("reminder is not enabled for this generation")


def _require_model_schedule(
    value: str,
    timezone_name: str,
    *,
    now: datetime,
    access: AgentAccessContext,
    previous_wakeups: Iterable[datetime] = (),
) -> AcceptedSchedule:
    try:
        explicit = parse_schedule(value, timezone_name, now=now)
        if access.trigger_kind == "scheduled":
            return schedule_autonomous(
                explicit.utc,
                timezone_name,
                previous_wakeups=previous_wakeups,
                now=now,
            )
        return explicit
    except Exception as exc:
        raise DeliveryRejected(str(exc)) from None


def _expiry(value: str | None, timezone_name: str, *, now: datetime, due: datetime) -> datetime | None:
    if value is None:
        return None
    try:
        candidate = parse_schedule(value, timezone_name, now=now).utc
    except Exception as exc:
        raise DeliveryRejected(str(exc)) from None
    if candidate <= due:
        raise DeliveryRejected("expires_at must be after the scheduled time")
    return candidate


def _task_payload(task: Any, *, effect: str) -> dict[str, JSONType]:
    due = getattr(task, "due_at", None)
    if isinstance(due, datetime):
        if due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        due_text = due.astimezone(UTC).isoformat()
    else:
        due_text = ""
    return {
        "task_ref": str(task.task_ref),
        "status": str(task.status),
        "revision": int(task.revision),
        "due_at": due_text,
        "timezone": str(task.timezone),
        "mode": str(task.mode),
        "effect": effect,
    }


def _list_payload(tasks: list[Any]) -> dict[str, JSONType]:
    return {
        "items": [
            {
                "task_ref": str(task.task_ref),
                "status": str(task.status),
                "revision": int(task.revision),
                "due_at": (
                    (task.due_at.replace(tzinfo=UTC) if task.due_at.tzinfo is None else task.due_at)
                    .astimezone(UTC)
                    .isoformat()
                ),
                "timezone": str(task.timezone),
                "mode": str(task.mode),
            }
            for task in tasks
        ],
        "effect": "observed",
    }


def _json(payload: dict[str, JSONType]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


async def _owned_task(owner: BoundReminderOwner, task_ref: str) -> Any:
    task = await store.get_task(
        task_ref,
        platform=owner.platform,
        account_id=owner.account_id,
        unified_user_id=owner.unified_user_id,
        platform_user_id=owner.platform_user_id or None,
    )
    if task is None:
        raise DeliveryRejected("unknown reminder task")
    return task


def _require_dm(owner: BoundReminderOwner, *, require_direct: bool = False) -> None:
    access = owner.access
    if require_direct and not owner.direct:
        raise DeliveryRejected("DM reminders require the current direct channel")
    if not owner.platform_user_id:
        raise DeliveryRejected("DM reminders require a verified target user")
    if access.trigger_kind != "scheduled" and not (
        "reminder.dm" in access.capability_names or _DM_WORDS.search(access.raw_user_text)
    ):
        raise DeliveryRejected("DM delivery requires explicit authorization for the current user")


def _require_scheduled_task(owner: BoundReminderOwner, task: Any) -> None:
    access = owner.access
    if access.trigger_kind != "scheduled" or not access.reminder_task_ref:
        raise DeliveryRejected("resolve is available only in a scheduled reminder generation")
    if access.reminder_task_ref != task.task_ref:
        raise DeliveryRejected("reminder task does not match the current scheduled generation")
    target = str(task.delivery_target_type)
    if target == "dm":
        _require_dm(owner, require_direct=True)
        if task.delivery_channel_id != owner.channel_id or task.delivery_user_id != owner.platform_user_id:
            raise DeliveryRejected("scheduled DM target does not match the current Session")
    elif task.delivery_channel_id != owner.channel_id:
        raise DeliveryRejected("scheduled channel target does not match the current Session")


async def _create(owner: BoundReminderOwner, request: CreateReminder) -> dict[str, JSONType]:
    if owner.access.trigger_kind == "scheduled":
        raise DeliveryRejected("scheduled generations may resolve their current task, not create another task")
    goal = _text(request.goal, field="goal", required=True)
    background = _text(request.background, field="background")
    continuation = _text(request.continuation_state, field="continuation_state")
    now = _now()
    schedule = _require_model_schedule(request.schedule, request.timezone, now=now, access=owner.access)
    expires = _expiry(request.expires_at, request.timezone, now=now, due=schedule.utc)
    if request.delivery == "dm":
        _require_dm(owner)
        target_type = "dm"
        delivery_channel = ""
        delivery_user = owner.platform_user_id
        authorized = True
    else:
        target_type = "channel"
        delivery_channel = owner.channel_id
        delivery_user = owner.platform_user_id
        authorized = False
    operation_id = current_tool_execution_ref()
    if not operation_id:
        raise DeliveryRejected("reminder durable effects require a tracked tool execution")
    try:
        task = await store.create_task(
            operation_id=operation_id,
            platform=owner.platform,
            account_id=owner.account_id,
            unified_user_id=owner.unified_user_id,
            platform_user_id=owner.platform_user_id,
            source_scope_id=owner.scope_id,
            source_channel_id=owner.channel_id,
            delivery_target_type=target_type,
            delivery_channel_id=delivery_channel,
            delivery_user_id=delivery_user,
            dm_authorized=authorized,
            origin="user",
            mode=request.mode,
            goal=goal,
            background=background,
            continuation_state=continuation,
            timezone_name=schedule.timezone_name,
            due_at=schedule.utc,
            expires_at=expires,
            source_turn_ref=owner.source_turn_ref,
            source_event_ref=owner.source_event_ref,
            source_execution_ref=operation_id,
        )
    except Exception as exc:
        raise DeliveryRejected(str(exc)) from None
    return _task_payload(task, effect="confirmed")


async def _list(owner: BoundReminderOwner, request: ListReminders) -> dict[str, JSONType]:
    if owner.access.trigger_kind == "scheduled":
        raise DeliveryRejected("scheduled generations may only resolve their current reminder")
    values = tuple(request.statuses) or None
    tasks = await store.list_tasks(
        account_id=owner.account_id,
        unified_user_id=owner.unified_user_id,
        platform=owner.platform,
        platform_user_id=owner.platform_user_id or None,
        statuses=values,
        limit=request.limit,
        offset=request.offset,
    )
    return _list_payload(tasks)


async def _update(owner: BoundReminderOwner, request: UpdateReminder) -> dict[str, JSONType]:
    if owner.access.trigger_kind == "scheduled":
        raise DeliveryRejected("scheduled generations may not update task state directly")
    task = await _owned_task(owner, request.task_ref)
    changed = any(
        value is not None
        for value in (
            request.goal,
            request.schedule,
            request.background,
            request.continuation_state,
            request.expires_at,
            request.timezone,
        )
    )
    if request.status is not None and changed:
        raise DeliveryRejected("status changes must be submitted separately from field changes")
    operation_id = current_tool_execution_ref()
    if not operation_id:
        raise DeliveryRejected("reminder durable effects require a tracked tool execution")
    try:
        if request.status is not None:
            updated = (
                await store.pause_task(
                    request.task_ref,
                    operation_id=operation_id,
                    expected_revision=int(task.revision),
                    account_id=owner.account_id,
                    unified_user_id=owner.unified_user_id,
                    platform=owner.platform,
                    platform_user_id=owner.platform_user_id or None,
                )
                if request.status == "paused"
                else await store.resume_task(
                    request.task_ref,
                    operation_id=operation_id,
                    expected_revision=int(task.revision),
                    account_id=owner.account_id,
                    unified_user_id=owner.unified_user_id,
                    platform=owner.platform,
                    platform_user_id=owner.platform_user_id or None,
                )
            )
            return _task_payload(updated, effect="confirmed")
        now = _now()
        timezone_name = request.timezone or str(task.timezone)
        due = (
            _require_model_schedule(request.schedule, timezone_name, now=now, access=owner.access).utc
            if request.schedule is not None
            else None
        )
        if request.goal is not None:
            _text(request.goal, field="goal", required=True)
        if request.background is not None:
            _text(request.background, field="background")
        if request.continuation_state is not None:
            _text(request.continuation_state, field="continuation_state")
        current_due = task.due_at.replace(tzinfo=UTC) if task.due_at.tzinfo is None else task.due_at
        expires = _expiry(request.expires_at, timezone_name, now=now, due=due or current_due)
        updated = await store.update_task(
            request.task_ref,
            operation_id=operation_id,
            expected_revision=int(task.revision),
            account_id=owner.account_id,
            unified_user_id=owner.unified_user_id,
            platform=owner.platform,
            platform_user_id=owner.platform_user_id or None,
            goal=request.goal,
            background=request.background,
            continuation_state=request.continuation_state,
            timezone_name=timezone_name if request.timezone is not None else None,
            due_at=due,
            expires_at=expires,
        )
    except Exception as exc:
        raise DeliveryRejected(str(exc)) from None
    return _task_payload(updated, effect="confirmed")


async def _cancel(owner: BoundReminderOwner, request: CancelReminder) -> dict[str, JSONType]:
    if owner.access.trigger_kind == "scheduled":
        raise DeliveryRejected("scheduled generations may not cancel task state directly")
    task = await _owned_task(owner, request.task_ref)
    operation_id = current_tool_execution_ref()
    if not operation_id:
        raise DeliveryRejected("reminder durable effects require a tracked tool execution")
    try:
        cancelled = await store.cancel_task(
            request.task_ref,
            operation_id=operation_id,
            expected_revision=int(task.revision),
            account_id=owner.account_id,
            unified_user_id=owner.unified_user_id,
            platform=owner.platform,
            platform_user_id=owner.platform_user_id or None,
            reason=_text(request.reason, field="reason"),
        )
    except Exception as exc:
        raise DeliveryRejected(str(exc)) from None
    return _task_payload(cancelled, effect="confirmed")


async def _preferences(owner: BoundReminderOwner, request: ReminderPreferences) -> dict[str, JSONType]:
    if owner.access.trigger_kind == "scheduled":
        raise DeliveryRejected("scheduled generations may not change reminder preferences")
    operation_id = current_tool_execution_ref()
    if not operation_id:
        raise DeliveryRejected("reminder durable effects require a tracked tool execution")
    try:
        preference = await store.set_preference(
            operation_id=operation_id,
            account_id=owner.account_id,
            unified_user_id=owner.unified_user_id,
            enabled=request.enabled,
        )
    except Exception as exc:
        raise DeliveryRejected(str(exc)) from None
    return {
        "status": "enabled" if preference.enabled else "disabled",
        "effect": "confirmed",
    }


async def _resolve(owner: BoundReminderOwner, request: ResolveReminder) -> dict[str, JSONType]:
    task_ref = request.task_ref or owner.access.reminder_task_ref or ""
    task = await _owned_task(owner, task_ref)
    _require_scheduled_task(owner, task)
    timezone_name = request.timezone or str(task.timezone)
    now = _now()
    next_due: datetime | None = None
    if request.decision == "continue":
        if str(task.mode) != "adaptive":
            raise DeliveryRejected("one-shot reminders cannot continue")
        assert request.schedule is not None
        history = await store.get_wakeup_history(task.task_ref)
        next_due = _require_model_schedule(
            request.schedule,
            timezone_name,
            now=now,
            access=owner.access,
            previous_wakeups=history,
        ).utc
    continuation = _text(request.continuation_state, field="continuation_state")
    reason = _text(request.reason, field="reason")
    status = "active" if request.decision == "continue" else request.decision
    decision = ReminderDecision(
        task_ref=task.task_ref,
        status=status,
        next_due_at=next_due,
        timezone_name=timezone_name,
        continuation_state=continuation,
        reason=reason,
    )
    try:
        propose_reminder_decision(decision)
    except ValueError as exc:
        raise DeliveryRejected(str(exc)) from None
    return {
        "task_ref": task.task_ref,
        "status": status,
        "revision": int(task.revision),
        "effect": "none",
        "proposed": True,
    }


async def reminder(session: Session, request: ReminderRequestBody) -> str:
    """Manage one host-bound reminder through a strict action request."""

    owner = _bind_owner(session)
    parsed = validate_reminder_request(request)
    _require_capability(owner.access, cast(Any, parsed).action)
    if isinstance(parsed, CreateReminder):
        payload = await _create(owner, parsed)
    elif isinstance(parsed, ListReminders):
        payload = await _list(owner, parsed)
    elif isinstance(parsed, UpdateReminder):
        payload = await _update(owner, parsed)
    elif isinstance(parsed, CancelReminder):
        payload = await _cancel(owner, parsed)
    elif isinstance(parsed, ReminderPreferences):
        payload = await _preferences(owner, parsed)
    elif isinstance(parsed, ResolveReminder):
        payload = await _resolve(owner, parsed)
    else:
        raise DeliveryRejected("unsupported reminder action")
    return _json(payload)


def register_reminder_tools(dispatcher: PluginDispatcher[JSONType]) -> Subscriber[JSONType]:
    """Register the sole native reminder Function under its utility owner."""

    from ..tools._registration import register_tool

    return register_tool(dispatcher, reminder)


__all__ = [
    "CancelReminder",
    "CreateReminder",
    "ReminderRequestBody",
    "ListReminders",
    "REMINDER_REQUEST_ADAPTER",
    "ReminderPreferences",
    "ReminderRequest",
    "ResolveReminder",
    "UpdateReminder",
    "register_reminder_tools",
    "reminder",
    "reminder_request_json_schema",
    "validate_reminder_request",
]
