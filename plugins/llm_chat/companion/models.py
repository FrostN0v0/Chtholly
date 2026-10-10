"""Additive, scope-local tables for opt-in companion state and delivery audits."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Text, Index, CheckConstraint, UniqueConstraint
from entari_plugin_database import Base, Mapped, mapped_column


class CompanionMode(Base):
    __tablename__ = "chat_companion_modes"
    __table_args__ = (
        CheckConstraint("mode IN ('normal', 'companion')", name="ck_companion_mode"),
        Index("ix_companion_review_due", "mode", "next_review_at"),
    )
    scope_id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(default="normal")
    revision: Mapped[int] = mapped_column(default=0)
    persona_key: Mapped[str]
    context_revision: Mapped[int] = mapped_column(default=0)
    next_review_at: Mapped[datetime | None] = mapped_column(default=None)
    review_token: Mapped[str | None] = mapped_column(default=None)
    review_deadline: Mapped[datetime | None] = mapped_column(default=None)
    updated_at: Mapped[datetime]


class CompanionLife(Base):
    __tablename__ = "chat_companion_life"
    scope_id: Mapped[int] = mapped_column(primary_key=True)
    persona_key: Mapped[str] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(default=0)
    state_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime]


class CompanionLifeRecord(Base):
    __tablename__ = "chat_companion_life_records"
    scope_id: Mapped[int] = mapped_column(primary_key=True)
    persona_key: Mapped[str] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    state_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime]


class CompanionMember(Base):
    __tablename__ = "chat_companion_members"
    __table_args__ = (
        UniqueConstraint("scope_id", "persona_key", "user_id", name="uq_companion_member_user"),
        UniqueConstraint("scope_id", "persona_key", "platform_user_id", name="uq_companion_member_target"),
    )
    scope_id: Mapped[int] = mapped_column(primary_key=True)
    persona_key: Mapped[str] = mapped_column(primary_key=True)
    member_ref: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[str]
    platform_user_id: Mapped[str]
    display_name: Mapped[str] = mapped_column(default="")
    version: Mapped[int] = mapped_column(default=0)
    last_turn_id: Mapped[int] = mapped_column(default=0)
    last_user_at: Mapped[datetime]
    last_contact_at: Mapped[datetime | None] = mapped_column(default=None)


class CompanionChatEvidence(Base):
    __tablename__ = "chat_companion_chat_evidence"
    scope_id: Mapped[int] = mapped_column(primary_key=True)
    persona_key: Mapped[str] = mapped_column(primary_key=True)
    turn_id: Mapped[int] = mapped_column(primary_key=True)
    member_ref: Mapped[str] = mapped_column(index=True)
    received_at: Mapped[datetime]


class CompanionAttention(Base):
    __tablename__ = "chat_companion_attention"
    scope_id: Mapped[int] = mapped_column(primary_key=True)
    persona_key: Mapped[str] = mapped_column(primary_key=True)
    member_ref: Mapped[str] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    state_json: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime]


class CompanionReview(Base):
    __tablename__ = "chat_companion_reviews"
    __table_args__ = (Index("ix_companion_review_budget", "scope_id", "budget_day"),)
    lease_token: Mapped[str] = mapped_column(primary_key=True)
    scope_id: Mapped[int] = mapped_column(index=True)
    persona_key: Mapped[str]
    mode_revision: Mapped[int]
    context_revision: Mapped[int]
    life_version: Mapped[int]
    lease_deadline: Mapped[datetime]
    budget_day: Mapped[str]
    status: Mapped[str] = mapped_column(default="active")
    reason: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    committed_life_version: Mapped[int | None] = mapped_column(default=None)
    contact_proposal_json: Mapped[str | None] = mapped_column(Text, default=None)


class CompanionContact(Base):
    __tablename__ = "chat_companion_contacts"
    __table_args__ = (
        UniqueConstraint("scope_id", "persona_key", "member_ref", "identity", name="uq_companion_contact_identity"),
        Index("ix_companion_contact_budget", "scope_id", "user_id", "budget_day", "status"),
        Index("ix_companion_contact_user_history", "scope_id", "user_id", "transport_started_at"),
        Index("ix_companion_contact_lease", "status", "lease_deadline"),
    )
    attempt_ref: Mapped[str] = mapped_column(primary_key=True)
    scope_id: Mapped[int] = mapped_column(index=True)
    persona_key: Mapped[str]
    mode_revision: Mapped[int]
    context_revision: Mapped[int]
    origin_review_token: Mapped[str]
    member_ref: Mapped[str]
    member_version: Mapped[int]
    user_id: Mapped[str]
    platform_user_id: Mapped[str]
    display_name: Mapped[str]
    proposal_key: Mapped[str]
    identity: Mapped[str]
    proposal_json: Mapped[str] = mapped_column(Text)
    limits_json: Mapped[str] = mapped_column(Text)
    previous_contact_at: Mapped[datetime | None] = mapped_column(default=None)
    budget_day: Mapped[str]
    lease_token: Mapped[str]
    lease_deadline: Mapped[datetime]
    turn_id: Mapped[int | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(default="active")
    reason: Mapped[str] = mapped_column(Text, default="")
    transport_started_at: Mapped[datetime | None] = mapped_column(default=None)
    transport_inflight: Mapped[bool] = mapped_column(default=False)
    confirmed_receipts_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime]
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


OWNED_TABLES = (
    CompanionMode.__table__,
    CompanionLife.__table__,
    CompanionMember.__table__,
    CompanionChatEvidence.__table__,
    CompanionAttention.__table__,
    CompanionReview.__table__,
    CompanionContact.__table__,
    CompanionLifeRecord.__table__,
)
