from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from plot_init.canonical import canonical_hash, canonical_json, sha256_text  # noqa: E402
from plot_init.engine import create_initial_state  # noqa: E402
from plot_init.errors import PlotInitError  # noqa: E402
from plot_init.remote_cache import SQLiteRemoteResponseCache  # noqa: E402
from plot_init.service import PlotInitService  # noqa: E402
import plot_init.storage as storage_module  # noqa: E402
from plot_init.storage import BLOB_REFERENCE_KEY, InitStorage  # noqa: E402
from plot_init_storage import main as storage_cli_main  # noqa: E402


V1_SCHEMA_SQL = """
CREATE TABLE initialization_meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO initialization_meta(key, value) VALUES('schema_version', '1');

CREATE TABLE initialization_sessions(
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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE initialization_revisions(
    session_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL,
    operation TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(session_id, session_revision)
);

CREATE TABLE initialization_checkpoints(
    checkpoint_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    source_snapshot_hash TEXT,
    dependency_hash TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE initialization_idempotency(
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);
"""


def state_payload(*, padding: int = 0, session_id: str = "session-1") -> dict[str, Any]:
    return {
        "session_id": session_id,
        "workspace_root": "C:/fixture",
        "project_root": "C:/fixture/novel",
        "mode": "new",
        "target_profile": "plot_ready",
        "interaction_profile": "deep",
        "stage": "CREATED",
        "status": "ACTIVE",
        "session_revision": 1,
        "expected_canon_revision": 0,
        "source_snapshot_hash": "source-hash",
        "proposal_id": None,
        "schema_version": "plot-rag-init/v1",
        "bundle_schema_version": "plot-rag-init/v1",
        "requested_bundle_schema_version": "auto",
        "power_model_status": "unmodeled",
        "power_model_compatibility": "v1_fallback",
        "source_manifest": [],
        "padding": "x" * padding,
        "created_at": "2026-07-16T00:00:00+00:00",
        "updated_at": "2026-07-16T00:00:00+00:00",
    }


def checkpoint_payloads(count: int = 2) -> list[dict[str, Any]]:
    return [
        {
            "checkpoint_id": f"checkpoint-{index}",
            "stage": "CREATED",
            "status": "complete",
            "source_snapshot_hash": "source-hash",
            "dependency_hash": f"dependency-{index}",
        }
        for index in range(count)
    ]


def create_v1_fixture(
    database_path: Path,
    *,
    padding: int = 0,
    checkpoint_count: int = 2,
    fixture_state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = (
        json.loads(canonical_json(fixture_state))
        if fixture_state is not None
        else state_payload(padding=padding)
    )
    response = {"status": "ACTIVE", "session_id": state["session_id"]}
    state_json = canonical_json(state)
    now = "2026-07-16T00:00:00+00:00"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.executescript(V1_SCHEMA_SQL)
        connection.execute(
            """
            INSERT INTO initialization_sessions(
                session_id, workspace_root, project_root, mode,
                target_profile, interaction_profile, stage, status,
                session_revision, expected_canon_revision,
                source_snapshot_hash, proposal_id, state_json,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                state["session_id"],
                state["workspace_root"],
                state["project_root"],
                state["mode"],
                state["target_profile"],
                state["interaction_profile"],
                state["stage"],
                state["status"],
                state["session_revision"],
                state["expected_canon_revision"],
                state["source_snapshot_hash"],
                state["proposal_id"],
                state_json,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO initialization_revisions(
                session_id, session_revision, operation, state_hash,
                state_json, created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                state["session_id"],
                1,
                "start",
                canonical_hash(state),
                state_json,
                now,
            ),
        )
        for checkpoint in checkpoint_payloads(checkpoint_count):
            connection.execute(
                """
                INSERT INTO initialization_checkpoints(
                    checkpoint_id, session_id, session_revision, stage,
                    status, source_snapshot_hash, dependency_hash,
                    state_hash, state_json, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    checkpoint["checkpoint_id"],
                    state["session_id"],
                    1,
                    checkpoint["stage"],
                    checkpoint["status"],
                    checkpoint["source_snapshot_hash"],
                    checkpoint["dependency_hash"],
                    canonical_hash(state),
                    state_json,
                    now,
                ),
            )
        connection.execute(
            """
            INSERT INTO initialization_idempotency(
                scope, idempotency_key, request_hash, response_json, created_at
            ) VALUES(?,?,?,?,?)
            """,
            (
                "start",
                "key-1",
                "request-hash",
                canonical_json(response),
                now,
            ),
        )
        connection.commit()
    return state, response


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class InitStorageV2Tests(unittest.TestCase):
    def test_new_writes_use_shared_content_addressed_blobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "init.sqlite3"
            storage = InitStorage(database)
            state = state_payload(padding=8192)
            response = {"status": "ACTIVE", "session_id": state["session_id"]}

            storage.create_session(
                state,
                checkpoint_payloads(3),
                scope="start",
                idempotency_key="key-1",
                request_hash="request-hash",
                response=response,
            )

            with closing(sqlite3.connect(database)) as connection:
                connection.row_factory = sqlite3.Row
                session = connection.execute(
                    """
                    SELECT state_json, state_blob_hash
                    FROM initialization_sessions
                    """
                ).fetchone()
                revisions = connection.execute(
                    "SELECT state_json, state_blob_hash "
                    "FROM initialization_revisions"
                ).fetchall()
                checkpoints = connection.execute(
                    "SELECT state_json, state_blob_hash "
                    "FROM initialization_checkpoints"
                ).fetchall()
                idempotency = connection.execute(
                    """
                    SELECT response_json, response_blob_hash
                    FROM initialization_idempotency
                    """
                ).fetchone()
                blobs = connection.execute(
                    """
                    SELECT blob_hash, codec, uncompressed_bytes
                    FROM initialization_payload_blobs
                    """
                ).fetchall()

            state_hashes = {
                str(session["state_blob_hash"]),
                *(str(row["state_blob_hash"]) for row in revisions),
                *(str(row["state_blob_hash"]) for row in checkpoints),
            }
            self.assertEqual(1, len(state_hashes))
            self.assertEqual(2, len(blobs))
            self.assertTrue(any(str(row["codec"]) == "zlib" for row in blobs))
            self.assertEqual(
                {BLOB_REFERENCE_KEY: str(session["state_blob_hash"])},
                json.loads(str(session["state_json"])),
            )
            self.assertEqual(
                {BLOB_REFERENCE_KEY: str(idempotency["response_blob_hash"])},
                json.loads(str(idempotency["response_json"])),
            )
            self.assertEqual(state, storage.load_session(state["session_id"]))
            replay = storage.lookup_idempotency(
                "start",
                "key-1",
                "request-hash",
            )
            self.assertEqual(True, replay["idempotent"])
            self.assertEqual(response["session_id"], replay["session_id"])
            key_replay = storage.lookup_idempotency_key("start", "key-1")
            self.assertEqual(True, key_replay["idempotent"])
            self.assertEqual(response["session_id"], key_replay["session_id"])
            self.assertIsNone(
                storage.lookup_idempotency_key("start", "missing-key")
            )

    def test_v1_read_compatibility_and_schema_upgrade_do_not_bulk_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "init.sqlite3"
            expected_state, expected_response = create_v1_fixture(database)
            storage = InitStorage(database)

            self.assertEqual(
                expected_state,
                storage.load_session(expected_state["session_id"]),
            )
            replay = storage.lookup_idempotency(
                "start",
                "key-1",
                "request-hash",
            )
            self.assertEqual(expected_response["session_id"], replay["session_id"])

            storage._initialize()
            with closing(sqlite3.connect(database)) as connection:
                schema_version = connection.execute(
                    """
                    SELECT value FROM initialization_meta
                    WHERE key='schema_version'
                    """
                ).fetchone()[0]
                state_json, blob_hash = connection.execute(
                    """
                    SELECT state_json, state_blob_hash
                    FROM initialization_sessions
                    """
                ).fetchone()
                blob_count = connection.execute(
                    "SELECT COUNT(*) FROM initialization_payload_blobs"
                ).fetchone()[0]
            self.assertEqual("2", schema_version)
            self.assertEqual(canonical_json(expected_state), state_json)
            self.assertIsNone(blob_hash)
            self.assertEqual(0, blob_count)
            self.assertTrue(storage.migration_plan()["migration_required"])

    def test_full_inline_payload_must_match_populated_blob_column(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "init.sqlite3"
            storage = InitStorage(database)
            state = state_payload()
            storage.create_session(
                state,
                checkpoint_payloads(1),
                scope="start",
                idempotency_key="key-1",
                request_hash="request-hash",
                response={"status": "ACTIVE"},
            )
            tampered = dict(state)
            tampered["mode"] = "ingest"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """
                    UPDATE initialization_sessions
                    SET state_json=?
                    WHERE session_id=?
                    """,
                    (canonical_json(tampered), state["session_id"]),
                )
                connection.commit()

            with self.assertRaises(PlotInitError) as raised:
                storage.load_session(state["session_id"])
            self.assertEqual(
                "CORRUPT_INIT_BLOB_REFERENCE",
                raised.exception.code,
            )
            plan = storage.migration_plan()
            self.assertTrue(plan["blocked"])
            self.assertEqual(1, plan["rows"]["corrupt"])
            self.assertEqual(
                0,
                plan["tables"]["sessions"]["valid_row_refs"],
            )
            with self.assertRaises(PlotInitError) as migration_error:
                storage.migrate_payload_storage(dry_run=False)
            self.assertEqual(
                "INIT_STORAGE_MIGRATION_BLOCKED",
                migration_error.exception.code,
            )
            self.assertFalse((database.parent / "backups").exists())

            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """
                    UPDATE initialization_sessions
                    SET state_json=?
                    WHERE session_id=?
                    """,
                    (canonical_json(state), state["session_id"]),
                )
                connection.commit()
            self.assertEqual(state, storage.load_session(state["session_id"]))
            repaired = storage.migrate_payload_storage(dry_run=False)
            self.assertEqual(1, repaired["migrated_rows"])
            self.assertEqual("ok", repaired["quick_check"])

    def test_dry_run_is_byte_for_byte_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "init.sqlite3"
            create_v1_fixture(database, padding=64 * 1024)
            storage = InitStorage(database)
            before_hash = file_sha256(database)
            before_files = sorted(path.name for path in root.iterdir())

            plan = storage.migration_plan()
            result = storage.migrate_payload_storage(dry_run=True)

            self.assertEqual(before_hash, file_sha256(database))
            self.assertEqual(before_files, sorted(path.name for path in root.iterdir()))
            self.assertEqual("ok", plan["quick_check"])
            self.assertEqual("dry_run", result["status"])
            self.assertIsNone(result["backup_path"])
            self.assertGreater(result["row_refs"], 0)
            self.assertGreater(result["dedup_bytes"], 0)

    def test_foreign_or_versionless_database_fails_closed(self) -> None:
        fixtures = {
            "foreign": (
                (
                    "CREATE TABLE user_finance("
                    "id INTEGER PRIMARY KEY, amount INT);"
                    "INSERT INTO user_finance(amount) VALUES(7);"
                ),
                "INIT_STORAGE_DATABASE_UNOWNED",
            ),
            "meta_without_version": (
                (
                    "CREATE TABLE initialization_meta("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                ),
                "INIT_STORAGE_SCHEMA_VERSION_MISSING",
            ),
            "partial_init": (
                (
                    "CREATE TABLE initialization_sessions("
                    "session_id TEXT PRIMARY KEY);"
                ),
                "INIT_STORAGE_SCHEMA_VERSION_MISSING",
            ),
        }
        for label, (schema, expected_code) in fixtures.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                database = Path(temporary) / "init.sqlite3"
                with closing(sqlite3.connect(database)) as connection:
                    connection.executescript(schema)
                    connection.commit()
                before = database.read_bytes()
                before_files = {
                    path.name: path.read_bytes()
                    for path in database.parent.iterdir()
                    if path.is_file()
                }
                storage = InitStorage(database)

                with self.assertRaises(PlotInitError) as raised:
                    storage.create_session(
                        state_payload(),
                        checkpoint_payloads(1),
                        scope="start",
                        idempotency_key="foreign-key",
                        request_hash="foreign-request",
                        response={"status": "ACTIVE"},
                    )

                self.assertEqual(
                    expected_code,
                    raised.exception.code,
                )
                self.assertEqual(before, database.read_bytes())
                self.assertEqual(
                    before_files,
                    {
                        path.name: path.read_bytes()
                        for path in database.parent.iterdir()
                        if path.is_file()
                    },
                )
                self.assertFalse((database.parent / "backups").exists())

    def test_negative_initialization_schema_version_is_read_only_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "init.sqlite3"
            create_v1_fixture(database)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """
                    UPDATE initialization_meta
                    SET value='-1'
                    WHERE key='schema_version'
                    """
                )
                connection.commit()
            before = database.read_bytes()
            storage = InitStorage(database)

            with self.assertRaises(PlotInitError) as raised:
                storage.migration_plan()

            self.assertEqual("CORRUPT_INIT_STORAGE", raised.exception.code)
            self.assertEqual(before, database.read_bytes())

    def test_space_preflight_covers_backup_and_compact_across_volumes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "init.sqlite3"
            create_v1_fixture(database)
            storage = InitStorage(database)
            metrics = storage.migration_plan()["file"]
            estimated = max(
                int(metrics["database_bytes"]),
                int(metrics["allocated_page_bytes"]),
                1,
            )
            backup_parent = root / "custom-backup"
            high_usage = shutil.disk_usage(root)._replace(free=10**12)
            low_usage = high_usage._replace(free=1)

            def volume_key(path: Path) -> str:
                return (
                    "source"
                    if Path(path) == storage.database_path.parent
                    else "backup"
                )

            def cross_volume_usage(path: Path) -> Any:
                return (
                    low_usage
                    if Path(path) == storage.database_path.parent
                    else high_usage
                )

            with (
                mock.patch.object(
                    storage,
                    "_volume_key",
                    side_effect=volume_key,
                ),
                mock.patch.object(
                    storage,
                    "_disk_usage_for_path",
                    side_effect=cross_volume_usage,
                ),
            ):
                no_compact = storage._migration_space_check(
                    backup_parent / "backup.sqlite3",
                    metrics,
                    compact=False,
                )
                self.assertFalse(no_compact["same_volume"])
                self.assertEqual(0, no_compact["source"]["required_free_bytes"])
                with self.assertRaises(PlotInitError) as compact_error:
                    storage._migration_space_check(
                        backup_parent / "backup.sqlite3",
                        metrics,
                        compact=True,
                    )
                self.assertEqual(
                    "INIT_STORAGE_COMPACT_SPACE_LOW",
                    compact_error.exception.code,
                )

            with (
                mock.patch.object(
                    storage,
                    "_volume_key",
                    return_value="same",
                ),
                mock.patch.object(
                    storage,
                    "_disk_usage_for_path",
                    return_value=high_usage,
                ),
            ):
                same_volume = storage._migration_space_check(
                    backup_parent / "backup.sqlite3",
                    metrics,
                    compact=True,
                )
            expected_vacuum_peak = max(
                estimated * 2,
                16 * 1024 * 1024,
            )
            self.assertEqual(
                estimated + expected_vacuum_peak,
                same_volume["source"]["required_free_bytes"],
            )

            with (
                mock.patch.object(
                    storage,
                    "_volume_key",
                    side_effect=volume_key,
                ),
                mock.patch.object(
                    storage,
                    "_disk_usage_for_path",
                    side_effect=lambda path: (
                        high_usage
                        if Path(path) == storage.database_path.parent
                        else low_usage
                    ),
                ),
            ):
                with self.assertRaises(PlotInitError) as backup_error:
                    storage._migration_space_check(
                        backup_parent / "backup.sqlite3",
                        metrics,
                        compact=False,
                    )
            self.assertEqual(
                "INIT_STORAGE_BACKUP_SPACE_LOW",
                backup_error.exception.code,
            )

            with (
                mock.patch.object(
                    storage,
                    "_volume_key",
                    return_value="same",
                ),
                mock.patch.object(
                    storage,
                    "_disk_usage_for_path",
                    return_value=low_usage,
                ),
                mock.patch.object(storage, "_backup_database") as backup_call,
                self.assertRaises(PlotInitError) as integrated_error,
            ):
                storage.migrate_payload_storage(dry_run=False)
            self.assertEqual(
                "INIT_STORAGE_BACKUP_SPACE_LOW",
                integrated_error.exception.code,
            )
            backup_call.assert_not_called()

    def test_failed_payload_migration_keeps_backup_and_retries_idempotently(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "init.sqlite3"
            create_v1_fixture(database)
            storage = InitStorage(database)

            injected = PlotInitError(
                "INJECTED_MIGRATION_FAILURE",
                "fixture failure",
            )
            with (
                mock.patch.object(
                    storage,
                    "_migrate_payload_rows",
                    side_effect=injected,
                ),
                self.assertRaises(PlotInitError) as raised,
            ):
                storage.migrate_payload_storage(dry_run=False)
            self.assertEqual(
                "INJECTED_MIGRATION_FAILURE",
                raised.exception.code,
            )
            self.assertFalse(raised.exception.details["migration_committed"])
            self.assertEqual(
                "payload_migration",
                raised.exception.details["failure_phase"],
            )
            self.assertTrue(
                Path(raised.exception.details["backup_path"]).is_file()
            )
            with closing(sqlite3.connect(database)) as connection:
                schema_version = connection.execute(
                    """
                    SELECT value FROM initialization_meta
                    WHERE key='schema_version'
                    """
                ).fetchone()[0]
                session_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(initialization_sessions)"
                    )
                }
                state_json = connection.execute(
                    "SELECT state_json FROM initialization_sessions"
                ).fetchone()[0]
            self.assertEqual("1", schema_version)
            self.assertNotIn("state_blob_hash", session_columns)
            self.assertNotIn(BLOB_REFERENCE_KEY, json.loads(state_json))

            retried = storage.migrate_payload_storage(dry_run=False)
            self.assertGreater(retried["migrated_rows"], 0)
            self.assertEqual("ok", retried["quick_check"])

    def test_cli_reports_committed_migration_when_compact_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "init.sqlite3"
            create_v1_fixture(database, padding=64 * 1024)
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    InitStorage,
                    "_compact_database",
                    side_effect=sqlite3.OperationalError("fixture vacuum failure"),
                ),
                redirect_stdout(stdout),
            ):
                exit_code = storage_cli_main(
                    [
                        "migrate",
                        "--database",
                        str(database),
                        "--compact",
                    ]
                )
            self.assertEqual(2, exit_code)
            error = json.loads(stdout.getvalue())
            self.assertEqual("INIT_STORAGE_COMPACT_FAILED", error["code"])
            self.assertTrue(error["details"]["migration_committed"])
            self.assertEqual("compact", error["details"]["failure_phase"])
            self.assertTrue(Path(error["details"]["backup_path"]).is_file())
            after_failure = InitStorage(database).migration_plan()
            self.assertFalse(after_failure["migration_required"])
            self.assertEqual("ok", after_failure["quick_check"])

    def test_compact_failure_releases_backup_only_after_compact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "init.sqlite3"
            create_v1_fixture(database, padding=64 * 1024)

            class ObservedStorage(InitStorage):
                release_calls = 0
                release_calls_at_compact: int | None = None

                @staticmethod
                def _release_backup_identity(held, **kwargs) -> None:
                    ObservedStorage.release_calls += 1
                    InitStorage._release_backup_identity(held, **kwargs)

                def _compact_database(self) -> None:
                    type(self).release_calls_at_compact = (
                        type(self).release_calls
                    )
                    raise sqlite3.OperationalError("fixture vacuum failure")

            with self.assertRaises(PlotInitError) as raised:
                ObservedStorage(database).migrate_payload_storage(
                    dry_run=False,
                    compact=True,
                )

            self.assertEqual(
                "INIT_STORAGE_COMPACT_FAILED",
                raised.exception.code,
            )
            self.assertEqual(0, ObservedStorage.release_calls_at_compact)
            self.assertEqual(1, ObservedStorage.release_calls)
            self.assertTrue(
                Path(raised.exception.details["backup_path"]).is_file()
            )

    def test_precommit_failure_closes_live_database_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "init.sqlite3"
            moved = root / "moved.sqlite3"
            create_v1_fixture(database)

            class FailingMigrationStorage(InitStorage):
                def _migrate_payload_rows(self, connection):
                    raise RuntimeError("injected payload migration failure")

            with self.assertRaises(PlotInitError):
                FailingMigrationStorage(database).migrate_payload_storage(
                    dry_run=False
                )

            os.replace(database, moved)
            os.replace(moved, database)
            self.assertTrue(database.is_file())

    def test_commit_failure_closes_live_database_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "init.sqlite3"
            moved = root / "moved.sqlite3"
            create_v1_fixture(database)
            storage = InitStorage(database)
            expected_rw_uri = (
                f"{storage.database_path.as_uri()}?mode=rw"
            )
            original_connect = storage_module.sqlite3.connect

            class FailingCommitConnection(sqlite3.Connection):
                def commit(self) -> None:
                    raise sqlite3.OperationalError("injected commit failure")

            def intercept_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
                if args and args[0] == expected_rw_uri:
                    return original_connect(
                        *args,
                        **kwargs,
                        factory=FailingCommitConnection,
                    )
                return original_connect(*args, **kwargs)

            with (
                mock.patch.object(
                    storage_module.sqlite3,
                    "connect",
                    side_effect=intercept_connect,
                ),
                self.assertRaises(PlotInitError) as raised,
            ):
                storage.migrate_payload_storage(dry_run=False)

            self.assertEqual(
                "INIT_STORAGE_MIGRATION_FAILED",
                raised.exception.code,
            )
            self.assertFalse(raised.exception.details["migration_committed"])
            os.replace(database, moved)
            os.replace(moved, database)
            self.assertTrue(database.is_file())

    def test_postcommit_identity_failure_closes_live_database_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "init.sqlite3"
            moved = root / "moved.sqlite3"
            create_v1_fixture(database)

            class FailingPostCommitVerifyStorage(InitStorage):
                verify_calls = 0

                def _verify_backup_identity(self, held) -> None:
                    type(self).verify_calls += 1
                    if type(self).verify_calls == 3:
                        raise PlotInitError(
                            "INJECTED_POST_COMMIT_VERIFY_FAILURE",
                            "fixture post-commit identity failure",
                        )
                    super()._verify_backup_identity(held)

            with self.assertRaises(PlotInitError) as raised:
                FailingPostCommitVerifyStorage(
                    database
                ).migrate_payload_storage(dry_run=False)

            self.assertEqual(
                "INJECTED_POST_COMMIT_VERIFY_FAILURE",
                raised.exception.code,
            )
            self.assertTrue(raised.exception.details["migration_committed"])
            os.replace(database, moved)
            os.replace(moved, database)
            self.assertTrue(database.is_file())

    def test_v1_database_supports_remote_cache_before_v2_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "init.sqlite3"
            expected_state, _response = create_v1_fixture(database)
            cache = SQLiteRemoteResponseCache(database)
            resolve_args = {
                "provider": "siliconflow",
                "base_url": "https://api.siliconflow.cn/v1",
                "model": "fixture-model",
                "prompt": {"task": "classification", "source": "legacy-v1"},
                "system_prompt": "system contract v1",
                "schema": {"type": "object"},
                "source_hash": "legacy-v1-source",
                "generation_parameters": {
                    "temperature": 0.0,
                    "top_p": 1.0,
                    "max_tokens": 2400,
                    "response_format": {"type": "json_object"},
                },
            }

            first = cache.resolve(
                **resolve_args,
                loader=lambda: {"value": "cached-before-migration"},
            )
            self.assertFalse(first["cache_hit"])
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    "1",
                    connection.execute(
                        """
                        SELECT value FROM initialization_meta
                        WHERE key='schema_version'
                        """
                    ).fetchone()[0],
                )

            migrated = InitStorage(database).migrate_payload_storage(
                dry_run=False
            )
            self.assertEqual("migrated", migrated["status"])
            self.assertEqual(
                expected_state,
                InitStorage(database).load_session(expected_state["session_id"]),
            )
            second = cache.resolve(
                **resolve_args,
                loader=lambda: self.fail("cache entry should survive migration"),
            )
            self.assertTrue(second["cache_hit"])
            self.assertEqual(
                {"value": "cached-before-migration"},
                second["response"],
            )
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    "2",
                    connection.execute(
                        """
                        SELECT value FROM initialization_meta
                        WHERE key='schema_version'
                        """
                    ).fetchone()[0],
                )

    def test_v1_service_advance_caches_remote_review_before_v2_save(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            project.mkdir()
            source = workspace / "片段.txt"
            source_text = "叶舟把霜河城视为最后退路。"
            source.write_text(source_text, encoding="utf-8")
            database = workspace / "init.sqlite3"
            initial = create_initial_state(
                session_id="session-v1-service",
                workspace_root=workspace,
                project_root=project,
                mode="ingest",
                target_profile="normalize_only",
                interaction_profile="minimal",
                seed=None,
                source_paths=[source],
                expected_canon_revision=0,
                bundle_schema_version="plot-rag-init/v1",
                session_revision=1,
            )
            create_v1_fixture(
                database,
                checkpoint_count=0,
                fixture_state=initial,
            )
            service = PlotInitService(
                workspace,
                database_path=database,
            )
            remote_calls: list[str] = []

            def remote_response(
                _config: Any,
                *,
                system_prompt: str,
                user_payload: dict[str, Any],
            ) -> dict[str, Any]:
                del system_prompt
                task = str(user_payload["task"])
                remote_calls.append(task)
                if task == "classification":
                    return {
                        "source_role": "note",
                        "confidence": 0.95,
                        "exact_evidence": source_text,
                    }
                return {
                    "claims": [
                        {
                            "subject": "叶舟",
                            "predicate": "actor.goal",
                            "object_or_value": "霜河城",
                            "exact_evidence": source_text,
                            "confidence": 0.95,
                        }
                    ]
                }

            environment = {
                "PLOT_RAG_INIT_REMOTE_ENABLED": "true",
                "PLOT_RAG_LLM_BASE_URL": "https://api.siliconflow.cn/v1",
                "PLOT_RAG_LLM_MODEL": "fixture-model",
                "PLOT_RAG_LLM_API_KEY": "TOKEN_TEST_ONLY_V1_SERVICE",
            }
            with (
                mock.patch.dict(os.environ, environment, clear=False),
                mock.patch(
                    "plot_init.remote_model._remote_json",
                    side_effect=remote_response,
                ),
            ):
                advanced = service.advance(
                    initial["session_id"],
                    expected_session_revision=1,
                    idempotency_key="advance-v1-service",
                )

            self.assertEqual(2, advanced["session_revision"])
            self.assertTrue(remote_calls)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    "2",
                    connection.execute(
                        """
                        SELECT value FROM initialization_meta
                        WHERE key='schema_version'
                        """
                    ).fetchone()[0],
                )
                self.assertGreater(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM initialization_remote_response_cache
                        """
                    ).fetchone()[0],
                    0,
                )

    def test_v1_service_retry_reuses_cache_after_save_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            project.mkdir()
            source = workspace / "片段.txt"
            source_text = "叶舟把霜河城视为最后退路。"
            source.write_text(source_text, encoding="utf-8")
            database = workspace / "init.sqlite3"
            initial = create_initial_state(
                session_id="session-v1-retry",
                workspace_root=workspace,
                project_root=project,
                mode="ingest",
                target_profile="normalize_only",
                interaction_profile="minimal",
                seed=None,
                source_paths=[source],
                expected_canon_revision=0,
                bundle_schema_version="plot-rag-init/v1",
                session_revision=1,
            )
            create_v1_fixture(
                database,
                checkpoint_count=0,
                fixture_state=initial,
            )
            service = PlotInitService(
                workspace,
                database_path=database,
            )
            remote_calls: list[str] = []

            def remote_response(
                _config: Any,
                *,
                system_prompt: str,
                user_payload: dict[str, Any],
            ) -> dict[str, Any]:
                del system_prompt
                task = str(user_payload["task"])
                remote_calls.append(task)
                if task == "classification":
                    return {
                        "source_role": "note",
                        "confidence": 0.95,
                        "exact_evidence": source_text,
                    }
                return {
                    "claims": [
                        {
                            "subject": "叶舟",
                            "predicate": "actor.goal",
                            "object_or_value": "霜河城",
                            "exact_evidence": source_text,
                            "confidence": 0.95,
                        }
                    ]
                }

            environment = {
                "PLOT_RAG_INIT_REMOTE_ENABLED": "true",
                "PLOT_RAG_LLM_BASE_URL": "https://api.siliconflow.cn/v1",
                "PLOT_RAG_LLM_MODEL": "fixture-model",
                "PLOT_RAG_LLM_API_KEY": "TOKEN_TEST_ONLY_V1_RETRY",
            }
            with (
                mock.patch.dict(os.environ, environment, clear=False),
                mock.patch(
                    "plot_init.remote_model._remote_json",
                    side_effect=remote_response,
                ),
            ):
                with (
                    mock.patch.object(
                        service.storage,
                        "save_session",
                        side_effect=PlotInitError(
                            "INJECTED_SAVE_FAILURE",
                            "fixture save failure after remote cache write",
                        ),
                    ),
                    self.assertRaises(PlotInitError) as raised,
                ):
                    service.advance(
                        initial["session_id"],
                        expected_session_revision=1,
                        idempotency_key="advance-v1-retry",
                    )
                self.assertEqual(
                    "INJECTED_SAVE_FAILURE",
                    raised.exception.code,
                )
                calls_after_failure = list(remote_calls)
                with closing(sqlite3.connect(database)) as connection:
                    self.assertEqual(
                        "1",
                        connection.execute(
                            """
                            SELECT value FROM initialization_meta
                            WHERE key='schema_version'
                            """
                        ).fetchone()[0],
                    )
                    self.assertGreater(
                        connection.execute(
                            """
                            SELECT COUNT(*)
                            FROM initialization_remote_response_cache
                            """
                        ).fetchone()[0],
                        0,
                    )

                retried = service.advance(
                    initial["session_id"],
                    expected_session_revision=1,
                    idempotency_key="advance-v1-retry",
                )

            self.assertEqual(calls_after_failure, remote_calls)
            self.assertEqual(2, retried["session_revision"])
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    "2",
                    connection.execute(
                        """
                        SELECT value FROM initialization_meta
                        WHERE key='schema_version'
                        """
                    ).fetchone()[0],
                )

    @unittest.skipIf(
        os.name == "nt",
        "Windows prevents replacing an open initialization database",
    )
    def test_compact_detects_live_database_replacement_and_keeps_backup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "init.sqlite3"
            displaced = root / "migrated-original.sqlite3"
            attacker = root / "attacker.sqlite3"
            backup = root / "backups" / "pre-migration.sqlite3"
            create_v1_fixture(database, padding=64 * 1024)
            with closing(sqlite3.connect(attacker)) as connection, connection:
                connection.execute(
                    "CREATE TABLE attacker_marker(value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO attacker_marker(value) VALUES('replacement')"
                )

            class ReplacingCompactStorage(InitStorage):
                def _compact_database(self) -> None:
                    os.replace(self.database_path, displaced)
                    os.replace(attacker, self.database_path)

            with self.assertRaises(PlotInitError) as raised:
                ReplacingCompactStorage(database).migrate_payload_storage(
                    dry_run=False,
                    backup_path=backup,
                    compact=True,
                )

            self.assertEqual("INIT_STORAGE_PATH_CHANGED", raised.exception.code)
            self.assertTrue(raised.exception.details["migration_committed"])
            self.assertEqual(
                backup,
                Path(raised.exception.details["backup_path"]),
            )
            with closing(sqlite3.connect(backup)) as connection:
                self.assertEqual(
                    "1",
                    connection.execute(
                        """
                        SELECT value FROM initialization_meta
                        WHERE key='schema_version'
                        """
                    ).fetchone()[0],
                )
            with closing(sqlite3.connect(displaced)) as connection:
                self.assertEqual(
                    "2",
                    connection.execute(
                        """
                        SELECT value FROM initialization_meta
                        WHERE key='schema_version'
                        """
                    ).fetchone()[0],
                )
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    "replacement",
                    connection.execute(
                        "SELECT value FROM attacker_marker"
                    ).fetchone()[0],
                )

    @unittest.skipIf(
        os.name == "nt",
        "POSIX permits replacing an open retained backup",
    )
    def test_compact_backup_replacement_publishes_recovery_copy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "init.sqlite3"
            backup = root / "backups" / "compact-window.sqlite3"
            attacker = root / "attacker.sqlite3"
            create_v1_fixture(database, padding=64 * 1024)
            with closing(sqlite3.connect(attacker)) as connection, connection:
                connection.execute(
                    "CREATE TABLE attacker_marker(value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO attacker_marker(value) VALUES('not-source')"
                )

            class ReplacingDuringCompact(InitStorage):
                def _compact_database(self) -> None:
                    os.replace(attacker, backup)
                    raise sqlite3.OperationalError("fixture vacuum failure")

            with self.assertRaises(PlotInitError) as raised:
                ReplacingDuringCompact(database).migrate_payload_storage(
                    dry_run=False,
                    backup_path=backup,
                    compact=True,
                )

            self.assertEqual(
                "INIT_STORAGE_COMPACT_FAILED",
                raised.exception.code,
            )
            self.assertEqual(
                backup,
                Path(raised.exception.details["invalid_backup_path"]),
            )
            recovery = Path(
                raised.exception.details["recovery_backup_path"]
            )
            self.assertEqual(
                recovery,
                Path(raised.exception.details["backup_path"]),
            )
            self.assertNotEqual(backup, recovery)
            self.assertTrue(recovery.is_file())
            with closing(sqlite3.connect(recovery)) as connection:
                self.assertEqual(
                    "1",
                    connection.execute(
                        """
                        SELECT value FROM initialization_meta
                        WHERE key='schema_version'
                        """
                    ).fetchone()[0],
                )
            with closing(sqlite3.connect(backup)) as connection:
                self.assertEqual(
                    "not-source",
                    connection.execute(
                        "SELECT value FROM attacker_marker"
                    ).fetchone()[0],
                )

    def test_actual_migration_creates_restorable_backup_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "含 空格" / "init 数据.sqlite3"
            database.parent.mkdir(parents=True)
            custom_backup = root / "备份 目录" / "在线 备份.sqlite3"
            expected_state, _response = create_v1_fixture(
                database,
                padding=32 * 1024,
                checkpoint_count=3,
            )
            storage = InitStorage(database)

            wal_connection = sqlite3.connect(database)
            try:
                wal_connection.execute("PRAGMA journal_mode=WAL")
                wal_connection.execute("PRAGMA wal_autocheckpoint=0")
                wal_connection.execute(
                    """
                    INSERT INTO initialization_meta(key, value)
                    VALUES('online_backup_sentinel', 'committed-wal')
                    """
                )
                wal_connection.commit()
                self.assertTrue(Path(f"{database}-wal").is_file())
                first = storage.migrate_payload_storage(
                    dry_run=False,
                    backup_path=custom_backup,
                )
            finally:
                wal_connection.close()
            backup = Path(first["backup_path"])
            self.assertEqual(custom_backup.resolve(), backup)
            self.assertTrue(backup.is_file())
            self.assertEqual("ok", first["backup_quick_check"])
            self.assertEqual("ok", first["quick_check"])
            self.assertEqual(6, first["migrated_rows"])
            self.assertEqual(6, first["row_refs"])
            self.assertEqual(2, first["unique_blobs"])
            self.assertGreater(first["dedup_bytes"], 0)
            self.assertEqual(
                expected_state,
                InitStorage(backup).load_session(expected_state["session_id"]),
            )
            with closing(sqlite3.connect(backup)) as connection:
                backup_version = connection.execute(
                    """
                    SELECT value FROM initialization_meta
                    WHERE key='schema_version'
                    """
                ).fetchone()[0]
                backup_has_blob_table = connection.execute(
                    """
                    SELECT COUNT(*) FROM sqlite_master
                    WHERE type='table'
                      AND name='initialization_payload_blobs'
                    """
                ).fetchone()[0]
                sentinel = connection.execute(
                    """
                    SELECT value FROM initialization_meta
                    WHERE key='online_backup_sentinel'
                    """
                ).fetchone()[0]
            self.assertEqual("1", backup_version)
            self.assertEqual(0, backup_has_blob_table)
            self.assertEqual("committed-wal", sentinel)

            second = storage.migrate_payload_storage(dry_run=False)
            self.assertEqual(0, second["migrated_rows"])
            self.assertEqual(0, second["reconciled_rows"])
            self.assertEqual(first["row_refs"], second["row_refs"])
            self.assertEqual(first["unique_blobs"], second["unique_blobs"])

    def test_concurrent_auto_migrations_create_independent_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "init.sqlite3"
            create_v1_fixture(database, padding=32 * 1024)
            start_barrier = threading.Barrier(2)

            class FrozenDateTime:
                @classmethod
                def now(cls, tz: timezone | None = None) -> datetime:
                    return datetime(
                        2026,
                        7,
                        16,
                        12,
                        0,
                        0,
                        tzinfo=timezone.utc,
                    )

            results: list[dict[str, Any]] = []
            errors: list[BaseException] = []

            def migrate() -> None:
                try:
                    start_barrier.wait(timeout=10)
                    results.append(
                        InitStorage(database).migrate_payload_storage(
                            dry_run=False
                        )
                    )
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            with mock.patch.object(
                storage_module,
                "datetime",
                FrozenDateTime,
            ):
                threads = [
                    threading.Thread(target=migrate, daemon=True)
                    for _ in range(2)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual([], errors)
            self.assertEqual(2, len(results))
            backup_paths = {
                Path(result["backup_path"]).resolve() for result in results
            }
            self.assertEqual(2, len(backup_paths))
            self.assertTrue(all(path.is_file() for path in backup_paths))
            self.assertTrue(
                all(result["backup_quick_check"] == "ok" for result in results)
            )
            self.assertEqual(
                [0, 5],
                sorted(int(result["migrated_rows"]) for result in results),
            )

    def test_migration_rejects_database_sidecar_backup_paths(self) -> None:
        for suffix in ("-journal", "-wal", "-shm"):
            with (
                self.subTest(suffix=suffix),
                tempfile.TemporaryDirectory() as temporary,
            ):
                database = Path(temporary) / "init.sqlite3"
                create_v1_fixture(database)
                with self.assertRaises(PlotInitError) as raised:
                    InitStorage(database).migrate_payload_storage(
                        dry_run=False,
                        backup_path=Path(f"{database}{suffix}"),
                    )
                self.assertEqual(
                    "INIT_STORAGE_BACKUP_PATH_INVALID",
                    raised.exception.code,
                )
                with closing(sqlite3.connect(database)) as connection:
                    version = connection.execute(
                        """
                        SELECT value FROM initialization_meta
                        WHERE key='schema_version'
                        """
                    ).fetchone()[0]
                self.assertEqual("1", version)

    @unittest.skipIf(
        os.name == "nt",
        "Windows prevents replacing an open SQLite database",
    )
    def test_migration_rejects_database_path_replacement_before_backup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "init.sqlite3"
            create_v1_fixture(database)
            replacement = root / "replacement-init.sqlite3"
            displaced = root / "displaced-init.sqlite3"
            shutil.copyfile(database, replacement)

            class ReplacingStorage(InitStorage):
                def _backup_database(self, destination: Path, **kwargs):
                    os.replace(self.database_path, displaced)
                    os.replace(replacement, self.database_path)
                    return super()._backup_database(
                        destination,
                        **kwargs,
                    )

            with self.assertRaises(PlotInitError) as raised:
                ReplacingStorage(database).migrate_payload_storage(
                    dry_run=False
                )
            self.assertEqual("INIT_STORAGE_PATH_CHANGED", raised.exception.code)

            for path in (database, displaced):
                with closing(sqlite3.connect(path)) as connection:
                    version = connection.execute(
                        """
                        SELECT value FROM initialization_meta
                        WHERE key='schema_version'
                        """
                    ).fetchone()[0]
                self.assertEqual("1", version)

    def test_migration_write_lock_spans_backup_schema_and_payload_rewrite(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "init.sqlite3"
            create_v1_fixture(database, padding=32 * 1024)

            migration_entered = threading.Event()
            release_migration = threading.Event()
            writer_attempting = threading.Event()
            writer_finished = threading.Event()
            migration_result: dict[str, Any] = {}
            errors: list[BaseException] = []

            class PausedMigrationStorage(InitStorage):
                def _migrate_payload_rows(
                    self,
                    connection: sqlite3.Connection,
                ) -> dict[str, int]:
                    migration_entered.set()
                    if not release_migration.wait(timeout=10):
                        raise AssertionError("migration release timed out")
                    return super()._migrate_payload_rows(connection)

            def migrate() -> None:
                try:
                    migration_result.update(
                        PausedMigrationStorage(
                            database
                        ).migrate_payload_storage(dry_run=False)
                    )
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            def write_during_migration() -> None:
                connection = sqlite3.connect(database, timeout=10.0)
                try:
                    writer_attempting.set()
                    connection.execute(
                        """
                        INSERT INTO initialization_meta(key, value)
                        VALUES(
                            'concurrent_writer_sentinel',
                            'after-migration'
                        )
                        """
                    )
                    connection.commit()
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)
                finally:
                    connection.close()
                    writer_finished.set()

            migration_thread = threading.Thread(target=migrate, daemon=True)
            migration_thread.start()
            self.assertTrue(migration_entered.wait(timeout=10))

            writer_thread = threading.Thread(
                target=write_during_migration,
                daemon=True,
            )
            writer_thread.start()
            self.assertTrue(writer_attempting.wait(timeout=5))
            self.assertFalse(writer_finished.wait(timeout=0.2))

            release_migration.set()
            migration_thread.join(timeout=20)
            writer_thread.join(timeout=20)

            self.assertFalse(migration_thread.is_alive())
            self.assertFalse(writer_thread.is_alive())
            self.assertEqual([], errors)
            self.assertEqual("migrated", migration_result["status"])
            backup = Path(migration_result["backup_path"])
            with closing(sqlite3.connect(backup)) as connection:
                backup_rows = connection.execute(
                    """
                    SELECT COUNT(*) FROM initialization_meta
                    WHERE key='concurrent_writer_sentinel'
                    """
                ).fetchone()[0]
            with closing(sqlite3.connect(database)) as connection:
                live_rows = connection.execute(
                    """
                    SELECT COUNT(*) FROM initialization_meta
                    WHERE key='concurrent_writer_sentinel'
                    """
                ).fetchone()[0]
            self.assertEqual(0, backup_rows)
            self.assertEqual(1, live_rows)

    def test_backup_connection_failure_releases_reserved_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "init.sqlite3"
            create_v1_fixture(database)
            backup = root / "backups" / "reserved-then-failed.bak"
            storage = InitStorage(database)

            with (
                mock.patch.object(
                    storage_module.sqlite3,
                    "connect",
                    side_effect=sqlite3.OperationalError(
                        "fixture connection failure"
                    ),
                ),
                self.assertRaises(sqlite3.OperationalError),
            ):
                storage._backup_database(backup)

            self.assertFalse(backup.exists())
            self.assertEqual(
                [],
                list(backup.parent.glob(f".{backup.name}.staging.*.sqlite3")),
            )

    def test_backup_publish_is_no_replace_under_destination_race(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "init.sqlite3"
            create_v1_fixture(database)
            backup = root / "backups" / "raced-backup.bak"
            attacker = root / "attacker.sqlite3"
            with closing(sqlite3.connect(attacker)) as connection:
                connection.execute(
                    "CREATE TABLE attacker_marker(value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO attacker_marker(value) VALUES('preserve-me')"
                )
                connection.commit()
            storage = InitStorage(database)
            real_link = storage_module.os.link
            attack_triggered = False

            def replace_before_publish(
                source_path: str | os.PathLike[str],
                destination_path: str | os.PathLike[str],
            ) -> None:
                nonlocal attack_triggered
                self.assertEqual(backup, Path(destination_path))
                attack_triggered = True
                attacker.replace(backup)
                real_link(source_path, destination_path)

            with (
                mock.patch.object(
                    storage_module.os,
                    "link",
                    side_effect=replace_before_publish,
                ),
                self.assertRaises(PlotInitError) as raised,
            ):
                storage._backup_database(backup)

            self.assertTrue(attack_triggered)
            self.assertEqual(
                "INIT_STORAGE_BACKUP_EXISTS",
                raised.exception.code,
            )
            self.assertTrue(backup.is_file())
            with closing(sqlite3.connect(backup)) as connection:
                self.assertEqual(
                    "preserve-me",
                    connection.execute(
                        "SELECT value FROM attacker_marker"
                    ).fetchone()[0],
                )
                self.assertIsNone(
                    connection.execute(
                        """
                        SELECT 1 FROM sqlite_master
                        WHERE type='table'
                          AND name='initialization_sessions'
                        """
                    ).fetchone()
                )
            self.assertEqual(
                [],
                list(backup.parent.glob(f".{backup.name}.staging.*.sqlite3")),
            )

    def test_backup_cancellation_closes_connections_and_releases_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "init.sqlite3"
            create_v1_fixture(database)
            backup = root / "backups" / "cancelled-backup.bak"
            storage = InitStorage(database)

            class InterruptingSource:
                def __init__(self) -> None:
                    self.closed = False

                def execute(self, _sql: str) -> None:
                    pass

                def backup(self, _target) -> None:
                    raise KeyboardInterrupt("fixture cancellation")

                def close(self) -> None:
                    self.closed = True

            class BackupTarget:
                def __init__(self) -> None:
                    self.closed = False

                def close(self) -> None:
                    self.closed = True

            source = InterruptingSource()
            target = BackupTarget()
            with (
                mock.patch.object(
                    storage_module.sqlite3,
                    "connect",
                    side_effect=(source, target),
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                storage._backup_database(backup)

            self.assertTrue(source.closed)
            self.assertTrue(target.closed)
            self.assertFalse(backup.exists())
            self.assertEqual(
                [],
                list(backup.parent.glob(f".{backup.name}.staging.*.sqlite3")),
            )

    def test_migration_rolls_back_if_published_backup_content_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "init.sqlite3"
            backup = root / "backups" / "changed-backup.sqlite3"
            attacker = root / "attacker.sqlite3"
            create_v1_fixture(database)
            with closing(sqlite3.connect(attacker)) as connection, connection:
                connection.execute(
                    "CREATE TABLE attacker_marker(value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO attacker_marker(value) VALUES('not-source')"
                )

            class ReplacingBackupStorage(InitStorage):
                def _migrate_payload_rows(
                    self,
                    connection: sqlite3.Connection,
                ) -> dict[str, int]:
                    backup.write_bytes(attacker.read_bytes())
                    return super()._migrate_payload_rows(connection)

            with self.assertRaises(PlotInitError) as raised:
                ReplacingBackupStorage(database).migrate_payload_storage(
                    dry_run=False,
                    backup_path=backup,
                )

            self.assertEqual(
                "INIT_STORAGE_BACKUP_CHANGED",
                raised.exception.code,
            )
            self.assertFalse(
                raised.exception.details["migration_committed"]
            )
            with closing(sqlite3.connect(database)) as connection:
                version = connection.execute(
                    """
                    SELECT value FROM initialization_meta
                    WHERE key='schema_version'
                    """
                ).fetchone()[0]
            self.assertEqual("1", version)
            self.assertFalse(backup.exists())
            self.assertEqual(
                [],
                list(
                    backup.parent.glob(
                        f".{backup.name}.staging.*.sqlite3"
                    )
                ),
            )

    @unittest.skipIf(
        os.name == "nt",
        "Windows prevents replacing an open retained backup",
    )
    def test_post_commit_backup_replacement_publishes_recovery_copy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "init.sqlite3"
            backup = root / "backups" / "post-commit-backup.sqlite3"
            attacker = root / "attacker.sqlite3"
            create_v1_fixture(database)
            with closing(sqlite3.connect(attacker)) as connection, connection:
                connection.execute(
                    "CREATE TABLE attacker_marker(value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO attacker_marker(value) VALUES('not-source')"
                )

            class ReplacingAfterCommitStorage(InitStorage):
                verify_calls = 0

                def _verify_backup_identity(self, held) -> None:
                    type(self).verify_calls += 1
                    if type(self).verify_calls == 3:
                        os.replace(attacker, held.path)
                    super()._verify_backup_identity(held)

            with self.assertRaises(PlotInitError) as raised:
                ReplacingAfterCommitStorage(
                    database
                ).migrate_payload_storage(
                    dry_run=False,
                    backup_path=backup,
                )

            self.assertEqual(
                "INIT_STORAGE_BACKUP_CHANGED",
                raised.exception.code,
            )
            self.assertTrue(
                raised.exception.details["migration_committed"]
            )
            self.assertEqual(
                "post_commit_identity_verify",
                raised.exception.details["failure_phase"],
            )
            invalid_public = Path(
                raised.exception.details["invalid_backup_path"]
            )
            recovery = Path(
                raised.exception.details["recovery_backup_path"]
            )
            self.assertEqual(backup, invalid_public)
            self.assertEqual(
                recovery,
                Path(raised.exception.details["backup_path"]),
            )
            self.assertNotEqual(invalid_public, recovery)
            self.assertTrue(recovery.is_file())

            with closing(sqlite3.connect(recovery)) as connection:
                backup_version = connection.execute(
                    """
                    SELECT value FROM initialization_meta
                    WHERE key='schema_version'
                    """
                ).fetchone()[0]
            with closing(sqlite3.connect(database)) as connection:
                live_version = connection.execute(
                    """
                    SELECT value FROM initialization_meta
                    WHERE key='schema_version'
                    """
                ).fetchone()[0]
            with closing(sqlite3.connect(invalid_public)) as connection:
                attacker_value = connection.execute(
                    "SELECT value FROM attacker_marker"
                ).fetchone()[0]

            self.assertEqual("1", backup_version)
            self.assertEqual("2", live_version)
            self.assertEqual("not-source", attacker_value)
            self.assertEqual(
                [],
                list(
                    backup.parent.glob(
                        f".{backup.name}.staging.*.sqlite3"
                    )
                ),
            )

    @unittest.skipIf(
        os.name == "nt",
        "POSIX in-place backup corruption regression",
    )
    def test_post_commit_backup_rewrite_preserves_private_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "init.sqlite3"
            backup = root / "backups" / "post-commit-rewrite.sqlite3"
            create_v1_fixture(database)

            class RewritingAfterCommitStorage(InitStorage):
                verify_calls = 0
                expected_backup_hash = ""

                def _verify_backup_identity(self, held) -> None:
                    type(self).verify_calls += 1
                    if type(self).verify_calls == 3:
                        type(self).expected_backup_hash = held.sha256
                        with closing(
                            sqlite3.connect(held.path)
                        ) as connection, connection:
                            connection.execute(
                                "CREATE TABLE attacker_marker("
                                "value TEXT NOT NULL)"
                            )
                            connection.execute(
                                "INSERT INTO attacker_marker(value) "
                                "VALUES('in-place-corruption')"
                            )
                    super()._verify_backup_identity(held)

            with self.assertRaises(PlotInitError) as raised:
                RewritingAfterCommitStorage(
                    database
                ).migrate_payload_storage(
                    dry_run=False,
                    backup_path=backup,
                )

            self.assertEqual(
                "INIT_STORAGE_BACKUP_CHANGED",
                raised.exception.code,
            )
            self.assertTrue(
                raised.exception.details["migration_committed"]
            )
            recovery = Path(
                raised.exception.details["recovery_backup_path"]
            )
            invalid_public = Path(
                raised.exception.details["invalid_backup_path"]
            )
            self.assertEqual(backup, invalid_public)
            self.assertNotEqual(invalid_public, recovery)
            self.assertEqual(
                recovery,
                Path(raised.exception.details["backup_path"]),
            )
            self.assertEqual(
                RewritingAfterCommitStorage.expected_backup_hash,
                hashlib.sha256(recovery.read_bytes()).hexdigest(),
            )

            with closing(sqlite3.connect(recovery)) as connection:
                backup_version = connection.execute(
                    """
                    SELECT value FROM initialization_meta
                    WHERE key='schema_version'
                    """
                ).fetchone()[0]
                recovery_marker = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='attacker_marker'
                    """
                ).fetchone()
            with closing(sqlite3.connect(database)) as connection:
                live_version = connection.execute(
                    """
                    SELECT value FROM initialization_meta
                    WHERE key='schema_version'
                    """
                ).fetchone()[0]
            with closing(sqlite3.connect(invalid_public)) as connection:
                attacker_value = connection.execute(
                    "SELECT value FROM attacker_marker"
                ).fetchone()[0]

            self.assertEqual("1", backup_version)
            self.assertIsNone(recovery_marker)
            self.assertEqual("2", live_version)
            self.assertEqual("in-place-corruption", attacker_value)

    def test_payload_limit_rolls_back_late_transaction_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "init.sqlite3"
            storage = InitStorage(database, max_payload_bytes=2048)
            state = state_payload()
            oversized_response = {"payload": "x" * 8192}

            with self.assertRaises(PlotInitError) as raised:
                storage.create_session(
                    state,
                    checkpoint_payloads(2),
                    scope="start",
                    idempotency_key="key-1",
                    request_hash="request-hash",
                    response=oversized_response,
                )
            self.assertEqual("INIT_PAYLOAD_TOO_LARGE", raised.exception.code)
            with closing(sqlite3.connect(database)) as connection:
                for table in (
                    "initialization_sessions",
                    "initialization_revisions",
                    "initialization_checkpoints",
                    "initialization_idempotency",
                    "initialization_payload_blobs",
                ):
                    self.assertEqual(
                        0,
                        connection.execute(
                            f'SELECT COUNT(*) FROM "{table}"'
                        ).fetchone()[0],
                    )

    def test_oversized_legacy_rows_block_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "init.sqlite3"
            create_v1_fixture(
                database,
                padding=8 * 1024,
                checkpoint_count=2,
            )
            storage = InitStorage(database, max_payload_bytes=2048)

            plan = storage.migration_plan()
            self.assertTrue(plan["blocked"])
            self.assertIn("oversized_payloads", plan["blocked_reasons"])
            self.assertEqual(4, plan["rows"]["oversized"])
            self.assertEqual(0, plan["rows"]["corrupt"])
            dry_run = storage.migrate_payload_storage(dry_run=True)
            self.assertTrue(dry_run["blocked"])
            self.assertEqual(
                plan["rows"]["projected_row_refs"],
                dry_run["row_refs"],
            )
            with self.assertRaises(PlotInitError) as raised:
                storage.migrate_payload_storage(dry_run=False)
            self.assertEqual(
                "INIT_STORAGE_MIGRATION_BLOCKED",
                raised.exception.code,
            )
            self.assertFalse((database.parent / "backups").exists())

    def test_missing_and_tampered_blobs_are_detected(self) -> None:
        for corruption in ("missing", "tampered"):
            with self.subTest(corruption=corruption):
                with tempfile.TemporaryDirectory() as temporary:
                    database = Path(temporary) / "init.sqlite3"
                    storage = InitStorage(database)
                    state = state_payload(padding=4096)
                    storage.create_session(
                        state,
                        checkpoint_payloads(1),
                        scope="start",
                        idempotency_key="key-1",
                        request_hash="request-hash",
                        response={"status": "ACTIVE"},
                    )
                    with closing(sqlite3.connect(database)) as connection:
                        blob_hash = connection.execute(
                            """
                            SELECT state_blob_hash
                            FROM initialization_sessions
                            """
                        ).fetchone()[0]
                        if corruption == "missing":
                            connection.execute(
                                """
                                DELETE FROM initialization_payload_blobs
                                WHERE blob_hash=?
                                """,
                                (blob_hash,),
                            )
                        else:
                            connection.execute(
                                """
                                UPDATE initialization_payload_blobs
                                SET payload=?
                                WHERE blob_hash=?
                                """,
                                (b"not-a-valid-blob", blob_hash),
                            )
                        connection.commit()
                    with self.assertRaises(PlotInitError) as raised:
                        storage.load_session(state["session_id"])
                    self.assertIn(
                        raised.exception.code,
                        {"INIT_BLOB_NOT_FOUND", "CORRUPT_INIT_BLOB"},
                    )
                    plan = storage.migration_plan()
                    self.assertTrue(plan["blocked"])
                    self.assertGreater(plan["rows"]["corrupt"], 0)
                    with self.assertRaises(PlotInitError) as migration_error:
                        storage.migrate_payload_storage(dry_run=False)
                    self.assertEqual(
                        "INIT_STORAGE_MIGRATION_BLOCKED",
                        migration_error.exception.code,
                    )

    def test_compact_reclaims_inline_payload_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "init.sqlite3"
            create_v1_fixture(
                database,
                padding=512 * 1024,
                checkpoint_count=6,
            )
            storage = InitStorage(database)
            before_bytes = database.stat().st_size

            result = storage.migrate_payload_storage(
                dry_run=False,
                compact=True,
            )

            self.assertTrue(result["compacted"])
            self.assertEqual("ok", result["quick_check"])
            self.assertEqual(before_bytes, result["before_bytes"])
            self.assertLess(result["after_bytes"], result["before_bytes"])
            self.assertGreater(
                result["compact_space"]["required_free_bytes"],
                0,
            )

    def test_orphan_cleanup_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "init.sqlite3"
            create_v1_fixture(database)
            storage = InitStorage(database)
            storage.migrate_payload_storage(dry_run=False)
            orphan_text = canonical_json({"orphan": True})
            orphan_hash = sha256_text(orphan_text)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """
                    INSERT INTO initialization_payload_blobs(
                        blob_hash, media_type, codec, payload,
                        uncompressed_bytes, created_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        orphan_hash,
                        "application/json",
                        "utf8",
                        orphan_text.encode("utf-8"),
                        len(orphan_text.encode("utf-8")),
                        "2026-07-16T00:00:00+00:00",
                    ),
                )
                connection.commit()

            inspected = storage.migration_plan()
            self.assertEqual(1, inspected["blobs"]["existing_orphan"])
            dry_run = storage.migrate_payload_storage(
                dry_run=True,
                cleanup_orphans=True,
            )
            self.assertEqual(
                inspected["blobs"]["projected_referenced_unique"],
                dry_run["unique_blobs"],
            )
            result = storage.migrate_payload_storage(
                dry_run=False,
                cleanup_orphans=True,
            )
            self.assertEqual(1, result["orphan_blobs_removed"])
            self.assertEqual(0, result["after"]["blobs"]["existing_orphan"])
            self.assertEqual(dry_run["unique_blobs"], result["unique_blobs"])

    def test_standalone_cli_supports_inspect_and_backup_compact_migrate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "init.sqlite3"
            create_v1_fixture(database, padding=256 * 1024)
            cli = SCRIPTS / "plot_init_storage.py"

            inspect_process = subprocess.run(
                [
                    sys.executable,
                    str(cli),
                    "inspect",
                    "--database",
                    str(database),
                ],
                cwd=PLUGIN_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, inspect_process.returncode, inspect_process.stderr)
            inspected = json.loads(inspect_process.stdout)
            self.assertEqual("inspected", inspected["status"])
            self.assertEqual("ok", inspected["quick_check"])

            migrate_process = subprocess.run(
                [
                    sys.executable,
                    str(cli),
                    "migrate",
                    "--database",
                    str(database),
                    "--backup",
                    "--compact",
                ],
                cwd=PLUGIN_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, migrate_process.returncode, migrate_process.stderr)
            migrated = json.loads(migrate_process.stdout)
            self.assertEqual("migrated", migrated["status"])
            for field in (
                "before_bytes",
                "after_bytes",
                "row_refs",
                "unique_blobs",
                "dedup_bytes",
                "backup_path",
                "quick_check",
            ):
                self.assertIn(field, migrated)
            self.assertTrue(Path(migrated["backup_path"]).is_file())
            self.assertEqual("ok", migrated["quick_check"])

    def test_standalone_cli_prefers_canonical_database_and_falls_back_to_legacy(
        self,
    ) -> None:
        cli = SCRIPTS / "plot_init_storage.py"
        for layout in ("canonical", "legacy", "missing"):
            with self.subTest(layout=layout):
                with tempfile.TemporaryDirectory() as temporary:
                    workspace = Path(temporary)
                    canonical = workspace / ".plot-rag" / "init.sqlite3"
                    legacy = workspace / ".plot-rag-init" / "init.sqlite3"
                    selected = (
                        legacy
                        if layout == "legacy"
                        else canonical
                    )
                    if layout != "missing":
                        selected.parent.mkdir(parents=True)
                        create_v1_fixture(selected)
                    if layout == "canonical":
                        legacy.parent.mkdir(parents=True)
                        create_v1_fixture(legacy)

                    process = subprocess.run(
                        [
                            sys.executable,
                            str(cli),
                            "inspect",
                            "--workspace-root",
                            str(workspace),
                        ],
                        cwd=PLUGIN_ROOT,
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    self.assertEqual(0, process.returncode, process.stderr)
                    inspected = json.loads(process.stdout)
                    self.assertEqual(
                        str(selected.resolve()),
                        inspected["database_path"],
                    )
                    self.assertEqual(
                        layout != "missing",
                        inspected["exists"],
                    )
                    if layout == "missing":
                        self.assertEqual([], list(workspace.iterdir()))


if __name__ == "__main__":
    unittest.main()
