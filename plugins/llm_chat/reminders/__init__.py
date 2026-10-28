"""Persistent reminder storage and the utility-owned native reminder tool."""

from hashlib import sha256
from pathlib import Path

from .tool import (
    REMINDER_REQUEST_ADAPTER,
    ListReminders,
    CancelReminder,
    CreateReminder,
    UpdateReminder,
    ResolveReminder,
    ReminderPreferences,
    ReminderRequestBody,
    reminder,
    register_reminder_tools,
    validate_reminder_request,
    reminder_request_json_schema,
)
from .store import (
    ReminderClaim,
    ReminderConflict,
    ReminderNotFound,
    get_task,
    list_tasks,
    pause_task,
    cancel_task,
    create_task,
    resume_task,
    update_task,
    get_mutation,
    recover_stale,
    try_claim_due,
    finalize_claim,
    get_preference,
    set_preference,
    record_confirmed,
    get_wakeup_history,
    finalize_occurrence,
    record_model_started,
    record_external_started,
    record_confirmed_receipt,
    record_transport_started,
)
from .runtime import (
    ReminderDecision,
    reminder_decision_scope,
    current_reminder_decision,
    propose_reminder_decision,
)

# Entari imports this package as a real utility subplugin from the llm_chat
# root. Direct imports in tests and storage workers do not create an owner.
try:
    from arclet.entari import metadata
    from arclet.entari.plugin import PluginRole
    from arclet.entari.plugin.model import Plugin

    _owner = Plugin.current()
except (ImportError, LookupError):
    _owner = None

registered_tools: list[str] = []
registered_tool_schemas: list[dict[str, str]] = []
if _owner is not None and _owner.module.__name__ == __name__:
    from .tool import register_reminder_tools as _register_reminder_tools
    from ..agno_compat import register_llm_chat_tool
    from ..tools._registration import create_tool_dispatcher

    metadata(
        name="llm_chat_reminders",
        author=[{"name": "FrostN0v0"}],
        version="0.1.0",
        description="Host-bound persistent reminder management for llm_chat.",
        role=PluginRole.UTILITY,
    )
    _dispatcher = create_tool_dispatcher("reminder_tools")
    if register_llm_chat_tool not in _dispatcher.register_hooks:
        _dispatcher.register_hooks.append(register_llm_chat_tool)
    _register_reminder_tools(_dispatcher)
    registered_tools.append("reminder")
    _tool_path = Path(__file__).with_name("tool.py")
    registered_tool_schemas.append({"name": "reminder", "source_hash": sha256(_tool_path.read_bytes()).hexdigest()})


__all__ = [
    "CancelReminder",
    "CreateReminder",
    "ListReminders",
    "REMINDER_REQUEST_ADAPTER",
    "ReminderClaim",
    "ReminderConflict",
    "ReminderDecision",
    "ReminderNotFound",
    "ReminderPreferences",
    "ReminderRequestBody",
    "ResolveReminder",
    "UpdateReminder",
    "cancel_task",
    "create_task",
    "current_reminder_decision",
    "finalize_claim",
    "finalize_occurrence",
    "get_mutation",
    "get_preference",
    "get_wakeup_history",
    "get_task",
    "list_tasks",
    "pause_task",
    "propose_reminder_decision",
    "record_confirmed",
    "record_confirmed_receipt",
    "record_external_started",
    "record_model_started",
    "record_transport_started",
    "recover_stale",
    "register_reminder_tools",
    "reminder",
    "reminder_decision_scope",
    "reminder_request_json_schema",
    "resume_task",
    "set_preference",
    "try_claim_due",
    "update_task",
    "validate_reminder_request",
]
