"""Reply-first pacing: in group chat a member answers before running long lookups."""

from __future__ import annotations

import time
from dataclasses import field, dataclass
from collections.abc import Callable

# Lookups that only gather information. Delivery preparation, meme catalog reads and the
# clock stay free because they serve the reply itself.
RESEARCH_TOOLS = frozenset(
    {
        "web_search",
        "read_web_page",
        "read_channel_messages",
        "find_channel_participants",
        "get_channel_avatar",
        "inspect_image",
        "list_sessions",
        "read_session_handoff",
        "list_tool_executions",
        "read_agent_event",
        "read_tool_execution",
        "list_web_artifacts",
        "read_web_artifact",
        "list_workshop_plugins",
        "read_workshop_plugin",
    }
)
SOFT_LOOKUPS = 4
HARD_LOOKUPS = 8
SOFT_SECONDS = 25.0
HARD_SECONDS = 60.0


@dataclass(slots=True)
class ReplyPacing:
    """Count lookups made before anything is sent in one message-triggered generation."""

    clock: Callable[[], float] = time.monotonic
    started_at: float = field(default=0.0)
    lookups: int = 0
    nudged: bool = False

    def __post_init__(self) -> None:
        self.started_at = self.clock()

    def _elapsed(self) -> float:
        return max(0.0, self.clock() - self.started_at)

    def admit(self, tool_name: str, *, replied: bool) -> str | None:
        """Reserve one lookup, or explain why it must wait until a reply is sent."""
        if replied or tool_name not in RESEARCH_TOOLS:
            return None
        elapsed = self._elapsed()
        if self.lookups >= HARD_LOOKUPS or elapsed >= HARD_SECONDS:
            return (
                f"Reply first: nothing has been sent after {self.lookups} lookups and {round(elapsed)} s. "
                "Send what you can now with send_msg, either an answer from the evidence you already have or a short "
                "note of what you are still checking. Further lookups are allowed after something is sent."
            )
        self.lookups += 1
        return None

    def notice(self, *, replied: bool) -> str | None:
        """Return one host reminder once lookups start delaying the first reply."""
        if replied or self.nudged:
            return None
        if self.lookups < SOFT_LOOKUPS and self._elapsed() < SOFT_SECONDS:
            return None
        self.nudged = True
        return (
            "Host notice: the member is still waiting and nothing has been sent. If the evidence so far is "
            "enough, reply now; casual chat rarely needs more lookups. If the request truly needs more research, "
            "send a short note first."
        )
