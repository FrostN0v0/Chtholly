"""Typed source payloads shared by native submission tools and their schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, ConfigDict
from arclet.entari.config.models.pyd import BaseModel


class WebSourceFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(description="Relative file path, for example index.html or styles/theme.css.")
    content: str = Field(description="Complete file contents, not a filename-to-source object.")
    encoding: Literal["utf-8", "utf8", "base64"] = "utf-8"


class PluginSourceFiles(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    __pydantic_extra__: dict[str, str] = Field(init=False)
    entry_source: str = Field(
        alias="__init__.py",
        description="Complete package entry source at the root, without a plugin-name directory prefix.",
    )


class PluginCommandCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command: str = Field(description="A real invocation of a declared command, including any required arguments.")
    expected_contains: str = Field(description="Text that the command must actually return.")
    operator: bool = Field(
        default=False, description="Whether to execute as an operator. Use JSON true or false, not 1 or 0."
    )
    repeatable: bool = Field(
        default=True, description="Whether the check can safely run again after reload. Use a JSON boolean."
    )


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str
    description: str
    commands: list[str] = Field(description="Distinct bare command heads, without prefixes, arguments or spaces.")
    permissions: list[str] = Field(description="Capability declarations, not runtime grants.")
    data_description: str
    configuration: dict[str, Any] = Field(
        default_factory=dict, description="JSON configuration for this exact version."
    )
    checks: list[PluginCommandCheck] = Field(
        description="Cover every command and include at least one repeatable check."
    )
