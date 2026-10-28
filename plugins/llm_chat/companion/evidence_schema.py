"""Lossless, idempotent multi-member companion chat evidence migration."""

from sqlalchemy import Table, MetaData, PrimaryKeyConstraint, text, inspect
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateTable

_TABLE = "chat_companion_chat_evidence"
_KEY = ("scope_id", "persona_key", "turn_id", "member_ref")


def migrate_companion_evidence(connection: Connection) -> None:
    inspector = inspect(connection)
    if not inspector.has_table(_TABLE):
        return
    primary = inspector.get_pk_constraint(_TABLE)
    if tuple(primary["constrained_columns"]) == _KEY:
        return
    if tuple(primary["constrained_columns"]) != _KEY[:-1]:
        raise RuntimeError("Companion evidence has an unsupported primary key")
    quote = connection.dialect.identifier_preparer.quote
    if connection.dialect.name != "sqlite":
        constraint = primary.get("name")
        if not constraint:
            raise RuntimeError("Companion evidence primary key has no constraint name")
        connection.execute(text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {quote(constraint)}"))
        connection.execute(text(f"ALTER TABLE {_TABLE} ADD PRIMARY KEY ({', '.join(_KEY)})"))
        return
    original = Table(_TABLE, MetaData(), autoload_with=connection)
    replacement = original.to_metadata(MetaData(), name=f"{_TABLE}_members")
    replacement.constraints.remove(replacement.primary_key)
    for column in replacement.columns:
        column.primary_key = False
    replacement.append_constraint(PrimaryKeyConstraint(*(replacement.c[name] for name in _KEY)))
    objects = (
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
        connection.execute(text(f"INSERT INTO {quote(replacement.name)} ({names}) SELECT {names} FROM {_TABLE}"))
        connection.execute(text(f"DROP TABLE {_TABLE}"))
        connection.execute(text(f"ALTER TABLE {quote(replacement.name)} RENAME TO {_TABLE}"))
        for statement in objects:
            connection.execute(text(statement))
