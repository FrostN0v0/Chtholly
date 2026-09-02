"""Best-effort message reaction feedback for one llm_chat turn."""

from __future__ import annotations

from typing import Literal
import asyncio
from contextlib import suppress, contextmanager
from contextvars import ContextVar
from dataclasses import field, dataclass
from collections.abc import Callable, Iterator, Awaitable

from arclet.entari import Session

ReactionStage = Literal[
    "processing",
    "thinking",
    "researching",
    "creating_media",
    "recoverable_error",
    "success",
    "partial",
    "failed",
    "superseded",
    "declined",
]
TerminalReactionStage = Literal["success", "partial", "failed", "superseded", "declined"]
WarningSink = Callable[[str], object]

_REACTION_TIMEOUT_SECONDS = 2.0
_REACTION_EMOJIS: dict[ReactionStage, str] = {
    "processing": "125",
    "thinking": "314",
    "researching": "269",
    "creating_media": "294",
    "recoverable_error": "174",
    "success": "124",
    "partial": "27",
    "failed": "123",
    "superseded": "129",
    "declined": "284",
}
_RESEARCH_TOOLS = frozenset(
    {
        "describe_channel_image",
        "describe_channel_participant_avatar",
        "find_channel_participants",
        "list_sessions",
        "list_tool_executions",
        "read_agent_event",
        "read_channel_messages",
        "read_session_handoff",
        "read_tool_execution",
        "read_web_page",
        "screenshot_web_page",
        "web_search",
    }
)
_MEDIA_TOOLS = frozenset(
    {
        "generate_image",
        "html2pic",
        "jinja2pic",
        "list_image_resources",
        "list_tts_voices",
        "markdown2pic",
        "send_audio",
        "send_channel_image",
        "send_external_image",
        "send_image",
        "speak",
        "tag_image",
    }
)


def _tool_stage(tool_name: str) -> ReactionStage:
    if tool_name in _RESEARCH_TOOLS:
        return "researching"
    if tool_name in _MEDIA_TOOLS:
        return "creating_media"
    return "thinking"


@dataclass(slots=True)
class MessageReactionFeedback:
    """Replace one transient reaction with a bounded terminal status."""

    session: Session
    warn: WarningSink
    timeout_seconds: float = _REACTION_TIMEOUT_SECONDS
    _current_emoji: str | None = field(default=None, init=False)
    _disabled: bool = field(default=False, init=False)
    _terminal: bool = field(default=False, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def set_stage(self, stage: ReactionStage) -> None:
        await self._replace(stage, terminal=False)

    @property
    def terminal(self) -> bool:
        return self._terminal

    async def tool_started(self, tool_name: str) -> None:
        await self.set_stage(_tool_stage(tool_name))

    async def tool_failed(self) -> None:
        await self.set_stage("recoverable_error")

    async def finish(self, stage: TerminalReactionStage) -> None:
        await self._replace(stage, terminal=True)

    async def clear_transient(self) -> None:
        async with self._lock:
            if self._disabled or self._terminal or self._current_emoji is None:
                return
            if await self._call(
                self.session.reaction_delete(self._current_emoji),
                action="delete",
                stage="cancelled",
            ):
                self._current_emoji = None

    async def _replace(self, stage: ReactionStage, *, terminal: bool) -> None:
        emoji_id = _REACTION_EMOJIS[stage]
        async with self._lock:
            if self._terminal:
                return
            if self._disabled:
                if terminal:
                    self._terminal = True
                return
            if self._current_emoji == emoji_id:
                self._terminal = terminal
                return
            if self._current_emoji is not None:
                if not await self._call(
                    self.session.reaction_delete(self._current_emoji),
                    action="delete",
                    stage=stage,
                ):
                    if terminal:
                        self._terminal = True
                    return
                self._current_emoji = None
            if not await self._call(
                self.session.reaction_create(emoji_id),
                action="create",
                stage=stage,
            ):
                if terminal:
                    self._terminal = True
                return
            self._current_emoji = emoji_id
            self._terminal = terminal

    async def _call(
        self,
        operation: Awaitable[None],
        *,
        action: str,
        stage: str,
    ) -> bool:
        try:
            await asyncio.wait_for(operation, timeout=max(0.1, float(self.timeout_seconds)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._disabled = True
            self.warn(f"message reaction {action} failed at {stage}: {type(exc).__name__}")
            return False
        return True


_ACTIVE_REACTION_FEEDBACK: ContextVar[MessageReactionFeedback | None] = ContextVar(
    "llm_chat_reaction_feedback",
    default=None,
)


@contextmanager
def llm_chat_reaction_scope(feedback: MessageReactionFeedback) -> Iterator[None]:
    token = _ACTIVE_REACTION_FEEDBACK.set(feedback)
    try:
        yield
    finally:
        _ACTIVE_REACTION_FEEDBACK.reset(token)


def current_reaction_feedback() -> MessageReactionFeedback | None:
    return _ACTIVE_REACTION_FEEDBACK.get()


async def settle_reaction_update(operation: Awaitable[None]) -> None:
    """Finish one bounded reaction update despite caller cancellation."""

    task = asyncio.ensure_future(operation)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(asyncio.CancelledError):
            await task
