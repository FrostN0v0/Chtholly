"""Additive durable intent for the companion channel's single dialogue owner."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, CheckConstraint
from entari_plugin_database import Base, Mapped, mapped_column


class CompanionDialogue(Base):
    __tablename__ = "chat_companion_dialogues"
    __table_args__ = (
        CheckConstraint("action IN ('observe', 'wait', 'end')", name="ck_companion_dialogue_action"),
        CheckConstraint(
            "status IN ('idle', 'scheduled', 'running', 'unknown', 'cancelled', 'failed')",
            name="ck_companion_dialogue_status",
        ),
        Index("ix_companion_dialogue_wake", "status", "wake_at"),
    )
    scope_id: Mapped[int] = mapped_column(primary_key=True)
    persona_key: Mapped[str]
    mode_revision: Mapped[int]
    revision: Mapped[int] = mapped_column(default=0)
    action: Mapped[str] = mapped_column(default="end")
    not_before: Mapped[datetime | None] = mapped_column(default=None)
    wake_at: Mapped[datetime | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(default="idle")
    run_token: Mapped[str | None] = mapped_column(default=None)
    confirmed_deliveries: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime]


OWNED_TABLES = (CompanionDialogue.__table__,)
