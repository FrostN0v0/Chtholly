"""Evidence-key cutovers retain legacy bytes, unknown provenance and idempotency."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from arclet.entari.config import EntariConfig

if not hasattr(EntariConfig, "instance"):
    EntariConfig.instance = EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.full.example.yml")

from sqlalchemy.ext.asyncio import create_async_engine

from plugins.llm_chat.relationships.schema import migrate_evidence_identity
from plugins.llm_chat.companion.evidence_schema import migrate_companion_evidence


@pytest.mark.asyncio
async def test_relationship_identity_migration_preserves_every_legacy_field_and_index(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-relationship.db'}")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("""
                CREATE TABLE chat_relationship_evidence (
                    turn_id INTEGER PRIMARY KEY,
                    user_id VARCHAR NOT NULL,
                    channel_id VARCHAR NOT NULL,
                    persona_prompt TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    event_id INTEGER NOT NULL,
                    status VARCHAR NOT NULL DEFAULT 'pending',
                    evaluation_ref VARCHAR NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    queued_at DATETIME NOT NULL,
                    claimed_at DATETIME,
                    finished_at DATETIME,
                    error VARCHAR NOT NULL DEFAULT '',
                    unknown_provenance BLOB
                )
            """)
            )
            await connection.execute(
                text("CREATE INDEX deployed_evidence_index ON chat_relationship_evidence(channel_id, status)")
            )
            await connection.execute(
                text("""
                INSERT INTO chat_relationship_evidence
                (turn_id,user_id,channel_id,persona_prompt,payload_json,event_id,status,evaluation_ref,attempts,
                 queued_at,claimed_at,finished_at,error,unknown_provenance)
                VALUES (41,'old-alice','group','Old persona',:payload,77,'failed','old-batch',4,
                        '2026-10-01 12:00:00','2026-10-01 12:00:01',NULL,'原始失败',:provenance)
            """),
                {"payload": '{ "user": "original bytes", "unknown": [1,2,3] }', "provenance": b"\x00\xff\r\n"},
            )
            before = dict((await connection.execute(text("SELECT * FROM chat_relationship_evidence"))).mappings().one())
            await connection.run_sync(migrate_evidence_identity)
            await connection.run_sync(migrate_evidence_identity)
            after = dict((await connection.execute(text("SELECT * FROM chat_relationship_evidence"))).mappings().one())
            assert {key: after[key] for key in before} == before
            assert after["subject_user_id"] == "old-alice"
            indexes = (
                (
                    await connection.execute(
                        text(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='index' AND tbl_name='chat_relationship_evidence'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert "deployed_evidence_index" in indexes
            await connection.execute(
                text("""
                INSERT INTO chat_relationship_evidence
                (turn_id,subject_user_id,user_id,channel_id,persona_prompt,payload_json,event_id,queued_at)
                VALUES (41,'bob','bob','group','Same genuine turn','{}',78,'2026-10-01 12:00:02')
            """)
            )
            assert (
                await connection.execute(text("SELECT count(*) FROM chat_relationship_evidence WHERE turn_id=41"))
            ).scalar_one() == 2
            await connection.run_sync(migrate_evidence_identity)
            original = dict(
                (
                    await connection.execute(
                        text("SELECT * FROM chat_relationship_evidence WHERE subject_user_id='old-alice'")
                    )
                )
                .mappings()
                .one()
            )
            assert original == after
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_companion_evidence_migration_retains_unknown_history_and_allows_two_members_one_turn(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-companion.db'}")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("""
                CREATE TABLE chat_companion_chat_evidence (
                    scope_id INTEGER NOT NULL,
                    persona_key VARCHAR NOT NULL,
                    turn_id INTEGER NOT NULL,
                    member_ref VARCHAR NOT NULL,
                    received_at DATETIME NOT NULL,
                    deployed_provenance TEXT DEFAULT 'unknown',
                    PRIMARY KEY (scope_id,persona_key,turn_id)
                )
            """)
            )
            await connection.execute(
                text("CREATE INDEX legacy_member_index ON chat_companion_chat_evidence(member_ref)")
            )
            await connection.execute(
                text("""
                INSERT INTO chat_companion_chat_evidence
                VALUES (3,'alpha',41,'member_alice','2026-10-01 12:00:00',:provenance)
            """),
                {"provenance": ' {"origin":"legacy","unknown":true} '},
            )
            before = dict(
                (await connection.execute(text("SELECT * FROM chat_companion_chat_evidence"))).mappings().one()
            )
            await connection.run_sync(migrate_companion_evidence)
            await connection.run_sync(migrate_companion_evidence)
            after = dict(
                (await connection.execute(text("SELECT * FROM chat_companion_chat_evidence"))).mappings().one()
            )
            assert after == before
            await connection.execute(
                text("""
                INSERT INTO chat_companion_chat_evidence
                (scope_id,persona_key,turn_id,member_ref,received_at)
                VALUES (3,'alpha',41,'member_bob','2026-10-01 12:00:01')
            """)
            )
            await connection.run_sync(migrate_companion_evidence)
            assert (
                await connection.execute(text("SELECT count(*) FROM chat_companion_chat_evidence WHERE turn_id=41"))
            ).scalar_one() == 2
            indexes = (
                (
                    await connection.execute(
                        text(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='index' AND tbl_name='chat_companion_chat_evidence'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert "legacy_member_index" in indexes
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_partial_relationship_identity_migration_preserves_known_subjects_and_fills_only_unknowns(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'partial-relationship.db'}")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("""
                CREATE TABLE chat_relationship_evidence (
                    turn_id INTEGER PRIMARY KEY,
                    user_id VARCHAR NOT NULL,
                    subject_user_id VARCHAR,
                    payload_json TEXT NOT NULL CHECK (length(payload_json) > 0),
                    unknown_provenance BLOB
                )
            """)
            )
            await connection.execute(text("CREATE TABLE evidence_insert_audit (turn_id INTEGER NOT NULL)"))
            await connection.execute(
                text("""
                CREATE TRIGGER evidence_insert_trigger AFTER INSERT ON chat_relationship_evidence
                BEGIN INSERT INTO evidence_insert_audit VALUES (NEW.turn_id); END
            """)
            )
            await connection.execute(
                text("""
                INSERT INTO chat_relationship_evidence VALUES
                (1,'canonical-owner','captured-old-author',:original,:bytes),
                (2,'unknown-owner',NULL,:unknown,NULL)
            """),
                {"original": ' {"original":true} ', "unknown": '{"unknown":true}', "bytes": b"\x00\xff\r\n"},
            )
            before = (
                (await connection.execute(text("SELECT * FROM chat_relationship_evidence ORDER BY turn_id")))
                .mappings()
                .all()
            )
            await connection.run_sync(migrate_evidence_identity)
            await connection.run_sync(migrate_evidence_identity)
            after = (
                (await connection.execute(text("SELECT * FROM chat_relationship_evidence ORDER BY turn_id")))
                .mappings()
                .all()
            )
            assert dict(after[0]) == dict(before[0])
            assert dict(after[1]) == {**before[1], "subject_user_id": "unknown-owner"}
            await connection.execute(
                text("INSERT INTO chat_relationship_evidence VALUES (1,'other-owner','other-author','{}',NULL)")
            )
            assert (
                await connection.execute(text("SELECT turn_id FROM evidence_insert_audit ORDER BY rowid"))
            ).scalars().all() == [1, 2, 1]
            schema = (
                await connection.execute(text("SELECT sql FROM sqlite_master WHERE name='chat_relationship_evidence'"))
            ).scalar_one()
            assert "CHECK" in schema
    finally:
        await engine.dispose()
