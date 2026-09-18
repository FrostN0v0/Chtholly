"""Idempotent additive migration before channel-perception readers and writers."""

from sqlalchemy import text, inspect
from entari_plugin_database import get_session

from .models import AmbientMessage


async def initialize_message_store() -> None:
    async with get_session() as session:
        connection = await session.connection()
        await connection.run_sync(lambda conn: AmbientMessage.__table__.create(conn, checkfirst=True))
        columns = await connection.run_sync(
            lambda conn: {column["name"] for column in inspect(conn).get_columns(AmbientMessage.__tablename__)}
        )
        if "mentions_json" not in columns:
            await session.execute(text("ALTER TABLE channel_perception_messages ADD COLUMN mentions_json TEXT NULL"))
        await session.commit()
