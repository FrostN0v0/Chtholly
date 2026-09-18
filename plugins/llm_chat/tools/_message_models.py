"""Strict, tagged model-controlled message composition segments."""

from __future__ import annotations

from typing import Literal, Annotated

from pydantic import Field, ConfigDict, TypeAdapter
from arclet.entari.config.models.pyd import BaseModel


class _Segment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TextSegment(_Segment):
    type: Literal["text"]
    text: str = Field(description="Literal text; spacing and punctuation are preserved, never parsed as markup.")


class MentionSegment(_Segment):
    type: Literal["mention"]
    target: str = Field(
        pattern=r"^(current_user|participant_[0-9a-f]{10})$",
        description="current_user or one exact opaque current-channel participant_ref; never a platform ID.",
    )


class LinkSegment(_Segment):
    type: Literal["link"]
    url: str = Field(description="Public HTTP(S) destination, without credentials or private hosts.")
    text: str = Field(description="Literal visible link label.")


class EmojiSegment(_Segment):
    type: Literal["emoji"]
    id: str = Field(description="A supported native emoji ID for the current platform, not Unicode or markup.")


class MediaSegment(_Segment):
    type: Literal["media"]
    media_ref: str = Field(
        pattern=r"^media_[0-9a-f]{32}$",
        description="Exact media_ref returned by a preparation tool in this generation.",
    )


class BreakSegment(_Segment):
    type: Literal["break"]


class StyleSegment(_Segment):
    type: Literal["style"]
    style: Literal["bold", "italic", "underline", "strike", "spoiler", "code"]
    text: str = Field(description="Literal styled text, without nested markup.")


MessageSegment = Annotated[
    TextSegment | MentionSegment | LinkSegment | EmojiSegment | MediaSegment | BreakSegment | StyleSegment,
    Field(discriminator="type"),
]
MESSAGE_SEGMENTS = TypeAdapter(list[MessageSegment])


__all__ = [
    "TextSegment",
    "MentionSegment",
    "LinkSegment",
    "EmojiSegment",
    "MediaSegment",
    "BreakSegment",
    "StyleSegment",
    "MessageSegment",
    "MESSAGE_SEGMENTS",
]
