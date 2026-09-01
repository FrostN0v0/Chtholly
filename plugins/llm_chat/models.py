"""ORM models for llm_chat (tables auto-created by entari-plugin-database)."""

from secrets import token_hex
from datetime import datetime

from sqlalchemy import Text, Index, UniqueConstraint
from entari_plugin_database import Base, Mapped, mapped_column


def _opaque_ref(prefix: str) -> str:
    return f"{prefix}_{token_hex(10)}"


def _scope_ref() -> str:
    return _opaque_ref("scope")


def _session_ref() -> str:
    return _opaque_ref("session")


def _turn_ref() -> str:
    return _opaque_ref("turn")


def _event_ref() -> str:
    return _opaque_ref("event")


class Conversation(Base):
    __tablename__ = "chat_conversations"
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[str] = mapped_column(index=True)
    user_id: Mapped[str]
    user_name: Mapped[str]
    role: Mapped[str]
    """Stored role: "user" or "assistant"."""
    content: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class ToolExecution(Base):
    __tablename__ = "chat_tool_executions"
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[str] = mapped_column(index=True)
    turn_id: Mapped[int] = mapped_column(index=True)
    sequence: Mapped[int]
    tool_name: Mapped[str] = mapped_column(index=True)
    status: Mapped[str]
    effect: Mapped[str]
    arguments_json: Mapped[str] = mapped_column(Text, default="{}")
    outcome_json: Mapped[str] = mapped_column(Text, default="{}")
    duration_ms: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class ChatScope(Base):
    __tablename__ = "chat_agent_scopes"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "account_id",
            "channel_id",
            name="uq_chat_agent_scope",
        ),
        Index("ix_chat_agent_scope_activity", "updated_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    scope_ref: Mapped[str] = mapped_column(default=_scope_ref, unique=True)
    platform: Mapped[str]
    account_id: Mapped[str]
    guild_id: Mapped[str] = mapped_column(default="")
    channel_id: Mapped[str]
    display_name: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class ContextSession(Base):
    __tablename__ = "chat_context_sessions"
    __table_args__ = (
        UniqueConstraint("scope_id", "sequence", name="uq_chat_context_session_sequence"),
        Index("ix_chat_context_session_scope_status", "scope_id", "status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    session_ref: Mapped[str] = mapped_column(default=_session_ref, unique=True)
    scope_id: Mapped[int] = mapped_column(index=True)
    previous_session_id: Mapped[int | None] = mapped_column(default=None)
    sequence: Mapped[int]
    status: Mapped[str] = mapped_column(default="active")
    start_reason: Mapped[str] = mapped_column(default="initial")
    close_reason: Mapped[str] = mapped_column(default="")
    model_name: Mapped[str] = mapped_column(default="")
    persona_hash: Mapped[str] = mapped_column(default="")
    system_version: Mapped[str] = mapped_column(default="")
    tool_schema_hash: Mapped[str] = mapped_column(default="")
    policy_version: Mapped[str] = mapped_column(default="")
    handoff_json: Mapped[str] = mapped_column(Text, default="{}")
    turn_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    last_turn_at: Mapped[datetime | None] = mapped_column(default=None)
    closed_at: Mapped[datetime | None] = mapped_column(default=None)


class AgentTurn(Base):
    __tablename__ = "chat_agent_turns"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_chat_agent_turn_sequence"),
        Index("ix_chat_agent_turn_session_time", "session_id", "created_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    turn_ref: Mapped[str] = mapped_column(default=_turn_ref, unique=True)
    session_id: Mapped[int] = mapped_column(index=True)
    sequence: Mapped[int]
    trigger_message_id: Mapped[str] = mapped_column(default="")
    conversation_user_id: Mapped[int | None] = mapped_column(default=None, index=True)
    user_id: Mapped[str] = mapped_column(index=True)
    user_name: Mapped[str]
    status: Mapped[str] = mapped_column(default="running")
    fresh_context: Mapped[bool] = mapped_column(default=False)
    final_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


class AgentEvent(Base):
    __tablename__ = "chat_agent_events"
    __table_args__ = (
        UniqueConstraint("turn_id", "sequence", name="uq_chat_agent_event_sequence"),
        Index("ix_chat_agent_event_turn_type", "turn_id", "event_type"),
        Index("ix_chat_agent_event_tool", "tool_name", "created_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    event_ref: Mapped[str] = mapped_column(default=_event_ref, unique=True)
    turn_id: Mapped[int] = mapped_column(index=True)
    sequence: Mapped[int]
    attempt: Mapped[int] = mapped_column(default=0)
    event_type: Mapped[str] = mapped_column(index=True)
    role: Mapped[str] = mapped_column(default="")
    tool_call_id: Mapped[str] = mapped_column(default="")
    execution_ref: Mapped[str] = mapped_column(default="", index=True)
    tool_name: Mapped[str] = mapped_column(default="", index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(default="")
    effect: Mapped[str] = mapped_column(default="")
    duration_ms: Mapped[int] = mapped_column(default=0)
    model_visible: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class ContextAnchor(Base):
    __tablename__ = "chat_context_anchors"
    __table_args__ = (
        UniqueConstraint("scope_id", "event_id", name="uq_chat_context_anchor_event"),
        Index("ix_chat_context_anchor_scope_active", "scope_id", "active"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    scope_id: Mapped[int] = mapped_column(index=True)
    event_id: Mapped[int] = mapped_column(index=True)
    label: Mapped[str]
    created_by_user_id: Mapped[str] = mapped_column(default="")
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class MigrationState(Base):
    __tablename__ = "chat_agent_migrations"
    key: Mapped[str] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(default="pending")
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class UserRelation(Base):
    __tablename__ = "chat_user_relations"
    user_id: Mapped[str] = mapped_column(primary_key=True)
    channel_id: Mapped[str] = mapped_column(primary_key=True)
    affection: Mapped[float] = mapped_column(default=30.0)
    trust: Mapped[float] = mapped_column(default=30.0)
    dependence: Mapped[float] = mapped_column(default=0.0)
    resentment: Mapped[float] = mapped_column(default=0.0)
    familiarity: Mapped[float] = mapped_column(default=0.0)
    impression: Mapped[str] = mapped_column(default="")
    eval_counter: Mapped[int] = mapped_column(default=0)
    last_interaction: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class UserProfileFact(Base):
    __tablename__ = "chat_user_profile_facts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(index=True)
    channel_id: Mapped[str] = mapped_column(index=True)
    category: Mapped[str] = mapped_column(index=True)
    key: Mapped[str] = mapped_column(index=True)
    value: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(default=0.0)
    evidence_count: Mapped[int] = mapped_column(default=0)
    last_evidence: Mapped[str] = mapped_column(Text, default="")
    embedding_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class UserMemory(Base):
    __tablename__ = "chat_user_memories"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(index=True)
    channel_id: Mapped[str] = mapped_column(index=True)
    text: Mapped[str] = mapped_column(Text)
    importance: Mapped[float] = mapped_column(default=0.5)
    embedding_json: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(default="conversation")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class BotState(Base):
    __tablename__ = "chat_bot_state"
    channel_id: Mapped[str] = mapped_column(primary_key=True)
    mood: Mapped[float] = mapped_column(default=0.0)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class ImageTag(Base):
    __tablename__ = "chat_image_tags"
    id: Mapped[int] = mapped_column(primary_key=True)
    file_path: Mapped[str] = mapped_column(unique=True)
    tags: Mapped[str]
    """Canonical structured JSON metadata; legacy comma tags remain readable during migration."""
    embedding_json: Mapped[str] = mapped_column(Text, default="")
