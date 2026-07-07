"""ORM models for llm_chat (tables auto-created by entari-plugin-database)."""

from datetime import datetime

from sqlalchemy import Text
from entari_plugin_database import Base, Mapped, mapped_column


class Conversation(Base):
    __tablename__ = "chat_conversations"
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[str] = mapped_column(index=True)
    user_id: Mapped[str]
    user_name: Mapped[str]
    role: Mapped[str]
    """"user" | "assistant" """
    content: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


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
    """Comma-separated keywords."""
