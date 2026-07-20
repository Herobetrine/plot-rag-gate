"""Independent SQLite persistence for non-canonical initialization sessions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
import zlib
from contextlib import closing, contextmanager, nullcontext, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from .canonical import canonical_hash, canonical_json, sha256_text, utc_now
from .constants import (
    ACTIVE_STATUSES,
    DATABASE_SCHEMA_VERSION,
    PROTOCOL_AUTO,
    PROTOCOL_V1,
    PROTOCOL_V2,
)
from .errors import PlotInitError
from .remote_cache import REMOTE_CACHE_SCHEMA_SQL, REMOTE_CACHE_SHARED_TABLES


DEFAULT_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
BLOB_REFERENCE_KEY = "$plot_rag_init_blob"
BLOB_MEDIA_TYPE = "application/json"
BLOB_COMPRESSION_THRESHOLD = 1024
_BLOB_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class _HeldInitBackupIdentity:
    path: Path
    staging_path: Path
    anchor: BinaryIO
    stat: os.stat_result
    public_anchor: BinaryIO
    public_stat: os.stat_result
    sha256: str


PAYLOAD_MIGRATION_TABLES = (
    {
        "name": "sessions",
        "table": "initialization_sessions",
        "json_column": "state_json",
        "blob_column": "state_blob_hash",
        "payload_kind": "session_state",
    },
    {
        "name": "revisions",
        "table": "initialization_revisions",
        "json_column": "state_json",
        "blob_column": "state_blob_hash",
        "payload_kind": "session_revision",
    },
    {
        "name": "checkpoints",
        "table": "initialization_checkpoints",
        "json_column": "state_json",
        "blob_column": "state_blob_hash",
        "payload_kind": "checkpoint_state",
    },
    {
        "name": "idempotency",
        "table": "initialization_idempotency",
        "json_column": "response_json",
        "blob_column": "response_blob_hash",
        "payload_kind": "idempotency_response",
    },
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS initialization_meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS initialization_payload_blobs(
    blob_hash TEXT PRIMARY KEY,
    media_type TEXT NOT NULL,
    codec TEXT NOT NULL,
    payload BLOB NOT NULL,
    uncompressed_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS initialization_sessions(
    session_id TEXT PRIMARY KEY,
    workspace_root TEXT NOT NULL,
    project_root TEXT,
    mode TEXT,
    target_profile TEXT NOT NULL,
    interaction_profile TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    session_revision INTEGER NOT NULL,
    expected_canon_revision INTEGER NOT NULL,
    source_snapshot_hash TEXT NOT NULL,
    proposal_id TEXT,
    state_json TEXT NOT NULL,
    state_blob_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS initialization_sessions_project_status
ON initialization_sessions(project_root, status, updated_at);

CREATE TABLE IF NOT EXISTS initialization_revisions(
    session_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL,
    operation TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    state_json TEXT NOT NULL,
    state_blob_hash TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(session_id, session_revision),
    FOREIGN KEY(session_id) REFERENCES initialization_sessions(session_id)
);

CREATE TABLE IF NOT EXISTS initialization_journal(
    journal_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    stage TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES initialization_sessions(session_id)
);

CREATE INDEX IF NOT EXISTS initialization_journal_session
ON initialization_journal(session_id, journal_sequence);

CREATE TABLE IF NOT EXISTS initialization_checkpoints(
    checkpoint_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    source_snapshot_hash TEXT,
    dependency_hash TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    state_json TEXT NOT NULL,
    state_blob_hash TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES initialization_sessions(session_id)
);

CREATE INDEX IF NOT EXISTS initialization_checkpoints_session
ON initialization_checkpoints(session_id, session_revision, stage);

CREATE TABLE IF NOT EXISTS initialization_idempotency(
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    response_blob_hash TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS initialization_proposals(
    proposal_id TEXT PRIMARY KEY,
    package_hash TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS initialization_session_proposals(
    session_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL,
    PRIMARY KEY(session_id, proposal_id),
    FOREIGN KEY(session_id) REFERENCES initialization_sessions(session_id),
    FOREIGN KEY(proposal_id) REFERENCES initialization_proposals(proposal_id)
);

CREATE TABLE IF NOT EXISTS initialization_source_versions(
    source_version_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    normalized_real_path TEXT NOT NULL,
    descriptor_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS initialization_source_versions_source
ON initialization_source_versions(source_id, first_seen_at);

CREATE TABLE IF NOT EXISTS initialization_session_sources(
    session_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_version_id TEXT NOT NULL,
    head_revision INTEGER NOT NULL,
    active_revision INTEGER,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(session_id, source_id),
    FOREIGN KEY(session_id) REFERENCES initialization_sessions(session_id),
    FOREIGN KEY(source_version_id) REFERENCES initialization_source_versions(source_version_id)
);
""" + REMOTE_CACHE_SCHEMA_SQL


class InitStorage:
    """Persistence boundary. Construction and read-only inspection never create files."""

    def __init__(
        self,
        database_path: Path | str,
        *,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve(strict=False)
        self.max_payload_bytes = int(max_payload_bytes)
        if self.max_payload_bytes <= 0:
            raise ValueError("max_payload_bytes must be positive")

    @property
    def exists(self) -> bool:
        return self.database_path.is_file()

    def _assert_database_identity(self, anchor_stat: os.stat_result) -> None:
        try:
            current_stat = os.stat(
                self.database_path,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise PlotInitError(
                "INIT_STORAGE_PATH_CHANGED",
                "initialization database path became unavailable during migration",
                database_path=str(self.database_path),
            ) from exc
        if not os.path.samestat(anchor_stat, current_stat):
            raise PlotInitError(
                "INIT_STORAGE_PATH_CHANGED",
                "initialization database path now references a different file",
                database_path=str(self.database_path),
            )

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _column_exists(
        connection: sqlite3.Connection,
        table: str,
        column: str,
    ) -> bool:
        if not InitStorage._table_exists(connection, table):
            return False
        return any(
            str(row[1]) == column
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        )

    @staticmethod
    def _stored_schema_version(connection: sqlite3.Connection) -> int:
        if not InitStorage._table_exists(connection, "initialization_meta"):
            return 0
        row = connection.execute(
            "SELECT value FROM initialization_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError) as exc:
            raise PlotInitError(
                "CORRUPT_INIT_STORAGE",
                "initialization schema version is invalid",
            ) from exc

    @staticmethod
    def _user_tables(connection: sqlite3.Connection) -> set[str]:
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

    @classmethod
    def _validated_schema_version(
        cls,
        connection: sqlite3.Connection,
    ) -> int:
        user_tables = cls._user_tables(connection)
        unexpected = sorted(user_tables - REMOTE_CACHE_SHARED_TABLES)
        if unexpected:
            raise PlotInitError(
                "INIT_STORAGE_DATABASE_UNOWNED",
                "initialization database contains foreign user tables",
                tables=unexpected,
            )
        stored_version = cls._stored_schema_version(connection)
        if stored_version < 0:
            raise PlotInitError(
                "CORRUPT_INIT_STORAGE",
                "initialization schema version must be non-negative",
                stored_schema_version=stored_version,
            )
        if stored_version > DATABASE_SCHEMA_VERSION:
            raise PlotInitError(
                "INIT_STORAGE_SCHEMA_NEWER",
                "initialization database was created by a newer runtime",
                stored_schema_version=stored_version,
                supported_schema_version=DATABASE_SCHEMA_VERSION,
            )
        if stored_version == 0 and user_tables:
            raise PlotInitError(
                "INIT_STORAGE_SCHEMA_VERSION_MISSING",
                "existing database has user tables but no initialization "
                "schema version",
            )
        return stored_version

    @staticmethod
    def _ensure_v2_columns(connection: sqlite3.Connection) -> None:
        additions = (
            ("initialization_sessions", "state_blob_hash", "TEXT"),
            ("initialization_revisions", "state_blob_hash", "TEXT"),
            ("initialization_checkpoints", "state_blob_hash", "TEXT"),
            ("initialization_idempotency", "response_blob_hash", "TEXT"),
        )
        for table, column, declaration in additions:
            if not InitStorage._column_exists(connection, table, column):
                connection.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN "{column}" {declaration}'
                )

    @staticmethod
    def _execute_script_in_transaction(
        connection: sqlite3.Connection,
        script: str,
    ) -> None:
        """Execute a DDL script without ``executescript`` committing early."""

        statement = ""
        for line in script.splitlines():
            statement += line + "\n"
            if not sqlite3.complete_statement(statement):
                continue
            sql = statement.strip()
            statement = ""
            if sql:
                connection.execute(sql)
        if statement.strip():
            raise PlotInitError(
                "INIT_STORAGE_SCHEMA_SCRIPT_INCOMPLETE",
                "initialization schema script contains an incomplete statement",
            )

    def _upgrade_schema_in_transaction(
        self,
        connection: sqlite3.Connection,
    ) -> int:
        """Upgrade schema metadata using the caller's existing write transaction."""

        stored_version = self._validated_schema_version(connection)
        self._execute_script_in_transaction(connection, SCHEMA_SQL)
        self._ensure_v2_columns(connection)
        connection.execute(
            """
            INSERT INTO initialization_meta(key, value)
            VALUES('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(DATABASE_SCHEMA_VERSION),),
        )
        return stored_version

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(
            sqlite3.connect(self.database_path, timeout=30.0)
        ) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._upgrade_schema_in_transaction(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        self._initialize()
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _read_connection(
        self,
        existing: sqlite3.Connection | None = None,
    ) -> Iterator[sqlite3.Connection]:
        if existing is not None:
            yield existing
            return
        if not self.exists:
            raise PlotInitError(
                "INIT_STORAGE_NOT_CREATED",
                f"initialization database does not exist: {self.database_path}",
            )
        uri = f"{self.database_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=30.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _blob_reference(blob_hash: str) -> str:
        return canonical_json({BLOB_REFERENCE_KEY: str(blob_hash)})

    @staticmethod
    def _reference_hash(value: str) -> str | None:
        try:
            payload = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None
        if (
            isinstance(payload, dict)
            and set(payload) == {BLOB_REFERENCE_KEY}
            and isinstance(payload.get(BLOB_REFERENCE_KEY), str)
        ):
            return str(payload[BLOB_REFERENCE_KEY])
        return None

    @staticmethod
    def _validated_blob_hash(blob_hash: str) -> str:
        normalized = str(blob_hash or "").strip().casefold()
        if not _BLOB_HASH_RE.fullmatch(normalized):
            raise PlotInitError(
                "CORRUPT_INIT_BLOB_REFERENCE",
                "initialization payload blob reference is not a SHA-256 hash",
                blob_hash=str(blob_hash or ""),
            )
        return normalized

    def _canonical_payload(
        self,
        value: dict[str, Any],
        *,
        payload_kind: str,
    ) -> tuple[str, bytes, str]:
        text = canonical_json(value)
        raw = text.encode("utf-8")
        if len(raw) > self.max_payload_bytes:
            raise PlotInitError(
                "INIT_PAYLOAD_TOO_LARGE",
                "initialization payload exceeds the configured storage limit",
                payload_kind=payload_kind,
                payload_bytes=len(raw),
                max_payload_bytes=self.max_payload_bytes,
            )
        return text, raw, sha256_text(text)

    @staticmethod
    def _encoded_blob(raw: bytes) -> tuple[str, bytes]:
        if len(raw) < BLOB_COMPRESSION_THRESHOLD:
            return "utf8", raw
        compressed = zlib.compress(raw)
        if len(compressed) >= len(raw):
            return "utf8", raw
        return "zlib", compressed

    def _decode_blob_bytes(
        self,
        *,
        blob_hash: str,
        codec: str,
        payload: bytes,
        uncompressed_bytes: int,
    ) -> str:
        if uncompressed_bytes < 0 or uncompressed_bytes > self.max_payload_bytes:
            raise PlotInitError(
                "INIT_BLOB_SIZE_INVALID",
                "stored initialization blob exceeds the configured storage limit",
                blob_hash=blob_hash,
                uncompressed_bytes=uncompressed_bytes,
                max_payload_bytes=self.max_payload_bytes,
            )
        if codec == "utf8":
            raw = bytes(payload)
        elif codec == "zlib":
            try:
                inflater = zlib.decompressobj()
                raw = inflater.decompress(
                    bytes(payload),
                    self.max_payload_bytes + 1,
                )
                if len(raw) > self.max_payload_bytes or inflater.unconsumed_tail:
                    raise PlotInitError(
                        "INIT_BLOB_SIZE_INVALID",
                        "compressed initialization blob exceeds the configured storage limit",
                        blob_hash=blob_hash,
                        max_payload_bytes=self.max_payload_bytes,
                    )
                remaining = self.max_payload_bytes + 1 - len(raw)
                raw += inflater.flush(remaining)
            except zlib.error as exc:
                raise PlotInitError(
                    "CORRUPT_INIT_BLOB",
                    "compressed initialization blob is invalid",
                    blob_hash=blob_hash,
                ) from exc
            if (
                len(raw) > self.max_payload_bytes
                or not inflater.eof
                or inflater.unused_data
            ):
                raise PlotInitError(
                    "CORRUPT_INIT_BLOB",
                    "compressed initialization blob is truncated or has trailing data",
                    blob_hash=blob_hash,
                )
        else:
            raise PlotInitError(
                "CORRUPT_INIT_BLOB",
                "initialization blob uses an unsupported codec",
                blob_hash=blob_hash,
                codec=codec,
            )
        if len(raw) != uncompressed_bytes:
            raise PlotInitError(
                "CORRUPT_INIT_BLOB",
                "initialization blob length does not match its metadata",
                blob_hash=blob_hash,
                expected_bytes=uncompressed_bytes,
                actual_bytes=len(raw),
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PlotInitError(
                "CORRUPT_INIT_BLOB",
                "initialization blob is not valid UTF-8 JSON",
                blob_hash=blob_hash,
            ) from exc
        if sha256_text(text) != blob_hash:
            raise PlotInitError(
                "CORRUPT_INIT_BLOB",
                "initialization blob hash verification failed",
                blob_hash=blob_hash,
            )
        return text

    def _load_blob(
        self,
        connection: sqlite3.Connection,
        blob_hash: str,
    ) -> dict[str, Any]:
        blob_hash = self._validated_blob_hash(blob_hash)
        if not self._table_exists(connection, "initialization_payload_blobs"):
            raise PlotInitError(
                "INIT_BLOB_NOT_FOUND",
                "initialization payload blob table is missing",
                blob_hash=blob_hash,
            )
        row = connection.execute(
            """
            SELECT media_type, codec, payload, uncompressed_bytes
            FROM initialization_payload_blobs
            WHERE blob_hash=?
            """,
            (blob_hash,),
        ).fetchone()
        if row is None:
            raise PlotInitError(
                "INIT_BLOB_NOT_FOUND",
                "referenced initialization payload blob is missing",
                blob_hash=blob_hash,
            )
        if str(row["media_type"]) != BLOB_MEDIA_TYPE:
            raise PlotInitError(
                "CORRUPT_INIT_BLOB",
                "initialization blob media type is invalid",
                blob_hash=blob_hash,
                media_type=str(row["media_type"]),
            )
        try:
            uncompressed_bytes = int(row["uncompressed_bytes"])
        except (TypeError, ValueError) as exc:
            raise PlotInitError(
                "CORRUPT_INIT_BLOB",
                "initialization blob size metadata is invalid",
                blob_hash=blob_hash,
            ) from exc
        text = self._decode_blob_bytes(
            blob_hash=blob_hash,
            codec=str(row["codec"]),
            payload=bytes(row["payload"]),
            uncompressed_bytes=uncompressed_bytes,
        )
        return self._decode(text)

    def _store_blob(
        self,
        connection: sqlite3.Connection,
        value: dict[str, Any],
        *,
        payload_kind: str,
    ) -> tuple[str, str]:
        text, raw, blob_hash = self._canonical_payload(
            value,
            payload_kind=payload_kind,
        )
        codec, encoded = self._encoded_blob(raw)
        connection.execute(
            """
            INSERT OR IGNORE INTO initialization_payload_blobs(
                blob_hash, media_type, codec, payload,
                uncompressed_bytes, created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                blob_hash,
                BLOB_MEDIA_TYPE,
                codec,
                encoded,
                len(raw),
                utc_now(),
            ),
        )
        existing = connection.execute(
            """
            SELECT media_type, codec, payload, uncompressed_bytes
            FROM initialization_payload_blobs
            WHERE blob_hash=?
            """,
            (blob_hash,),
        ).fetchone()
        if existing is None:
            raise PlotInitError(
                "INIT_BLOB_WRITE_FAILED",
                "initialization payload blob was not persisted",
                blob_hash=blob_hash,
            )
        existing_text = self._decode_blob_bytes(
            blob_hash=blob_hash,
            codec=str(existing["codec"]),
            payload=bytes(existing["payload"]),
            uncompressed_bytes=int(existing["uncompressed_bytes"]),
        )
        if (
            str(existing["media_type"]) != BLOB_MEDIA_TYPE
            or existing_text != text
        ):
            raise PlotInitError(
                "INIT_BLOB_HASH_CONFLICT",
                "content-addressed initialization blob conflicts with existing data",
                blob_hash=blob_hash,
            )
        return blob_hash, self._blob_reference(blob_hash)

    def _decode_payload(
        self,
        connection: sqlite3.Connection,
        inline_json: str,
        blob_hash: str | None = None,
    ) -> dict[str, Any]:
        explicit_reference = str(blob_hash or "").strip()
        inline_reference = self._reference_hash(inline_json)
        if (
            explicit_reference
            and inline_reference
            and explicit_reference.casefold() != inline_reference.casefold()
        ):
            raise PlotInitError(
                "CORRUPT_INIT_BLOB_REFERENCE",
                "inline and column initialization blob references disagree",
                column_blob_hash=explicit_reference,
                inline_blob_hash=inline_reference,
            )
        reference = explicit_reference or inline_reference
        if reference:
            normalized_reference = self._validated_blob_hash(reference)
            payload = self._load_blob(
                connection,
                normalized_reference,
            )
            if explicit_reference and not inline_reference:
                encoded = inline_json.encode("utf-8")
                if len(encoded) > self.max_payload_bytes:
                    raise PlotInitError(
                        "INIT_PAYLOAD_TOO_LARGE",
                        "inline initialization payload exceeds the configured storage limit",
                        payload_bytes=len(encoded),
                        max_payload_bytes=self.max_payload_bytes,
                    )
                inline_payload = self._decode(inline_json)
                try:
                    inline_text = canonical_json(inline_payload)
                except (TypeError, ValueError) as exc:
                    raise PlotInitError(
                        "CORRUPT_INIT_STORAGE",
                        "inline initialization payload is not canonical JSON",
                    ) from exc
                if sha256_text(inline_text) != normalized_reference:
                    raise PlotInitError(
                        "CORRUPT_INIT_BLOB_REFERENCE",
                        "inline initialization payload does not match its blob hash",
                        column_blob_hash=normalized_reference,
                    )
            return payload
        encoded = inline_json.encode("utf-8")
        if len(encoded) > self.max_payload_bytes:
            raise PlotInitError(
                "INIT_PAYLOAD_TOO_LARGE",
                "legacy initialization payload exceeds the configured storage limit",
                payload_bytes=len(encoded),
                max_payload_bytes=self.max_payload_bytes,
            )
        return self._decode(inline_json)

    @staticmethod
    def _decode(value: str) -> dict[str, Any]:
        try:
            payload = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PlotInitError(
                "CORRUPT_INIT_STORAGE",
                "stored JSON payload is invalid",
            ) from exc
        if not isinstance(payload, dict):
            raise PlotInitError("CORRUPT_INIT_STORAGE", "stored JSON object is invalid")
        return payload

    @staticmethod
    def _with_schema_compatibility(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Make legacy v1 rows explicit without fabricating a power model."""

        value = dict(payload)
        stored = str(
            value.get("bundle_schema_version")
            or value.get("schema_version")
            or PROTOCOL_V1
        )
        if stored not in {PROTOCOL_V1, PROTOCOL_V2}:
            stored = PROTOCOL_V1
        value.setdefault("schema_version", stored)
        value.setdefault("bundle_schema_version", stored)
        value.setdefault("requested_bundle_schema_version", PROTOCOL_AUTO)
        if stored == PROTOCOL_V1:
            value.setdefault("power_model_status", "unmodeled")
            value.setdefault("power_model_compatibility", "v1_fallback")
        return value

    @staticmethod
    def _idempotency_row(
        connection: sqlite3.Connection,
        scope: str,
        idempotency_key: str,
    ) -> sqlite3.Row | None:
        blob_projection = (
            "response_blob_hash"
            if InitStorage._column_exists(
                connection,
                "initialization_idempotency",
                "response_blob_hash",
            )
            else "NULL AS response_blob_hash"
        )
        return connection.execute(
            f"""
            SELECT request_hash, response_json, {blob_projection}
            FROM initialization_idempotency
            WHERE scope=? AND idempotency_key=?
            """,
            (scope, idempotency_key),
        ).fetchone()

    def _check_idempotency_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row | None,
        request_hash: str,
        *,
        scope: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        if str(row["request_hash"]) != request_hash:
            raise PlotInitError(
                "IDEMPOTENCY_CONFLICT",
                "idempotency key was already used with a different request",
                scope=scope,
                idempotency_key=idempotency_key,
            )
        return self._decode_idempotency_row(connection, row)

    def _decode_idempotency_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        response = self._decode_payload(
            connection,
            str(row["response_json"]),
            str(row["response_blob_hash"] or ""),
        )
        if isinstance(response, dict):
            response["idempotent"] = True
        return response

    def lookup_idempotency_key(
        self,
        scope: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        """Replay a response for a caller-proven stable idempotency key."""

        if not self.exists:
            return None
        with self._read_connection() as connection:
            row = self._idempotency_row(connection, scope, idempotency_key)
            if row is None:
                return None
            return self._decode_idempotency_row(connection, row)

    def lookup_idempotency(
        self,
        scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        if not self.exists:
            return None
        with self._read_connection() as connection:
            row = self._idempotency_row(connection, scope, idempotency_key)
            return self._check_idempotency_row(
                connection,
                row,
                request_hash,
                scope=scope,
                idempotency_key=idempotency_key,
            )

    def _insert_idempotency(
        self,
        connection: sqlite3.Connection,
        *,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        response: dict[str, Any],
    ) -> None:
        blob_hash, reference = self._store_blob(
            connection,
            response,
            payload_kind="idempotency_response",
        )
        connection.execute(
            """
            INSERT INTO initialization_idempotency(
                scope, idempotency_key, request_hash, response_json,
                response_blob_hash, created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                scope,
                idempotency_key,
                request_hash,
                reference,
                blob_hash,
                utc_now(),
            ),
        )

    def _insert_revision(
        self,
        connection: sqlite3.Connection,
        state: dict[str, Any],
        operation: str,
    ) -> None:
        blob_hash, reference = self._store_blob(
            connection,
            state,
            payload_kind="session_revision",
        )
        connection.execute(
            """
            INSERT INTO initialization_revisions(
                session_id, session_revision, operation, state_hash,
                state_json, state_blob_hash, created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                state["session_id"],
                int(state["session_revision"]),
                operation,
                canonical_hash(state),
                reference,
                blob_hash,
                utc_now(),
            ),
        )

    @staticmethod
    def _insert_journal(
        connection: sqlite3.Connection,
        state: dict[str, Any],
        *,
        event_type: str,
        stage: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO initialization_journal(
                session_id, session_revision, event_type, stage,
                payload_hash, payload_json, created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                state["session_id"],
                int(state["session_revision"]),
                event_type,
                stage,
                canonical_hash(payload),
                canonical_json(payload),
                utc_now(),
            ),
        )

    def _insert_checkpoints(
        self,
        connection: sqlite3.Connection,
        state: dict[str, Any],
        checkpoints: list[dict[str, Any]],
    ) -> None:
        blob_hash, reference = self._store_blob(
            connection,
            state,
            payload_kind="checkpoint_state",
        )
        state_hash = canonical_hash(state)
        for checkpoint in checkpoints:
            connection.execute(
                """
                INSERT OR IGNORE INTO initialization_checkpoints(
                    checkpoint_id, session_id, session_revision, stage, status,
                    source_snapshot_hash, dependency_hash, state_hash,
                    state_json, state_blob_hash, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    checkpoint["checkpoint_id"],
                    state["session_id"],
                    int(state["session_revision"]),
                    checkpoint["stage"],
                    checkpoint["status"],
                    checkpoint.get("source_snapshot_hash"),
                    checkpoint["dependency_hash"],
                    state_hash,
                    reference,
                    blob_hash,
                    utc_now(),
                ),
            )
            InitStorage._insert_journal(
                connection,
                state,
                event_type="stage_checkpoint",
                stage=str(checkpoint["stage"]),
                payload=checkpoint,
            )

    @staticmethod
    def _sync_sources(
        connection: sqlite3.Connection,
        state: dict[str, Any],
    ) -> None:
        session_id = str(state["session_id"])
        now = utc_now()
        current_source_ids: list[str] = []
        for descriptor in state.get("source_manifest") or []:
            source_id = str(descriptor["source_id"])
            source_version_id = str(descriptor["source_version_id"])
            current_source_ids.append(source_id)
            connection.execute(
                """
                INSERT OR IGNORE INTO initialization_source_versions(
                    source_version_id, source_id, content_hash,
                    normalized_real_path, descriptor_json, first_seen_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    source_version_id,
                    source_id,
                    descriptor["content_hash"],
                    descriptor["normalized_real_path"],
                    canonical_json(descriptor),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO initialization_session_sources(
                    session_id, source_id, source_version_id, head_revision,
                    active_revision, observed_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(session_id, source_id) DO UPDATE SET
                    source_version_id=excluded.source_version_id,
                    head_revision=excluded.head_revision,
                    active_revision=excluded.active_revision,
                    observed_at=excluded.observed_at
                """,
                (
                    session_id,
                    source_id,
                    source_version_id,
                    int(descriptor.get("head_revision") or 1),
                    descriptor.get("active_revision"),
                    now,
                ),
            )
        if current_source_ids:
            placeholders = ",".join("?" for _ in current_source_ids)
            connection.execute(
                f"""
                DELETE FROM initialization_session_sources
                WHERE session_id=? AND source_id NOT IN ({placeholders})
                """,
                (session_id, *current_source_ids),
            )
        else:
            connection.execute(
                "DELETE FROM initialization_session_sources WHERE session_id=?",
                (session_id,),
            )

    @staticmethod
    def _quick_check(connection: sqlite3.Connection) -> str:
        values = [
            str(row[0])
            for row in connection.execute("PRAGMA quick_check").fetchall()
        ]
        if values == ["ok"]:
            return "ok"
        return "; ".join(values) if values else "no_result"

    def _file_metrics(
        self,
        connection: sqlite3.Connection,
    ) -> dict[str, int]:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(
            connection.execute("PRAGMA freelist_count").fetchone()[0]
        )
        database_bytes = int(self.database_path.stat().st_size)
        wal_path = Path(f"{self.database_path}-wal")
        shm_path = Path(f"{self.database_path}-shm")
        wal_bytes = int(wal_path.stat().st_size) if wal_path.is_file() else 0
        shm_bytes = int(shm_path.stat().st_size) if shm_path.is_file() else 0
        return {
            "database_bytes": database_bytes,
            "wal_bytes": wal_bytes,
            "shm_bytes": shm_bytes,
            "total_storage_bytes": database_bytes + wal_bytes + shm_bytes,
            "page_size": page_size,
            "page_count": page_count,
            "freelist_count": freelist_count,
            "allocated_page_bytes": page_size * page_count,
            "free_page_bytes": page_size * freelist_count,
            "used_page_bytes": page_size * (page_count - freelist_count),
        }

    def migration_plan(
        self,
        *,
        _connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Inspect legacy inline payloads without creating or modifying files."""

        if not self.exists:
            return {
                "status": "inspected",
                "database_path": str(self.database_path),
                "exists": False,
                "schema": {
                    "stored_version": 0,
                    "supported_version": DATABASE_SCHEMA_VERSION,
                },
                "file": {
                    "database_bytes": 0,
                    "wal_bytes": 0,
                    "shm_bytes": 0,
                    "total_storage_bytes": 0,
                    "page_size": 0,
                    "page_count": 0,
                    "freelist_count": 0,
                    "allocated_page_bytes": 0,
                    "free_page_bytes": 0,
                    "used_page_bytes": 0,
                },
                "tables": {},
                "rows": {
                    "total": 0,
                    "valid": 0,
                    "row_refs": 0,
                    "projected_row_refs": 0,
                    "legacy": 0,
                    "rewrite": 0,
                    "reconcile": 0,
                    "oversized": 0,
                    "corrupt": 0,
                },
                "blobs": {
                    "existing": 0,
                    "existing_referenced": 0,
                    "existing_orphan": 0,
                    "candidate_unique": 0,
                    "projected_unique": 0,
                    "projected_referenced_unique": 0,
                    "projected_orphan": 0,
                },
                "bytes": {
                    "inline": 0,
                    "legacy_inline": 0,
                    "existing_blob_payload": 0,
                    "candidate_blob_payload": 0,
                    "projected_reference": 0,
                    "projected_payload_storage": 0,
                    "projected_payload_storage_with_orphan_cleanup": 0,
                    "estimated_savings": 0,
                    "dedup": 0,
                    "compression": 0,
                },
                "row_refs": 0,
                "projected_row_refs": 0,
                "unique_blobs": 0,
                "projected_unique_blobs": 0,
                "dedup_bytes": 0,
                "quick_check": "not_created",
                "migration_required": False,
                "blocked": False,
                "blocked_reasons": [],
            }

        with self._read_connection(_connection) as connection:
            stored_version = self._validated_schema_version(connection)
            quick_check = self._quick_check(connection)
            file_metrics = self._file_metrics(connection)

            blob_metadata: dict[str, dict[str, int | str]] = {}
            existing_blob_payload_bytes = 0
            if self._table_exists(
                connection,
                "initialization_payload_blobs",
            ):
                for row in connection.execute(
                    """
                    SELECT blob_hash, media_type, codec, length(payload),
                           uncompressed_bytes
                    FROM initialization_payload_blobs
                    """
                ):
                    blob_hash = str(row[0])
                    encoded_bytes = int(row[3] or 0)
                    try:
                        uncompressed_bytes = int(row[4])
                    except (TypeError, ValueError):
                        uncompressed_bytes = -1
                    blob_metadata[blob_hash] = {
                        "media_type": str(row[1]),
                        "codec": str(row[2]),
                        "encoded_bytes": encoded_bytes,
                        "uncompressed_bytes": uncompressed_bytes,
                    }
                    existing_blob_payload_bytes += encoded_bytes

            verified_blob_payloads: dict[str, dict[str, Any]] = {}
            referenced_hashes: set[str] = set()
            valid_referenced_hashes: set[str] = set()
            candidate_hashes: dict[str, dict[str, int]] = {}
            projected_hashes: set[str] = set()
            table_results: dict[str, dict[str, int | bool]] = {}
            total_rows = 0
            valid_rows = 0
            valid_current_refs = 0
            legacy_rows = 0
            rewrite_rows = 0
            reconcile_rows = 0
            oversized_rows = 0
            corrupt_rows = 0
            inline_bytes_total = 0
            legacy_inline_bytes = 0
            current_referenced_uncompressed_bytes = 0
            projected_referenced_uncompressed_bytes = 0
            projected_reference_bytes = 0

            def verify_blob(blob_hash: str) -> tuple[str, int]:
                normalized = self._validated_blob_hash(blob_hash)
                if normalized not in verified_blob_payloads:
                    verified_blob_payloads[normalized] = self._load_blob(
                        connection,
                        normalized,
                    )
                metadata = blob_metadata.get(normalized)
                if metadata is None:
                    raise PlotInitError(
                        "INIT_BLOB_NOT_FOUND",
                        "referenced initialization payload blob metadata is missing",
                        blob_hash=normalized,
                    )
                uncompressed_bytes = int(metadata["uncompressed_bytes"])
                if uncompressed_bytes < 0:
                    raise PlotInitError(
                        "CORRUPT_INIT_BLOB",
                        "initialization blob size metadata is invalid",
                        blob_hash=normalized,
                    )
                return normalized, uncompressed_bytes

            for descriptor in PAYLOAD_MIGRATION_TABLES:
                name = str(descriptor["name"])
                table = str(descriptor["table"])
                json_column = str(descriptor["json_column"])
                blob_column = str(descriptor["blob_column"])
                stats: dict[str, int | bool] = {
                    "present": False,
                    "blob_column_present": False,
                    "rows": 0,
                    "valid_rows": 0,
                    "row_refs": 0,
                    "valid_row_refs": 0,
                    "legacy_rows": 0,
                    "rewrite_rows": 0,
                    "reconcile_rows": 0,
                    "oversized_rows": 0,
                    "corrupt_rows": 0,
                    "inline_bytes": 0,
                    "legacy_inline_bytes": 0,
                }
                table_results[name] = stats
                if not self._table_exists(connection, table):
                    continue
                stats["present"] = True
                if not self._column_exists(connection, table, json_column):
                    stats["corrupt_rows"] = 1
                    corrupt_rows += 1
                    continue
                has_blob_column = self._column_exists(
                    connection,
                    table,
                    blob_column,
                )
                stats["blob_column_present"] = has_blob_column
                blob_projection = (
                    f'"{blob_column}" AS "_blob_hash"'
                    if has_blob_column
                    else 'NULL AS "_blob_hash"'
                )
                rows = connection.execute(
                    f"""
                    SELECT rowid AS "_storage_rowid",
                           "{json_column}" AS "_inline_json",
                           {blob_projection}
                    FROM "{table}"
                    ORDER BY rowid
                    """
                )
                for row in rows:
                    stats["rows"] = int(stats["rows"]) + 1
                    total_rows += 1
                    if row["_inline_json"] is None:
                        stats["corrupt_rows"] = int(stats["corrupt_rows"]) + 1
                        corrupt_rows += 1
                        continue
                    inline_json = str(row["_inline_json"])
                    encoded = inline_json.encode("utf-8")
                    inline_size = len(encoded)
                    stats["inline_bytes"] = int(stats["inline_bytes"]) + inline_size
                    inline_bytes_total += inline_size
                    explicit_reference = str(row["_blob_hash"] or "").strip()
                    inline_reference = self._reference_hash(inline_json)
                    stats["row_refs"] = int(stats["row_refs"]) + int(
                        bool(explicit_reference or inline_reference)
                    )

                    if (
                        explicit_reference
                        and inline_reference
                        and explicit_reference.casefold()
                        != inline_reference.casefold()
                    ):
                        stats["corrupt_rows"] = int(stats["corrupt_rows"]) + 1
                        corrupt_rows += 1
                        continue

                    effective_reference = (
                        explicit_reference or inline_reference or ""
                    )
                    if effective_reference:
                        try:
                            normalized_hash, uncompressed_bytes = verify_blob(
                                effective_reference
                            )
                        except PlotInitError:
                            stats["corrupt_rows"] = (
                                int(stats["corrupt_rows"]) + 1
                            )
                            corrupt_rows += 1
                            continue
                        referenced_hashes.add(normalized_hash)

                        if inline_reference:
                            canonical_reference = self._blob_reference(
                                normalized_hash
                            )
                            if (
                                not explicit_reference
                                or inline_json != canonical_reference
                            ):
                                stats["reconcile_rows"] = (
                                    int(stats["reconcile_rows"]) + 1
                                )
                                reconcile_rows += 1
                        else:
                            stats["rewrite_rows"] = (
                                int(stats["rewrite_rows"]) + 1
                            )
                            rewrite_rows += 1
                            stats["legacy_inline_bytes"] = (
                                int(stats["legacy_inline_bytes"]) + inline_size
                            )
                            legacy_inline_bytes += inline_size
                            if inline_size > self.max_payload_bytes:
                                stats["oversized_rows"] = (
                                    int(stats["oversized_rows"]) + 1
                                )
                                oversized_rows += 1
                                continue
                            try:
                                inline_payload = self._decode(inline_json)
                                _text, raw, inline_hash = (
                                    self._canonical_payload(
                                        inline_payload,
                                        payload_kind=str(
                                            descriptor["payload_kind"]
                                        ),
                                    )
                                )
                            except (PlotInitError, TypeError, ValueError):
                                stats["corrupt_rows"] = (
                                    int(stats["corrupt_rows"]) + 1
                                )
                                corrupt_rows += 1
                                continue
                            if inline_hash != normalized_hash:
                                stats["corrupt_rows"] = (
                                    int(stats["corrupt_rows"]) + 1
                                )
                                corrupt_rows += 1
                                continue
                            uncompressed_bytes = len(raw)
                        valid_referenced_hashes.add(normalized_hash)
                        projected_hashes.add(normalized_hash)
                        stats["valid_row_refs"] = (
                            int(stats["valid_row_refs"]) + 1
                        )
                        valid_current_refs += 1
                        current_referenced_uncompressed_bytes += (
                            uncompressed_bytes
                        )
                    else:
                        stats["legacy_rows"] = int(stats["legacy_rows"]) + 1
                        stats["rewrite_rows"] = int(stats["rewrite_rows"]) + 1
                        stats["legacy_inline_bytes"] = (
                            int(stats["legacy_inline_bytes"]) + inline_size
                        )
                        legacy_rows += 1
                        rewrite_rows += 1
                        legacy_inline_bytes += inline_size
                        if inline_size > self.max_payload_bytes:
                            stats["oversized_rows"] = (
                                int(stats["oversized_rows"]) + 1
                            )
                            oversized_rows += 1
                            continue
                        try:
                            inline_payload = self._decode(inline_json)
                            _text, raw, normalized_hash = (
                                self._canonical_payload(
                                    inline_payload,
                                    payload_kind=str(
                                        descriptor["payload_kind"]
                                    ),
                                )
                            )
                            codec, candidate_payload = self._encoded_blob(raw)
                        except (PlotInitError, TypeError, ValueError):
                            stats["corrupt_rows"] = (
                                int(stats["corrupt_rows"]) + 1
                            )
                            corrupt_rows += 1
                            continue
                        if normalized_hash in blob_metadata:
                            try:
                                _verified_hash, uncompressed_bytes = (
                                    verify_blob(normalized_hash)
                                )
                            except PlotInitError:
                                stats["corrupt_rows"] = (
                                    int(stats["corrupt_rows"]) + 1
                                )
                                corrupt_rows += 1
                                continue
                            if uncompressed_bytes != len(raw):
                                stats["corrupt_rows"] = (
                                    int(stats["corrupt_rows"]) + 1
                                )
                                corrupt_rows += 1
                                continue
                        else:
                            candidate_hashes.setdefault(
                                normalized_hash,
                                {
                                    "uncompressed_bytes": len(raw),
                                    "encoded_bytes": len(candidate_payload),
                                    "codec_is_zlib": int(codec == "zlib"),
                                },
                            )
                            uncompressed_bytes = len(raw)
                        projected_hashes.add(normalized_hash)

                    valid_rows += 1
                    stats["valid_rows"] = int(stats["valid_rows"]) + 1
                    projected_referenced_uncompressed_bytes += (
                        uncompressed_bytes
                    )
                    projected_reference_bytes += len(
                        self._blob_reference(normalized_hash).encode("utf-8")
                    )

            existing_hashes = set(blob_metadata)
            current_unique_uncompressed = sum(
                int(blob_metadata[blob_hash]["uncompressed_bytes"])
                for blob_hash in valid_referenced_hashes
                if blob_hash in blob_metadata
                and int(blob_metadata[blob_hash]["uncompressed_bytes"]) >= 0
            )
            projected_unique_uncompressed = 0
            projected_referenced_blob_payload_bytes = 0
            for blob_hash in projected_hashes:
                if blob_hash in blob_metadata:
                    projected_unique_uncompressed += int(
                        blob_metadata[blob_hash]["uncompressed_bytes"]
                    )
                    projected_referenced_blob_payload_bytes += int(
                        blob_metadata[blob_hash]["encoded_bytes"]
                    )
                else:
                    projected_unique_uncompressed += int(
                        candidate_hashes[blob_hash]["uncompressed_bytes"]
                    )
                    projected_referenced_blob_payload_bytes += int(
                        candidate_hashes[blob_hash]["encoded_bytes"]
                    )
            candidate_blob_payload_bytes = sum(
                int(value["encoded_bytes"])
                for value in candidate_hashes.values()
            )
            current_payload_storage_bytes = (
                inline_bytes_total + existing_blob_payload_bytes
            )
            projected_payload_storage_bytes = (
                projected_reference_bytes
                + existing_blob_payload_bytes
                + candidate_blob_payload_bytes
            )
            projected_payload_storage_with_cleanup = (
                projected_reference_bytes
                + projected_referenced_blob_payload_bytes
            )
            current_orphan_hashes = existing_hashes - referenced_hashes
            projected_orphan_hashes = existing_hashes - projected_hashes
            dedup_bytes = max(
                projected_referenced_uncompressed_bytes
                - projected_unique_uncompressed,
                0,
            )
            compression_bytes = max(
                projected_unique_uncompressed
                - projected_referenced_blob_payload_bytes,
                0,
            )
            blocked_reasons: list[str] = []
            if quick_check != "ok":
                blocked_reasons.append("quick_check_failed")
            if oversized_rows:
                blocked_reasons.append("oversized_payloads")
            if corrupt_rows:
                blocked_reasons.append("corrupt_payloads")
            migration_required = bool(
                stored_version < DATABASE_SCHEMA_VERSION
                or rewrite_rows
                or reconcile_rows
            )
            return {
                "status": "inspected",
                "database_path": str(self.database_path),
                "exists": True,
                "schema": {
                    "stored_version": stored_version,
                    "supported_version": DATABASE_SCHEMA_VERSION,
                    "payload_blob_table_present": self._table_exists(
                        connection,
                        "initialization_payload_blobs",
                    ),
                },
                "file": file_metrics,
                "tables": table_results,
                "rows": {
                    "total": total_rows,
                    "valid": valid_rows,
                    "row_refs": valid_current_refs,
                    "projected_row_refs": valid_rows,
                    "legacy": legacy_rows,
                    "rewrite": rewrite_rows,
                    "reconcile": reconcile_rows,
                    "oversized": oversized_rows,
                    "corrupt": corrupt_rows,
                },
                "blobs": {
                    "existing": len(existing_hashes),
                    "existing_referenced": len(referenced_hashes),
                    "existing_orphan": len(current_orphan_hashes),
                    "candidate_unique": len(candidate_hashes),
                    "projected_unique": len(
                        existing_hashes | set(candidate_hashes)
                    ),
                    "projected_referenced_unique": len(projected_hashes),
                    "projected_orphan": len(projected_orphan_hashes),
                },
                "bytes": {
                    "inline": inline_bytes_total,
                    "legacy_inline": legacy_inline_bytes,
                    "existing_blob_payload": existing_blob_payload_bytes,
                    "candidate_blob_payload": candidate_blob_payload_bytes,
                    "current_payload_storage": current_payload_storage_bytes,
                    "projected_reference": projected_reference_bytes,
                    "projected_payload_storage": (
                        projected_payload_storage_bytes
                    ),
                    "projected_payload_storage_with_orphan_cleanup": (
                        projected_payload_storage_with_cleanup
                    ),
                    "estimated_savings": max(
                        current_payload_storage_bytes
                        - projected_payload_storage_bytes,
                        0,
                    ),
                    "estimated_savings_with_orphan_cleanup": max(
                        current_payload_storage_bytes
                        - projected_payload_storage_with_cleanup,
                        0,
                    ),
                    "current_dedup": max(
                        current_referenced_uncompressed_bytes
                        - current_unique_uncompressed,
                        0,
                    ),
                    "dedup": dedup_bytes,
                    "compression": compression_bytes,
                },
                "row_refs": valid_current_refs,
                "projected_row_refs": valid_rows,
                "unique_blobs": len(existing_hashes),
                "projected_unique_blobs": len(
                    existing_hashes | set(candidate_hashes)
                ),
                "dedup_bytes": dedup_bytes,
                "quick_check": quick_check,
                "migration_required": migration_required,
                "blocked": bool(blocked_reasons),
                "blocked_reasons": blocked_reasons,
            }

    def _default_backup_path(self, stored_version: int) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return (
            self.database_path.parent
            / "backups"
            / (
                f"{self.database_path.name}.storage-v{stored_version}."
                f"{stamp}.{uuid.uuid4().hex}.bak"
            )
        )

    def _resolve_backup_path(
        self,
        backup_path: Path | str | None,
        *,
        stored_version: int,
    ) -> Path:
        destination = (
            self._default_backup_path(stored_version)
            if backup_path is None or str(backup_path) == "auto"
            else Path(backup_path).expanduser().resolve(strict=False)
        )
        destination = destination.resolve(strict=False)
        sidecar_paths = {
            Path(f"{self.database_path}{suffix}").resolve(strict=False)
            for suffix in ("-journal", "-wal", "-shm")
        }
        if destination == self.database_path or destination in sidecar_paths:
            raise PlotInitError(
                "INIT_STORAGE_BACKUP_PATH_INVALID",
                "initialization backup path must differ from the source database "
                "and its SQLite sidecar files",
                backup_path=str(destination),
            )
        if destination.exists():
            raise PlotInitError(
                "INIT_STORAGE_BACKUP_EXISTS",
                "initialization backup path already exists",
                backup_path=str(destination),
            )
        return destination

    @staticmethod
    def _nearest_existing_directory(path: Path) -> Path:
        candidate = path
        while not candidate.exists():
            parent = candidate.parent
            if parent == candidate:
                raise PlotInitError(
                    "INIT_STORAGE_VOLUME_NOT_FOUND",
                    "no existing directory was found for the storage path",
                    path=str(path),
                )
            candidate = parent
        if not candidate.is_dir():
            raise PlotInitError(
                "INIT_STORAGE_PATH_INVALID",
                "storage path is nested beneath a non-directory path",
                path=str(path),
                existing_path=str(candidate),
            )
        return candidate

    @staticmethod
    def _volume_key(path: Path) -> str:
        existing = InitStorage._nearest_existing_directory(path)
        if os.name == "nt":
            drive, _tail = os.path.splitdrive(str(existing))
            anchor = drive or existing.anchor
            return f"windows:{os.path.normcase(anchor)}"
        return f"device:{int(os.stat(existing).st_dev)}"

    @staticmethod
    def _disk_usage_for_path(path: Path) -> Any:
        existing = InitStorage._nearest_existing_directory(path)
        return shutil.disk_usage(existing)

    def _migration_space_check(
        self,
        backup_path: Path,
        file_metrics: dict[str, Any],
        *,
        compact: bool,
    ) -> dict[str, Any]:
        estimated_backup_bytes = max(
            int(file_metrics.get("database_bytes") or 0),
            int(file_metrics.get("allocated_page_bytes") or 0),
            1,
        )
        minimum_reserve = 16 * 1024 * 1024
        backup_peak_bytes = max(
            estimated_backup_bytes * 2,
            minimum_reserve,
        )
        vacuum_peak_bytes = max(
            estimated_backup_bytes * 2,
            minimum_reserve,
        )
        source_path = self.database_path.parent
        backup_parent = backup_path.parent
        same_volume = (
            self._volume_key(source_path)
            == self._volume_key(backup_parent)
        )
        source_usage = self._disk_usage_for_path(source_path)
        backup_usage = (
            source_usage
            if same_volume
            else self._disk_usage_for_path(backup_parent)
        )

        if same_volume:
            required_free_bytes = max(
                backup_peak_bytes,
                (
                    estimated_backup_bytes + vacuum_peak_bytes
                    if compact
                    else 0
                ),
            )
            if int(source_usage.free) < required_free_bytes:
                raise PlotInitError(
                    (
                        "INIT_STORAGE_COMPACT_SPACE_LOW"
                        if compact
                        else "INIT_STORAGE_BACKUP_SPACE_LOW"
                    ),
                    "insufficient free space for initialization backup and migration",
                    volume=self._volume_key(source_path),
                    free_bytes=int(source_usage.free),
                    required_free_bytes=required_free_bytes,
                    estimated_backup_bytes=estimated_backup_bytes,
                    compact=bool(compact),
                )
            source_required = required_free_bytes
            backup_required = required_free_bytes
        else:
            backup_required = backup_peak_bytes
            source_required = vacuum_peak_bytes if compact else 0
            if int(backup_usage.free) < backup_required:
                raise PlotInitError(
                    "INIT_STORAGE_BACKUP_SPACE_LOW",
                    "insufficient free space on the initialization backup volume",
                    volume=self._volume_key(backup_parent),
                    free_bytes=int(backup_usage.free),
                    required_free_bytes=backup_required,
                    estimated_backup_bytes=estimated_backup_bytes,
                )
            if compact and int(source_usage.free) < source_required:
                raise PlotInitError(
                    "INIT_STORAGE_COMPACT_SPACE_LOW",
                    "insufficient free space on the initialization database volume",
                    volume=self._volume_key(source_path),
                    free_bytes=int(source_usage.free),
                    required_free_bytes=source_required,
                    estimated_backup_bytes=estimated_backup_bytes,
                )
        return {
            "same_volume": same_volume,
            "estimated_backup_bytes": estimated_backup_bytes,
            "backup_peak_bytes": backup_peak_bytes,
            "vacuum_peak_bytes": vacuum_peak_bytes if compact else 0,
            "source": {
                "path": str(source_path),
                "free_bytes": int(source_usage.free),
                "required_free_bytes": source_required,
            },
            "backup": {
                "path": str(backup_parent),
                "free_bytes": int(backup_usage.free),
                "required_free_bytes": backup_required,
            },
        }

    def _backup_database(
        self,
        destination: Path,
        *,
        source: sqlite3.Connection | None = None,
        anchor_stat: os.stat_result | None = None,
        retain_identity: bool = False,
    ) -> (
        tuple[Path, str]
        | tuple[Path, str, _HeldInitBackupIdentity]
    ):
        destination.parent.mkdir(parents=True, exist_ok=True)

        descriptor, raw_staged_path = tempfile.mkstemp(
            prefix=f".{destination.name}.staging.",
            suffix=".sqlite3",
            dir=destination.parent,
        )
        staged_path = Path(raw_staged_path)
        staged_anchor = os.fdopen(descriptor, "rb")
        staged_stat = os.fstat(staged_anchor.fileno())
        publication_path: Path | None = None
        publication_anchor: BinaryIO | None = None
        publication_stat: os.stat_result | None = None
        retained_public_anchor: BinaryIO | None = None
        published = False

        def path_matches(
            path: Path,
            expected: os.stat_result,
        ) -> bool:
            try:
                current = os.stat(path, follow_symlinks=False)
            except OSError:
                return False
            return os.path.samestat(expected, current)

        def require_staged_identity() -> None:
            if not path_matches(staged_path, staged_stat):
                raise PlotInitError(
                    "INIT_STORAGE_BACKUP_STAGE_CHANGED",
                    "initialization backup staging path changed during backup",
                    backup_path=str(destination),
                    staging_path=str(staged_path),
                )

        def require_destination_identity() -> None:
            if (
                publication_stat is None
                or not path_matches(destination, publication_stat)
            ):
                raise PlotInitError(
                    "INIT_STORAGE_BACKUP_PATH_CHANGED",
                    "initialization backup destination changed during publication",
                    backup_path=str(destination),
                )

        def open_file_sha256(stream: BinaryIO) -> str:
            stream.seek(0)
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            return digest.hexdigest()

        def cleanup_owned_paths() -> None:
            if path_matches(staged_path, staged_stat):
                with suppress(OSError):
                    staged_path.unlink()
            if (
                publication_path is not None
                and publication_stat is not None
                and path_matches(publication_path, publication_stat)
            ):
                with suppress(OSError):
                    publication_path.unlink()
            if (
                published
                and publication_stat is not None
                and path_matches(destination, publication_stat)
            ):
                with suppress(OSError):
                    destination.unlink()

        try:
            source_context = (
                closing(
                    sqlite3.connect(
                        f"{self.database_path.as_uri()}?mode=ro",
                        uri=True,
                        timeout=30.0,
                    )
                )
                if source is None
                else nullcontext(source)
            )
            with source_context as backup_source, closing(
                sqlite3.connect(staged_path, timeout=30.0)
            ) as target:
                backup_source.execute("PRAGMA busy_timeout=30000")
                if anchor_stat is not None:
                    self._assert_database_identity(anchor_stat)
                require_staged_identity()
                backup_source.backup(target)
                target.commit()
                if anchor_stat is not None:
                    self._assert_database_identity(anchor_stat)
                require_staged_identity()
                target.row_factory = sqlite3.Row
                quick_check = self._quick_check(target)
                if quick_check != "ok":
                    raise PlotInitError(
                        "INIT_STORAGE_BACKUP_INVALID",
                        "initialization online backup failed its integrity check",
                        backup_path=str(destination),
                        quick_check=quick_check,
                    )
            require_staged_identity()
            staged_sha256 = open_file_sha256(staged_anchor)
            require_staged_identity()

            publication_descriptor, raw_publication_path = tempfile.mkstemp(
                prefix=f".{destination.name}.publication.",
                suffix=".sqlite3",
                dir=destination.parent,
            )
            publication_path = Path(raw_publication_path)
            publication_anchor = os.fdopen(
                publication_descriptor,
                "w+b",
            )
            publication_stat = os.fstat(publication_anchor.fileno())
            staged_anchor.seek(0)
            shutil.copyfileobj(
                staged_anchor,
                publication_anchor,
                length=1024 * 1024,
            )
            publication_anchor.flush()
            os.fsync(publication_anchor.fileno())
            if not path_matches(publication_path, publication_stat):
                raise PlotInitError(
                    "INIT_STORAGE_BACKUP_PUBLISH_STAGE_CHANGED",
                    "initialization backup publication staging changed during copy",
                    backup_path=str(destination),
                    publication_path=str(publication_path),
                )
            if open_file_sha256(publication_anchor) != staged_sha256:
                raise PlotInitError(
                    "INIT_STORAGE_BACKUP_PUBLISH_COPY_CHANGED",
                    "initialization backup publication copy does not match private staging",
                    backup_path=str(destination),
                    publication_path=str(publication_path),
                )
            publication_anchor.close()
            publication_anchor = None
            try:
                os.link(publication_path, destination)
            except FileExistsError as exc:
                raise PlotInitError(
                    "INIT_STORAGE_BACKUP_EXISTS",
                    "initialization backup path already exists",
                    backup_path=str(destination),
                ) from exc
            except OSError as exc:
                raise PlotInitError(
                    "INIT_STORAGE_BACKUP_PUBLISH_FAILED",
                    "initialization backup could not be published atomically",
                    backup_path=str(destination),
                    staging_path=str(staged_path),
                    error_type=type(exc).__name__,
                ) from exc
            published = True
            require_staged_identity()
            require_destination_identity()
            if (
                publication_path is not None
                and publication_stat is not None
                and path_matches(publication_path, publication_stat)
            ):
                publication_path.unlink()
        except BaseException:
            if publication_anchor is not None:
                publication_anchor.close()
            if retained_public_anchor is not None:
                retained_public_anchor.close()
            staged_anchor.close()
            cleanup_owned_paths()
            raise
        if retain_identity:
            try:
                retained_public_anchor = destination.open("rb")
                opened_public_stat = os.fstat(
                    retained_public_anchor.fileno()
                )
                if (
                    publication_stat is None
                    or not os.path.samestat(
                        publication_stat,
                        opened_public_stat,
                    )
                    or not path_matches(destination, opened_public_stat)
                    or open_file_sha256(retained_public_anchor)
                    != staged_sha256
                ):
                    raise PlotInitError(
                        "INIT_STORAGE_BACKUP_PATH_CHANGED",
                        "initialization backup changed while retaining "
                        "its public identity",
                        backup_path=str(destination),
                    )
            except BaseException:
                if retained_public_anchor is not None:
                    retained_public_anchor.close()
                staged_anchor.close()
                cleanup_owned_paths()
                raise
            return (
                destination,
                quick_check,
                _HeldInitBackupIdentity(
                    path=destination,
                    staging_path=staged_path,
                    anchor=staged_anchor,
                    stat=staged_stat,
                    public_anchor=retained_public_anchor,
                    public_stat=opened_public_stat,
                    sha256=staged_sha256,
                ),
            )
        staged_anchor.close()
        try:
            require_staged_identity()
            staged_path.unlink()
            require_destination_identity()
        except BaseException:
            cleanup_owned_paths()
            raise
        return destination, quick_check

    @staticmethod
    def _verify_backup_identity(
        held: _HeldInitBackupIdentity,
    ) -> None:
        try:
            destination_stat = os.stat(
                held.path,
                follow_symlinks=False,
            )
            staging_stat = os.stat(
                held.staging_path,
                follow_symlinks=False,
            )
            held.anchor.seek(0)
            digest = hashlib.sha256()
            for chunk in iter(
                lambda: held.anchor.read(1024 * 1024),
                b"",
            ):
                digest.update(chunk)
            held.public_anchor.seek(0)
            public_digest = hashlib.sha256()
            for chunk in iter(
                lambda: held.public_anchor.read(1024 * 1024),
                b"",
            ):
                public_digest.update(chunk)
        except (OSError, ValueError) as exc:
            raise PlotInitError(
                "INIT_STORAGE_BACKUP_CHANGED",
                "initialization backup became unavailable during migration",
                backup_path=str(held.path),
            ) from exc
        if (
            not os.path.samestat(held.public_stat, destination_stat)
            or not os.path.samestat(held.stat, staging_stat)
            or digest.hexdigest() != held.sha256
            or public_digest.hexdigest() != held.sha256
        ):
            raise PlotInitError(
                "INIT_STORAGE_BACKUP_CHANGED",
                "initialization backup identity or content changed during migration",
                backup_path=str(held.path),
            )

    @staticmethod
    def _held_private_backup_is_valid(
        held: _HeldInitBackupIdentity,
    ) -> bool:
        try:
            staging_stat = os.stat(
                held.staging_path,
                follow_symlinks=False,
            )
            held.anchor.seek(0)
            digest = hashlib.sha256()
            for chunk in iter(
                lambda: held.anchor.read(1024 * 1024),
                b"",
            ):
                digest.update(chunk)
        except (OSError, ValueError):
            return False
        return (
            os.path.samestat(held.stat, staging_stat)
            and digest.hexdigest() == held.sha256
        )

    @staticmethod
    def _publish_backup_recovery(
        held: _HeldInitBackupIdentity,
    ) -> Path:
        if not InitStorage._held_private_backup_is_valid(held):
            raise PlotInitError(
                "INIT_STORAGE_BACKUP_RECOVERY_LOST",
                "retained initialization backup staging is unavailable",
                backup_path=str(held.path),
                staging_path=str(held.staging_path),
            )
        for _attempt in range(8):
            recovery_path = held.path.with_name(
                f"{held.path.name}.recovery-{uuid.uuid4().hex}.bak"
            )
            try:
                os.link(held.staging_path, recovery_path)
            except FileExistsError:
                continue
            except OSError:
                # The staging file is already private and uniquely named.
                return held.staging_path
            try:
                recovery_stat = os.stat(
                    recovery_path,
                    follow_symlinks=False,
                )
            except OSError:
                continue
            if os.path.samestat(held.stat, recovery_stat):
                return recovery_path
        return held.staging_path

    @staticmethod
    def _release_backup_identity(
        held: _HeldInitBackupIdentity | None,
        *,
        remove_invalid_owned_publication: bool = False,
        preserve_staging: bool = False,
    ) -> None:
        if held is None:
            return
        with suppress(OSError, ValueError):
            held.anchor.close()
        with suppress(OSError, ValueError):
            held.public_anchor.close()
        try:
            staging_stat = os.stat(
                held.staging_path,
                follow_symlinks=False,
            )
        except OSError:
            staging_stat = None
        if (
            staging_stat is not None
            and os.path.samestat(held.stat, staging_stat)
            and not preserve_staging
        ):
            with suppress(OSError):
                held.staging_path.unlink()
        if not remove_invalid_owned_publication:
            return
        try:
            destination_stat = os.stat(
                held.path,
                follow_symlinks=False,
            )
        except OSError:
            return
        if os.path.samestat(held.public_stat, destination_stat):
            with suppress(OSError):
                held.path.unlink()

    def _migrate_payload_rows(
        self,
        connection: sqlite3.Connection,
    ) -> dict[str, int]:
        migrated_rows = 0
        reconciled_rows = 0
        for descriptor in PAYLOAD_MIGRATION_TABLES:
            table = str(descriptor["table"])
            json_column = str(descriptor["json_column"])
            blob_column = str(descriptor["blob_column"])
            payload_kind = str(descriptor["payload_kind"])
            last_rowid: int | None = None
            while True:
                if last_rowid is None:
                    row = connection.execute(
                        f"""
                        SELECT rowid AS "_storage_rowid",
                               "{json_column}" AS "_inline_json",
                               "{blob_column}" AS "_blob_hash"
                        FROM "{table}"
                        ORDER BY rowid
                        LIMIT 1
                        """
                    ).fetchone()
                else:
                    row = connection.execute(
                        f"""
                        SELECT rowid AS "_storage_rowid",
                               "{json_column}" AS "_inline_json",
                               "{blob_column}" AS "_blob_hash"
                        FROM "{table}"
                        WHERE rowid > ?
                        ORDER BY rowid
                        LIMIT 1
                        """,
                        (last_rowid,),
                    ).fetchone()
                if row is None:
                    break
                rowid = int(row["_storage_rowid"])
                last_rowid = rowid
                inline_json = str(row["_inline_json"])
                explicit_reference = str(row["_blob_hash"] or "").strip()
                inline_reference = self._reference_hash(inline_json)
                if (
                    explicit_reference
                    and inline_reference
                    and explicit_reference.casefold()
                    != inline_reference.casefold()
                ):
                    raise PlotInitError(
                        "CORRUPT_INIT_BLOB_REFERENCE",
                        "inline and column initialization blob references disagree",
                        table=table,
                        rowid=rowid,
                    )
                if inline_reference:
                    blob_hash = self._validated_blob_hash(
                        explicit_reference or inline_reference
                    )
                    self._load_blob(connection, blob_hash)
                    reference = self._blob_reference(blob_hash)
                    if (
                        explicit_reference != blob_hash
                        or inline_json != reference
                    ):
                        connection.execute(
                            f"""
                            UPDATE "{table}"
                            SET "{json_column}"=?, "{blob_column}"=?
                            WHERE rowid=?
                            """,
                            (reference, blob_hash, rowid),
                        )
                        reconciled_rows += 1
                    continue

                payload = self._decode_payload(
                    connection,
                    inline_json,
                    None,
                )
                blob_hash, reference = self._store_blob(
                    connection,
                    payload,
                    payload_kind=payload_kind,
                )
                if (
                    explicit_reference
                    and self._validated_blob_hash(explicit_reference)
                    != blob_hash
                ):
                    raise PlotInitError(
                        "CORRUPT_INIT_BLOB_REFERENCE",
                        "inline payload and column blob hash disagree",
                        table=table,
                        rowid=rowid,
                    )
                connection.execute(
                    f"""
                    UPDATE "{table}"
                    SET "{json_column}"=?, "{blob_column}"=?
                    WHERE rowid=?
                    """,
                    (reference, blob_hash, rowid),
                )
                migrated_rows += 1
        return {
            "migrated_rows": migrated_rows,
            "reconciled_rows": reconciled_rows,
        }

    @staticmethod
    def _delete_orphan_blobs(connection: sqlite3.Connection) -> int:
        return int(
            connection.execute(
                """
                DELETE FROM initialization_payload_blobs AS blob
                WHERE NOT EXISTS(
                    SELECT 1 FROM initialization_sessions
                    WHERE state_blob_hash=blob.blob_hash
                )
                  AND NOT EXISTS(
                    SELECT 1 FROM initialization_revisions
                    WHERE state_blob_hash=blob.blob_hash
                )
                  AND NOT EXISTS(
                    SELECT 1 FROM initialization_checkpoints
                    WHERE state_blob_hash=blob.blob_hash
                )
                  AND NOT EXISTS(
                    SELECT 1 FROM initialization_idempotency
                    WHERE response_blob_hash=blob.blob_hash
                )
                """
            ).rowcount
        )

    def _compact_database(self) -> None:
        with closing(
            sqlite3.connect(self.database_path, timeout=30.0)
        ) as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("VACUUM")

    def migrate_payload_storage(
        self,
        *,
        dry_run: bool = True,
        backup_path: Path | str | None = None,
        compact: bool = False,
        cleanup_orphans: bool = False,
    ) -> dict[str, Any]:
        """Explicitly migrate inline state payloads to schema-v2 blob storage."""

        if dry_run:
            before = self.migration_plan()
            before_bytes = int(before["file"]["database_bytes"])
            projected_unique_blobs = int(
                before["blobs"]["projected_referenced_unique"]
                if cleanup_orphans
                else before["projected_unique_blobs"]
            )
            return {
                "status": "dry_run",
                "dry_run": True,
                "database_path": str(self.database_path),
                "before_bytes": before_bytes,
                "after_bytes": before_bytes,
                "row_refs": int(before["projected_row_refs"]),
                "unique_blobs": projected_unique_blobs,
                "dedup_bytes": int(before["dedup_bytes"]),
                "backup_path": None,
                "quick_check": before["quick_check"],
                "migration_required": bool(before["migration_required"]),
                "blocked": bool(before["blocked"]),
                "blocked_reasons": list(before["blocked_reasons"]),
                "cleanup_orphans": bool(cleanup_orphans),
                "compacted": False,
                "space_check": None,
                "plan": before,
            }
        if not self.exists:
            raise PlotInitError(
                "INIT_STORAGE_NOT_CREATED",
                f"initialization database does not exist: {self.database_path}",
            )
        backup: Path | None = None
        backup_quick_check = ""
        space_check: dict[str, Any] | None = None
        compact_space: dict[str, Any] | None = None
        counts = {"migrated_rows": 0, "reconciled_rows": 0}
        orphan_blobs_removed = 0
        failure_phase = "locked_inspection"
        migration_committed = False
        backup_identity_invalid = False
        held_backup: _HeldInitBackupIdentity | None = None
        preserve_recovery_staging = False
        initial_phase_complete = False

        def attach_post_commit_recovery(exc: BaseException) -> None:
            nonlocal preserve_recovery_staging
            if held_backup is None:
                return
            invalid_backup_path = str(backup or held_backup.path)
            try:
                recovery_path = self._publish_backup_recovery(held_backup)
            except PlotInitError as recovery_exc:
                if isinstance(exc, PlotInitError):
                    exc.details.setdefault(
                        "backup_recovery_error_code",
                        recovery_exc.code,
                    )
                    exc.details.setdefault(
                        "backup_recovery_error",
                        str(recovery_exc),
                    )
                return
            preserve_recovery_staging = (
                recovery_path == held_backup.staging_path
            )
            if isinstance(exc, PlotInitError):
                exc.details["invalid_backup_path"] = invalid_backup_path
                exc.details["backup_path"] = str(recovery_path)
                exc.details["recovery_backup_path"] = str(recovery_path)

        anchor = self.database_path.open("rb")
        anchor_stat = os.fstat(anchor.fileno())
        connection: sqlite3.Connection | None = None
        backup_source: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{self.database_path.as_uri()}?mode=rw",
                uri=True,
                timeout=30.0,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            self._assert_database_identity(anchor_stat)
            backup_source = sqlite3.connect(
                f"{self.database_path.as_uri()}?mode=ro",
                uri=True,
                timeout=30.0,
            )
            backup_source.execute("PRAGMA busy_timeout=30000")
            self._assert_database_identity(anchor_stat)
            connection.execute("BEGIN IMMEDIATE")
            self._assert_database_identity(anchor_stat)
            before = self.migration_plan(_connection=connection)
            before_bytes = int(before["file"]["database_bytes"])
            if bool(before["blocked"]):
                raise PlotInitError(
                    "INIT_STORAGE_MIGRATION_BLOCKED",
                    "initialization payload migration inspection found blockers",
                    blocked_reasons=list(before["blocked_reasons"]),
                    oversized_rows=int(before["rows"]["oversized"]),
                    corrupt_rows=int(before["rows"]["corrupt"]),
                    quick_check=before["quick_check"],
                )

            stored_version = int(before["schema"]["stored_version"])
            failure_phase = "backup_preflight"
            destination = self._resolve_backup_path(
                backup_path,
                stored_version=stored_version,
            )
            space_check = self._migration_space_check(
                destination,
                dict(before["file"]),
                compact=compact,
            )
            compact_space = (
                {
                    **dict(space_check["source"]),
                    "same_volume_as_backup": bool(space_check["same_volume"]),
                }
                if compact
                else None
            )
            failure_phase = "backup"
            backup, backup_quick_check, held_backup = self._backup_database(
                destination,
                source=backup_source,
                anchor_stat=anchor_stat,
                retain_identity=True,
            )
            failure_phase = "backup_identity_verify"
            try:
                self._verify_backup_identity(held_backup)
            except PlotInitError:
                backup_identity_invalid = True
                raise
            failure_phase = "schema_initialize"
            self._assert_database_identity(anchor_stat)
            self._upgrade_schema_in_transaction(connection)
            failure_phase = "payload_migration"
            counts = self._migrate_payload_rows(connection)
            orphan_blobs_removed = (
                self._delete_orphan_blobs(connection)
                if cleanup_orphans
                else 0
            )
            self._assert_database_identity(anchor_stat)
            failure_phase = "pre_commit_backup_verify"
            try:
                self._verify_backup_identity(held_backup)
            except PlotInitError:
                backup_identity_invalid = True
                raise
            connection.commit()
            migration_committed = True
            failure_phase = "post_commit_identity_verify"
            self._assert_database_identity(anchor_stat)
            try:
                self._verify_backup_identity(held_backup)
            except PlotInitError:
                backup_identity_invalid = True
                raise
            initial_phase_complete = True
        except BaseException as exc:
            if connection is not None:
                with suppress(sqlite3.Error):
                    connection.rollback()
            if migration_committed:
                attach_post_commit_recovery(exc)
            self._release_backup_identity(
                held_backup,
                remove_invalid_owned_publication=(
                    backup_identity_invalid and not migration_committed
                ),
                preserve_staging=preserve_recovery_staging,
            )
            held_backup = None
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, PlotInitError):
                if backup is not None:
                    exc.details.setdefault("backup_path", str(backup))
                if failure_phase not in {
                    "locked_inspection",
                    "backup_preflight",
                }:
                    exc.details.setdefault("failure_phase", failure_phase)
                    exc.details.setdefault(
                        "migration_committed",
                        migration_committed,
                    )
                raise
            details: dict[str, Any] = {
                "failure_phase": failure_phase,
                "migration_committed": migration_committed,
                "error_type": type(exc).__name__,
            }
            if backup is not None:
                details["backup_path"] = str(backup)
            raise PlotInitError(
                "INIT_STORAGE_MIGRATION_FAILED",
                "initialization storage migration failed",
                **details,
            ) from exc
        finally:
            if backup_source is not None:
                backup_source.close()
            if connection is not None:
                connection.close()
            if not initial_phase_complete:
                anchor.close()

        if backup is None or space_check is None:
            anchor.close()
            raise PlotInitError(
                "INIT_STORAGE_MIGRATION_FAILED",
                "initialization storage migration finished without a backup",
                failure_phase="backup",
                migration_committed=False,
            )

        after: dict[str, Any] | None = None
        try:
            self._assert_database_identity(anchor_stat)
            self._verify_backup_identity(held_backup)
            try:
                with closing(
                    sqlite3.connect(
                        f"{backup.as_uri()}?mode=ro&immutable=1",
                        uri=True,
                        timeout=30.0,
                    )
                ) as backup_check:
                    backup_check.row_factory = sqlite3.Row
                    post_commit_backup_check = self._quick_check(backup_check)
            except (OSError, sqlite3.DatabaseError) as exc:
                raise PlotInitError(
                    "INIT_STORAGE_BACKUP_LOST",
                    "initialization migration committed but its backup is unavailable",
                    backup_path=str(backup),
                    failure_phase="post_commit_backup_verify",
                    migration_committed=True,
                    error_type=type(exc).__name__,
                ) from exc
            if post_commit_backup_check != "ok":
                raise PlotInitError(
                    "INIT_STORAGE_BACKUP_LOST",
                    "initialization migration committed but its backup is invalid",
                    backup_path=str(backup),
                    failure_phase="post_commit_backup_verify",
                    migration_committed=True,
                    quick_check=post_commit_backup_check,
                )

            if compact:
                try:
                    self._assert_database_identity(anchor_stat)
                    self._compact_database()
                    self._assert_database_identity(anchor_stat)
                except Exception as exc:
                    if isinstance(exc, PlotInitError):
                        exc.details.setdefault("backup_path", str(backup))
                        exc.details.setdefault("failure_phase", "compact")
                        exc.details.setdefault("migration_committed", True)
                        raise
                    raise PlotInitError(
                        "INIT_STORAGE_COMPACT_FAILED",
                        "initialization payloads migrated but database "
                        "compaction failed",
                        backup_path=str(backup),
                        failure_phase="compact",
                        migration_committed=True,
                        error_type=type(exc).__name__,
                    ) from exc

            try:
                self._assert_database_identity(anchor_stat)
                after = self.migration_plan()
                self._assert_database_identity(anchor_stat)
            except Exception as exc:
                if isinstance(exc, PlotInitError):
                    exc.details.setdefault("backup_path", str(backup))
                    exc.details.setdefault(
                        "failure_phase",
                        "post_migration_verify",
                    )
                    exc.details.setdefault("migration_committed", True)
                    raise
                raise PlotInitError(
                    "INIT_STORAGE_POST_VERIFY_FAILED",
                    "initialization payloads migrated but post-migration "
                    "verification failed",
                    backup_path=str(backup),
                    failure_phase="post_migration_verify",
                    migration_committed=True,
                    error_type=type(exc).__name__,
                ) from exc
            if bool(after["blocked"]) or bool(after["migration_required"]):
                raise PlotInitError(
                    "INIT_STORAGE_POST_VERIFY_FAILED",
                    "initialization payload migration committed but "
                    "verification is incomplete",
                    backup_path=str(backup),
                    failure_phase="post_migration_verify",
                    migration_committed=True,
                    blocked=bool(after["blocked"]),
                    blocked_reasons=list(after["blocked_reasons"]),
                    migration_required=bool(after["migration_required"]),
                )

            # Keep both the public inode and the independent private staging
            # anchored until VACUUM and the final live-database verification
            # have finished.  Otherwise a late backup replacement can destroy
            # the only restorable pre-migration snapshot.
            self._assert_database_identity(anchor_stat)
            self._verify_backup_identity(held_backup)
        except BaseException as exc:
            try:
                self._verify_backup_identity(held_backup)
            except PlotInitError as backup_exc:
                if isinstance(exc, PlotInitError):
                    exc.details.setdefault(
                        "backup_validation_error_code",
                        backup_exc.code,
                    )
                    exc.details.setdefault(
                        "backup_validation_error",
                        str(backup_exc),
                    )
                    attach_post_commit_recovery(exc)
                else:
                    try:
                        recovery_path = self._publish_backup_recovery(
                            held_backup
                        )
                    except PlotInitError:
                        pass
                    else:
                        preserve_recovery_staging = (
                            recovery_path == held_backup.staging_path
                        )
            if isinstance(exc, PlotInitError):
                exc.details.setdefault("backup_path", str(backup))
                exc.details.setdefault("migration_committed", True)
            raise
        finally:
            self._release_backup_identity(
                held_backup,
                preserve_staging=preserve_recovery_staging,
            )
            held_backup = None
            anchor.close()

        if after is None:
            raise PlotInitError(
                "INIT_STORAGE_POST_VERIFY_FAILED",
                "initialization payload migration finished without a "
                "post-migration verification result",
                backup_path=str(backup),
                failure_phase="post_migration_verify",
                migration_committed=True,
            )
        after_bytes = int(after["file"]["database_bytes"])
        return {
            "status": "migrated",
            "dry_run": False,
            "database_path": str(self.database_path),
            "before_bytes": before_bytes,
            "after_bytes": after_bytes,
            "row_refs": int(after["row_refs"]),
            "unique_blobs": int(after["unique_blobs"]),
            "dedup_bytes": int(after["dedup_bytes"]),
            "backup_path": str(backup),
            "backup_quick_check": backup_quick_check,
            "quick_check": after["quick_check"],
            "migrated_rows": int(counts["migrated_rows"]),
            "reconciled_rows": int(counts["reconciled_rows"]),
            "orphan_blobs_removed": orphan_blobs_removed,
            "cleanup_orphans": bool(cleanup_orphans),
            "compacted": bool(compact),
            "compact_space": compact_space,
            "space_check": space_check,
            "before": before,
            "after": after,
        }

    def create_session(
        self,
        state: dict[str, Any],
        checkpoints: list[dict[str, Any]],
        *,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        with self._write_connection() as connection:
            replay = self._check_idempotency_row(
                connection,
                self._idempotency_row(connection, scope, idempotency_key),
                request_hash,
                scope=scope,
                idempotency_key=idempotency_key,
            )
            if replay is not None:
                return replay
            existing = connection.execute(
                "SELECT session_id FROM initialization_sessions WHERE session_id=?",
                (state["session_id"],),
            ).fetchone()
            if existing is not None:
                raise PlotInitError(
                    "SESSION_ALREADY_EXISTS",
                    f"initialization session already exists: {state['session_id']}",
                )
            state_blob_hash, state_reference = self._store_blob(
                connection,
                state,
                payload_kind="session_state",
            )
            connection.execute(
                """
                INSERT INTO initialization_sessions(
                    session_id, workspace_root, project_root, mode,
                    target_profile, interaction_profile, stage, status,
                    session_revision, expected_canon_revision,
                    source_snapshot_hash, proposal_id, state_json,
                    state_blob_hash, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    state["session_id"],
                    state["workspace_root"],
                    state.get("project_root"),
                    state.get("mode"),
                    state["target_profile"],
                    state["interaction_profile"],
                    state["stage"],
                    state["status"],
                    int(state["session_revision"]),
                    int(state["expected_canon_revision"]),
                    state["source_snapshot_hash"],
                    state.get("proposal_id"),
                    state_reference,
                    state_blob_hash,
                    state["created_at"],
                    state["updated_at"],
                ),
            )
            self._insert_revision(connection, state, "start")
            self._insert_journal(
                connection,
                state,
                event_type="session_started",
                stage=state["stage"],
                payload={"request_hash": request_hash},
            )
            self._insert_checkpoints(connection, state, checkpoints)
            self._sync_sources(connection, state)
            self._insert_idempotency(
                connection,
                scope=scope,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    def save_session(
        self,
        state: dict[str, Any],
        checkpoints: list[dict[str, Any]],
        *,
        expected_previous_revision: int,
        operation: str,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        response: dict[str, Any],
        proposal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._write_connection() as connection:
            replay = self._check_idempotency_row(
                connection,
                self._idempotency_row(connection, scope, idempotency_key),
                request_hash,
                scope=scope,
                idempotency_key=idempotency_key,
            )
            if replay is not None:
                return replay
            row = connection.execute(
                """
                SELECT session_revision
                FROM initialization_sessions
                WHERE session_id=?
                """,
                (state["session_id"],),
            ).fetchone()
            if row is None:
                raise PlotInitError(
                    "SESSION_NOT_FOUND",
                    f"initialization session not found: {state['session_id']}",
                )
            actual_revision = int(row["session_revision"])
            if actual_revision != int(expected_previous_revision):
                raise PlotInitError(
                    "SESSION_REVISION_CONFLICT",
                    "initialization session revision changed",
                    expected_session_revision=int(expected_previous_revision),
                    actual_session_revision=actual_revision,
                )
            if int(state["session_revision"]) != actual_revision + 1:
                raise PlotInitError(
                    "INVALID_SESSION_REVISION",
                    "mutating operation must increment session revision exactly once",
                    previous_revision=actual_revision,
                    new_revision=state["session_revision"],
                )

            if proposal is not None:
                proposal_id = str(proposal["proposal_id"])
                package_hash = str(proposal["package_hash"])
                existing = connection.execute(
                    """
                    SELECT package_hash, proposal_json
                    FROM initialization_proposals
                    WHERE proposal_id=?
                    """,
                    (proposal_id,),
                ).fetchone()
                if existing is not None and str(existing["package_hash"]) != package_hash:
                    raise PlotInitError(
                        "PROPOSAL_HASH_CONFLICT",
                        "proposal id already exists with different immutable content",
                        proposal_id=proposal_id,
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO initialization_proposals(
                        proposal_id, package_hash, proposal_json, created_at
                    ) VALUES(?,?,?,?)
                    """,
                    (
                        proposal_id,
                        package_hash,
                        canonical_json(proposal),
                        utc_now(),
                    ),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO initialization_session_proposals(
                        session_id, proposal_id, session_revision
                    ) VALUES(?,?,?)
                    """,
                    (
                        state["session_id"],
                        proposal_id,
                        int(state["session_revision"]),
                    ),
                )

            state_blob_hash, state_reference = self._store_blob(
                connection,
                state,
                payload_kind="session_state",
            )
            updated = connection.execute(
                """
                UPDATE initialization_sessions
                SET mode=?, target_profile=?, interaction_profile=?,
                    stage=?, status=?, session_revision=?,
                    expected_canon_revision=?, source_snapshot_hash=?,
                    proposal_id=?, state_json=?, state_blob_hash=?, updated_at=?
                WHERE session_id=? AND session_revision=?
                """,
                (
                    state.get("mode"),
                    state["target_profile"],
                    state["interaction_profile"],
                    state["stage"],
                    state["status"],
                    int(state["session_revision"]),
                    int(state["expected_canon_revision"]),
                    state["source_snapshot_hash"],
                    state.get("proposal_id"),
                    state_reference,
                    state_blob_hash,
                    state["updated_at"],
                    state["session_id"],
                    actual_revision,
                ),
            ).rowcount
            if updated != 1:
                raise PlotInitError(
                    "SESSION_REVISION_CONFLICT",
                    "initialization session changed during update",
                )
            self._insert_revision(connection, state, operation)
            self._insert_journal(
                connection,
                state,
                event_type=operation,
                stage=state["stage"],
                payload={"request_hash": request_hash},
            )
            self._insert_checkpoints(connection, state, checkpoints)
            self._sync_sources(connection, state)
            self._insert_idempotency(
                connection,
                scope=scope,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    def load_session(self, session_id: str) -> dict[str, Any]:
        with self._read_connection() as connection:
            blob_projection = (
                "state_blob_hash"
                if self._column_exists(
                    connection,
                    "initialization_sessions",
                    "state_blob_hash",
                )
                else "NULL AS state_blob_hash"
            )
            row = connection.execute(
                f"""
                SELECT state_json, {blob_projection}
                FROM initialization_sessions
                WHERE session_id=?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                raise PlotInitError(
                    "SESSION_NOT_FOUND",
                    f"initialization session not found: {session_id}",
                )
            return self._with_schema_compatibility(
                self._decode_payload(
                    connection,
                    str(row["state_json"]),
                    str(row["state_blob_hash"] or ""),
                )
            )

    def load_proposal(self, proposal_id: str) -> dict[str, Any]:
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT proposal_json
                FROM initialization_proposals
                WHERE proposal_id=?
                """,
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise PlotInitError(
                    "PROPOSAL_NOT_FOUND",
                    f"initialization proposal not found: {proposal_id}",
                )
            return self._decode(str(row["proposal_json"]))

    def journal(self, session_id: str) -> list[dict[str, Any]]:
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT journal_sequence, session_revision, event_type, stage,
                       payload_hash, payload_json, created_at
                FROM initialization_journal
                WHERE session_id=?
                ORDER BY journal_sequence
                """,
                (session_id,),
            ).fetchall()
            return [
                {
                    "journal_sequence": int(row["journal_sequence"]),
                    "session_revision": int(row["session_revision"]),
                    "event_type": str(row["event_type"]),
                    "stage": str(row["stage"]),
                    "payload_hash": str(row["payload_hash"]),
                    "payload": json.loads(str(row["payload_json"])),
                    "created_at": str(row["created_at"]),
                }
                for row in rows
            ]

    def checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT checkpoint_id, session_revision, stage, status,
                       source_snapshot_hash, dependency_hash, state_hash,
                       created_at
                FROM initialization_checkpoints
                WHERE session_id=?
                ORDER BY session_revision, rowid
                """,
                (session_id,),
            ).fetchall()
            return [
                {
                    "checkpoint_id": str(row["checkpoint_id"]),
                    "session_revision": int(row["session_revision"]),
                    "stage": str(row["stage"]),
                    "status": str(row["status"]),
                    "source_snapshot_hash": row["source_snapshot_hash"],
                    "dependency_hash": str(row["dependency_hash"]),
                    "state_hash": str(row["state_hash"]),
                    "created_at": str(row["created_at"]),
                }
                for row in rows
            ]

    def list_sessions(
        self,
        *,
        project_root: str | None = None,
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        if not self.exists:
            return []
        clauses: list[str] = []
        parameters: list[Any] = []
        if project_root is not None:
            clauses.append("project_root=?")
            parameters.append(project_root)
        if active_only:
            placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
            clauses.append(f"status IN ({placeholders})")
            parameters.extend(ACTIVE_STATUSES)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._read_connection() as connection:
            blob_projection = (
                "state_blob_hash"
                if self._column_exists(
                    connection,
                    "initialization_sessions",
                    "state_blob_hash",
                )
                else "NULL AS state_blob_hash"
            )
            rows = connection.execute(
                f"""
                SELECT session_id, project_root, mode, target_profile,
                       interaction_profile, stage, status, session_revision,
                       proposal_id, state_json, {blob_projection},
                       created_at, updated_at
                FROM initialization_sessions
                {where}
                ORDER BY updated_at DESC, session_id
                """,
                tuple(parameters),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                state = self._with_schema_compatibility(
                    self._decode_payload(
                        connection,
                        str(item.pop("state_json")),
                        str(item.pop("state_blob_hash") or ""),
                    )
                )
                item["bundle_schema_version"] = state[
                    "bundle_schema_version"
                ]
                item["power_model_status"] = (
                    ((state.get("bundle") or {}).get("validation") or {}).get(
                        "power_model_status"
                    )
                    or state.get("power_model_status")
                    or "unmodeled"
                )
                item["host_session_id"] = str(
                    state.get("host_session_id") or ""
                )
                item["host_turn_id"] = str(
                    state.get("host_turn_id") or ""
                )
                result.append(item)
            return result
