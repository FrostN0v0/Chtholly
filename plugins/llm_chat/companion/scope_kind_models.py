"""Additive host-observed channel-kind ledger for companion destinations."""

from sqlalchemy import CheckConstraint
from entari_plugin_database import Base, Mapped, mapped_column


class CompanionScopeKind(Base):
    __tablename__ = "chat_companion_scope_kinds"
    __table_args__ = (CheckConstraint("kind IN ('text', 'direct')", name="ck_companion_scope_kind"),)
    scope_id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]
