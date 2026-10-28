"""Idempotent evidence-key migration preserving every legacy row and column."""

from __future__ import annotations

from sqlalchemy import Table, Column, String, MetaData, PrimaryKeyConstraint, text, inspect
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateTable

_TABLE = "chat_relationship_evidence"


def migrate_evidence_identity(connection: Connection) -> None:
    inspector = inspect(connection)
    if not inspector.has_table(_TABLE):
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    primary = inspector.get_pk_constraint(_TABLE)
    key = tuple(primary["constrained_columns"])
    if "subject_user_id" in columns and key == ("turn_id", "subject_user_id"):
        return
    if key != ("turn_id",):
        raise RuntimeError("Relationship evidence has an unsupported primary key")
    quote = connection.dialect.identifier_preparer.quote
    if connection.dialect.name != "sqlite":
        constraint = primary.get("name")
        if not constraint:
            raise RuntimeError("Relationship evidence primary key has no constraint name")
        if "subject_user_id" not in columns:
            connection.execute(text(f"ALTER TABLE {_TABLE} ADD COLUMN subject_user_id VARCHAR"))
        connection.execute(text(f"UPDATE {_TABLE} SET subject_user_id = user_id WHERE subject_user_id IS NULL"))
        connection.execute(text(f"ALTER TABLE {_TABLE} ALTER COLUMN subject_user_id SET NOT NULL"))
        connection.execute(text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {quote(constraint)}"))
        connection.execute(text(f"ALTER TABLE {_TABLE} ADD PRIMARY KEY (turn_id, subject_user_id)"))
        return

    # Reflect rather than duplicate the schema: unknown deployed columns,
    # defaults and CHECK/UNIQUE constraints survive this table-key cutover.
    metadata = MetaData()
    original = Table(_TABLE, metadata, autoload_with=connection)
    replacement = original.to_metadata(MetaData(), name=f"{_TABLE}_members")
    replacement.constraints.remove(replacement.primary_key)
    for column in replacement.columns:
        column.primary_key = False
    if "subject_user_id" not in columns:
        replacement.append_column(Column("subject_user_id", String, nullable=False))
    else:
        replacement.c.subject_user_id.nullable = False
    replacement.append_constraint(PrimaryKeyConstraint(replacement.c.turn_id, replacement.c.subject_user_id))
    schema_objects = (
        connection.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE tbl_name = :table "
                "AND type IN ('index', 'trigger') AND sql IS NOT NULL"
            ),
            {"table": _TABLE},
        )
        .scalars()
        .all()
    )
    names = ", ".join(quote(column.name) for column in original.columns)
    with connection.begin_nested():
        connection.execute(CreateTable(replacement))
        if "subject_user_id" in columns:
            values = ", ".join(
                "COALESCE(subject_user_id, user_id)" if column.name == "subject_user_id" else quote(column.name)
                for column in original.columns
            )
            connection.execute(text(f"INSERT INTO {quote(replacement.name)} ({names}) SELECT {values} FROM {_TABLE}"))
        else:
            connection.execute(
                text(
                    f"INSERT INTO {quote(replacement.name)} ({names}, subject_user_id) "
                    f"SELECT {names}, user_id FROM {_TABLE}"
                )
            )
        connection.execute(text(f"DROP TABLE {_TABLE}"))
        connection.execute(text(f"ALTER TABLE {quote(replacement.name)} RENAME TO {_TABLE}"))
        for statement in schema_objects:
            connection.execute(text(statement))
