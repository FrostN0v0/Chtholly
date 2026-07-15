"""Stable local type boundaries for LLM messages and tool payloads."""

from __future__ import annotations

from typing import Any, Literal, TypeAlias, TypedDict
from typing_extensions import NotRequired

JSONValue: TypeAlias = str | int | float | bool | None
JSONType: TypeAlias = dict[str, "JSONType"] | list["JSONType"] | JSONValue


class SystemMessage(TypedDict):
    role: Literal["system"]
    content: str
    name: NotRequired[str | None]


class UserMessage(TypedDict):
    role: Literal["user"]
    content: str | list[dict[str, Any]]
    name: NotRequired[str | None]


class AssistantMessage(TypedDict):
    role: Literal["assistant"]
    content: str | None
    tool_calls: NotRequired[list[dict[str, Any]] | None]
    reasoning_content: NotRequired[str | None]


class ToolMessage(TypedDict):
    role: Literal["tool"]
    content: str
    tool_call_id: str
    name: NotRequired[str | None]


ChatMessage: TypeAlias = SystemMessage | UserMessage | AssistantMessage | ToolMessage
