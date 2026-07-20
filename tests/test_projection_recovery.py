from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import longform.projections as projections  # noqa: E402
from longform.projections import (  # noqa: E402
    ProjectionJournal,
    ProjectionRunError,
)


class ProjectionRecoveryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "projection.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _insert_running(
        self,
        run_id: str,
        *,
        owner_host: str | None,
        owner_pid: int | None,
        owner_token: str | None = None,
    ) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                INSERT INTO projection_runs(
                    run_id, projection_name, commit_id, input_sha256,
                    input_json, status, attempt, retry_of, started_at,
                    owner_host, owner_pid, owner_token
                ) VALUES (?, 'vector', 'commit-1', 'input-hash', ?,
                          'running', 1, NULL, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    '{"canon_status":"accepted","commit_id":"commit-1"}',
                    "2026-07-16T00:00:00+00:00",
                    owner_host,
                    owner_pid,
                    owner_token,
                ),
            )
            connection.commit()

    def _insert_owned_running(
        self,
        journal: ProjectionJournal,
        commit: dict[str, object],
        *,
        attempt: int,
        retry_of: str | None,
        owner_pid: int,
        owner_token: str,
    ) -> str:
        with journal._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run_id = journal._insert_run(
                connection,
                projection_name="vector",
                commit_id=str(commit["commit_id"]),
                input_payload=commit,
                attempt=attempt,
                retry_of=retry_of,
                owner=(
                    socket.gethostname().strip().casefold(),
                    owner_pid,
                    owner_token,
                ),
            )
            connection.commit()
        return run_id

    def _start_live_owner(self) -> tuple[subprocess.Popen[bytes], str]:
        owner = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
            ]
        )
        owner_state, owner_token = projections._process_probe(owner.pid)
        if owner_state != "alive" or not owner_token:
            owner.kill()
            owner.wait(timeout=5)
            self.skipTest(
                "this platform does not expose subprocess birth tokens"
            )
        return owner, owner_token

    @staticmethod
    def _stop_owner(owner: subprocess.Popen[bytes]) -> None:
        if owner.poll() is None:
            owner.terminate()
        try:
            owner.wait(timeout=5)
        except subprocess.TimeoutExpired:
            owner.kill()
            owner.wait(timeout=5)

    def _create_legacy_schema(self, *, schema_version: int = 1) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                f"""
                CREATE TABLE longform_projection_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO longform_projection_meta(key, value)
                VALUES ('schema_version', '{schema_version}');
                CREATE TABLE projection_runs (
                    run_id TEXT PRIMARY KEY,
                    projection_name TEXT NOT NULL,
                    commit_id TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    retry_of TEXT,
                    output_sha256 TEXT,
                    error_text TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE projection_outputs (
                    projection_name TEXT NOT NULL,
                    commit_id TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    output_sha256 TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    PRIMARY KEY(projection_name, commit_id, input_sha256)
                );
                """
            )

    def _projection_columns(self) -> list[str]:
        with closing(sqlite3.connect(self.database)) as connection:
            return [
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(projection_runs)"
                )
            ]

    def test_reopen_recovers_run_owned_by_dead_local_process(self) -> None:
        ProjectionJournal(self.database)
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        dead_pid = finished.pid
        self.assertEqual(0, finished.wait())
        self._insert_running(
            "dead-owner",
            owner_host=socket.gethostname().casefold(),
            owner_pid=dead_pid,
        )

        reopened = ProjectionJournal(self.database)
        row = next(
            item for item in reopened.runs() if item["run_id"] == "dead-owner"
        )
        self.assertEqual("failed", row["status"])
        self.assertIn("owner process", row["error_text"])
        retried = reopened.retry(
            "dead-owner",
            lambda _payload: {"status": "success", "projected": True},
        )
        self.assertEqual("succeeded", retried["status"])

    @unittest.skipUnless(
        os.name != "nt" and Path("/proc/self/stat").is_file(),
        "Linux procfs is required for zombie-owner recovery",
    )
    def test_linux_zombie_owner_is_recovered_as_dead(self) -> None:
        journal = ProjectionJournal(self.database)
        owner = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
            ]
        )
        try:
            owner_state, owner_token = projections._linux_process_probe(
                owner.pid
            )
            self.assertEqual("alive", owner_state)
            self.assertTrue(owner_token)
            owner.terminate()

            deadline = time.monotonic() + 5
            process_state = ""
            while time.monotonic() < deadline:
                try:
                    stat_payload = (
                        Path("/proc")
                        .joinpath(str(owner.pid), "stat")
                        .read_text(encoding="utf-8")
                    )
                except FileNotFoundError:
                    break
                closing_parenthesis = stat_payload.rfind(")")
                if closing_parenthesis >= 0:
                    fields = stat_payload[closing_parenthesis + 2 :].split()
                    process_state = fields[0] if fields else ""
                    if process_state in {"Z", "X", "x"}:
                        break
                time.sleep(0.01)
            self.assertIn(process_state, {"Z", "X", "x"})

            self._insert_running(
                "zombie-owner",
                owner_host=socket.gethostname().casefold(),
                owner_pid=owner.pid,
                owner_token=owner_token,
            )
            recovered = journal.recover_interrupted_runs(
                run_ids=("zombie-owner",)
            )
            self.assertEqual(
                ["zombie-owner"],
                [item["run_id"] for item in recovered],
            )
            row = journal.inspect_run("zombie-owner")
            self.assertEqual("failed", row["status"])
            self.assertIn("no longer running", row["error_text"])
        finally:
            try:
                owner.kill()
            except ProcessLookupError:
                pass
            owner.wait(timeout=5)

    def test_reopen_preserves_run_owned_by_live_local_process(self) -> None:
        journal = ProjectionJournal(self.database)
        owner_token = projections._current_process_token()
        self._insert_running(
            "live-owner",
            owner_host=socket.gethostname().casefold(),
            owner_pid=os.getpid(),
            owner_token=owner_token,
        )

        reopened = ProjectionJournal(self.database)
        row = next(
            item for item in reopened.runs() if item["run_id"] == "live-owner"
        )
        self.assertEqual("running", row["status"])
        recovered = journal.recover_interrupted_runs(run_ids=("live-owner",))
        self.assertEqual([], recovered)
        final = next(
            item for item in journal.runs() if item["run_id"] == "live-owner"
        )
        self.assertEqual("running", final["status"])

    def test_reopen_recovers_reused_pid_with_different_birth_token(self) -> None:
        ProjectionJournal(self.database)
        current_token = projections._current_process_token()
        if current_token.startswith("session:"):
            self.skipTest("this platform does not expose process birth tokens")
        self._insert_running(
            "reused-pid",
            owner_host=socket.gethostname().casefold(),
            owner_pid=os.getpid(),
            owner_token=current_token + "-stale",
        )

        reopened = ProjectionJournal(self.database)
        row = next(
            item for item in reopened.runs() if item["run_id"] == "reused-pid"
        )
        self.assertEqual("failed", row["status"])
        self.assertIn("PID", row["error_text"])
        self.assertIn("reused", row["error_text"])

    def test_legacy_running_row_requires_exact_explicit_recovery(self) -> None:
        journal = ProjectionJournal(self.database)
        self._insert_running(
            "legacy-ownerless",
            owner_host=None,
            owner_pid=None,
        )

        reopened = ProjectionJournal(self.database)
        row = next(
            item
            for item in reopened.runs()
            if item["run_id"] == "legacy-ownerless"
        )
        self.assertEqual("running", row["status"])
        recovered = reopened.recover_interrupted_runs(
            run_ids=("legacy-ownerless",)
        )
        self.assertEqual(
            ["legacy-ownerless"],
            [item["run_id"] for item in recovered],
        )
        final = next(
            item
            for item in reopened.runs()
            if item["run_id"] == "legacy-ownerless"
        )
        self.assertEqual("failed", final["status"])
        self.assertIn("explicitly recovered", final["error_text"])

    def test_live_legacy_pid_without_birth_token_is_fail_closed(self) -> None:
        journal = ProjectionJournal(self.database)
        self._insert_running(
            "legacy-live-pid",
            owner_host=socket.gethostname().casefold(),
            owner_pid=os.getpid(),
            owner_token=None,
        )

        recovered = journal.recover_interrupted_runs(
            run_ids=("legacy-live-pid",)
        )

        self.assertEqual([], recovered)
        final = next(
            item
            for item in journal.runs()
            if item["run_id"] == "legacy-live-pid"
        )
        self.assertEqual("running", final["status"])

    def test_future_schema_mismatch_does_not_apply_additive_migration(
        self,
    ) -> None:
        self._create_legacy_schema(schema_version=2)
        before = self._projection_columns()
        before_bytes = self.database.read_bytes()

        with self.assertRaisesRegex(RuntimeError, "schema version mismatch"):
            ProjectionJournal(self.database)

        self.assertEqual(before, self._projection_columns())
        self.assertEqual(before_bytes, self.database.read_bytes())
        self.assertNotIn("owner_host", self._projection_columns())
        with closing(sqlite3.connect(self.database)) as connection:
            version = connection.execute(
                """
                SELECT value FROM longform_projection_meta
                WHERE key = 'schema_version'
                """
            ).fetchone()[0]
        self.assertEqual("2", version)

    def test_foreign_sqlite_database_is_rejected_without_injection(
        self,
    ) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                """
                CREATE TABLE user_finance (
                    account_id TEXT PRIMARY KEY,
                    balance_cents INTEGER NOT NULL
                );
                INSERT INTO user_finance(account_id, balance_cents)
                VALUES ('fixture-account', 12345);
                """
            )
        before_bytes = self.database.read_bytes()

        with self.assertRaisesRegex(
            RuntimeError,
            "foreign tables",
        ):
            ProjectionJournal(self.database)

        self.assertEqual(before_bytes, self.database.read_bytes())
        with closing(sqlite3.connect(self.database)) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table'
                    """
                )
            }
            finance_row = connection.execute(
                """
                SELECT account_id, balance_cents
                FROM user_finance
                """
            ).fetchone()
        self.assertEqual({"user_finance"}, tables)
        self.assertEqual(("fixture-account", 12345), finance_row)
        self.assertFalse(
            Path(str(self.database) + "-journal").exists()
        )

    def test_owned_projection_metadata_with_foreign_table_is_rejected(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                """
                CREATE TABLE longform_projection_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO longform_projection_meta(key, value)
                VALUES ('schema_version', '1');
                CREATE TABLE user_finance (
                    account_id TEXT PRIMARY KEY,
                    balance_cents INTEGER NOT NULL
                );
                INSERT INTO user_finance(account_id, balance_cents)
                VALUES ('fixture-account', 12345);
                """
            )
        before_bytes = self.database.read_bytes()

        with self.assertRaisesRegex(RuntimeError, "foreign tables"):
            ProjectionJournal(self.database, auto_recover=False)

        self.assertEqual(before_bytes, self.database.read_bytes())
        with closing(sqlite3.connect(self.database)) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table'
                    """
                )
            }
            finance_row = connection.execute(
                """
                SELECT account_id, balance_cents
                FROM user_finance
                """
            ).fetchone()
        self.assertEqual(
            {"longform_projection_meta", "user_finance"},
            tables,
        )
        self.assertEqual(("fixture-account", 12345), finance_row)
        self.assertFalse(Path(str(self.database) + "-journal").exists())

    def test_versionless_projection_metadata_is_rejected_without_writes(
        self,
    ) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                """
                CREATE TABLE longform_projection_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO longform_projection_meta(key, value)
                VALUES ('fixture_marker', 'preserve-me');
                """
            )
        before_bytes = self.database.read_bytes()

        with self.assertRaisesRegex(
            RuntimeError,
            "schema version is missing",
        ):
            ProjectionJournal(self.database)

        self.assertEqual(before_bytes, self.database.read_bytes())
        with closing(sqlite3.connect(self.database)) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table'
                    """
                )
            }
            metadata = connection.execute(
                """
                SELECT key, value FROM longform_projection_meta
                """
            ).fetchall()
        self.assertEqual({"longform_projection_meta"}, tables)
        self.assertEqual(
            [("fixture_marker", "preserve-me")],
            metadata,
        )
        self.assertFalse(
            Path(str(self.database) + "-journal").exists()
        )

    def test_concurrent_legacy_schema_migration_is_serialized(self) -> None:
        self._create_legacy_schema()
        start = threading.Barrier(3)
        journals: list[ProjectionJournal] = []
        errors: list[BaseException] = []
        original_connection = projections._ClosingConnection

        class SlowMigrationConnection(original_connection):
            def execute(
                self,
                sql: str,
                parameters: object = (),
            ) -> sqlite3.Cursor:
                cursor = super().execute(sql, parameters)
                if "PRAGMA table_info(projection_runs)" in sql:
                    time.sleep(0.15)
                return cursor

        def open_journal() -> None:
            try:
                start.wait(timeout=5)
                journals.append(ProjectionJournal(self.database))
            except BaseException as error:
                errors.append(error)

        with mock.patch.object(
            projections,
            "_ClosingConnection",
            SlowMigrationConnection,
        ):
            threads = [
                threading.Thread(target=open_journal),
                threading.Thread(target=open_journal),
            ]
            for thread in threads:
                thread.start()
            start.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())

        self.assertEqual([], errors)
        self.assertEqual(2, len(journals))
        self.assertTrue(
            {"owner_host", "owner_pid", "owner_token"}.issubset(
                self._projection_columns()
            )
        )

    def test_recovered_run_cannot_overwrite_retry_output(self) -> None:
        journal = ProjectionJournal(self.database)
        commit = {
            "commit_id": "commit-cas",
            "canon_status": "accepted",
        }
        original_run_id = journal._new_run(
            projection_name="vector",
            commit_id="commit-cas",
            input_payload=commit,
            attempt=1,
            retry_of=None,
        )
        entered = threading.Event()
        release = threading.Event()
        original_errors: list[BaseException] = []

        def delayed_projector(_payload: object) -> dict[str, str]:
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return {"projected": "original"}

        def finish_original() -> None:
            try:
                journal._execute(
                    run_id=original_run_id,
                    projection_name="vector",
                    commit_id="commit-cas",
                    input_payload=commit,
                    projector=delayed_projector,
                )
            except BaseException as error:
                original_errors.append(error)

        original = threading.Thread(target=finish_original)
        original.start()
        self.assertTrue(entered.wait(timeout=5))
        with mock.patch.object(
            projections,
            "_process_probe",
            return_value=("dead", None),
        ):
            recovered = journal.recover_interrupted_runs(
                run_ids=(original_run_id,)
            )
        self.assertEqual(
            [original_run_id],
            [item["run_id"] for item in recovered],
        )
        retry = journal.retry(
            original_run_id,
            lambda _payload: {"projected": "retry"},
        )
        self.assertEqual("succeeded", retry["status"])

        release.set()
        original.join(timeout=10)
        self.assertFalse(original.is_alive())
        self.assertEqual(1, len(original_errors))
        self.assertIsInstance(original_errors[0], ProjectionRunError)
        original_row = next(
            item
            for item in journal.runs()
            if item["run_id"] == original_run_id
        )
        self.assertEqual("failed", original_row["status"])
        with closing(sqlite3.connect(self.database)) as connection:
            output_json = connection.execute(
                """
                SELECT output_json FROM projection_outputs
                WHERE projection_name = 'vector'
                  AND commit_id = 'commit-cas'
                """
            ).fetchone()[0]
        self.assertEqual({"projected": "retry"}, json.loads(output_json))

    def test_retry_is_single_owner_and_attempts_are_monotonic(self) -> None:
        journal = ProjectionJournal(self.database)
        commit = {
            "commit_id": "commit-retry",
            "canon_status": "accepted",
        }

        def fail_projection(_payload: object) -> object:
            raise RuntimeError("first attempt failed")

        with self.assertRaises(ProjectionRunError) as raised:
            journal.run("vector", commit, fail_projection)
        failed_run_id = raised.exception.run_id
        entered = threading.Event()
        release = threading.Event()
        first_results: list[dict[str, object]] = []
        first_errors: list[BaseException] = []

        def waiting_projector(_payload: object) -> dict[str, str]:
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return {"status": "degraded", "projected": "retry-2"}

        def first_retry() -> None:
            try:
                first_results.append(
                    journal.retry(failed_run_id, waiting_projector)
                )
            except BaseException as error:
                first_errors.append(error)

        retry_thread = threading.Thread(target=first_retry)
        retry_thread.start()
        self.assertTrue(entered.wait(timeout=5))
        with self.assertRaisesRegex(ValueError, "running retry"):
            journal.retry(
                failed_run_id,
                lambda _payload: {"projected": "duplicate"},
            )
        release.set()
        retry_thread.join(timeout=10)
        self.assertFalse(retry_thread.is_alive())
        self.assertEqual([], first_errors)
        self.assertEqual("degraded", first_results[0]["status"])

        third_attempt = journal.retry(
            failed_run_id,
            lambda _payload: {"projected": "retry-3"},
        )
        self.assertEqual("succeeded", third_attempt["status"])
        cached = journal.retry(
            failed_run_id,
            lambda _payload: self.fail("successful output must be cached"),
        )
        self.assertEqual("cached", cached["status"])
        retries = [
            item
            for item in journal.runs()
            if item["retry_of"] == failed_run_id
        ]
        self.assertEqual([2, 3], sorted(int(item["attempt"]) for item in retries))

    def test_run_claim_is_single_owner_for_concurrent_same_input(self) -> None:
        first_journal = ProjectionJournal(self.database)
        second_journal = ProjectionJournal(
            self.database,
            auto_recover=False,
        )
        commit = {
            "commit_id": "commit-concurrent-run",
            "canon_status": "accepted",
            "operation": "accept",
        }
        start = threading.Barrier(3)
        projector_entered = threading.Event()
        release_projector = threading.Event()
        call_lock = threading.Lock()
        projector_calls = 0
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def projector(_payload: object) -> dict[str, object]:
            nonlocal projector_calls
            with call_lock:
                projector_calls += 1
            projector_entered.set()
            if not release_projector.wait(timeout=5):
                raise TimeoutError("fixture projector was not released")
            return {"status": "success", "projected": True}

        def run_projection(journal: ProjectionJournal) -> None:
            try:
                start.wait(timeout=5)
                results.append(
                    journal.run(
                        "vector",
                        commit,
                        projector,
                        wait_timeout_seconds=5,
                    )
                )
            except BaseException as error:
                errors.append(error)

        threads = [
            threading.Thread(
                target=run_projection,
                args=(journal,),
            )
            for journal in (first_journal, second_journal)
        ]
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        self.assertTrue(projector_entered.wait(timeout=5))
        time.sleep(0.10)
        with call_lock:
            self.assertEqual(1, projector_calls)
        release_projector.set()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

        self.assertEqual([], errors)
        self.assertEqual(
            ["cached", "succeeded"],
            sorted(str(item["status"]) for item in results),
        )
        self.assertEqual(
            [
                {"status": "success", "projected": True},
                {"status": "success", "projected": True},
            ],
            [item["output"] for item in results],
        )
        rows = first_journal.runs("vector")
        self.assertEqual(1, len(rows))
        self.assertEqual(1, int(rows[0]["attempt"]))
        self.assertEqual(
            projections.stable_normalized_hash(commit),
            rows[0]["input_sha256"],
        )

    def test_run_followers_take_over_when_observed_owner_dies(self) -> None:
        first_journal = ProjectionJournal(self.database)
        second_journal = ProjectionJournal(
            self.database,
            auto_recover=False,
        )
        commit = {
            "commit_id": "commit-follower-owner-death",
            "canon_status": "accepted",
            "operation": "accept",
        }
        owner, owner_token = self._start_live_owner()
        release_projector = threading.Event()
        projector_entered = threading.Event()
        call_lock = threading.Lock()
        projector_calls = 0
        start = threading.Barrier(3)
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []
        try:
            abandoned_run_id = self._insert_owned_running(
                first_journal,
                commit,
                attempt=1,
                retry_of=None,
                owner_pid=owner.pid,
                owner_token=owner_token,
            )

            def projector(_payload: object) -> dict[str, object]:
                nonlocal projector_calls
                with call_lock:
                    projector_calls += 1
                projector_entered.set()
                if not release_projector.wait(timeout=5):
                    raise TimeoutError("fixture projector was not released")
                return {"projected": True}

            def follow(journal: ProjectionJournal) -> None:
                try:
                    start.wait(timeout=5)
                    results.append(
                        journal.run(
                            "vector",
                            commit,
                            projector,
                            wait_timeout_seconds=3,
                        )
                    )
                except BaseException as error:
                    errors.append(error)

            followers = [
                threading.Thread(target=follow, args=(journal,))
                for journal in (first_journal, second_journal)
            ]
            for follower in followers:
                follower.start()
            start.wait(timeout=5)
            time.sleep(0.15)
            self.assertTrue(all(follower.is_alive() for follower in followers))
            with call_lock:
                self.assertEqual(0, projector_calls)
            self.assertEqual(
                "running",
                first_journal.inspect_run(abandoned_run_id)["status"],
            )

            self._stop_owner(owner)
            self.assertTrue(projector_entered.wait(timeout=3))
            time.sleep(0.10)
            with call_lock:
                self.assertEqual(1, projector_calls)
            release_projector.set()
            for follower in followers:
                follower.join(timeout=10)
                self.assertFalse(follower.is_alive())

            self.assertEqual([], errors)
            self.assertEqual(
                ["cached", "succeeded"],
                sorted(str(item["status"]) for item in results),
            )
            rows = first_journal.runs("vector")
            self.assertEqual(2, len(rows))
            abandoned = first_journal.inspect_run(abandoned_run_id)
            self.assertEqual("failed", abandoned["status"])
            self.assertIn("interrupted:", abandoned["error_text"])
            successor = next(
                row for row in rows if row["run_id"] != abandoned_run_id
            )
            self.assertEqual(2, int(successor["attempt"]))
            self.assertEqual(abandoned_run_id, successor["retry_of"])
        finally:
            release_projector.set()
            self._stop_owner(owner)

    def test_retry_followers_take_over_when_observed_owner_dies(self) -> None:
        first_journal = ProjectionJournal(self.database)
        second_journal = ProjectionJournal(
            self.database,
            auto_recover=False,
        )
        commit = {
            "commit_id": "commit-retry-follower-owner-death",
            "canon_status": "accepted",
            "operation": "accept",
        }
        with self.assertRaises(ProjectionRunError) as failed:
            first_journal.run(
                "vector",
                commit,
                lambda _payload: (_ for _ in ()).throw(
                    RuntimeError("fixture initial failure")
                ),
            )
        failed_run_id = failed.exception.run_id
        owner, owner_token = self._start_live_owner()
        release_projector = threading.Event()
        projector_entered = threading.Event()
        call_lock = threading.Lock()
        projector_calls = 0
        start = threading.Barrier(3)
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []
        try:
            abandoned_retry_id = self._insert_owned_running(
                first_journal,
                commit,
                attempt=2,
                retry_of=failed_run_id,
                owner_pid=owner.pid,
                owner_token=owner_token,
            )

            def projector(_payload: object) -> dict[str, object]:
                nonlocal projector_calls
                with call_lock:
                    projector_calls += 1
                projector_entered.set()
                if not release_projector.wait(timeout=5):
                    raise TimeoutError("fixture projector was not released")
                return {"projected": "retry-takeover"}

            def follow(journal: ProjectionJournal) -> None:
                try:
                    start.wait(timeout=5)
                    results.append(
                        journal.retry(
                            failed_run_id,
                            projector,
                            wait_for_running=True,
                            wait_timeout_seconds=3,
                        )
                    )
                except BaseException as error:
                    errors.append(error)

            followers = [
                threading.Thread(target=follow, args=(journal,))
                for journal in (first_journal, second_journal)
            ]
            for follower in followers:
                follower.start()
            start.wait(timeout=5)
            time.sleep(0.15)
            self.assertTrue(all(follower.is_alive() for follower in followers))
            with call_lock:
                self.assertEqual(0, projector_calls)
            self.assertEqual(
                "running",
                first_journal.inspect_run(abandoned_retry_id)["status"],
            )

            self._stop_owner(owner)
            self.assertTrue(projector_entered.wait(timeout=3))
            time.sleep(0.10)
            with call_lock:
                self.assertEqual(1, projector_calls)
            release_projector.set()
            for follower in followers:
                follower.join(timeout=10)
                self.assertFalse(follower.is_alive())

            self.assertEqual([], errors)
            self.assertEqual(
                ["cached", "succeeded"],
                sorted(str(item["status"]) for item in results),
            )
            abandoned = first_journal.inspect_run(abandoned_retry_id)
            self.assertEqual("failed", abandoned["status"])
            self.assertIn("interrupted:", abandoned["error_text"])
            rows = first_journal.runs("vector")
            successor = next(
                row
                for row in rows
                if int(row["attempt"]) == 3
            )
            self.assertEqual(failed_run_id, successor["retry_of"])
            self.assertEqual(
                [1, 2, 3],
                sorted(int(row["attempt"]) for row in rows),
            )
        finally:
            release_projector.set()
            self._stop_owner(owner)

    def test_follower_keeps_live_and_remote_owners_fail_closed(self) -> None:
        current_token = projections._current_process_token()
        fixtures = (
            (
                "live-local",
                socket.gethostname().strip().casefold(),
                os.getpid(),
                current_token,
            ),
            (
                "remote",
                "remote-owner.invalid",
                os.getpid(),
                current_token,
            ),
        )
        for label, owner_host, owner_pid, owner_token in fixtures:
            with self.subTest(owner=label):
                database = Path(self.temporary.name) / f"{label}.sqlite3"
                journal = ProjectionJournal(database)
                commit = {
                    "commit_id": f"commit-{label}-owner",
                    "canon_status": "accepted",
                    "operation": "accept",
                }
                with journal._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    running_id = journal._insert_run(
                        connection,
                        projection_name="vector",
                        commit_id=str(commit["commit_id"]),
                        input_payload=commit,
                        attempt=1,
                        retry_of=None,
                        owner=(
                            owner_host,
                            owner_pid,
                            owner_token,
                        ),
                    )
                    connection.commit()
                projector_calls = 0

                def projector(_payload: object) -> object:
                    nonlocal projector_calls
                    projector_calls += 1
                    return {"projected": True}

                started_at = time.monotonic()
                with self.assertRaises(TimeoutError):
                    journal.run(
                        "vector",
                        commit,
                        projector,
                        wait_timeout_seconds=0.12,
                    )
                self.assertLess(time.monotonic() - started_at, 1.0)
                self.assertEqual(0, projector_calls)
                self.assertEqual(
                    "running",
                    journal.inspect_run(running_id)["status"],
                )

    def test_follower_recovers_reused_pid_and_takes_over(self) -> None:
        journal = ProjectionJournal(self.database)
        current_token = projections._current_process_token()
        if current_token.startswith("session:"):
            self.skipTest("this platform does not expose process birth tokens")
        commit = {
            "commit_id": "commit-follower-pid-reuse",
            "canon_status": "accepted",
            "operation": "accept",
        }
        with journal._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            abandoned_run_id = journal._insert_run(
                connection,
                projection_name="vector",
                commit_id=str(commit["commit_id"]),
                input_payload=commit,
                attempt=1,
                retry_of=None,
                owner=(
                    socket.gethostname().strip().casefold(),
                    os.getpid(),
                    current_token + "-stale",
                ),
            )
            connection.commit()
        projector_calls = 0

        def projector(_payload: object) -> object:
            nonlocal projector_calls
            projector_calls += 1
            return {"projected": "after-pid-reuse"}

        result = journal.run(
            "vector",
            commit,
            projector,
            wait_timeout_seconds=1,
        )

        self.assertEqual("succeeded", result["status"])
        self.assertEqual(1, projector_calls)
        abandoned = journal.inspect_run(abandoned_run_id)
        self.assertEqual("failed", abandoned["status"])
        self.assertIn("PID", abandoned["error_text"])
        self.assertIn("reused", abandoned["error_text"])
        successor = journal.inspect_run(str(result["run_id"]))
        self.assertEqual(2, int(successor["attempt"]))
        self.assertEqual(abandoned_run_id, successor["retry_of"])

    def test_forced_follower_waits_for_current_run_not_stale_output(self) -> None:
        first_journal = ProjectionJournal(self.database)
        second_journal = ProjectionJournal(
            self.database,
            auto_recover=False,
        )
        commit = {
            "commit_id": "commit-force-refresh",
            "canon_status": "accepted",
            "operation": "accept",
        }
        seeded = first_journal.run(
            "snapshot",
            commit,
            lambda _payload: {"generation": 1},
        )
        self.assertEqual({"generation": 1}, seeded["output"])

        owner_entered = threading.Event()
        release_owner = threading.Event()
        follower_finished = threading.Event()
        results: list[tuple[str, dict[str, object]]] = []
        errors: list[BaseException] = []

        def refreshed_projector(_payload: object) -> dict[str, int]:
            owner_entered.set()
            if not release_owner.wait(timeout=5):
                raise TimeoutError("fixture forced projector was not released")
            return {"generation": 2}

        def run_owner() -> None:
            try:
                results.append(
                    (
                        "owner",
                        first_journal.run(
                            "snapshot",
                            commit,
                            refreshed_projector,
                            force=True,
                            wait_timeout_seconds=5,
                        ),
                    )
                )
            except BaseException as error:
                errors.append(error)

        def run_follower() -> None:
            try:
                results.append(
                    (
                        "follower",
                        second_journal.run(
                            "snapshot",
                            commit,
                            lambda _payload: {"generation": 999},
                            force=True,
                            wait_timeout_seconds=5,
                        ),
                    )
                )
            except BaseException as error:
                errors.append(error)
            finally:
                follower_finished.set()

        owner = threading.Thread(target=run_owner)
        owner.start()
        self.assertTrue(owner_entered.wait(timeout=5))
        follower = threading.Thread(target=run_follower)
        follower.start()
        self.assertFalse(follower_finished.wait(timeout=0.2))

        release_owner.set()
        owner.join(timeout=10)
        follower.join(timeout=10)
        self.assertFalse(owner.is_alive())
        self.assertFalse(follower.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(
            ["cached", "succeeded"],
            sorted(str(item["status"]) for _label, item in results),
        )
        self.assertEqual(
            [{"generation": 2}, {"generation": 2}],
            [item["output"] for _label, item in results],
        )

    def test_run_cleans_claim_when_commit_succeeds_then_is_cancelled(
        self,
    ) -> None:
        class InterruptAfterCommitConnection(projections._ClosingConnection):
            armed = False

            def commit(self) -> None:
                super().commit()
                if type(self).armed:
                    type(self).armed = False
                    raise KeyboardInterrupt(
                        "fixture cancellation after durable claim commit"
                    )

        class InterruptJournal(ProjectionJournal):
            def _connect(self) -> sqlite3.Connection:
                connection = sqlite3.connect(
                    self.database_path,
                    factory=InterruptAfterCommitConnection,
                )
                connection.row_factory = sqlite3.Row
                return connection

        journal = InterruptJournal(self.database, auto_recover=False)
        commit = {
            "commit_id": "commit-cancelled-claim",
            "canon_status": "accepted",
            "operation": "accept",
        }
        InterruptAfterCommitConnection.armed = True

        with self.assertRaises(KeyboardInterrupt):
            journal.run(
                "vector",
                commit,
                lambda _payload: self.fail("projector must not run"),
            )

        rows = journal.runs("vector")
        self.assertEqual(1, len(rows))
        self.assertEqual("failed", rows[0]["status"])
        self.assertIn("KeyboardInterrupt", rows[0]["error_text"])
        retried = journal.run(
            "vector",
            commit,
            lambda _payload: {"projected": True},
            wait_timeout_seconds=0.1,
        )
        self.assertEqual("succeeded", retried["status"])

    def test_retry_cleans_claim_when_handoff_is_cancelled(self) -> None:
        journal = ProjectionJournal(self.database)
        commit = {
            "commit_id": "commit-cancelled-retry-handoff",
            "canon_status": "accepted",
            "operation": "accept",
        }

        with self.assertRaises(ProjectionRunError) as failed:
            journal.run(
                "vector",
                commit,
                lambda _payload: (_ for _ in ()).throw(
                    RuntimeError("fixture first attempt failed")
                ),
            )
        failed_run_id = failed.exception.run_id

        with (
            mock.patch.object(
                journal,
                "_execute",
                side_effect=KeyboardInterrupt(
                    "fixture cancellation before projector handoff"
                ),
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            journal.retry(
                failed_run_id,
                lambda _payload: self.fail("projector must not run"),
            )

        retry_rows = [
            row
            for row in journal.runs("vector")
            if row["retry_of"] == failed_run_id
        ]
        self.assertEqual(1, len(retry_rows))
        self.assertEqual(2, int(retry_rows[0]["attempt"]))
        self.assertEqual("failed", retry_rows[0]["status"])
        self.assertIn("KeyboardInterrupt", retry_rows[0]["error_text"])

        retried = journal.retry(
            failed_run_id,
            lambda _payload: {"projected": "after-cancellation"},
        )
        self.assertEqual("succeeded", retried["status"])
        self.assertEqual(3, int(journal.inspect_run(retried["run_id"])["attempt"]))

    def test_failure_cleanup_cannot_overwrite_other_owner_or_terminal_row(
        self,
    ) -> None:
        journal = ProjectionJournal(self.database)
        commit = {
            "commit_id": "commit-cleanup-cas",
            "canon_status": "accepted",
            "operation": "accept",
        }
        run_id = journal._new_run(
            projection_name="vector",
            commit_id=commit["commit_id"],
            input_payload=commit,
            attempt=1,
            retry_of=None,
        )
        original = journal.inspect_run(run_id)
        owner = (
            str(original["owner_host"]),
            int(original["owner_pid"]),
            str(original["owner_token"]),
        )

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                UPDATE projection_runs
                SET owner_token=?
                WHERE run_id=?
                """,
                (owner[2] + "-other", run_id),
            )
            connection.commit()
        changed_other_owner = journal._fail_owned_run(
            run_id,
            KeyboardInterrupt("must not replace another owner"),
            owner=owner,
        )
        self.assertFalse(changed_other_owner)
        self.assertEqual("running", journal.inspect_run(run_id)["status"])

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                UPDATE projection_runs
                SET owner_token=?, status='succeeded'
                WHERE run_id=?
                """,
                (owner[2], run_id),
            )
            connection.commit()
        changed_terminal = journal._fail_owned_run(
            run_id,
            KeyboardInterrupt("must not replace terminal status"),
            owner=owner,
        )
        self.assertFalse(changed_terminal)
        terminal = journal.inspect_run(run_id)
        self.assertEqual("succeeded", terminal["status"])
        self.assertIsNone(terminal["error_text"])

    def test_retry_does_not_reuse_output_older_than_failed_force_run(
        self,
    ) -> None:
        journal = ProjectionJournal(self.database)
        commit = {
            "commit_id": "commit-stale-retry-output",
            "canon_status": "accepted",
            "operation": "accept",
        }
        seeded = journal.run(
            "vector",
            commit,
            lambda _payload: {"generation": 1},
        )
        self.assertEqual("succeeded", seeded["status"])
        degraded = journal.run(
            "vector",
            commit,
            lambda _payload: {
                "status": "degraded",
                "generation": 2,
            },
            force=True,
        )
        calls = 0

        def recovered(_payload: object) -> dict[str, int]:
            nonlocal calls
            calls += 1
            return {"generation": 3}

        retried = journal.retry(degraded["run_id"], recovered)

        self.assertEqual(1, calls)
        self.assertEqual("succeeded", retried["status"])
        self.assertEqual({"generation": 3}, retried["output"])
        self.assertIsNotNone(retried["run_id"])
        self.assertEqual(
            degraded["run_id"],
            journal.inspect_run(retried["run_id"])["retry_of"],
        )

    def test_lock_blocked_cancel_cleanup_is_deferred_without_losing_error(
        self,
    ) -> None:
        database = self.database
        lockers: list[sqlite3.Connection] = []

        class InterruptAndLockConnection(projections._ClosingConnection):
            armed = False

            def commit(self) -> None:
                super().commit()
                if type(self).armed:
                    type(self).armed = False
                    locker = sqlite3.connect(database, timeout=0.1)
                    locker.execute("BEGIN IMMEDIATE")
                    lockers.append(locker)
                    raise KeyboardInterrupt(
                        "fixture cancellation after durable claim commit"
                    )

        class InterruptJournal(ProjectionJournal):
            def _connect(self) -> sqlite3.Connection:
                connection = sqlite3.connect(
                    self.database_path,
                    timeout=0.05,
                    factory=InterruptAndLockConnection,
                )
                connection.row_factory = sqlite3.Row
                return connection

        journal = InterruptJournal(database, auto_recover=False)
        commit = {
            "commit_id": "commit-lock-blocked-cancel-cleanup",
            "canon_status": "accepted",
            "operation": "accept",
        }
        InterruptAndLockConnection.armed = True
        try:
            with self.assertRaises(KeyboardInterrupt):
                journal.run(
                    "vector",
                    commit,
                    lambda _payload: self.fail("projector must not run"),
                )
        finally:
            for locker in lockers:
                locker.rollback()
                locker.close()

        reopened = ProjectionJournal(database, auto_recover=False)
        retried = reopened.run(
            "vector",
            commit,
            lambda _payload: {"projected": "after-cancellation"},
            wait_timeout_seconds=0.1,
        )
        self.assertEqual("succeeded", retried["status"])
        rows = reopened.runs("vector")
        self.assertEqual(
            ["failed", "succeeded"],
            [row["status"] for row in rows],
        )
        self.assertIn("KeyboardInterrupt", rows[0]["error_text"])
        deferred_key = reopened._deferred_failure_key(rows[0]["run_id"])
        with projections._DEFERRED_FAILURE_LOCK:
            self.assertNotIn(
                deferred_key,
                projections._DEFERRED_OWNED_FAILURES,
            )
            self.assertNotIn(
                deferred_key,
                projections._DEFERRED_FAILURE_WORKERS,
            )

    def test_deferred_worker_is_started_before_drainer_can_join_it(
        self,
    ) -> None:
        journal = ProjectionJournal(self.database, auto_recover=False)
        commit = {
            "commit_id": "commit-worker-start-order",
            "canon_status": "accepted",
            "operation": "accept",
        }
        owner = projections._current_owner()
        run_id = self._insert_owned_running(
            journal,
            commit,
            attempt=1,
            retry_of=None,
            owner_pid=owner[1],
            owner_token=owner[2],
        )
        deferred_key = journal._deferred_failure_key(run_id)
        with projections._DEFERRED_FAILURE_LOCK:
            projections._DEFERRED_OWNED_FAILURES[deferred_key] = (
                owner,
                "KeyboardInterrupt: fixture start-order cancellation",
            )

        real_thread = threading.Thread
        start_entered = threading.Event()
        allow_start = threading.Event()
        drain_entered = threading.Event()
        schedule_errors: list[BaseException] = []
        drain_errors: list[BaseException] = []

        class PausedStartThread(real_thread):
            def start(self) -> None:
                start_entered.set()
                if not allow_start.wait(timeout=5):
                    raise TimeoutError("fixture did not release worker start")
                super().start()

        def schedule_worker() -> None:
            try:
                journal._schedule_deferred_owned_failure(deferred_key)
            except BaseException as error:
                schedule_errors.append(error)

        def drain_worker() -> None:
            drain_entered.set()
            try:
                journal._drain_deferred_owned_failures()
            except BaseException as error:
                drain_errors.append(error)

        scheduler = real_thread(target=schedule_worker)
        drainer = real_thread(target=drain_worker)
        try:
            with mock.patch.object(
                projections.threading,
                "Thread",
                PausedStartThread,
            ):
                scheduler.start()
                self.assertTrue(start_entered.wait(timeout=5))
                drainer.start()
                self.assertTrue(drain_entered.wait(timeout=5))
                time.sleep(0.05)
                self.assertTrue(drainer.is_alive())
                allow_start.set()
                scheduler.join(timeout=5)
                drainer.join(timeout=5)
        finally:
            allow_start.set()
            scheduler.join(timeout=5)
            drainer.join(timeout=5)

        self.assertFalse(scheduler.is_alive())
        self.assertFalse(drainer.is_alive())
        self.assertEqual([], schedule_errors)
        self.assertEqual([], drain_errors)
        with projections._DEFERRED_FAILURE_LOCK:
            self.assertNotIn(
                deferred_key,
                projections._DEFERRED_OWNED_FAILURES,
            )
            self.assertNotIn(
                deferred_key,
                projections._DEFERRED_FAILURE_WORKERS,
            )
        row = journal.inspect_run(run_id)
        self.assertEqual("failed", row["status"])
        self.assertIn("KeyboardInterrupt", row["error_text"])

    def test_deferred_cleanup_daemon_unblocks_a_child_process(
        self,
    ) -> None:
        database = self.database
        lockers: list[sqlite3.Connection] = []

        class InterruptAndLockConnection(projections._ClosingConnection):
            armed = False

            def commit(self) -> None:
                super().commit()
                if type(self).armed:
                    type(self).armed = False
                    locker = sqlite3.connect(database, timeout=0.1)
                    locker.execute("BEGIN IMMEDIATE")
                    lockers.append(locker)
                    raise KeyboardInterrupt(
                        "fixture cancellation after durable claim commit"
                    )

        class InterruptJournal(ProjectionJournal):
            def _connect(self) -> sqlite3.Connection:
                connection = sqlite3.connect(
                    self.database_path,
                    timeout=0.05,
                    factory=InterruptAndLockConnection,
                )
                connection.row_factory = sqlite3.Row
                return connection

        journal = InterruptJournal(database, auto_recover=False)
        commit = {
            "commit_id": "commit-child-waits-for-deferred-cleanup",
            "canon_status": "accepted",
            "operation": "accept",
        }
        InterruptAndLockConnection.armed = True
        try:
            with self.assertRaises(KeyboardInterrupt):
                journal.run(
                    "vector",
                    commit,
                    lambda _payload: self.fail("projector must not run"),
                )
            database_key = str(database.expanduser().resolve())
            with projections._DEFERRED_FAILURE_LOCK:
                queued = [
                    key
                    for key in projections._DEFERRED_OWNED_FAILURES
                    if key[0] == database_key
                ]
            self.assertEqual(1, len(queued))
        finally:
            for locker in lockers:
                locker.rollback()
                locker.close()

        child_script = r"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from longform.projections import ProjectionJournal

database = Path(sys.argv[2])
journal = ProjectionJournal(database)
deadline = time.monotonic() + 5.0
prior = None
while time.monotonic() < deadline:
    rows = journal.runs("vector")
    prior = next(
        (
            row
            for row in rows
            if row["commit_id"]
            == "commit-child-waits-for-deferred-cleanup"
        ),
        None,
    )
    if prior is not None and prior["status"] != "running":
        break
    time.sleep(0.02)
if prior is None or prior["status"] != "failed":
    print(json.dumps({"prior": prior}, ensure_ascii=False))
    raise SystemExit(4)
result = journal.run(
    "vector",
    {
        "commit_id": "commit-child-waits-for-deferred-cleanup",
        "canon_status": "accepted",
        "operation": "accept",
    },
    lambda _payload: {"projected": "in-child"},
    wait_timeout_seconds=1.0,
)
print(
    json.dumps(
        {
            "prior_status": prior["status"],
            "prior_error": prior["error_text"],
            "result_status": result["status"],
            "output": result["output"],
        },
        ensure_ascii=False,
    )
)
"""
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                child_script,
                str(SCRIPTS),
                str(database),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(
            0,
            completed.returncode,
            msg=f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual("failed", payload["prior_status"])
        self.assertIn("KeyboardInterrupt", payload["prior_error"])
        self.assertEqual("succeeded", payload["result_status"])
        self.assertEqual({"projected": "in-child"}, payload["output"])


if __name__ == "__main__":
    unittest.main()
