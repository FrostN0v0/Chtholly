"""ORM models for account-scoped public-channel perception."""

from datetime import datetime

from sqlalchemy import Text, Index, UniqueConstraint
from entari_plugin_database import Base, Mapped, mapped_column


class ChannelParticipant(Base):
    __tablename__ = "channel_perception_participants"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "account_id",
            "channel_id",
            "platform_user_id",
            name="uq_channel_perception_participant",
        ),
        Index("ix_channel_perception_participant_scope", "platform", "account_id", "channel_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str]
    account_id: Mapped[str]
    guild_id: Mapped[str] = mapped_column(default="")
    channel_id: Mapped[str]
    platform_user_id: Mapped[str]
    person_id: Mapped[int] = mapped_column(index=True)
    public_ref: Mapped[str] = mapped_column(unique=True)
    platform_nickname: Mapped[str] = mapped_column(default="")
    group_card: Mapped[str] = mapped_column(default="")
    avatar_url: Mapped[str] = mapped_column(Text, default="")
    avatar_hash: Mapped[str] = mapped_column(default="")
    avatar_description: Mapped[str] = mapped_column(Text, default="")
    avatar_observed_at: Mapped[datetime | None] = mapped_column(default=None)
    identity_history_json: Mapped[str] = mapped_column(Text, default="[]")
    first_seen_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    observed_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class AmbientMessage(Base):
    __tablename__ = "channel_perception_messages"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "account_id",
            "channel_id",
            "message_id",
            name="uq_channel_perception_message",
        ),
        Index("ix_channel_perception_message_scope_time", "platform", "account_id", "channel_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str]
    account_id: Mapped[str]
    guild_id: Mapped[str] = mapped_column(default="")
    channel_id: Mapped[str]
    message_id: Mapped[str]
    person_id: Mapped[int | None] = mapped_column(default=None, index=True)
    participant_ref: Mapped[str] = mapped_column(default="")
    display_name: Mapped[str] = mapped_column(default="")
    content: Mapped[str] = mapped_column(Text, default="")
    reply_to_message_id: Mapped[str] = mapped_column(default="")
    image_count: Mapped[int] = mapped_column(default=0)
    directed_to_bot: Mapped[bool] = mapped_column(default=False)
    is_command: Mapped[bool] = mapped_column(default=False)
    is_bot: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(default=None)
