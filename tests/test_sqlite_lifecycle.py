from __future__ import annotations

import ast
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import continuity.store as continuity_store_module  # noqa: E402
import grill_gate as grill_gate_module  # noqa: E402
from grill_gate import GrillGateService  # noqa: E402
import longform.memory as longform_memory_module  # noqa: E402
import longform.authority as authority_module  # noqa: E402
import longform.methods as longform_methods_module  # noqa: E402
import longform.projections as projections_module  # noqa: E402
from longform.authority import AuthorityIndex, AuthorityIndexError  # noqa: E402
from longform.memory import (  # noqa: E402
    AcceptedSummaryStore,
    LayeredMemoryStore,
)
from longform.methods import ProjectPatternStore  # noqa: E402
from longform.projections import ProjectionJournal  # noqa: E402
from plot_init.remote_cache import SQLiteRemoteResponseCache  # noqa: E402
import plot_init.storage as init_storage_module  # noqa: E402
from plot_init.storage import InitStorage  # noqa: E402
from plot_rag import _open_database as open_legacy_index  # noqa: E402
import plot_rag as plot_rag_module  # noqa: E402
from continuity.service import ContinuityService  # noqa: E402
from continuity.store import ContinuityStore  # noqa: E402
import state_rag as state_rag_module  # noqa: E402
from state_rag import _open_database as open_legacy_state  # noqa: E402


class SQLiteLifecycleTests(unittest.TestCase):
    def test_scripts_package_imports_work_without_scripts_path_injection(
        self,
    ) -> None:
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-c",
                (
                    "from scripts.plot_init.storage import InitStorage; "
                    "from scripts.longform.authority import AuthorityIndex; "
                    "from scripts.longform.memory import LayeredMemoryStore; "
                    "from scripts.longform.methods import ProjectPatternStore; "
                    "from scripts import grill_gate, plot_rag, state_rag; "
                    "print('PACKAGE_IMPORT_OK')"
                ),
            ],
            cwd=PLUGIN_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(
            0,
            completed.returncode,
            msg=f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )
        self.assertEqual("PACKAGE_IMPORT_OK", completed.stdout.strip())

    @staticmethod
    def _snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
        return {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in directory.iterdir()
            if path.is_file()
        }

    @staticmethod
    def _replace_round_trip(database: Path) -> None:
        moved = database.with_name(f"{database.stem}.moved{database.suffix}")
        os.replace(database, moved)
        os.replace(moved, database)

    @staticmethod
    def _failing_connect(
        original_connect: Callable[..., sqlite3.Connection],
        *,
        statement: str | None = None,
        fail_row_factory: bool = False,
    ) -> tuple[
        Callable[..., sqlite3.Connection],
        list[sqlite3.Connection],
        list[sqlite3.Connection],
    ]:
        expected = (
            "".join(statement.split()).upper()
            if statement is not None
            else None
        )
        opened: list[sqlite3.Connection] = []
        closed: list[sqlite3.Connection] = []

        class FailingConnection(sqlite3.Connection):
            def __setattr__(self, name: str, value: Any) -> None:
                if fail_row_factory and name == "row_factory":
                    raise RuntimeError("injected row_factory failure")
                super().__setattr__(name, value)

            def execute(self, sql: str, parameters=(), /):
                normalized = "".join(str(sql).split()).upper()
                if expected is not None and normalized == expected:
                    raise RuntimeError(
                        f"injected SQLite setup failure: {statement}"
                    )
                return super().execute(sql, parameters)

            def close(self) -> None:
                closed.append(self)
                super().close()

        def intercept_connect(*args: Any, **kwargs: Any):
            kwargs["factory"] = FailingConnection
            connection = original_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        return intercept_connect, opened, closed

    def test_component_initializers_reject_foreign_sqlite_without_writes(
        self,
    ) -> None:
        constructors: tuple[
            tuple[str, Callable[[Path], Any]],
            ...,
        ] = (
            (
                "legacy-index",
                lambda path: open_legacy_index(path),
            ),
            (
                "legacy-state",
                lambda path: open_legacy_state(
                    SimpleNamespace(db_path=path)
                ),
            ),
            (
                "grill",
                lambda path: GrillGateService(path)._initialize(),
            ),
            ("authority", lambda path: AuthorityIndex(path)),
            ("memory", lambda path: LayeredMemoryStore(path)),
            ("summary", lambda path: AcceptedSummaryStore(path)),
            ("craft", lambda path: ProjectPatternStore(path)),
            (
                "remote-cache",
                lambda path: SQLiteRemoteResponseCache(path)._initialize(),
            ),
            (
                "init-storage",
                lambda path: InitStorage(path)._initialize(),
            ),
            (
                "projection-journal",
                lambda path: ProjectionJournal(path, auto_recover=False),
            ),
            (
                "continuity-store",
                lambda path: ContinuityStore(
                    path.parent,
                    db_path=path,
                ).ensure_schema(),
            ),
            (
                "continuity-service",
                lambda path: ContinuityService(
                    path.parent,
                    db_path=path,
                ).schema_status(),
            ),
        )
        for label, constructor in constructors:
            with (
                self.subTest(component=label),
                tempfile.TemporaryDirectory() as temporary,
            ):
                database = Path(temporary) / f"{label}.sqlite3"
                with closing(sqlite3.connect(database)) as connection:
                    connection.execute(
                        """
                        CREATE TABLE user_finance(
                            id INTEGER PRIMARY KEY,
                            amount INTEGER NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        "INSERT INTO user_finance(amount) VALUES(7)"
                    )
                    connection.commit()
                before = self._snapshot(database.parent)

                with self.assertRaisesRegex(
                    RuntimeError,
                    "(?i)FOREIGN|SCHEMA_MISSING|UNOWNED",
                ):
                    result = constructor(database)
                    if isinstance(result, sqlite3.Connection):
                        result.close()

                self.assertEqual(
                    before,
                    self._snapshot(database.parent),
                )
                with closing(sqlite3.connect(database)) as connection:
                    tables = {
                        str(row[0])
                        for row in connection.execute(
                            """
                            SELECT name FROM sqlite_master
                            WHERE type='table'
                              AND name NOT LIKE 'sqlite_%'
                            """
                        )
                    }
                self.assertEqual({"user_finance"}, tables)

    def test_owned_component_metadata_does_not_allow_foreign_tables(self) -> None:
        cases: tuple[
            tuple[str, str, Callable[[Path], Any], str],
            ...,
        ] = (
            (
                "init-storage",
                (
                    "CREATE TABLE initialization_meta("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                    "INSERT INTO initialization_meta VALUES("
                    "'schema_version', '2');"
                    "CREATE TABLE user_finance(id INTEGER PRIMARY KEY);"
                ),
                lambda path: InitStorage(path)._initialize(),
                "INIT_STORAGE_DATABASE_UNOWNED",
            ),
            (
                "continuity-store",
                (
                    "CREATE TABLE state_meta("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                    "updated_at TEXT NOT NULL);"
                    "INSERT INTO state_meta VALUES("
                    "'schema_version', '2', 'fixture');"
                    "CREATE TABLE user_finance(id INTEGER PRIMARY KEY);"
                ),
                lambda path: ContinuityStore(
                    path.parent,
                    db_path=path,
                ).ensure_schema(),
                "STATE_DATABASE_UNOWNED",
            ),
            (
                "continuity-service",
                (
                    "CREATE TABLE state_meta("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                    "updated_at TEXT NOT NULL);"
                    "INSERT INTO state_meta VALUES("
                    "'schema_version', '2', 'fixture');"
                    "CREATE TABLE user_finance(id INTEGER PRIMARY KEY);"
                ),
                lambda path: ContinuityService(
                    path.parent,
                    db_path=path,
                ).schema_status(),
                "STATE_DATABASE_UNOWNED",
            ),
        )
        for label, schema, constructor, expected_code in cases:
            with (
                self.subTest(component=label),
                tempfile.TemporaryDirectory() as temporary,
            ):
                database = Path(temporary) / f"{label}.sqlite3"
                with closing(sqlite3.connect(database)) as connection:
                    connection.executescript(schema)
                    connection.commit()
                before = self._snapshot(database.parent)

                with self.assertRaises(RuntimeError) as raised:
                    constructor(database)

                self.assertEqual(
                    expected_code,
                    getattr(raised.exception, "code", expected_code),
                )
                self.assertEqual(before, self._snapshot(database.parent))
                self.assertFalse((database.parent / "backups").exists())

    def test_legacy_index_initializer_closes_database_on_runtime_failures(
        self,
    ) -> None:
        failure_cases = (
            "foreign-keys",
            "busy-timeout",
            "begin",
            "schema-helper",
            "commit",
        )
        for failure_phase in failure_cases:
            with (
                self.subTest(failure_phase=failure_phase),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                database = root / "index.sqlite3"
                moved = root / "moved.sqlite3"
                original_connect = plot_rag_module.sqlite3.connect

                class FailingConnection(sqlite3.Connection):
                    def execute(self, sql: str, parameters=(), /):
                        normalized = " ".join(str(sql).split()).upper()
                        if (
                            failure_phase == "foreign-keys"
                            and normalized == "PRAGMA FOREIGN_KEYS = ON"
                        ):
                            raise RuntimeError(
                                "injected foreign_keys failure"
                            )
                        if (
                            failure_phase == "busy-timeout"
                            and normalized == "PRAGMA BUSY_TIMEOUT = 10000"
                        ):
                            raise RuntimeError(
                                "injected busy_timeout failure"
                            )
                        if (
                            failure_phase == "begin"
                            and normalized == "BEGIN IMMEDIATE"
                        ):
                            raise sqlite3.OperationalError(
                                "injected BEGIN failure"
                            )
                        return super().execute(sql, parameters)

                    def commit(self) -> None:
                        if failure_phase == "commit":
                            raise sqlite3.OperationalError(
                                "injected commit failure"
                            )
                        super().commit()

                def intercept_connect(*args: Any, **kwargs: Any):
                    kwargs["factory"] = FailingConnection
                    return original_connect(*args, **kwargs)

                helper_patch = (
                    mock.patch.object(
                        plot_rag_module,
                        "execute_sqlite_script_in_transaction",
                        side_effect=RuntimeError(
                            "injected schema helper failure"
                        ),
                    )
                    if failure_phase == "schema-helper"
                    else mock.patch.object(
                        plot_rag_module,
                        "execute_sqlite_script_in_transaction",
                        wraps=(
                            plot_rag_module
                            .execute_sqlite_script_in_transaction
                        ),
                    )
                )
                with (
                    mock.patch.object(
                        plot_rag_module.sqlite3,
                        "connect",
                        side_effect=intercept_connect,
                    ),
                    helper_patch,
                    self.assertRaises(
                        (RuntimeError, plot_rag_module.PlotRagError),
                    ),
                ):
                    open_legacy_index(database)

                os.replace(database, moved)
                os.replace(moved, database)
                self.assertTrue(database.is_file())

    def test_shared_longform_database_allows_only_known_siblings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "longform.sqlite3"

            LayeredMemoryStore(database)
            AcceptedSummaryStore(database)
            ProjectPatternStore(database)

            with closing(sqlite3.connect(database)) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type='table'
                          AND name NOT LIKE 'sqlite_%'
                        """
                    )
                }
            self.assertEqual(
                {
                    "longform_memory_meta",
                    "memory_entries",
                    "longform_summary_meta",
                    "accepted_summaries",
                    "craft_memory_meta",
                    "craft_patterns",
                },
                tables,
            )

    def test_shared_longform_rejects_incompatible_sibling_versions_without_writes(
        self,
    ) -> None:
        cases: tuple[
            tuple[
                str,
                Callable[[Path], Any],
                str,
                Callable[[Path], Any],
            ],
            ...,
        ] = (
            (
                "future-memory-before-summary",
                LayeredMemoryStore,
                "longform_memory_meta",
                AcceptedSummaryStore,
            ),
            (
                "future-summary-before-memory",
                AcceptedSummaryStore,
                "longform_summary_meta",
                LayeredMemoryStore,
            ),
            (
                "future-memory-before-craft",
                LayeredMemoryStore,
                "longform_memory_meta",
                ProjectPatternStore,
            ),
        )
        for label, seed, meta_table, follower in cases:
            with (
                self.subTest(case=label),
                tempfile.TemporaryDirectory() as temporary,
            ):
                database = Path(temporary) / "longform.sqlite3"
                seed(database)
                with closing(sqlite3.connect(database)) as connection:
                    connection.execute(
                        f"UPDATE {meta_table} SET value='999' "
                        "WHERE key='schema_version'"
                    )
                    connection.commit()
                before = self._snapshot(database.parent)

                with self.assertRaisesRegex(
                    RuntimeError,
                    "SCHEMA_UNSUPPORTED",
                ):
                    follower(database)

                self.assertEqual(before, self._snapshot(database.parent))

    def test_missing_component_version_is_read_only_failure(self) -> None:
        fixtures: tuple[
            tuple[str, str, Callable[[Path], Any]],
            ...,
        ] = (
            (
                "legacy-index",
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                lambda path: open_legacy_index(path),
            ),
            (
                "legacy-state",
                "CREATE TABLE state_meta("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                lambda path: open_legacy_state(
                    SimpleNamespace(db_path=path)
                ),
            ),
            (
                "grill",
                "CREATE TABLE grill_meta("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                lambda path: GrillGateService(path)._initialize(),
            ),
            (
                "authority",
                "CREATE TABLE authority_index_meta("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                lambda path: AuthorityIndex(path),
            ),
            (
                "memory",
                "CREATE TABLE longform_memory_meta("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                lambda path: LayeredMemoryStore(path),
            ),
            (
                "summary",
                "CREATE TABLE longform_summary_meta("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                lambda path: AcceptedSummaryStore(path),
            ),
            (
                "craft",
                "CREATE TABLE craft_memory_meta("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                lambda path: ProjectPatternStore(path),
            ),
        )
        for label, schema, constructor in fixtures:
            with (
                self.subTest(component=label),
                tempfile.TemporaryDirectory() as temporary,
            ):
                database = Path(temporary) / f"{label}.sqlite3"
                with closing(sqlite3.connect(database)) as connection:
                    connection.execute(schema)
                    connection.commit()
                before = self._snapshot(database.parent)

                with self.assertRaisesRegex(
                    RuntimeError,
                    "SCHEMA_MISSING",
                ):
                    result = constructor(database)
                    if isinstance(result, sqlite3.Connection):
                        result.close()

                self.assertEqual(before, self._snapshot(database.parent))

    def test_component_validation_and_schema_creation_share_writer_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "locked-initialization.sqlite3"
            validated = threading.Event()
            release = threading.Event()
            errors: list[BaseException] = []
            original_validate = (
                longform_memory_module.validate_sqlite_component_schema
            )

            def delayed_validate(
                connection: sqlite3.Connection,
                **kwargs: Any,
            ) -> set[str]:
                result = original_validate(connection, **kwargs)
                validated.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("fixture validation was not released")
                return result

            def initialize() -> None:
                try:
                    LayeredMemoryStore(database)
                except BaseException as exc:
                    errors.append(exc)

            with mock.patch.object(
                longform_memory_module,
                "validate_sqlite_component_schema",
                new=delayed_validate,
            ):
                worker = threading.Thread(target=initialize)
                worker.start()
                self.assertTrue(validated.wait(timeout=5))
                try:
                    with closing(
                        sqlite3.connect(database, timeout=0.05)
                    ) as competing:
                        with self.assertRaisesRegex(
                            sqlite3.OperationalError,
                            "locked",
                        ):
                            competing.execute(
                                "CREATE TABLE user_finance(id INTEGER)"
                            )
                finally:
                    release.set()
                worker.join(timeout=10)

            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            with closing(sqlite3.connect(database)) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type='table'
                          AND name NOT LIKE 'sqlite_%'
                        """
                    )
                }
            self.assertEqual(
                {"longform_memory_meta", "memory_entries"},
                tables,
            )

    def test_remote_cache_rejects_unknown_initialization_prefixed_table(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "remote-cache.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "CREATE TABLE initialization_user_finance(id INTEGER)"
                )
                connection.commit()
            before = self._snapshot(database.parent)

            with self.assertRaises(RuntimeError) as raised:
                SQLiteRemoteResponseCache(database)._initialize()

            self.assertEqual(
                "REMOTE_CACHE_DATABASE_UNOWNED",
                getattr(raised.exception, "code", ""),
            )
            self.assertEqual(before, self._snapshot(database.parent))

    def test_remote_cache_rejects_incompatible_init_schema_without_writes(
        self,
    ) -> None:
        for stored_version in (None, "-1", "999"):
            with (
                self.subTest(stored_version=stored_version),
                tempfile.TemporaryDirectory() as temporary,
            ):
                database = Path(temporary) / "remote-cache.sqlite3"
                with closing(sqlite3.connect(database)) as connection:
                    connection.execute(
                        """
                        CREATE TABLE initialization_meta(
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL
                        )
                        """
                    )
                    if stored_version is not None:
                        connection.execute(
                            """
                            INSERT INTO initialization_meta(key, value)
                            VALUES('schema_version', ?)
                            """,
                            (stored_version,),
                        )
                    connection.commit()
                before = self._snapshot(database.parent)

                with self.assertRaises(RuntimeError) as raised:
                    SQLiteRemoteResponseCache(database)._initialize()

                self.assertEqual(
                    "REMOTE_CACHE_SCHEMA_INVALID",
                    getattr(raised.exception, "code", ""),
                )
                self.assertEqual(before, self._snapshot(database.parent))

    def test_remote_cache_first_database_remains_init_storage_compatible(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "cache-first.sqlite3"

            SQLiteRemoteResponseCache(database)._initialize()
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

            InitStorage(database)._initialize()
            with closing(sqlite3.connect(database)) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type='table'
                          AND name NOT LIKE 'sqlite_%'
                        """
                    )
                }
            self.assertIn("initialization_sessions", tables)
            self.assertIn("initialization_remote_response_cache", tables)

    def test_legacy_state_rejects_foreign_tables_and_future_continuity(
        self,
    ) -> None:
        cases = (
            (
                "foreign-table",
                (
                    "CREATE TABLE state_meta("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                    "updated_at TEXT NOT NULL);"
                    "INSERT INTO state_meta VALUES("
                    "'schema_version', '2', 'fixture');"
                    "CREATE TABLE user_finance(id INTEGER PRIMARY KEY);"
                ),
            ),
            (
                "future-continuity",
                (
                    "CREATE TABLE state_meta("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                    "updated_at TEXT NOT NULL);"
                    "INSERT INTO state_meta VALUES("
                    "'schema_version', '2', 'fixture');"
                    "INSERT INTO state_meta VALUES("
                    "'continuity_schema_version', '999', 'fixture');"
                ),
            ),
        )
        for label, schema in cases:
            with (
                self.subTest(case=label),
                tempfile.TemporaryDirectory() as temporary,
            ):
                database = Path(temporary) / "state.sqlite3"
                with closing(sqlite3.connect(database)) as connection:
                    connection.executescript(schema)
                    connection.commit()
                before = self._snapshot(database.parent)

                with self.assertRaises(RuntimeError):
                    open_legacy_state(SimpleNamespace(db_path=database))

                self.assertEqual(before, self._snapshot(database.parent))

    def test_legacy_state_initializer_closes_database_on_runtime_failures(
        self,
    ) -> None:
        failure_cases = (
            "schema-helper",
            "alter",
            "commit",
            "wal",
        )
        for failure_phase in failure_cases:
            with (
                self.subTest(failure_phase=failure_phase),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                database = root / "state.sqlite3"
                moved = root / "moved.sqlite3"
                original_connect = state_rag_module.sqlite3.connect
                if failure_phase == "alter":
                    with closing(sqlite3.connect(database)) as seed:
                        seed.executescript(
                            """
                            CREATE TABLE state_meta(
                                key TEXT PRIMARY KEY,
                                value TEXT NOT NULL,
                                updated_at TEXT NOT NULL
                            );
                            INSERT INTO state_meta VALUES(
                                'schema_version', '2', 'fixture'
                            );
                            CREATE TABLE state_events(
                                event_id TEXT PRIMARY KEY,
                                request_id TEXT NOT NULL,
                                receipt_id TEXT NOT NULL,
                                session_id TEXT NOT NULL DEFAULT '',
                                category TEXT NOT NULL,
                                subject TEXT NOT NULL,
                                field TEXT NOT NULL,
                                operation TEXT NOT NULL,
                                value_json TEXT,
                                confidence REAL NOT NULL,
                                evidence TEXT NOT NULL,
                                source_hash TEXT NOT NULL,
                                created_at TEXT NOT NULL
                            );
                            """
                        )
                        seed.commit()

                class FailingConnection(sqlite3.Connection):
                    def execute(self, sql: str, parameters=(), /):
                        normalized = " ".join(str(sql).split()).upper()
                        if (
                            failure_phase == "alter"
                            and normalized.startswith("ALTER TABLE")
                        ):
                            raise RuntimeError("injected ALTER failure")
                        if (
                            failure_phase == "wal"
                            and normalized == "PRAGMA JOURNAL_MODE = WAL"
                        ):
                            raise RuntimeError("injected WAL failure")
                        return super().execute(sql, parameters)

                    def commit(self) -> None:
                        if failure_phase == "commit":
                            raise sqlite3.OperationalError(
                                "injected commit failure"
                            )
                        super().commit()

                def intercept_connect(*args: Any, **kwargs: Any):
                    kwargs["factory"] = FailingConnection
                    return original_connect(*args, **kwargs)

                patches = [
                    mock.patch.object(
                        state_rag_module.sqlite3,
                        "connect",
                        side_effect=intercept_connect,
                    )
                ]
                if failure_phase == "schema-helper":
                    patches.append(
                        mock.patch.object(
                            state_rag_module,
                            "execute_sqlite_script_in_transaction",
                            side_effect=RuntimeError(
                                "injected schema helper failure"
                            ),
                        )
                    )

                with patches[0]:
                    helper_context = (
                        patches[1]
                        if len(patches) == 2
                        else mock.patch.object(
                            state_rag_module,
                            "execute_sqlite_script_in_transaction",
                            wraps=(
                                state_rag_module
                                .execute_sqlite_script_in_transaction
                            ),
                        )
                    )
                    with helper_context, self.assertRaises(
                        (RuntimeError, sqlite3.OperationalError)
                    ):
                        open_legacy_state(
                            SimpleNamespace(db_path=database)
                        )

                os.replace(database, moved)
                os.replace(moved, database)
                self.assertTrue(database.is_file())

    def test_authority_future_version_preserves_public_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "authority.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """
                    CREATE TABLE authority_index_meta(
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO authority_index_meta(key, value)
                    VALUES('authority_index_schema_version', '999')
                    """
                )
                connection.commit()
            before = self._snapshot(database.parent)

            with self.assertRaisesRegex(
                AuthorityIndexError,
                "schema version does not match",
            ):
                AuthorityIndex(database)

            self.assertEqual(before, self._snapshot(database.parent))

    def test_write_connection_setup_failures_release_database_handles(
        self,
    ) -> None:
        cases = (
            ("continuity", "PRAGMA foreign_keys = ON"),
            ("continuity", "PRAGMA busy_timeout = 30000"),
            ("authority", "PRAGMA foreign_keys = ON"),
            ("grill", "PRAGMA busy_timeout = 30000"),
            ("grill", "BEGIN IMMEDIATE"),
            ("init-storage", "PRAGMA foreign_keys=ON"),
            ("init-storage", "BEGIN IMMEDIATE"),
        )
        for component, statement in cases:
            with (
                self.subTest(component=component, statement=statement),
                tempfile.TemporaryDirectory() as temporary,
            ):
                database = Path(temporary) / f"{component}.sqlite3"
                original_connect = sqlite3.connect

                if component == "continuity":
                    module = continuity_store_module
                    store = ContinuityStore(
                        database.parent,
                        db_path=database,
                    )

                    def operation() -> None:
                        store._connect()

                    initialization_patch = mock.patch.object(
                        store,
                        "ensure_schema",
                        wraps=store.ensure_schema,
                    )
                elif component == "authority":
                    module = authority_module
                    index = object.__new__(AuthorityIndex)
                    index.database_path = database

                    def operation() -> None:
                        index._connect()

                    initialization_patch = mock.patch.object(
                        index,
                        "_initialize",
                        wraps=index._initialize,
                    )
                elif component == "grill":
                    module = grill_gate_module
                    service = GrillGateService(database)
                    service._initialize()

                    def operation() -> None:
                        with service._write_connection():
                            pass

                    initialization_patch = mock.patch.object(
                        service,
                        "_initialize",
                        return_value=None,
                    )
                else:
                    module = init_storage_module
                    storage = InitStorage(database)
                    storage._initialize()

                    def operation() -> None:
                        with storage._write_connection():
                            pass

                    initialization_patch = mock.patch.object(
                        storage,
                        "_initialize",
                        return_value=None,
                    )

                intercept, opened, closed = self._failing_connect(
                    original_connect,
                    statement=statement,
                )
                with (
                    initialization_patch,
                    mock.patch.object(
                        module.sqlite3,
                        "connect",
                        side_effect=intercept,
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "injected SQLite setup failure",
                    ),
                ):
                    operation()

                self.assertTrue(opened)
                self.assertEqual(
                    {id(connection) for connection in opened},
                    {id(connection) for connection in closed},
                )
                self._replace_round_trip(database)

    def test_read_connection_setup_failures_release_database_handles(
        self,
    ) -> None:
        cases = (
            ("continuity", "PRAGMA query_only = ON"),
            ("continuity", "PRAGMA busy_timeout = 30000"),
            ("grill", "PRAGMA query_only = ON"),
            ("init-storage", "PRAGMA query_only=ON"),
            ("legacy-state", "PRAGMA query_only = ON"),
            ("legacy-state", "PRAGMA busy_timeout = 15000"),
            ("state-diagnostic", "PRAGMA query_only = ON"),
            ("state-diagnostic", "PRAGMA busy_timeout = 15000"),
        )
        for component, statement in cases:
            with (
                self.subTest(component=component, statement=statement),
                tempfile.TemporaryDirectory() as temporary,
            ):
                database = Path(temporary) / f"{component}.sqlite3"
                original_connect = sqlite3.connect

                if component == "continuity":
                    module = continuity_store_module
                    store = ContinuityStore(
                        database.parent,
                        db_path=database,
                    )
                    store.ensure_schema()

                    def operation() -> None:
                        with store.read_connection():
                            pass

                elif component == "grill":
                    module = grill_gate_module
                    service = GrillGateService(database)
                    service._initialize()

                    def operation() -> None:
                        with service._read_connection():
                            pass

                elif component == "init-storage":
                    module = init_storage_module
                    storage = InitStorage(database)
                    storage._initialize()

                    def operation() -> None:
                        with storage._read_connection():
                            pass

                elif component == "legacy-state":
                    module = state_rag_module
                    with closing(original_connect(database)) as connection:
                        connection.execute(
                            "CREATE TABLE fixture(id INTEGER PRIMARY KEY)"
                        )
                        connection.commit()
                    config = SimpleNamespace(db_path=database)

                    def operation() -> None:
                        state_rag_module._open_readonly_database(config)

                else:
                    module = state_rag_module
                    with closing(original_connect(database)) as connection:
                        connection.execute(
                            "CREATE TABLE fixture(id INTEGER PRIMARY KEY)"
                        )
                        connection.commit()
                    config = SimpleNamespace(db_path=database)

                    def operation() -> None:
                        with state_rag_module._open_diagnostic_database(
                            config
                        ):
                            pass

                intercept, opened, closed = self._failing_connect(
                    original_connect,
                    statement=statement,
                )
                with (
                    mock.patch.object(
                        module.sqlite3,
                        "connect",
                        side_effect=intercept,
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "injected SQLite setup failure",
                    ),
                ):
                    operation()

                self.assertTrue(opened)
                self.assertEqual(
                    {id(connection) for connection in opened},
                    {id(connection) for connection in closed},
                )
                self._replace_round_trip(database)

    def test_longform_row_factory_failures_release_database_handles(
        self,
    ) -> None:
        cases = (
            (
                "memory",
                longform_memory_module,
                LayeredMemoryStore,
            ),
            (
                "summary",
                longform_memory_module,
                AcceptedSummaryStore,
            ),
            (
                "methods",
                longform_methods_module,
                ProjectPatternStore,
            ),
            (
                "projections",
                projections_module,
                ProjectionJournal,
            ),
        )
        for label, module, store_type in cases:
            with (
                self.subTest(component=label),
                tempfile.TemporaryDirectory() as temporary,
            ):
                database = Path(temporary) / f"{label}.sqlite3"
                original_connect = sqlite3.connect
                store = object.__new__(store_type)
                store.database_path = database
                intercept, opened, closed = self._failing_connect(
                    original_connect,
                    fail_row_factory=True,
                )

                with (
                    mock.patch.object(
                        module.sqlite3,
                        "connect",
                        side_effect=intercept,
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "injected row_factory failure",
                    ),
                ):
                    store._connect()

                self.assertTrue(opened)
                self.assertEqual(
                    {id(connection) for connection in opened},
                    {id(connection) for connection in closed},
                )
                self._replace_round_trip(database)

    def test_sqlite_connections_are_not_used_as_closing_contexts(self) -> None:
        offenders: list[str] = []
        for source_root in (PLUGIN_ROOT / "scripts", PLUGIN_ROOT / "tests"):
            for path in sorted(source_root.rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.With, ast.AsyncWith)):
                        continue
                    for item in node.items:
                        expression = item.context_expr
                        if not isinstance(expression, ast.Call):
                            continue
                        function = expression.func
                        if (
                            isinstance(function, ast.Attribute)
                            and function.attr == "connect"
                            and isinstance(function.value, ast.Name)
                            and function.value.id == "sqlite3"
                        ):
                            relative = path.relative_to(PLUGIN_ROOT).as_posix()
                            offenders.append(f"{relative}:{expression.lineno}")

        self.assertEqual(
            [],
            offenders,
            "sqlite3.Connection context managers commit or roll back but do not "
            "close; wrap sqlite3.connect(...) with contextlib.closing instead",
        )


if __name__ == "__main__":
    unittest.main()
