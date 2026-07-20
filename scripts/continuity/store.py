"""Transactional SQLite store and additive migration through schema v7."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

from .advantages import (
    ADVANTAGE_PROJECTION_SCHEMA_VERSION,
    ADVANTAGE_PROJECTION_TABLES,
    compute_advantage_projection_hash,
    ensure_advantage_schema,
    read_advantage_projection_metadata,
    refresh_advantage_projection_metadata,
)
from .items import (
    compute_item_projection_hash,
    migrate_legacy_item_projection,
    read_item_projection_metadata,
)
from .schema import (
    CONTINUITY_V5_TABLES,
    CONTINUITY_V7_SCHEMA_SQL,
    ITEM_PROJECTION_SCHEMA_VERSION,
    LEGACY_V2_TABLES,
    LEGACY_V2_SCHEMA_SQL,
    SCHEMA_VERSION,
    STATE_DATABASE_TABLES,
    SchemaVersionError,
    validate_schema_versions,
)

_V6_ITEM_PROJECTION_TABLES = (
    "item_definitions",
    "item_instances",
    "item_stacks",
    "item_function_definitions",
    "item_function_bindings",
    "item_custody_state",
    "item_runtime_state",
    "item_function_runtime_state",
    "item_use_history",
    "item_observations",
)


class StoreError(RuntimeError):
    """Raised when the continuity store cannot be opened or migrated."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_stream(stream: BinaryIO) -> str:
    stream.seek(0)
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


@dataclass
class _HeldBackupIdentity:
    path: Path
    staging_path: Path | None
    anchor: BinaryIO
    stat: os.stat_result
    public_anchor: BinaryIO
    public_stat: os.stat_result
    sha256: str
    owned_publication: bool


class ContinuityStore:
    """Owns the v7 schema while preserving the plug-in's legacy v2 tables.

    ``ensure_schema`` is the only migration entry point.  If an existing
    database reports a version below seven, a consistent SQLite backup is
    created before the first schema mutation and the upgrade then runs in one
    ``BEGIN IMMEDIATE`` transaction.
    """

    def __init__(
        self,
        project_root: str | Path,
        db_path: str | Path | None = None,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.db_path = (
            Path(db_path).expanduser().resolve()
            if db_path is not None
            else self.project_root / ".plot-rag" / "state.sqlite3"
        )
        self._schema_lock = threading.RLock()
        self._schema_ready = False
        self.last_backup_path: Path | None = None
        self._held_backup_identity: _HeldBackupIdentity | None = None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            return connection
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                connection.close()
            raise

    @staticmethod
    def _connect_read_only_path(
        path: Path,
        *,
        timeout: float,
    ) -> sqlite3.Connection:
        """Open a path-bounded read connection without a file-URI authority.

        ``Path.as_uri()`` turns a Windows UNC path into ``file://HOST/...``.
        SQLite treats that host component as a URI authority and rejects it on
        supported Windows builds.  Opening the already resolved filesystem
        path directly avoids URI parsing; ``query_only`` then fences every
        SQLite write attempted through the connection.
        """

        connection = sqlite3.connect(
            str(path),
            timeout=timeout,
            isolation_level=None,
        )
        try:
            connection.execute("PRAGMA query_only = ON")
            return connection
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                connection.close()
            raise

    def _open_database_anchor(self):
        """Hold the database file identity stable across path-based opens."""

        while True:
            try:
                return self.db_path.open("rb")
            except FileNotFoundError:
                try:
                    descriptor = os.open(
                        self.db_path,
                        os.O_CREAT | os.O_EXCL | os.O_RDWR,
                        0o600,
                    )
                except FileExistsError:
                    continue
                return os.fdopen(descriptor, "rb")

    def _assert_database_identity(self, anchor_stat: os.stat_result) -> None:
        try:
            current_stat = os.stat(self.db_path, follow_symlinks=False)
        except OSError as exc:
            raise StoreError(
                "STATE_DATABASE_PATH_CHANGED: continuity database path "
                "became unavailable during migration"
            ) from exc
        if not os.path.samestat(anchor_stat, current_stat):
            raise StoreError(
                "STATE_DATABASE_PATH_CHANGED: continuity database path now "
                "references a different file during migration"
            )

    @staticmethod
    def _schema_versions_from_connection(
        connection: sqlite3.Connection,
    ) -> tuple[int, int]:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='state_meta'"
        ).fetchone()
        if row is None:
            return 0, 0
        legacy_value = connection.execute(
            "SELECT value FROM state_meta WHERE key='schema_version'"
        ).fetchone()
        continuity_value = connection.execute(
            "SELECT value FROM state_meta "
            "WHERE key='continuity_schema_version'"
        ).fetchone()
        legacy_version = int(legacy_value[0]) if legacy_value is not None else 0
        continuity_version = (
            int(continuity_value[0]) if continuity_value is not None else 0
        )
        return legacy_version, continuity_version

    def _stored_schema_versions(self) -> tuple[int, int]:
        if not self.db_path.is_file():
            return 0, 0
        try:
            connection = self._connect_read_only_path(
                self.db_path,
                timeout=10.0,
            )
            try:
                return self._schema_versions_from_connection(connection)
            finally:
                connection.close()
        except (sqlite3.DatabaseError, ValueError) as exc:
            raise StoreError(f"STATE_SCHEMA_UNREADABLE: {exc}") from exc

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

    @staticmethod
    def _file_snapshot_token(path: Path) -> tuple[int, int, int, int, int]:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return (0, 0, 0, 0, 0)
        return (
            1,
            int(stat.st_dev),
            int(stat.st_ino),
            int(stat.st_size),
            int(stat.st_mtime_ns),
        )

    def _database_source_fingerprint(self) -> str:
        """Hash the SQLite source files without trusting a stale path snapshot.

        ``ensure_schema`` calls this while holding ``BEGIN IMMEDIATE``.  The
        main database plus a committed WAL are the physical source snapshot
        that the SQLite backup API will read.  Rechecking file identity and
        metadata after hashing catches a concurrent checkpoint or path swap
        instead of assigning that moving source a reusable backup name.
        """

        source_paths = (
            ("main", self.db_path),
            ("wal", Path(f"{self.db_path}-wal")),
            ("journal", Path(f"{self.db_path}-journal")),
        )
        for _attempt in range(3):
            before = tuple(
                self._file_snapshot_token(path) for _label, path in source_paths
            )
            digest = hashlib.sha256()
            digest.update(b"plot-rag-continuity-source-v1\0")
            try:
                for label, path in source_paths:
                    digest.update(label.encode("ascii"))
                    digest.update(b"\0")
                    if not path.exists():
                        digest.update(b"missing\0")
                        continue
                    digest.update(b"present\0")
                    with path.open("rb") as stream:
                        for chunk in iter(
                            lambda: stream.read(1024 * 1024),
                            b"",
                        ):
                            digest.update(chunk)
            except FileNotFoundError:
                continue
            after = tuple(
                self._file_snapshot_token(path) for _label, path in source_paths
            )
            if before == after:
                return digest.hexdigest()
        raise StoreError(
            "STATE_BACKUP_SOURCE_CHANGED: database files changed while "
            "fingerprinting the migration source"
        )

    @staticmethod
    def _backup_is_reusable(path: Path) -> bool:
        connection: sqlite3.Connection | None = None
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                return False
            connection = ContinuityStore._connect_read_only_path(
                path,
                timeout=10.0,
            )
            row = connection.execute("PRAGMA quick_check(1)").fetchone()
            return row is not None and str(row[0]).lower() == "ok"
        except (OSError, sqlite3.DatabaseError):
            return False
        finally:
            if connection is not None:
                connection.close()

    def _backup_existing_database(
        self,
        from_version: int,
        *,
        source: sqlite3.Connection | None = None,
        anchor_stat: os.stat_result | None = None,
        retain_identity: bool = False,
    ) -> Path:
        if retain_identity and self._held_backup_identity is not None:
            raise StoreError(
                "STATE_BACKUP_IDENTITY_BUSY: a continuity backup identity "
                "is already retained"
            )
        backup_dir = self.db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        if anchor_stat is not None:
            self._assert_database_identity(anchor_stat)
        source_fingerprint = self._database_source_fingerprint()
        if anchor_stat is not None:
            self._assert_database_identity(anchor_stat)
        backup_path = backup_dir / (
            f"state.sqlite3.schema-v{from_version}."
            f"source-{source_fingerprint[:32]}.bak"
        )

        descriptor, raw_backup_path = tempfile.mkstemp(
            prefix=f"{backup_path.name}.tmp.",
            dir=backup_dir,
        )
        temporary_backup_path = Path(raw_backup_path)
        temporary_anchor = os.fdopen(descriptor, "rb")
        temporary_stat = os.fstat(temporary_anchor.fileno())

        def path_matches(
            path: Path,
            expected: os.stat_result,
        ) -> bool:
            try:
                current = os.stat(path, follow_symlinks=False)
            except OSError:
                return False
            return os.path.samestat(expected, current)

        def require_temporary_identity() -> None:
            if not path_matches(temporary_backup_path, temporary_stat):
                raise StoreError(
                    "STATE_BACKUP_STAGE_CHANGED: continuity backup staging "
                    "path changed during backup"
                )

        retained_anchor: BinaryIO | None = None
        retained_stat: os.stat_result | None = None
        retained_public_anchor: BinaryIO | None = None
        retained_public_stat: os.stat_result | None = None
        retained_sha256 = ""
        publication_path: Path | None = None
        publication_anchor: BinaryIO | None = None
        publication_stat: os.stat_result | None = None
        owned_publication = False
        identity_retained = False
        completed = False
        try:
            source_context = (
                contextlib.nullcontext(source)
                if source is not None
                else contextlib.closing(
                    self._connect_read_only_path(
                        self.db_path,
                        timeout=30.0,
                    )
                )
            )
            with source_context as backup_source, contextlib.closing(
                sqlite3.connect(
                    str(temporary_backup_path),
                    timeout=30.0,
                )
            ) as destination:
                if anchor_stat is not None:
                    self._assert_database_identity(anchor_stat)
                require_temporary_identity()
                backup_source.backup(destination)
                destination.commit()
                if anchor_stat is not None:
                    self._assert_database_identity(anchor_stat)
                require_temporary_identity()
            if not self._backup_is_reusable(temporary_backup_path):
                raise StoreError(
                    "STATE_BACKUP_INVALID: continuity online backup failed "
                    "its integrity check"
                )
            require_temporary_identity()
            temporary_sha256 = sha256_stream(temporary_anchor)
            require_temporary_identity()

            publication_descriptor, raw_publication_path = tempfile.mkstemp(
                prefix=f"{backup_path.name}.publish.",
                dir=backup_dir,
            )
            publication_path = Path(raw_publication_path)
            publication_anchor = os.fdopen(
                publication_descriptor,
                "w+b",
            )
            publication_stat = os.fstat(publication_anchor.fileno())
            temporary_anchor.seek(0)
            for chunk in iter(
                lambda: temporary_anchor.read(1024 * 1024),
                b"",
            ):
                publication_anchor.write(chunk)
            publication_anchor.flush()
            os.fsync(publication_anchor.fileno())
            if not path_matches(publication_path, publication_stat):
                raise StoreError(
                    "STATE_BACKUP_PUBLISH_STAGE_CHANGED: continuity backup "
                    "publication staging path changed during copy"
                )
            publication_sha256 = sha256_stream(publication_anchor)
            if publication_sha256 != temporary_sha256:
                raise StoreError(
                    "STATE_BACKUP_PUBLISH_COPY_CHANGED: continuity backup "
                    "publication copy does not match private staging"
                )
            publication_anchor.close()
            publication_anchor = None
            if not self._backup_is_reusable(publication_path):
                raise StoreError(
                    "STATE_BACKUP_PUBLISH_INVALID: continuity backup "
                    "publication copy failed its integrity check"
                )
            if not path_matches(publication_path, publication_stat):
                raise StoreError(
                    "STATE_BACKUP_PUBLISH_STAGE_CHANGED: continuity backup "
                    "publication staging path changed before publication"
                )
            try:
                os.link(publication_path, backup_path)
            except FileExistsError as exc:
                winner_anchor: BinaryIO | None = None
                try:
                    winner_anchor = backup_path.open("rb")
                except OSError as winner_exc:
                    raise StoreError(
                        "STATE_BACKUP_CONFLICT: continuity backup path was "
                        "created concurrently but cannot be opened safely"
                    ) from winner_exc
                try:
                    winner_stat = os.fstat(winner_anchor.fileno())
                    if (
                        anchor_stat is not None
                        and os.path.samestat(anchor_stat, winner_stat)
                    ):
                        raise StoreError(
                            "STATE_BACKUP_CONFLICT: continuity backup path "
                            "aliases the live database"
                        ) from exc
                    if (
                        not path_matches(backup_path, winner_stat)
                        or not self._backup_is_reusable(backup_path)
                    ):
                        raise StoreError(
                            "STATE_BACKUP_CONFLICT: continuity backup path was "
                            "created concurrently and is not a reusable backup"
                        ) from exc
                    winner_sha256 = sha256_stream(winner_anchor)
                    if (
                        not path_matches(backup_path, winner_stat)
                        or winner_sha256 != temporary_sha256
                    ):
                        raise StoreError(
                            "STATE_BACKUP_CONFLICT: continuity backup path was "
                            "created concurrently with different content"
                        ) from exc
                    if retain_identity:
                        # Keep the private staging inode as the recovery copy.
                        # The public winner has identical bytes but a separate
                        # identity that may later be replaced independently.
                        retained_anchor = temporary_anchor
                        retained_stat = temporary_stat
                        retained_public_anchor = winner_anchor
                        retained_public_stat = winner_stat
                        retained_sha256 = temporary_sha256
                        winner_anchor = None
                finally:
                    if winner_anchor is not None:
                        winner_anchor.close()
            except OSError as exc:
                raise StoreError(
                    "STATE_BACKUP_PUBLISH_FAILED: continuity backup could not "
                    "be published atomically"
                ) from exc
            else:
                if not path_matches(backup_path, publication_stat):
                    raise StoreError(
                        "STATE_BACKUP_PATH_CHANGED: continuity backup "
                        "destination changed during publication"
                )
                owned_publication = True
                if retain_identity:
                    if path_matches(publication_path, publication_stat):
                        publication_path.unlink()
                    retained_public_anchor = backup_path.open("rb")
                    opened_public_stat = os.fstat(
                        retained_public_anchor.fileno()
                    )
                    if (
                        not os.path.samestat(
                            publication_stat,
                            opened_public_stat,
                        )
                        or not path_matches(
                            backup_path,
                            opened_public_stat,
                        )
                        or sha256_stream(retained_public_anchor)
                        != temporary_sha256
                    ):
                        raise StoreError(
                            "STATE_BACKUP_PATH_CHANGED: continuity backup "
                            "changed while retaining its public identity"
                        )
                    retained_anchor = temporary_anchor
                    retained_stat = temporary_stat
                    retained_public_stat = opened_public_stat
                    retained_sha256 = temporary_sha256
            self.last_backup_path = backup_path
            if retain_identity:
                if (
                    retained_anchor is None
                    or retained_stat is None
                    or retained_public_anchor is None
                    or retained_public_stat is None
                ):
                    raise StoreError(
                        "STATE_BACKUP_IDENTITY_MISSING: continuity backup "
                        "publication did not retain an identity anchor"
                    )
                self._held_backup_identity = _HeldBackupIdentity(
                    path=backup_path,
                    staging_path=(
                        temporary_backup_path
                        if retained_anchor is temporary_anchor
                        else None
                    ),
                    anchor=retained_anchor,
                    stat=retained_stat,
                    public_anchor=retained_public_anchor,
                    public_stat=retained_public_stat,
                    sha256=retained_sha256,
                    owned_publication=owned_publication,
                )
                identity_retained = True
            completed = True
            return backup_path
        finally:
            if publication_anchor is not None:
                publication_anchor.close()
            if (
                publication_path is not None
                and publication_stat is not None
                and path_matches(publication_path, publication_stat)
            ):
                with contextlib.suppress(OSError):
                    publication_path.unlink()
            if retained_public_anchor is not None and not identity_retained:
                retained_public_anchor.close()
            if not identity_retained:
                temporary_anchor.close()
            if (
                not identity_retained
                and path_matches(temporary_backup_path, temporary_stat)
            ):
                with contextlib.suppress(OSError):
                    temporary_backup_path.unlink()
            if (
                not completed
                and owned_publication
                and publication_stat is not None
                and path_matches(backup_path, publication_stat)
            ):
                with contextlib.suppress(OSError):
                    backup_path.unlink()

    def _verify_held_backup_identity(self, path: Path) -> str:
        held = self._held_backup_identity
        if held is None or held.path != path:
            raise StoreError(
                "STATE_BACKUP_IDENTITY_MISSING: continuity migration has no "
                "retained backup identity"
            )
        try:
            current_stat = os.stat(path, follow_symlinks=False)
            current_sha256 = sha256_stream(held.anchor)
            public_sha256 = sha256_stream(held.public_anchor)
            staging_stat = (
                os.stat(held.staging_path, follow_symlinks=False)
                if held.staging_path is not None
                else None
            )
        except (OSError, ValueError) as exc:
            raise StoreError(
                "STATE_BACKUP_PATH_CHANGED: continuity backup became "
                "unavailable during migration"
            ) from exc
        if (
            not os.path.samestat(held.public_stat, current_stat)
            or (
                staging_stat is not None
                and not os.path.samestat(held.stat, staging_stat)
            )
            or current_sha256 != held.sha256
            or public_sha256 != held.sha256
        ):
            raise StoreError(
                "STATE_BACKUP_PATH_CHANGED: continuity backup identity or "
                "content changed during migration"
            )
        return held.sha256

    def _release_held_backup_identity(
        self,
        *,
        remove_invalid_owned_publication: bool = False,
        preserve_staging: bool = False,
    ) -> None:
        held = self._held_backup_identity
        self._held_backup_identity = None
        if held is None:
            return
        with contextlib.suppress(OSError, ValueError):
            held.anchor.close()
        with contextlib.suppress(OSError, ValueError):
            held.public_anchor.close()
        if held.staging_path is not None and not preserve_staging:
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
            ):
                with contextlib.suppress(OSError):
                    held.staging_path.unlink()
        elif held.staging_path is not None:
            self.last_backup_path = held.staging_path
        if not remove_invalid_owned_publication or not held.owned_publication:
            return
        try:
            current_stat = os.stat(held.path, follow_symlinks=False)
        except OSError:
            return
        if os.path.samestat(held.public_stat, current_stat):
            with contextlib.suppress(OSError):
                held.path.unlink()

    def _held_private_backup_is_valid(self) -> bool:
        held = self._held_backup_identity
        if held is None or held.staging_path is None:
            return False
        try:
            staging_stat = os.stat(
                held.staging_path,
                follow_symlinks=False,
            )
            current_sha256 = sha256_stream(held.anchor)
        except (OSError, ValueError):
            return False
        return (
            os.path.samestat(held.stat, staging_stat)
            and current_sha256 == held.sha256
        )

    def _publish_held_backup_recovery(self) -> Path:
        held = self._held_backup_identity
        if (
            held is None
            or held.staging_path is None
            or not self._held_private_backup_is_valid()
        ):
            raise StoreError(
                "STATE_BACKUP_RECOVERY_LOST: retained continuity backup "
                "staging is unavailable"
            )
        for _attempt in range(8):
            recovery_path = held.path.with_name(
                f"{held.path.name}.recovery-{uuid.uuid4().hex}.bak"
            )
            try:
                os.link(held.staging_path, recovery_path)
            except FileExistsError:
                continue
            except OSError as exc:
                # The already unique staging path is still a valid recovery
                # copy when the filesystem cannot publish another hard link.
                if not self._held_private_backup_is_valid():
                    raise StoreError(
                        "STATE_BACKUP_RECOVERY_LOST: retained continuity "
                        "backup staging changed during recovery publication"
                    ) from exc
                self.last_backup_path = held.staging_path
                return held.staging_path
            try:
                recovery_stat = os.stat(
                    recovery_path,
                    follow_symlinks=False,
                )
            except OSError:
                continue
            if (
                os.path.samestat(held.stat, recovery_stat)
                and self._held_private_backup_is_valid()
            ):
                self.last_backup_path = recovery_path
                return recovery_path
        if not self._held_private_backup_is_valid():
            raise StoreError(
                "STATE_BACKUP_RECOVERY_LOST: retained continuity backup "
                "staging changed during recovery publication"
            )
        self.last_backup_path = held.staging_path
        return held.staging_path

    @staticmethod
    def _ensure_legacy_columns(connection: sqlite3.Connection) -> None:
        def columns(table: str) -> set[str]:
            return {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }

        event_columns = columns("state_events")
        if "scope" not in event_columns:
            connection.execute(
                "ALTER TABLE state_events "
                "ADD COLUMN scope TEXT NOT NULL DEFAULT 'current'"
            )
        if "effective_at" not in event_columns:
            connection.execute(
                "ALTER TABLE state_events ADD COLUMN effective_at TEXT"
            )

        fact_columns = columns("current_facts")
        if "effective_at" not in fact_columns:
            connection.execute(
                "ALTER TABLE current_facts ADD COLUMN effective_at TEXT"
            )

        turn_columns = columns("turns")
        if "craft_json" not in turn_columns:
            connection.execute(
                "ALTER TABLE turns "
                "ADD COLUMN craft_json TEXT NOT NULL DEFAULT '{}'"
            )

        commit_columns = columns("turn_commits")
        if "craft_json" not in commit_columns:
            connection.execute(
                "ALTER TABLE turn_commits "
                "ADD COLUMN craft_json TEXT NOT NULL DEFAULT '{}'"
            )

    @staticmethod
    def _assert_v6_schema_surface(connection: sqlite3.Connection) -> None:
        """Fail before the version marker if the additive v6 DDL is partial."""

        required: dict[str, set[str]] = {
            "item_definitions": {
                "item_definition_id",
                "item_entity_id",
                "item_status",
                "item_kind",
                "stack_policy",
                "uniqueness_policy",
                "definition_json",
                "source_event_id",
                "updated_order",
            },
            "item_instances": {
                "item_instance_id",
                "item_definition_id",
                "item_entity_id",
                "instance_status",
                "instance_json",
                "source_event_id",
                "story_coordinate_json",
                "updated_order",
            },
            "item_stacks": {
                "stack_id",
                "item_definition_id",
                "quantity",
                "stack_status",
                "batch_json",
                "source_event_id",
                "story_coordinate_json",
                "updated_order",
            },
            "item_function_definitions": {
                "function_id",
                "item_definition_id",
                "function_status",
                "effect_owner",
                "definition_json",
                "source_event_id",
                "updated_order",
            },
            "item_function_bindings": {
                "binding_id",
                "item_definition_id",
                "item_instance_id",
                "stack_id",
                "function_id",
                "binding_status",
                "binding_json",
                "source_event_id",
                "updated_order",
            },
            "item_custody_state": {
                "custody_key",
                "subject_type",
                "subject_id",
                "item_instance_id",
                "stack_id",
                "legal_owner_entity_id",
                "custodian_entity_id",
                "carrier_entity_id",
                "location_entity_id",
                "container_instance_id",
                "access_controller_entity_id",
                "custody_status",
                "quantity",
                "state_json",
                "source_event_id",
                "story_coordinate_json",
                "updated_order",
            },
            "item_runtime_state": {
                "item_instance_id",
                "durability",
                "max_durability",
                "energy",
                "max_energy",
                "sealed",
                "damaged",
                "destroyed",
                "active",
                "equipped_by_entity_id",
                "slot_key",
                "bound_actor_entity_id",
                "state_json",
                "source_event_id",
                "story_coordinate_json",
                "updated_order",
            },
            "item_function_runtime_state": {
                "function_runtime_key",
                "item_instance_id",
                "function_id",
                "enabled",
                "unlock_state",
                "remaining_charges",
                "cooldown_until_json",
                "state_json",
                "source_event_id",
                "story_coordinate_json",
                "updated_order",
            },
            "item_use_history": {
                "source_event_id",
                "item_instance_id",
                "stack_id",
                "function_id",
                "actor_entity_id",
                "target_entity_id",
                "action",
                "delta_json",
                "before_json",
                "after_json",
                "story_coordinate_json",
                "chapter_no",
                "scene_index",
                "updated_order",
            },
            "item_observations": {
                "observation_key",
                "observer_entity_id",
                "item_instance_id",
                "stack_id",
                "function_id",
                "observation_action",
                "knowledge_plane",
                "observation_json",
                "source_event_id",
                "story_coordinate_json",
                "updated_order",
            },
            "item_projection_meta": {
                "meta_key",
                "value_json",
                "updated_order",
            },
            "extraction_jobs": {
                "job_id",
                "receipt_id",
                "request_id",
                "assistant_sha256",
                "prompt_hash",
                "retrieved_context_digest",
                "prepared_canon_revision",
                "active_projection_hash",
                "intent_contract_hash",
                "event_seed_manifest_hash",
                "event_experience_control_revision",
                "event_seed_references_json",
                "experience_contract_hashes_json",
                "artifact_context_json",
                "branch_id",
                "sequence_no",
                "extract_provider",
                "extract_base_url",
                "extract_model",
                "extract_schema_hash",
                "extract_prompt_template_hash",
                "min_confidence",
                "generation_params_json",
                "job_binding_hash",
                "job_status",
                "attempt_count",
                "remote_status",
                "result_kind",
                "result_proposal_id",
                "error",
                "lease_owner",
                "lease_expires_at",
                "heartbeat_at",
                "next_attempt_at",
                "created_at",
                "updated_at",
                "started_at",
                "completed_at",
            },
            "extraction_job_payloads": {
                "job_id",
                "assistant_text",
                "assistant_sha256",
                "payload_bytes",
                "created_at",
                "updated_at",
            },
            "extraction_barrier_resolutions": {
                "resolution_id",
                "job_id",
                "branch_id",
                "sequence_no",
                "expected_attempt_count",
                "action",
                "replacement_job_id",
                "target_branch_id",
                "reason",
                "binding_hash",
                "created_at",
            },
            "event_seeds": {
                "event_seed_id",
                "event_seed_revision",
                "payload_json",
                "status",
            },
            "event_experience_meta": {"key", "value"},
            "event_experience_arcs": {
                "arc_id",
                "arc_revision",
                "payload_json",
                "status",
            },
            "event_experience_contracts": {
                "contract_id",
                "contract_revision",
                "contract_hash",
                "payload_json",
                "status",
            },
            "event_experience_reviews": {
                "review_id",
                "review_revision",
                "review_hash",
                "payload_json",
                "status",
            },
            "event_experience_observed_reviews": {
                "review_id",
                "review_revision",
                "artifact_id",
                "artifact_revision",
                "branch_id",
                "source_commit_id",
                "source_content_hash",
                "assistant_sha256",
                "review_hash",
                "payload_json",
                "status",
            },
            "event_experience_questions": {
                "event_seed_manifest_hash",
                "question_json",
                "status",
            },
            "event_experience_idempotency": {
                "operation",
                "idempotency_key",
                "request_hash",
                "response_json",
            },
        }
        missing: list[str] = []
        for table, expected_columns in required.items():
            columns = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            absent = sorted(expected_columns - columns)
            if absent:
                missing.append(f"{table}({','.join(absent)})")
        if missing:
            raise StoreError(
                "STATE_SCHEMA_V6_INCOMPLETE: missing required surfaces: "
                + ", ".join(missing)
            )

        required_sql_fragments: dict[str, tuple[str, ...]] = {
            "item_definitions": (
                "item_status in (",
                "stack_policy in (",
                "uniqueness_policy in (",
            ),
            "item_instances": ("instance_status in (",),
            "item_stacks": (
                "quantity <= 1.0e308",
                "stack_status in (",
            ),
            "item_function_definitions": (
                "function_status in (",
                "effect_owner in ('inline', 'ability_bridge')",
            ),
            "item_function_bindings": (
                "(item_definition_id is not null) + "
                "(item_instance_id is not null) + "
                "(stack_id is not null) = 1",
                "binding_status in (",
            ),
            "item_custody_state": (
                "(item_instance_id is not null) + "
                "(stack_id is not null) = 1",
                "custody_status in (",
            ),
            "item_runtime_state": (
                "sealed in (0, 1)",
                "damaged in (0, 1)",
                "destroyed in (0, 1)",
                "active in (0, 1)",
                "durability <= max_durability",
                "energy <= max_energy",
            ),
            "item_function_runtime_state": (
                "enabled in (0, 1)",
                "unlock_state in (",
                "remaining_charges <= 1.0e308",
            ),
            "item_use_history": (
                "(item_instance_id is not null) + "
                "(stack_id is not null) = 1",
                "action in ('use', 'trigger', 'consume')",
            ),
            "item_observations": (
                "(item_instance_id is not null) + "
                "(stack_id is not null) = 1",
                "knowledge_plane in (",
                "observation_action in (",
            ),
            "extraction_jobs": (
                "typeof(prepared_canon_revision) = 'integer'",
                "prepared_canon_revision >= 0",
                "sequence_no is null",
                "typeof(sequence_no) = 'integer'",
                "sequence_no >= 0",
                "typeof(event_experience_control_revision) = 'integer'",
                "event_experience_control_revision >= 0",
                "typeof(min_confidence) in ('integer', 'real')",
                "min_confidence >= 0",
                "min_confidence <= 1",
                "job_status in (",
                "'queued', 'running', 'succeeded', 'failed', 'cancelled'",
                "result_kind in ('', 'proposal', 'no_delta')",
                "job_status = 'succeeded'",
                "result_kind = 'proposal'",
                "result_proposal_id is not null",
                "result_kind = 'no_delta'",
                "result_proposal_id is null",
                "job_status <> 'succeeded'",
                "result_kind = ''",
                "typeof(attempt_count) = 'integer'",
                "attempt_count >= 0",
                "unique(receipt_id, assistant_sha256)",
            ),
            "extraction_job_payloads": (
                "typeof(payload_bytes) = 'integer'",
                "payload_bytes >= 0",
            ),
            "extraction_barrier_resolutions": (
                "job_id text not null unique",
                "typeof(sequence_no) = 'integer'",
                "sequence_no >= 0",
                "typeof(expected_attempt_count) = 'integer'",
                "expected_attempt_count >= 0",
                "action in (",
                "'discard', 'rewrite', 'supersede', 'branch_switch'",
                "action in ('rewrite', 'supersede')",
                "replacement_job_id is not null",
                "action = 'discard'",
                "target_branch_id = ''",
                "action = 'branch_switch'",
                "replacement_job_id is null",
                "length(trim(target_branch_id)) > 0",
            ),
        }
        malformed_constraints: list[str] = []
        for table, fragments in required_sql_fragments.items():
            row = connection.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type='table' AND name=?
                """,
                (table,),
            ).fetchone()
            normalized_sql = re.sub(
                r"\s+",
                " ",
                str(row[0] if row is not None else "").casefold(),
            )
            absent = [
                fragment
                for fragment in fragments
                if fragment.casefold() not in normalized_sql
            ]
            if absent:
                malformed_constraints.append(
                    f"{table}({';'.join(absent)})"
                )
        if malformed_constraints:
            raise StoreError(
                "STATE_SCHEMA_V6_CONSTRAINTS_INCOMPLETE: "
                + ", ".join(malformed_constraints)
            )

        expected_foreign_keys: dict[
            str,
            set[tuple[str, str, str]],
        ] = {
            "item_definitions": {
                ("item_entity_id", "entities", "entity_id"),
                ("source_event_id", "continuity_events", "event_id"),
            },
            "item_instances": {
                (
                    "item_definition_id",
                    "item_definitions",
                    "item_definition_id",
                ),
                ("item_entity_id", "entities", "entity_id"),
                ("source_event_id", "continuity_events", "event_id"),
            },
            "item_stacks": {
                (
                    "item_definition_id",
                    "item_definitions",
                    "item_definition_id",
                ),
                ("source_event_id", "continuity_events", "event_id"),
            },
            "item_function_definitions": {
                (
                    "item_definition_id",
                    "item_definitions",
                    "item_definition_id",
                ),
                ("source_event_id", "continuity_events", "event_id"),
            },
            "item_function_bindings": {
                (
                    "item_definition_id",
                    "item_definitions",
                    "item_definition_id",
                ),
                (
                    "item_instance_id",
                    "item_instances",
                    "item_instance_id",
                ),
                ("stack_id", "item_stacks", "stack_id"),
                (
                    "function_id",
                    "item_function_definitions",
                    "function_id",
                ),
                ("source_event_id", "continuity_events", "event_id"),
            },
            "item_custody_state": {
                (
                    "item_instance_id",
                    "item_instances",
                    "item_instance_id",
                ),
                ("stack_id", "item_stacks", "stack_id"),
                ("legal_owner_entity_id", "entities", "entity_id"),
                ("custodian_entity_id", "entities", "entity_id"),
                ("carrier_entity_id", "entities", "entity_id"),
                ("location_entity_id", "entities", "entity_id"),
                (
                    "container_instance_id",
                    "item_instances",
                    "item_instance_id",
                ),
                ("access_controller_entity_id", "entities", "entity_id"),
                ("source_event_id", "continuity_events", "event_id"),
            },
            "item_runtime_state": {
                (
                    "item_instance_id",
                    "item_instances",
                    "item_instance_id",
                ),
                ("equipped_by_entity_id", "entities", "entity_id"),
                ("bound_actor_entity_id", "entities", "entity_id"),
                ("source_event_id", "continuity_events", "event_id"),
            },
            "item_function_runtime_state": {
                (
                    "item_instance_id",
                    "item_instances",
                    "item_instance_id",
                ),
                (
                    "function_id",
                    "item_function_definitions",
                    "function_id",
                ),
                ("source_event_id", "continuity_events", "event_id"),
            },
            "item_use_history": {
                (
                    "item_instance_id",
                    "item_instances",
                    "item_instance_id",
                ),
                ("stack_id", "item_stacks", "stack_id"),
                (
                    "function_id",
                    "item_function_definitions",
                    "function_id",
                ),
                ("actor_entity_id", "entities", "entity_id"),
                ("target_entity_id", "entities", "entity_id"),
                ("source_event_id", "continuity_events", "event_id"),
            },
            "item_observations": {
                ("observer_entity_id", "entities", "entity_id"),
                (
                    "item_instance_id",
                    "item_instances",
                    "item_instance_id",
                ),
                ("stack_id", "item_stacks", "stack_id"),
                (
                    "function_id",
                    "item_function_definitions",
                    "function_id",
                ),
                ("source_event_id", "continuity_events", "event_id"),
            },
            "extraction_jobs": {
                ("receipt_id", "turns", "receipt_id"),
                ("result_proposal_id", "proposals", "proposal_id"),
            },
            "extraction_job_payloads": {
                ("job_id", "extraction_jobs", "job_id"),
            },
            "extraction_barrier_resolutions": {
                ("job_id", "extraction_jobs", "job_id"),
                ("replacement_job_id", "extraction_jobs", "job_id"),
            },
        }
        malformed_foreign_keys: list[str] = []
        for table, expected in expected_foreign_keys.items():
            actual = {
                (str(row[3]), str(row[2]), str(row[4]))
                for row in connection.execute(
                    f'PRAGMA foreign_key_list("{table}")'
                )
            }
            absent = sorted(expected - actual)
            if absent:
                malformed_foreign_keys.append(
                    f"{table}({';'.join('/'.join(item) for item in absent)})"
                )
        if malformed_foreign_keys:
            raise StoreError(
                "STATE_SCHEMA_V6_FOREIGN_KEYS_INCOMPLETE: "
                + ", ".join(malformed_foreign_keys)
            )

        payload_foreign_keys = {
            (
                str(row[3]),
                str(row[2]),
                str(row[4]),
                str(row[6]).upper(),
            )
            for row in connection.execute(
                'PRAGMA foreign_key_list("extraction_job_payloads")'
            )
        }
        expected_payload_cascade = (
            "job_id",
            "extraction_jobs",
            "job_id",
            "CASCADE",
        )
        if expected_payload_cascade not in payload_foreign_keys:
            raise StoreError(
                "STATE_SCHEMA_V6_FOREIGN_KEYS_INCOMPLETE: "
                "extraction_job_payloads(job_id/extraction_jobs/job_id/"
                "CASCADE)"
            )

        required_indexes = {
            "idx_item_definitions_entity",
            "idx_item_definitions_kind",
            "idx_item_instances_entity",
            "idx_item_instances_definition",
            "idx_item_stacks_definition",
            "idx_item_functions_definition",
            "idx_item_function_bindings_target",
            "idx_item_function_bindings_function",
            "idx_item_custody_owner",
            "idx_item_custody_location",
            "idx_item_runtime_equipped",
            "idx_item_runtime_bound",
            "idx_item_function_runtime_ready",
            "idx_item_use_history_item",
            "idx_item_use_history_actor",
            "idx_item_observations_observer",
            "idx_extraction_jobs_status",
            "idx_extraction_jobs_barrier",
            "idx_extraction_jobs_result_proposal",
            "idx_extraction_barrier_resolutions_branch_sequence",
        }
        actual_indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        missing_indexes = sorted(required_indexes - actual_indexes)
        if missing_indexes:
            raise StoreError(
                "STATE_SCHEMA_V6_INDEXES_INCOMPLETE: "
                + ", ".join(missing_indexes)
            )
        for table, index_name in (
            ("item_definitions", "idx_item_definitions_entity"),
            ("item_instances", "idx_item_instances_entity"),
        ):
            index_rows = {
                str(row[1]): (int(row[2]), int(row[4]))
                for row in connection.execute(
                    f'PRAGMA index_list("{table}")'
                )
            }
            if index_rows.get(index_name) != (1, 1):
                raise StoreError(
                    "STATE_SCHEMA_V6_INDEXES_INCOMPLETE: "
                    f"{index_name} must be unique and partial"
                )

        expected_extraction_indexes = {
            "idx_extraction_jobs_status": (
                0,
                0,
                ("job_status", "next_attempt_at", "created_at"),
            ),
            "idx_extraction_jobs_barrier": (
                0,
                0,
                ("branch_id", "sequence_no", "job_status", "created_at"),
            ),
            "idx_extraction_jobs_result_proposal": (
                1,
                1,
                ("result_proposal_id",),
            ),
        }
        extraction_index_flags = {
            str(row[1]): (int(row[2]), int(row[4]))
            for row in connection.execute(
                'PRAGMA index_list("extraction_jobs")'
            )
        }
        malformed_extraction_indexes: list[str] = []
        for index_name, (unique, partial, expected_columns) in (
            expected_extraction_indexes.items()
        ):
            if extraction_index_flags.get(index_name) != (unique, partial):
                malformed_extraction_indexes.append(
                    f"{index_name}(unique={unique},partial={partial})"
                )
                continue
            actual_columns = tuple(
                str(row[2])
                for row in connection.execute(
                    f'PRAGMA index_info("{index_name}")'
                )
            )
            if actual_columns != expected_columns:
                malformed_extraction_indexes.append(
                    f"{index_name}({','.join(actual_columns)})"
                )
        result_index_row = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type='index'
              AND name='idx_extraction_jobs_result_proposal'
            """
        ).fetchone()
        result_index_sql = re.sub(
            r"\s+",
            " ",
            str(
                result_index_row[0]
                if result_index_row is not None
                else ""
            ).casefold(),
        )
        if "where result_proposal_id is not null" not in result_index_sql:
            malformed_extraction_indexes.append(
                "idx_extraction_jobs_result_proposal(predicate)"
            )
        if malformed_extraction_indexes:
            raise StoreError(
                "STATE_SCHEMA_V6_INDEXES_INCOMPLETE: "
                + ", ".join(malformed_extraction_indexes)
            )

        resolution_index_columns = tuple(
            str(row[2])
            for row in connection.execute(
                "PRAGMA index_info("
                "'idx_extraction_barrier_resolutions_branch_sequence'"
                ")"
            )
        )
        if resolution_index_columns != (
            "branch_id",
            "sequence_no",
            "created_at",
        ):
            raise StoreError(
                "STATE_SCHEMA_V6_INDEXES_INCOMPLETE: "
                "idx_extraction_barrier_resolutions_branch_sequence("
                + ",".join(resolution_index_columns)
                + ")"
            )

        violations: list[sqlite3.Row] = []
        for table in expected_foreign_keys:
            violations.extend(
                connection.execute(
                    f'PRAGMA foreign_key_check("{table}")'
                ).fetchmany(20 - len(violations))
            )
            if len(violations) >= 20:
                break
        if violations:
            rendered = [
                "/".join(str(value) for value in tuple(row))
                for row in violations
            ]
            raise StoreError(
                "STATE_SCHEMA_V6_FOREIGN_KEY_VIOLATION: "
                + ", ".join(rendered)
            )

    @staticmethod
    def _assert_advantage_schema_surface(
        connection: sqlite3.Connection,
    ) -> None:
        """Require the complete additive Advantage projection surface."""

        required: dict[str, set[str]] = {
            "advantage_definitions": {
                "advantage_id",
                "title",
                "profiles_json",
                "anchor_type",
                "advantage_status",
                "lifecycle_status",
                "definition_json",
                "source_event_id",
                "updated_order",
            },
            "advantage_anchors": {
                "anchor_id",
                "advantage_id",
                "anchor_type",
                "anchor_ref_id",
                "owner_entity_id",
                "binding_state",
                "authority_status",
                "anchor_status",
                "story_coordinate_json",
                "updated_order",
            },
            "advantage_module_definitions": {
                "module_id",
                "advantage_id",
                "module_kind",
                "authority_status",
                "module_status",
                "stage",
                "costs_json",
                "effects_json",
                "source_event_id",
                "updated_order",
            },
            "advantage_runtime_slots": {
                "slot_id",
                "advantage_id",
                "module_id",
                "stage",
                "capacity",
                "authority_status",
                "slot_status",
                "updated_order",
            },
            "advantage_runtime_state": {
                "runtime_key",
                "advantage_id",
                "branch_id",
                "owner_entity_id",
                "stage",
                "enabled",
                "charges",
                "max_charges",
                "resources_json",
                "pollution",
                "exposure",
                "debt",
                "unlocked_modules_json",
                "source_event_id",
                "story_coordinate_json",
                "updated_order",
            },
            "advantage_ledger": {
                "entry_id",
                "advantage_id",
                "module_id",
                "branch_id",
                "entry_kind",
                "input_json",
                "output_json",
                "loss_json",
                "provenance_json",
                "causal_event_id",
                "source_event_id",
                "story_coordinate_json",
                "updated_order",
            },
            "advantage_knowledge": {
                "knowledge_id",
                "advantage_id",
                "module_id",
                "observer_entity_id",
                "knowledge_plane",
                "knowledge_status",
                "claim_json",
                "confidence",
                "evidence_json",
                "reveal_stage",
                "misread_of",
                "source_event_id",
                "updated_order",
            },
            "advantage_contracts": {
                "contract_id",
                "advantage_id",
                "authority_status",
                "contract_status",
                "terms_json",
                "agency_json",
                "trust",
                "debt",
                "breach_effect_json",
                "source_event_id",
                "updated_order",
            },
            "advantage_narrative_contracts": {
                "narrative_contract_id",
                "advantage_id",
                "authority_status",
                "contract_status",
                "reading_promise_json",
                "reward_loop_json",
                "risk_loop_json",
                "reveal_ladder_json",
                "experience_binding_json",
                "source_event_id",
                "updated_order",
            },
            "advantage_projection_meta": {
                "meta_key",
                "value_json",
                "source_event_id",
                "updated_order",
            },
        }
        malformed: list[str] = []
        for table, expected in required.items():
            actual = {
                str(row[1])
                for row in connection.execute(
                    f'PRAGMA table_info("{table}")'
                )
            }
            absent = sorted(expected - actual)
            if absent:
                malformed.append(f"{table}({','.join(absent)})")
        if malformed:
            raise StoreError(
                "STATE_SCHEMA_V7_INCOMPLETE: missing Advantage surfaces: "
                + ", ".join(malformed)
            )

        violations: list[sqlite3.Row] = []
        for table in (*ADVANTAGE_PROJECTION_TABLES,):
            violations.extend(
                connection.execute(
                    f'PRAGMA foreign_key_check("{table}")'
                ).fetchmany(20 - len(violations))
            )
            if len(violations) >= 20:
                break
        if violations:
            rendered = [
                "/".join(str(value) for value in tuple(row))
                for row in violations
            ]
            raise StoreError(
                "STATE_SCHEMA_V7_FOREIGN_KEY_VIOLATION: "
                + ", ".join(rendered)
            )

    @staticmethod
    def _assert_v7_item_schema_surface(
        connection: sqlite3.Connection,
    ) -> None:
        """Require the two item projection surfaces introduced in v7."""

        required: dict[str, set[str]] = {
            "item_stack_function_runtime_state": {
                "function_runtime_key",
                "stack_id",
                "function_id",
                "enabled",
                "unlock_state",
                "remaining_charges",
                "cooldown_until_json",
                "state_json",
                "source_event_id",
                "story_coordinate_json",
                "updated_order",
            },
            "item_knowledge_observations": {
                "observation_key",
                "subject_type",
                "subject_id",
                "item_definition_id",
                "item_instance_id",
                "stack_id",
                "observer_entity_id",
                "function_id",
                "observation_action",
                "knowledge_plane",
                "observation_json",
                "source_event_id",
                "story_coordinate_json",
                "updated_order",
            },
        }
        malformed: list[str] = []
        for table, expected in required.items():
            actual = {
                str(row[1])
                for row in connection.execute(
                    f'PRAGMA table_info("{table}")'
                )
            }
            absent = sorted(expected - actual)
            if absent:
                malformed.append(f"{table}({','.join(absent)})")
        if malformed:
            raise StoreError(
                "STATE_SCHEMA_V7_INCOMPLETE: missing item surfaces: "
                + ", ".join(malformed)
            )

    @staticmethod
    def _assert_v5_schema_surface(
        connection: sqlite3.Connection,
        user_tables: set[str],
    ) -> None:
        """Reject a structurally incomplete v5 source before additive DDL."""

        required = LEGACY_V2_TABLES | CONTINUITY_V5_TABLES
        missing = sorted(required - user_tables)
        if missing:
            raise StoreError(
                "STATE_SCHEMA_V5_INCOMPLETE: missing required tables: "
                + ", ".join(missing)
            )

        required_columns: dict[str, set[str]] = {
            "state_meta": {"key", "value", "updated_at"},
            "canon_commits": {
                "commit_id",
                "head_revision_before",
                "head_revision_after",
                "active_revision_before",
                "active_revision_after",
                "projection_hash",
            },
            "continuity_events": {
                "event_id",
                "commit_id",
                "event_ordinal",
                "event_type",
                "payload_json",
                "evidence_json",
            },
            "inventory_state": {
                "inventory_key",
                "item_entity_id",
                "owner_entity_id",
                "quantity",
                "is_unique",
                "item_status",
                "source_event_id",
                "updated_order",
            },
        }
        malformed: list[str] = []
        for table, expected in required_columns.items():
            actual = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            absent = sorted(expected - actual)
            if absent:
                malformed.append(f"{table}({','.join(absent)})")
        if malformed:
            raise StoreError(
                "STATE_SCHEMA_V5_INCOMPLETE: missing required columns: "
                + ", ".join(malformed)
            )

    @classmethod
    def _v5_immutable_snapshot(
        cls,
        connection: sqlite3.Connection,
    ) -> str:
        """Hash every pre-v6 row except the version marker being advanced."""

        tables: dict[str, list[str]] = {}
        for table in sorted(LEGACY_V2_TABLES | CONTINUITY_V5_TABLES):
            columns = [
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            ]
            if not columns:
                raise StoreError(
                    f"STATE_SCHEMA_V5_INCOMPLETE: missing table {table}"
                )
            selected = ", ".join(f'"{column}"' for column in columns)
            sql = f'SELECT {selected} FROM "{table}"'
            parameters: tuple[str, ...] = ()
            if table == "state_meta":
                sql += " WHERE key<>?"
                parameters = ("continuity_schema_version",)
            rows = connection.execute(sql, parameters).fetchall()
            encoded_rows = [
                cls._canonical_json(
                    {
                        column: (
                            row[column]
                            if isinstance(row, sqlite3.Row)
                            else row[index]
                        )
                        for index, column in enumerate(columns)
                    }
                )
                for row in rows
            ]
            tables[table] = sorted(encoded_rows)
        return hashlib.sha256(
            cls._canonical_json(tables).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _item_projection_hash_for_tables(
        connection: sqlite3.Connection,
        tables: tuple[str, ...],
    ) -> str:
        payload: dict[str, object] = {
            "schema_version": ITEM_PROJECTION_SCHEMA_VERSION,
            "tables": {},
        }
        table_payload: dict[str, list[dict[str, object]]] = {}
        for table in tables:
            columns = [
                str(row[1])
                for row in connection.execute(
                    f'PRAGMA table_info("{table}")'
                )
            ]
            stable_columns = [
                column
                for column in columns
                if column
                not in {"created_at", "updated_at", "completed_at"}
            ]
            if not stable_columns:
                table_payload[table] = []
                continue
            selected = ", ".join(
                f'"{column}"' for column in stable_columns
            )
            rows = connection.execute(
                f'SELECT {selected} FROM "{table}" ORDER BY {selected}'
            ).fetchall()
            table_payload[table] = [
                {
                    column: (
                        row[column]
                        if isinstance(row, sqlite3.Row)
                        else row[index]
                    )
                    for index, column in enumerate(stable_columns)
                }
                for row in rows
            ]
        payload["tables"] = table_payload
        raw = ContinuityStore._canonical_json(payload).encode("utf-8")
        return "item_projection_" + hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _assert_v6_metadata(
        connection: sqlite3.Connection,
        *,
        legacy_item_surface: bool = False,
    ) -> None:
        """Validate independent v6 metadata without repairing it in place."""

        try:
            item_meta = read_item_projection_metadata(connection)
        except (sqlite3.DatabaseError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StoreError(
                f"STATE_ITEM_PROJECTION_META_INVALID: {exc}"
            ) from exc
        required_item_keys = {
            "schema_version",
            "projection_hash",
            "source_head_revision",
            "source_active_revision",
        }
        missing = sorted(required_item_keys - set(item_meta))
        if missing:
            raise StoreError(
                "STATE_ITEM_PROJECTION_META_MISSING: " + ", ".join(missing)
            )
        stored_version = item_meta["schema_version"]
        if (
            type(stored_version) is not int
            or stored_version != ITEM_PROJECTION_SCHEMA_VERSION
        ):
            raise StoreError(
                "STATE_ITEM_PROJECTION_SCHEMA_UNSUPPORTED: "
                f"stored={stored_version}, "
                f"supported={ITEM_PROJECTION_SCHEMA_VERSION}"
            )
        for key in ("source_head_revision", "source_active_revision"):
            value = item_meta[key]
            if type(value) is not int or value < 0:
                raise StoreError(
                    "STATE_ITEM_PROJECTION_META_INVALID: "
                    f"{key} must be a non-negative integer"
                )
        stored_hash = item_meta["projection_hash"]
        if not isinstance(stored_hash, str) or not stored_hash.startswith(
            "item_projection_"
        ):
            raise StoreError(
                "STATE_ITEM_PROJECTION_META_INVALID: projection_hash"
            )
        actual_hash = (
            ContinuityStore._item_projection_hash_for_tables(
                connection,
                _V6_ITEM_PROJECTION_TABLES,
            )
            if legacy_item_surface
            else compute_item_projection_hash(connection)
        )
        if stored_hash != actual_hash:
            raise StoreError(
                "STATE_ITEM_PROJECTION_HASH_MISMATCH: "
                f"stored={stored_hash}, actual={actual_hash}"
            )

        experience_version = connection.execute(
            """
            SELECT value FROM event_experience_meta
            WHERE key='schema_version'
            """
        ).fetchone()
        if experience_version is None:
            raise StoreError(
                "STATE_EVENT_EXPERIENCE_SCHEMA_MISSING"
            )
        if str(experience_version[0]) != "1":
            raise StoreError(
                "STATE_EVENT_EXPERIENCE_SCHEMA_UNSUPPORTED: "
                f"stored={experience_version[0]}, supported=1"
            )

    @staticmethod
    def _assert_advantage_metadata(
        connection: sqlite3.Connection,
    ) -> None:
        """Validate independent Advantage metadata without repairing it."""

        try:
            metadata = read_advantage_projection_metadata(connection)
        except (
            sqlite3.DatabaseError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise StoreError(
                f"STATE_ADVANTAGE_PROJECTION_META_INVALID: {exc}"
            ) from exc
        required = {
            "schema_version",
            "projection_hash",
            "source_head_revision",
            "source_active_revision",
        }
        missing = sorted(required - set(metadata))
        if missing:
            raise StoreError(
                "STATE_ADVANTAGE_PROJECTION_META_MISSING: "
                + ", ".join(missing)
            )
        version = metadata["schema_version"]
        if (
            type(version) is not int
            or version != ADVANTAGE_PROJECTION_SCHEMA_VERSION
        ):
            raise StoreError(
                "STATE_ADVANTAGE_PROJECTION_SCHEMA_UNSUPPORTED: "
                f"stored={version}, "
                f"supported={ADVANTAGE_PROJECTION_SCHEMA_VERSION}"
            )
        for key in ("source_head_revision", "source_active_revision"):
            value = metadata[key]
            if type(value) is not int or value < 0:
                raise StoreError(
                    "STATE_ADVANTAGE_PROJECTION_META_INVALID: "
                    f"{key} must be a non-negative integer"
                )
        stored_hash = metadata["projection_hash"]
        if not isinstance(stored_hash, str) or not stored_hash.startswith(
            "advantage_projection_"
        ):
            raise StoreError(
                "STATE_ADVANTAGE_PROJECTION_META_INVALID: projection_hash"
            )
        actual_hash = compute_advantage_projection_hash(connection)
        if stored_hash != actual_hash:
            raise StoreError(
                "STATE_ADVANTAGE_PROJECTION_HASH_MISMATCH: "
                f"stored={stored_hash}, actual={actual_hash}"
            )

    @staticmethod
    def _execute_script_in_transaction(
        connection: sqlite3.Connection,
        script: str,
    ) -> None:
        """Execute a DDL script without sqlite3.executescript's implicit COMMIT."""

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
            raise StoreError("STATE_SCHEMA_SCRIPT_INCOMPLETE")

    @staticmethod
    def _put_meta(
        connection: sqlite3.Connection,
        key: str,
        value: str,
        now: str,
        *,
        preserve_existing: bool = False,
    ) -> None:
        if preserve_existing:
            connection.execute(
                """
                INSERT INTO state_meta(key, value, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (key, value, now),
            )
            return
        connection.execute(
            """
            INSERT INTO state_meta(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE
            SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, now),
        )

    @staticmethod
    def _canonical_json(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _stable_id(prefix: str, value: object) -> str:
        raw = ContinuityStore._canonical_json(value)
        return prefix + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _migrate_v4_ability_projection(
        connection: sqlite3.Connection,
        *,
        from_version: int,
        source_db_hash: str,
    ) -> None:
        """Seed v5 ownership/runtime projections from the v4 compatibility row.

        Normal v4 databases already have immutable accepted ability events; in
        that case this is only a zero-loss projection split and the next replay
        recomputes it from the ledger.  An orphaned legacy row is exceptional:
        it is imported as one explicit bootstrap event with provenance rather
        than silently treating a mutable projection as canon.
        """

        if from_version <= 0 or from_version >= 5:
            return
        table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='ability_state'
            """
        ).fetchone()
        if table is None:
            return
        rows = connection.execute(
            """
            SELECT ability_key, owner_entity_id, ability_entity_id,
                   state_json, source_event_id, updated_order
            FROM ability_state
            ORDER BY ability_key
            """
        ).fetchall()
        if not rows:
            return

        now = utc_now()
        imported_event_ids: dict[str, str] = {}
        imported_payloads: list[dict[str, object]] = []
        for row in rows:
            source_event_id = str(row["source_event_id"])
            accepted = connection.execute(
                """
                SELECT 1
                FROM continuity_events AS e
                JOIN canon_commits AS c ON c.commit_id=e.commit_id
                WHERE e.event_id=? AND c.operation='accept'
                """,
                (source_event_id,),
            ).fetchone()
            if accepted is not None:
                continue
            try:
                legacy_state = dict(json.loads(str(row["state_json"]) or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                legacy_state = {"legacy_raw_state": str(row["state_json"])}
            legacy_action = str(legacy_state.pop("action", "") or "")
            acquired = bool(
                legacy_state.get(
                    "acquired",
                    legacy_action not in {"lose", "remove", "revoke"},
                )
            )
            event_id = ContinuityStore._stable_id(
                "story_event_legacy_power_",
                [
                    source_db_hash,
                    row["ability_key"],
                    row["owner_entity_id"],
                    row["ability_entity_id"],
                ],
            )
            imported_event_ids[str(row["ability_key"])] = event_id
            imported_payloads.append(
                {
                    "event_id": event_id,
                    "event_type": "ability",
                    "scope": "current",
                    "branch_id": "main",
                    "chapter_no": None,
                    "scene_index": None,
                    "story_time": None,
                    "story_coordinate": None,
                    "narrative_mode": "linear",
                    "owner_entity_id": str(row["owner_entity_id"]),
                    "ability_entity_id": str(row["ability_entity_id"]),
                    # A projection-only lose row must not manufacture
                    # ownership.  ``set(acquired=false)`` preserves that
                    # uncertainty without pretending a missing gain existed.
                    "action": "set",
                    "state": {
                        **legacy_state,
                        "acquired": acquired,
                        "provenance": {
                            "kind": "legacy_projection_import",
                            "source_db_hash": source_db_hash,
                            "legacy_source_event_id": source_event_id,
                            "uncertain_fields": sorted(legacy_state),
                        },
                    },
                    "evidence": {
                        "kind": "legacy_projection_import",
                        "source_db_hash": source_db_hash,
                    },
                }
            )

        if imported_payloads:
            proposal_id = ContinuityStore._stable_id(
                "proposal_legacy_power_", [source_db_hash, imported_payloads]
            )
            artifact_id = ContinuityStore._stable_id(
                "artifact_legacy_power_", source_db_hash
            )
            artifact_version_id = ContinuityStore._stable_id(
                "artifact_version_", [artifact_id, "main", 1]
            )
            token_hash = ContinuityStore._stable_id(
                "grant_token_legacy_power_", source_db_hash
            )
            binding = {
                "migration": "continuity-v4-to-v5",
                "source_db_hash": source_db_hash,
                "proposal_id": proposal_id,
            }
            proposal_payload = {"provenance": binding}
            payload_hash = ContinuityStore._stable_id(
                "payload_",
                {
                    "payload": proposal_payload,
                    "events": imported_payloads,
                },
            )
            binding_hash = ContinuityStore._stable_id(
                "grant_binding_", binding
            )
            head_before_row = connection.execute(
                "SELECT value FROM state_meta WHERE key='head_canon_revision'"
            ).fetchone()
            active_before_row = connection.execute(
                "SELECT value FROM state_meta WHERE key='active_canon_revision'"
            ).fetchone()
            head_before = int(head_before_row[0]) if head_before_row else 0
            active_before = (
                int(active_before_row[0]) if active_before_row else 0
            )
            head_after = head_before + 1
            active_after = head_after
            commit_id = ContinuityStore._stable_id(
                "canon_commit_legacy_power_",
                [proposal_id, head_before, source_db_hash],
            )
            expires_at = "9999-12-31T23:59:59.999999Z"
            connection.execute(
                """
                INSERT INTO artifacts(
                    artifact_version_id, artifact_id, artifact_kind,
                    artifact_stage, canon_status, branch_id, chapter_no,
                    scene_index, artifact_revision, source_role,
                    content_hash, content_json, active, created_at, updated_at
                ) VALUES(?, ?, 'migration', 'bootstrap', 'accepted', 'main',
                         NULL, NULL, 1, 'setting', ?, ?, 1, ?, ?)
                """,
                (
                    artifact_version_id,
                    artifact_id,
                    payload_hash,
                    ContinuityStore._canonical_json(
                        {
                            "provenance": binding,
                            "events": imported_payloads,
                        }
                    ),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO proposals(
                    proposal_id, artifact_version_id, artifact_id,
                    artifact_stage, canon_status, branch_id, chapter_no,
                    scene_index, artifact_revision, prepared_canon_revision,
                    source_role, proposal_kind, payload_hash, payload_json,
                    events_json, validation_status, status_reason,
                    accepted_commit_id, created_at, updated_at
                ) VALUES(?, ?, ?, 'bootstrap', 'accepted', 'main', NULL,
                         NULL, 1, ?, 'setting', 'legacy_power_import', ?, ?,
                         ?, 'valid', 'automatic v4 projection import', ?,
                         ?, ?)
                """,
                (
                    proposal_id,
                    artifact_version_id,
                    artifact_id,
                    active_before,
                    payload_hash,
                    ContinuityStore._canonical_json(proposal_payload),
                    ContinuityStore._canonical_json(imported_payloads),
                    commit_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO approval_grants(
                    token_hash, proposal_id, binding_hash, binding_json,
                    authorized_operations_json, expected_canon_revision,
                    issuer, channel, expires_at, consumed_request_hash,
                    accepted_commit_id, consumed_at, created_at
                ) VALUES(?, ?, ?, ?, '["accept"]', ?,
                         'schema-migration', 'continuity-v5', ?, ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    proposal_id,
                    binding_hash,
                    ContinuityStore._canonical_json(binding),
                    active_before,
                    expires_at,
                    ContinuityStore._stable_id(
                        "accepted_request_legacy_power_", binding
                    ),
                    commit_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO canon_commits(
                    commit_id, proposal_id, operation, artifact_id,
                    artifact_stage, branch_id, chapter_no, scene_index,
                    artifact_revision, head_revision_before,
                    head_revision_after, active_revision_before,
                    active_revision_after, changes_authority,
                    accepted_request_hash, grant_token_hash, payload_hash,
                    projection_hash, acceptance_source_json, created_at
                ) VALUES(?, ?, 'accept', ?, 'bootstrap', 'main', NULL, NULL,
                         1, ?, ?, ?, ?, 1, ?, ?, ?, '',
                         ?, ?)
                """,
                (
                    commit_id,
                    proposal_id,
                    artifact_id,
                    head_before,
                    head_after,
                    active_before,
                    active_after,
                    ContinuityStore._stable_id(
                        "accepted_request_legacy_power_", binding
                    ),
                    token_hash,
                    payload_hash,
                    ContinuityStore._canonical_json(
                        {
                            "issuer": "schema-migration",
                            "channel": "continuity-v5",
                            "binding_hash": binding_hash,
                            "operation": "accept",
                        }
                    ),
                    now,
                ),
            )
            for ordinal, event in enumerate(imported_payloads):
                connection.execute(
                    """
                    INSERT INTO continuity_events(
                        event_id, commit_id, event_ordinal, event_type, scope,
                        branch_id, artifact_id, artifact_revision, chapter_no,
                        scene_index, story_time, narrative_mode, entity_id,
                        subject_entity_id, target_entity_id, payload_json,
                        evidence_json, created_at
                    ) VALUES(?, ?, ?, 'ability', 'current', 'main', ?, 1,
                             NULL, NULL, NULL, 'linear', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["event_id"],
                        commit_id,
                        ordinal,
                        artifact_id,
                        event["owner_entity_id"],
                        event["owner_entity_id"],
                        event["ability_entity_id"],
                        ContinuityStore._canonical_json(event),
                        ContinuityStore._canonical_json(event["evidence"]),
                        now,
                    ),
                )
            ContinuityStore._put_meta(
                connection, "head_canon_revision", str(head_after), now
            )
            ContinuityStore._put_meta(
                connection, "active_canon_revision", str(active_after), now
            )

        for row in rows:
            try:
                state = dict(json.loads(str(row["state_json"]) or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                state = {"legacy_raw_state": str(row["state_json"])}
            action = str(state.pop("action", "") or "")
            acquired = bool(
                state.get(
                    "acquired",
                    action not in {"lose", "remove", "revoke"},
                )
            )
            ability_key = str(row["ability_key"])
            source_event_id = imported_event_ids.get(
                ability_key, str(row["source_event_id"])
            )
            runtime = {
                key: state[key]
                for key in (
                    "active",
                    "available",
                    "charges",
                    "cooldown_until",
                    "last_used_at",
                    "use_count",
                )
                if key in state
            }
            runtime["available"] = bool(
                runtime.get("available", acquired) and acquired
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO actor_ability_state(
                    ability_key, owner_entity_id, ability_entity_id, acquired,
                    ownership_json, source_event_id, story_coordinate_json,
                    updated_order
                ) VALUES(?, ?, ?, ?, ?, ?, '{}', ?)
                """,
                (
                    ability_key,
                    row["owner_entity_id"],
                    row["ability_entity_id"],
                    int(acquired),
                    ContinuityStore._canonical_json(state),
                    source_event_id,
                    int(row["updated_order"]),
                ),
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO ability_runtime_state(
                    ability_key, owner_entity_id, ability_entity_id, available,
                    runtime_json, source_event_id, story_coordinate_json,
                    updated_order
                ) VALUES(?, ?, ?, ?, ?, ?, '{}', ?)
                """,
                (
                    ability_key,
                    row["owner_entity_id"],
                    row["ability_entity_id"],
                    int(bool(runtime["available"])),
                    ContinuityStore._canonical_json(runtime),
                    source_event_id,
                    int(row["updated_order"]),
                ),
            )
            if ability_key in imported_event_ids:
                provenance = {
                    "kind": "legacy_projection_import",
                    "source_db_hash": source_db_hash,
                    "legacy_source_event_id": str(row["source_event_id"]),
                    "uncertain_fields": sorted(state),
                }
                connection.execute(
                    """
                    INSERT OR REPLACE INTO legacy_power_imports(
                        import_key, owner_entity_id, ability_entity_id,
                        state_json, imported_event_id, provenance_json,
                        created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ContinuityStore._stable_id(
                            "legacy_power_import_",
                            [source_db_hash, ability_key],
                        ),
                        row["owner_entity_id"],
                        row["ability_entity_id"],
                        str(row["state_json"]),
                        source_event_id,
                        ContinuityStore._canonical_json(provenance),
                        now,
                    ),
                )
            if not acquired:
                connection.execute(
                    "DELETE FROM ability_state WHERE ability_key=?",
                    (ability_key,),
                )

    def ensure_schema(self) -> Path | None:
        with self._schema_lock:
            if self._schema_ready:
                return self.last_backup_path

            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            anchor = self._open_database_anchor()
            anchor_stat = os.fstat(anchor.fileno())
            connection: sqlite3.Connection | None = None
            backup_source: sqlite3.Connection | None = None
            backup_identity_invalid = False
            migration_committed = False
            preserve_recovery_staging = False
            try:
                connection = self._connect()
                self._assert_database_identity(anchor_stat)
                backup_source = self._connect_read_only_path(
                    self.db_path,
                    timeout=30.0,
                )
                backup_source.execute("PRAGMA busy_timeout = 30000")
                self._assert_database_identity(anchor_stat)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._assert_database_identity(anchor_stat)
                    user_tables = self._user_tables(connection)
                    unexpected = sorted(user_tables - STATE_DATABASE_TABLES)
                    if unexpected:
                        raise StoreError(
                            "STATE_DATABASE_UNOWNED: continuity database "
                            "contains foreign user tables: "
                            f"{unexpected}"
                        )
                    # The write transaction is the cross-instance/process
                    # migration mutex.  Versions read before BEGIN IMMEDIATE
                    # can become stale while another store upgrades the same
                    # database, so backup and migration decisions must use the
                    # version observed after this lock is acquired.
                    try:
                        legacy_version, continuity_version = (
                            self._schema_versions_from_connection(connection)
                        )
                    except (sqlite3.DatabaseError, ValueError) as exc:
                        raise StoreError(
                            f"STATE_SCHEMA_UNREADABLE: {exc}"
                        ) from exc
                    if legacy_version < 0 or continuity_version < 0:
                        raise StoreError(
                            "STATE_SCHEMA_UNREADABLE: schema versions must "
                            "be non-negative"
                        )
                    stored_schema_present = bool(user_tables)
                    try:
                        validate_schema_versions(
                            user_tables_present=stored_schema_present,
                            legacy_version=legacy_version,
                            continuity_version=continuity_version,
                        )
                    except SchemaVersionError as exc:
                        if (
                            exc.code == "STATE_SCHEMA_VERSION_MISSING"
                            and stored_schema_present
                            and legacy_version == 0
                            and continuity_version == 0
                        ):
                            backup_path = self._backup_existing_database(
                                0,
                                source=backup_source,
                                anchor_stat=anchor_stat,
                                retain_identity=True,
                            )
                            try:
                                self._verify_held_backup_identity(backup_path)
                            except StoreError:
                                backup_identity_invalid = True
                                raise
                            raise StoreError(
                                f"{exc}; backup={backup_path}"
                            ) from exc
                        raise StoreError(str(exc)) from exc
                    if continuity_version == 5:
                        self._assert_v5_schema_surface(
                            connection,
                            user_tables,
                        )
                    elif continuity_version == 6:
                        self._assert_v6_schema_surface(connection)
                        self._assert_v6_metadata(
                            connection,
                            legacy_item_surface=True,
                        )
                    elif continuity_version == SCHEMA_VERSION:
                        # A database already claiming v7 is never repaired by
                        # CREATE IF NOT EXISTS.  Its complete surface and
                        # independent metadata must validate before any write.
                        self._assert_v6_schema_surface(connection)
                        self._assert_v7_item_schema_surface(connection)
                        self._assert_advantage_schema_surface(connection)
                        self._assert_v6_metadata(connection)
                        self._assert_advantage_metadata(connection)
                    source_db_hash = ""
                    if (
                        stored_schema_present
                        and continuity_version < SCHEMA_VERSION
                    ):
                        backup_path = self._backup_existing_database(
                            continuity_version or legacy_version,
                            source=backup_source,
                            anchor_stat=anchor_stat,
                            retain_identity=True,
                        )
                        try:
                            source_db_hash = (
                                self._verify_held_backup_identity(backup_path)
                            )
                        except StoreError:
                            backup_identity_invalid = True
                            raise

                    v5_immutable_snapshot = (
                        self._v5_immutable_snapshot(connection)
                        if continuity_version == 5
                        else None
                    )
                    self._execute_script_in_transaction(
                        connection, LEGACY_V2_SCHEMA_SQL
                    )
                    self._ensure_legacy_columns(connection)
                    self._execute_script_in_transaction(
                        connection, CONTINUITY_V7_SCHEMA_SQL
                    )
                    ensure_advantage_schema(connection)
                    self._assert_v6_schema_surface(connection)
                    self._assert_v7_item_schema_surface(connection)
                    self._assert_advantage_schema_surface(connection)
                    if continuity_version == 4:
                        legacy_head_row = connection.execute(
                            """
                            SELECT value FROM state_meta
                            WHERE key='head_canon_revision'
                            """
                        ).fetchone()
                        self._put_meta(
                            connection,
                            "legacy_v4_power_compat_head_revision",
                            str(
                                int(legacy_head_row[0])
                                if legacy_head_row is not None
                                else 0
                            ),
                            utc_now(),
                        )
                    self._migrate_v4_ability_projection(
                        connection,
                        from_version=continuity_version,
                        source_db_hash=source_db_hash,
                    )
                    if continuity_version < SCHEMA_VERSION:
                        migrate_legacy_item_projection(
                            connection,
                            from_version=continuity_version,
                        )
                    if (
                        v5_immutable_snapshot is not None
                        and self._v5_immutable_snapshot(connection)
                        != v5_immutable_snapshot
                    ):
                        raise StoreError(
                            "STATE_SCHEMA_V6_NON_ADDITIVE: v5 ledger, canon, "
                            "inventory, query, or projection state changed"
                        )
                    now = utc_now()
                    self._put_meta(
                        connection,
                        "schema_version",
                        str(legacy_version or 2),
                        now,
                        preserve_existing=True,
                    )
                    if continuity_version < SCHEMA_VERSION:
                        self._put_meta(
                            connection,
                            "continuity_schema_version",
                            str(SCHEMA_VERSION),
                            now,
                        )
                    self._put_meta(
                        connection,
                        "head_canon_revision",
                        "0",
                        now,
                        preserve_existing=True,
                    )
                    self._put_meta(
                        connection,
                        "active_canon_revision",
                        "0",
                        now,
                        preserve_existing=True,
                    )
                    event_experience_version = connection.execute(
                        """
                        SELECT value FROM event_experience_meta
                        WHERE key='schema_version'
                        """
                    ).fetchone()
                    if (
                        event_experience_version is not None
                        and str(event_experience_version[0]) != "1"
                    ):
                        raise StoreError(
                            "STATE_EVENT_EXPERIENCE_SCHEMA_UNSUPPORTED: "
                            f"stored={event_experience_version[0]}, supported=1"
                        )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO event_experience_meta(key, value)
                        VALUES('schema_version', '1')
                        """
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO event_experience_meta(key, value)
                        VALUES('control_revision', '0')
                        """
                    )
                    advantage_meta_count = int(
                        connection.execute(
                            "SELECT COUNT(*) "
                            "FROM advantage_projection_meta"
                        ).fetchone()[0]
                    )
                    if advantage_meta_count == 0:
                        refresh_advantage_projection_metadata(connection)
                    self._assert_v6_metadata(connection)
                    self._assert_advantage_metadata(connection)
                    self._assert_database_identity(anchor_stat)
                    if self._held_backup_identity is not None:
                        try:
                            self._verify_held_backup_identity(
                                self._held_backup_identity.path
                            )
                        except StoreError:
                            backup_identity_invalid = True
                            raise
                    connection.commit()
                    migration_committed = True
                    self._assert_database_identity(anchor_stat)
                    # Invalid or future schemas must remain byte-for-byte
                    # untouched.  Switch valid write-path stores to WAL only
                    # after the locked validation/migration transaction commits.
                    connection.execute("PRAGMA journal_mode = WAL")
                    self._assert_database_identity(anchor_stat)
                    if self._held_backup_identity is not None:
                        try:
                            self._verify_held_backup_identity(
                                self._held_backup_identity.path
                            )
                        except StoreError as exc:
                            backup_identity_invalid = True
                            try:
                                recovery_path = (
                                    self._publish_held_backup_recovery()
                                )
                            except StoreError as recovery_exc:
                                raise StoreError(
                                    f"{exc}; recovery_error={recovery_exc}"
                                ) from exc
                            preserve_recovery_staging = (
                                recovery_path
                                == self._held_backup_identity.staging_path
                            )
                            raise StoreError(
                                f"{exc}; recovery_backup={recovery_path}"
                            ) from exc
                except Exception:
                    connection.rollback()
                    raise
            except sqlite3.DatabaseError as exc:
                raise StoreError(f"STATE_SCHEMA_MIGRATION_FAILED: {exc}") from exc
            finally:
                if backup_source is not None:
                    backup_source.close()
                if connection is not None:
                    connection.close()
                anchor.close()
                self._release_held_backup_identity(
                    remove_invalid_owned_publication=(
                        backup_identity_invalid and not migration_committed
                    ),
                    preserve_staging=preserve_recovery_staging,
                )

            self._schema_ready = True
            return self.last_backup_path

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.ensure_schema()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                connection.rollback()
            raise
        finally:
            connection.close()

    @contextlib.contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        self.ensure_schema()
        connection = self._connect_read_only_path(
            self.db_path,
            timeout=30.0,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def get_meta_int(
        connection: sqlite3.Connection,
        key: str,
        default: int = 0,
    ) -> int:
        row = connection.execute(
            "SELECT value FROM state_meta WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return default
        return int(row[0])

    @staticmethod
    def set_meta_int(
        connection: sqlite3.Connection,
        key: str,
        value: int,
    ) -> None:
        now = utc_now()
        connection.execute(
            """
            INSERT INTO state_meta(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE
            SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, str(int(value)), now),
        )
