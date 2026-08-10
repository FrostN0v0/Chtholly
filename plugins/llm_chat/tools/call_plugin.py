"""call_plugin LLM tool implementation."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Sequence, Awaitable

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from .support import is_command_allowed
from ..core.types import JSONType
from ._registration import register_tool

CommandExecutor = Callable[[str, Session], Awaitable[object]]


@dataclass
class CommandToolContext:
    """Mutable dependencies for whitelisted command execution."""

    allowed_commands: Sequence[str]
    execute: CommandExecutor
    log_info: Callable[[str], object]


def register_call_plugin(
    dispatcher: PluginDispatcher[JSONType],
    runtime: CommandToolContext,
) -> Subscriber[JSONType] | None:
    """Register whitelisted command execution when commands are configured."""

    if not runtime.allowed_commands:
        return None

    async def call_plugin(session: Session, command_line: str) -> str:
        """Execute one whitelisted bot command only when the user explicitly asks for command execution.

        Remove one leading '/' or '.' from the command name before passing command_line, preserve the remaining
        command and necessary arguments, and do not discover, invent, broaden, or retry commands.

        Args:
            command_line (str): Full command line, for example "echo hello".
        Returns:
            str: Command result text.
        """

        allowed, head = is_command_allowed(command_line, runtime.allowed_commands)
        if not allowed:
            return f"指令 {head or '(空)'} 不在允许列表中"
        runtime.log_info(f"call_plugin executing whitelisted command: {head}")
        result = await runtime.execute(command_line, session)
        return str(result) if result is not None else "指令已执行"

    return register_tool(dispatcher, call_plugin)
