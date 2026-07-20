"""Read-only schema ownership checks for SQLite component databases."""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from typing import Any


class SQLiteComponentSchemaError(RuntimeError):
    """Raised before a component can mutate an unowned SQLite database."""

    def __init__(self, code: str, component: str, message: str) -> None:
        self.code = str(code)
        self.component = str(component)
        super().__init__(f"{self.code}: {self.component}: {message}")


def sqlite_user_tables(connection: sqlite3.Connection) -> set[str]:
    """Return non-internal table names without changing database state."""

    return {
        str(row[0])
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            """
        )
    }


def execute_sqlite_script_in_transaction(
    connection: sqlite3.Connection,
    script: str,
) -> None:
    """Execute a complete SQL script without ``executescript`` committing.

    Python's ``sqlite3.Connection.executescript`` commits an already-open
    transaction before it runs the script. Component initializers use this
    helper after ``BEGIN IMMEDIATE`` so ownership validation and schema DDL
    stay under one writer lock.
    """

    statement = ""
    for line in str(script).splitlines():
        statement += line + "\n"
        if not sqlite3.complete_statement(statement):
            continue
        sql = statement.strip()
        statement = ""
        if sql:
            connection.execute(sql)
    if statement.strip():
        raise sqlite3.OperationalError(
            "SQLite component schema script is incomplete"
        )


def validate_sqlite_component_schema(
    connection: sqlite3.Connection,
    *,
    component: str,
    meta_table: str,
    version_key: str,
    supported_version: Any,
    compatible_versions: Collection[Any] | None = None,
    owned_tables: Collection[str],
    allowed_tables: Collection[str] | None = None,
) -> set[str]:
    """Validate component ownership/version before any DDL or write PRAGMA.

    ``allowed_tables`` supports databases intentionally shared by multiple
    derived components.  When omitted, an existing database still must expose
    this component's metadata table, but extra tables are left to the owning
    runtime (for example the legacy state database plus continuity-v5 tables).
    """

    tables = sqlite_user_tables(connection)
    if not tables:
        return tables

    owned = {str(name) for name in owned_tables}
    allowed = (
        None
        if allowed_tables is None
        else {str(name) for name in allowed_tables}
    )
    if allowed is not None:
        unexpected = sorted(tables - allowed)
        if unexpected:
            raise SQLiteComponentSchemaError(
                "SQLITE_COMPONENT_FOREIGN_TABLES",
                component,
                "database contains tables outside the component ownership "
                f"contract: {unexpected}",
            )

    if meta_table not in tables:
        owned_without_meta = sorted((tables & owned) - {meta_table})
        if owned_without_meta:
            raise SQLiteComponentSchemaError(
                "SQLITE_COMPONENT_SCHEMA_MISSING",
                component,
                "component tables exist without the required metadata table: "
                f"{owned_without_meta}",
            )
        if allowed is not None:
            # The database can be intentionally shared with sibling
            # components whose tables are all covered by ``allowed_tables``.
            return tables
        raise SQLiteComponentSchemaError(
            "SQLITE_COMPONENT_SCHEMA_MISSING",
            component,
            "existing database has user tables but no component metadata",
        )

    try:
        row = connection.execute(
            f"SELECT value FROM {meta_table} WHERE key=?",
            (str(version_key),),
        ).fetchone()
    except sqlite3.Error as exc:
        raise SQLiteComponentSchemaError(
            "SQLITE_COMPONENT_SCHEMA_UNREADABLE",
            component,
            f"cannot read schema metadata: {exc}",
        ) from exc
    if row is None:
        raise SQLiteComponentSchemaError(
            "SQLITE_COMPONENT_SCHEMA_MISSING",
            component,
            f"metadata key {version_key!r} is missing",
        )
    stored_version = str(row[0])
    accepted_versions = {str(supported_version)}
    if compatible_versions is not None:
        accepted_versions.update(str(value) for value in compatible_versions)
    if stored_version not in accepted_versions:
        supported_description = (
            repr(str(supported_version))
            if len(accepted_versions) == 1
            else repr(sorted(accepted_versions))
        )
        raise SQLiteComponentSchemaError(
            "SQLITE_COMPONENT_SCHEMA_UNSUPPORTED",
            component,
            f"stored={stored_version!r}, supported={supported_description}",
        )
    return tables
