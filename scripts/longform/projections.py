from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


class _ClosingConnection(sqlite3.Connection):
    """Close SQLite handles when leaving a ``with`` block on Windows."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


PROJECTION_SCHEMA_VERSION = 1
PROJECTION_TABLES = frozenset(
    {
        "longform_projection_meta",
        "projection_runs",
        "projection_outputs",
    }
)
PROJECTION_NAMES = ("snapshot", "index", "summary", "memory", "vector")
PROJECTION_WAIT_TIMEOUT_SECONDS = 120.0
PROJECTION_WAIT_POLL_SECONDS = 0.05
PROJECTION_OWNER_PROBE_SECONDS = 0.25
_DEFERRED_FAILURE_INITIAL_RETRY_SECONDS = 0.01
_DEFERRED_FAILURE_MAX_RETRY_SECONDS = 0.25
_DEFERRED_FAILURE_LOCK = threading.Lock()
_DEFERRED_OWNED_FAILURES: dict[
    tuple[str, str],
    tuple[tuple[str, int, str], str],
] = {}
_DEFERRED_FAILURE_WORKERS: dict[
    tuple[str, str],
    threading.Thread,
] = {}
_DEFERRED_FAILURE_JOIN_SECONDS = 6.0
VOLATILE_HASH_KEYS = frozenset(
    {
        "updated_at",
        "created_at",
        "started_at",
        "finished_at",
        "attempt_timestamp",
        "run_id",
        "retry_of",
    }
)


class ProjectionRunError(RuntimeError):
    def __init__(self, run_id: str, message: str) -> None:
        super().__init__(message)
        self.run_id = run_id


class _ProjectionOwnershipLost(ProjectionRunError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_host() -> str:
    return socket.gethostname().strip().casefold()


_PROCESS_TOKEN_CACHE: tuple[int, str] | None = None


def _windows_process_probe(process_id: int) -> tuple[str, str | None]:
    """Return ``(state, birth_token)`` for one Windows PID."""

    try:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        error_access_denied = 5
        error_invalid_parameter = 87
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            process_id,
        )
        if not handle:
            error_code = int(ctypes.get_last_error())
            if error_code == error_invalid_parameter:
                return "dead", None
            if error_code == error_access_denied:
                return "unknown", None
            return "unknown", None
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(
                handle,
                ctypes.byref(exit_code),
            ):
                return "unknown", None
            if exit_code.value != still_active:
                return "dead", None
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return "unknown", None
            created_at = (
                int(creation.dwHighDateTime) << 32
            ) | int(creation.dwLowDateTime)
            return "alive", f"windows-filetime:{created_at}"
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return "unknown", None


def _linux_process_probe(process_id: int) -> tuple[str, str | None]:
    """Return ``(state, birth_token)`` from Linux procfs."""

    stat_path = Path("/proc") / str(process_id) / "stat"
    try:
        stat_payload = stat_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "dead", None
    except (OSError, UnicodeError):
        return "unknown", None
    closing_parenthesis = stat_payload.rfind(")")
    if closing_parenthesis < 0:
        return "unknown", None
    fields = stat_payload[closing_parenthesis + 2 :].split()
    if len(fields) <= 19:
        return "unknown", None
    if fields[0] in {"Z", "X", "x"}:
        return "dead", None
    start_ticks = fields[19]
    try:
        boot_id = (
            Path("/proc/sys/kernel/random/boot_id")
            .read_text(encoding="ascii")
            .strip()
            .casefold()
        )
    except (OSError, UnicodeError):
        return "alive", None
    if not boot_id:
        return "alive", None
    return "alive", f"linux-start:{boot_id}:{start_ticks}"


def _process_probe(process_id: int) -> tuple[str, str | None]:
    """Probe liveness and a PID-reuse-resistant process birth token."""

    pid = int(process_id)
    if pid <= 0:
        return "dead", None
    if os.name == "nt":
        return _windows_process_probe(pid)
    if Path("/proc/self/stat").is_file():
        return _linux_process_probe(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead", None
    except (PermissionError, OSError):
        return "unknown", None
    return "alive", None


def _current_process_token() -> str:
    """Return a stable owner token for this process, including after ``fork``."""

    global _PROCESS_TOKEN_CACHE
    pid = os.getpid()
    if _PROCESS_TOKEN_CACHE is not None and _PROCESS_TOKEN_CACHE[0] == pid:
        return _PROCESS_TOKEN_CACHE[1]
    state, birth_token = _process_probe(pid)
    token = (
        birth_token
        if state == "alive" and birth_token
        else f"session:{uuid.uuid4().hex}"
    )
    _PROCESS_TOKEN_CACHE = (pid, token)
    return token


def _current_owner() -> tuple[str, int, str]:
    return _local_host(), os.getpid(), _current_process_token()


def _owner_state(
    owner_host: str,
    owner_pid: int,
    owner_token: str,
) -> tuple[str, str]:
    """Classify an owner without treating an uncertain probe as abandoned."""

    local_host = _local_host()
    if not owner_host or owner_host != local_host or owner_pid <= 0:
        return "unknown", "projection owner cannot be verified on this host"
    state, current_token = _process_probe(owner_pid)
    if state == "dead":
        return (
            "dead",
            "projection owner process "
            f"{owner_pid} is no longer running on {local_host}",
        )
    if state != "alive":
        return "unknown", "projection owner process state is uncertain"
    if not owner_token:
        return "unknown", "projection owner has no verifiable birth token"
    if owner_token.startswith("session:"):
        if (
            owner_pid == os.getpid()
            and owner_token == _current_process_token()
        ):
            return "alive", "projection owner session is still active"
        return "unknown", "projection owner session token is not externally verifiable"
    if current_token is None:
        return "unknown", "projection owner birth token cannot be queried"
    if owner_token == current_token:
        return "alive", "projection owner process is still active"
    if owner_token.startswith(("windows-filetime:", "linux-start:")):
        return (
            "dead",
            f"projection owner PID {owner_pid} was reused on {local_host}",
        )
    return "unknown", "projection owner token format is not verifiable"


def _normalize_for_hash(value: Any, exclude_keys: frozenset[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_for_hash(item, exclude_keys)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in exclude_keys
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_for_hash(item, exclude_keys) for item in value]
    if isinstance(value, set):
        normalized = [_normalize_for_hash(item, exclude_keys) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, float):
        return float(format(value, ".15g"))
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def stable_normalized_hash(
    value: Any,
    *,
    exclude_keys: Iterable[str] = VOLATILE_HASH_KEYS,
) -> str:
    """Hash deterministic content while excluding projection-run metadata."""

    excluded = frozenset(str(key) for key in exclude_keys)
    normalized = _normalize_for_hash(value, excluded)
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ProjectionJournal:
    """Independent retryable logs for derived projection families."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        auto_recover: bool = True,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            current_version = self._existing_schema_version(connection)
            if (
                current_version is not None
                and current_version != PROJECTION_SCHEMA_VERSION
            ):
                raise RuntimeError(
                    "long-form projection schema version mismatch"
                )
            connection.execute("BEGIN IMMEDIATE")
            try:
                current_version = self._existing_schema_version(connection)
                if current_version is None:
                    connection.execute(
                        """
                        CREATE TABLE longform_projection_meta (
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL
                        )
                        """
                    )
                    current_version = PROJECTION_SCHEMA_VERSION
                if current_version != PROJECTION_SCHEMA_VERSION:
                    raise RuntimeError(
                        "long-form projection schema version mismatch"
                    )
                connection.execute(
                    """
                CREATE TABLE IF NOT EXISTS projection_runs (
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
                    finished_at TEXT,
                    owner_host TEXT,
                    owner_pid INTEGER,
                    owner_token TEXT
                )
                    """
                )
                connection.execute(
                    """
                CREATE INDEX IF NOT EXISTS projection_runs_lookup
                    ON projection_runs(
                        projection_name, commit_id, status, attempt
                    )
                    """
                )
                connection.execute(
                    """
                CREATE TABLE IF NOT EXISTS projection_outputs (
                    projection_name TEXT NOT NULL,
                    commit_id TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    output_sha256 TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    PRIMARY KEY(projection_name, commit_id, input_sha256)
                )
                    """
                )
                run_columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(projection_runs)"
                    )
                }
                for column_name, column_type in (
                    ("owner_host", "TEXT"),
                    ("owner_pid", "INTEGER"),
                    ("owner_token", "TEXT"),
                ):
                    if column_name in run_columns:
                        continue
                    connection.execute(
                        "ALTER TABLE projection_runs "
                        f"ADD COLUMN {column_name} {column_type}"
                    )
                connection.execute(
                    """
                    INSERT INTO longform_projection_meta(key, value)
                    VALUES ('schema_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(PROJECTION_SCHEMA_VERSION),),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        self._drain_deferred_owned_failures()
        if auto_recover:
            self.recover_interrupted_runs()

    @staticmethod
    def _existing_schema_version(
        connection: sqlite3.Connection,
    ) -> int | None:
        """Inspect journal identity without claiming a foreign database."""

        user_tables = {
            str(row["name"])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                """
            )
        }
        if not user_tables:
            return None
        unexpected = sorted(user_tables - PROJECTION_TABLES)
        if unexpected:
            raise RuntimeError(
                "refusing to initialize a projection journal in a SQLite "
                f"database containing foreign tables: {unexpected}"
            )
        if "longform_projection_meta" not in user_tables:
            raise RuntimeError(
                "refusing to initialize a projection journal in an "
                "existing versionless SQLite database"
            )
        try:
            current = connection.execute(
                """
                SELECT value FROM longform_projection_meta
                WHERE key = 'schema_version'
                """
            ).fetchone()
        except sqlite3.DatabaseError as error:
            raise RuntimeError(
                "long-form projection metadata is invalid"
            ) from error
        if current is None:
            raise RuntimeError(
                "long-form projection schema version is missing"
            )
        try:
            return int(current["value"])
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "long-form projection schema version is invalid"
            ) from error

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            factory=_ClosingConnection,
        )
        try:
            connection.row_factory = sqlite3.Row
            return connection
        except BaseException:
            with suppress(sqlite3.Error):
                connection.close()
            raise

    @staticmethod
    def _validate_projection_name(name: str) -> None:
        if name not in PROJECTION_NAMES:
            raise ValueError(f"unsupported projection: {name}")

    @staticmethod
    def _stable_json(value: Any) -> str:
        normalized = _normalize_for_hash(value, frozenset())
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _new_run(
        self,
        *,
        projection_name: str,
        commit_id: str,
        input_payload: Mapping[str, Any],
        attempt: int,
        retry_of: str | None,
    ) -> str:
        with self._connect() as connection:
            return self._insert_run(
                connection,
                projection_name=projection_name,
                commit_id=commit_id,
                input_payload=input_payload,
                attempt=attempt,
                retry_of=retry_of,
            )

    def _insert_run(
        self,
        connection: sqlite3.Connection,
        *,
        projection_name: str,
        commit_id: str,
        input_payload: Mapping[str, Any],
        attempt: int,
        retry_of: str | None,
        owner: tuple[str, int, str] | None = None,
    ) -> str:
        run_id = uuid.uuid4().hex
        owner_host, owner_pid, owner_token = owner or _current_owner()
        connection.execute(
            """
            INSERT INTO projection_runs(
                run_id, projection_name, commit_id, input_sha256,
                input_json, status, attempt, retry_of, started_at,
                owner_host, owner_pid, owner_token
            ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                projection_name,
                commit_id,
                stable_normalized_hash(input_payload),
                self._stable_json(input_payload),
                attempt,
                retry_of,
                _utc_now(),
                owner_host,
                owner_pid,
                owner_token,
            ),
        )
        return run_id

    def _fail_owned_run(
        self,
        run_id: str,
        error: BaseException,
        *,
        owner: tuple[str, int, str],
    ) -> bool:
        """Fail only the still-running row held by the exact local owner."""

        return self._fail_owned_run_text(
            run_id,
            f"{type(error).__name__}: {error}",
            owner=owner,
        )

    def _fail_owned_run_text(
        self,
        run_id: str,
        error_text: str,
        *,
        owner: tuple[str, int, str],
    ) -> bool:
        owner_host, owner_pid, owner_token = owner
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE projection_runs
                SET status = 'failed', error_text = ?, finished_at = ?
                WHERE run_id = ? AND status = 'running'
                  AND owner_host = ? AND owner_pid = ?
                  AND owner_token = ?
                """,
                (
                    error_text,
                    _utc_now(),
                    run_id,
                    owner_host,
                    owner_pid,
                    owner_token,
                ),
            )
        return updated.rowcount == 1

    def _deferred_failure_key(self, run_id: str) -> tuple[str, str]:
        return (
            str(self.database_path.expanduser().resolve()),
            str(run_id),
        )

    def _best_effort_fail_owned_run(
        self,
        run_id: str,
        error: BaseException,
        *,
        owner: tuple[str, int, str],
    ) -> bool:
        """Preserve the primary exception and defer a lock-blocked cleanup."""

        error_text = f"{type(error).__name__}: {error}"
        key = self._deferred_failure_key(run_id)
        try:
            changed = self._fail_owned_run_text(
                run_id,
                error_text,
                owner=owner,
            )
        except BaseException:
            with _DEFERRED_FAILURE_LOCK:
                _DEFERRED_OWNED_FAILURES[key] = (owner, error_text)
            self._schedule_deferred_owned_failure(key)
            return False
        with _DEFERRED_FAILURE_LOCK:
            _DEFERRED_OWNED_FAILURES.pop(key, None)
        return changed

    def _schedule_deferred_owned_failure(
        self,
        key: tuple[str, str],
    ) -> None:
        """Keep retrying a blocked cleanup even if this journal goes idle."""

        with _DEFERRED_FAILURE_LOCK:
            if (
                key not in _DEFERRED_OWNED_FAILURES
                or key in _DEFERRED_FAILURE_WORKERS
            ):
                return
            worker = threading.Thread(
                target=self._run_deferred_owned_failure_worker,
                args=(key,),
                name=f"projection-cleanup-{key[1][:12]}",
                daemon=True,
            )
            _DEFERRED_FAILURE_WORKERS[key] = worker
            try:
                worker.start()
            except BaseException:
                # Cleanup remains registered for the next journal call. Never
                # let thread construction replace the cancellation/failure
                # that caused this best-effort path.
                if _DEFERRED_FAILURE_WORKERS.get(key) is worker:
                    _DEFERRED_FAILURE_WORKERS.pop(key, None)

    def _run_deferred_owned_failure_worker(
        self,
        key: tuple[str, str],
    ) -> None:
        """CAS a deferred claim to failed after a transient DB lock clears."""

        retry_delay = _DEFERRED_FAILURE_INITIAL_RETRY_SECONDS
        try:
            while True:
                with _DEFERRED_FAILURE_LOCK:
                    pending = _DEFERRED_OWNED_FAILURES.get(key)
                if pending is None:
                    return
                owner, error_text = pending
                try:
                    self._fail_owned_run_text(
                        key[1],
                        error_text,
                        owner=owner,
                    )
                except Exception:
                    time.sleep(retry_delay)
                    retry_delay = min(
                        _DEFERRED_FAILURE_MAX_RETRY_SECONDS,
                        retry_delay * 2,
                    )
                    continue
                with _DEFERRED_FAILURE_LOCK:
                    if _DEFERRED_OWNED_FAILURES.get(key) == pending:
                        _DEFERRED_OWNED_FAILURES.pop(key, None)
                return
        finally:
            current_worker = threading.current_thread()
            with _DEFERRED_FAILURE_LOCK:
                if _DEFERRED_FAILURE_WORKERS.get(key) is current_worker:
                    _DEFERRED_FAILURE_WORKERS.pop(key, None)
                retry_still_pending = key in _DEFERRED_OWNED_FAILURES
            if retry_still_pending:
                self._schedule_deferred_owned_failure(key)

    def _drain_deferred_owned_failures(self) -> None:
        database_key = str(self.database_path.expanduser().resolve())
        with _DEFERRED_FAILURE_LOCK:
            pending = [
                (key, value)
                for key, value in _DEFERRED_OWNED_FAILURES.items()
                if key[0] == database_key
            ]
        for key, (owner, error_text) in pending:
            try:
                self._fail_owned_run_text(
                    key[1],
                    error_text,
                    owner=owner,
                )
            except Exception:
                self._schedule_deferred_owned_failure(key)
                continue
            with _DEFERRED_FAILURE_LOCK:
                current = _DEFERRED_OWNED_FAILURES.get(key)
                if current == (owner, error_text):
                    _DEFERRED_OWNED_FAILURES.pop(key, None)
                worker = _DEFERRED_FAILURE_WORKERS.get(key)
            if (
                worker is not None
                and worker is not threading.current_thread()
            ):
                worker.join(timeout=_DEFERRED_FAILURE_JOIN_SECONDS)

    def _wait_for_run_output(
        self,
        run_id: str,
        *,
        timeout_seconds: float,
    ) -> tuple[dict[str, Any] | None, int | None]:
        timeout = max(0.0, float(timeout_seconds))
        deadline = time.monotonic() + timeout
        next_owner_probe = 0.0
        while True:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT
                        runs.projection_name,
                        runs.commit_id,
                        runs.status,
                        runs.attempt,
                        runs.error_text,
                        runs.owner_host,
                        runs.owner_pid,
                        runs.owner_token,
                        outputs.output_sha256,
                        outputs.output_json
                    FROM projection_runs AS runs
                    LEFT JOIN projection_outputs AS outputs
                      ON outputs.projection_name = runs.projection_name
                     AND outputs.commit_id = runs.commit_id
                     AND outputs.input_sha256 = runs.input_sha256
                    WHERE runs.run_id = ?
                    """,
                    (run_id,),
                ).fetchone()
            if row is None:
                raise ProjectionRunError(
                    run_id,
                    "concurrent projection run no longer exists",
                )
            status = str(row["status"] or "")
            if status == "succeeded" and row["output_json"] is not None:
                return (
                    {
                        "run_id": None,
                        "projection_name": str(row["projection_name"]),
                        "commit_id": str(row["commit_id"]),
                        "status": "cached",
                        "output_sha256": str(row["output_sha256"]),
                        "output": json.loads(row["output_json"]),
                    },
                    None,
                )

            if status == "succeeded":
                raise ProjectionRunError(
                    run_id,
                    "concurrent projection run succeeded without a cached output",
                )
            if status != "running":
                detail = str(row["error_text"] or "").strip()
                if status == "failed" and detail.startswith("interrupted:"):
                    return None, int(row["attempt"])
                suffix = f": {detail}" if detail else ""
                raise ProjectionRunError(
                    run_id,
                    f"concurrent projection run finished as {status}{suffix}",
                )

            now = time.monotonic()
            if now >= next_owner_probe:
                owner_state, _owner_reason = _owner_state(
                    str(row["owner_host"] or "").strip().casefold(),
                    int(row["owner_pid"] or 0),
                    str(row["owner_token"] or "").strip(),
                )
                if owner_state == "dead":
                    recovered = self.recover_interrupted_runs(
                        run_ids=(run_id,)
                    )
                    if any(
                        str(item.get("run_id") or "") == run_id
                        for item in recovered
                    ):
                        return None, int(row["attempt"])
                next_owner_probe = (
                    now + max(0.0, PROJECTION_OWNER_PROBE_SECONDS)
                )

            remaining = deadline - now
            if remaining <= 0:
                raise TimeoutError(
                    "timed out waiting for concurrent projection run "
                    f"{run_id} after {timeout:.1f} seconds"
                )
            time.sleep(min(PROJECTION_WAIT_POLL_SECONDS, remaining))

    def recover_interrupted_runs(
        self,
        *,
        run_ids: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Turn provably abandoned ``running`` rows into retryable failures.

        New rows carry their local owner PID and are recovered automatically
        after that process exits. Legacy rows without owner metadata are only
        changed when their exact run IDs are supplied by an operator. When
        ``run_ids`` is non-empty, inspection and mutation are limited to those
        exact rows so an explicit recovery cannot alter unrelated runs.
        """

        requested = {
            str(run_id).strip()
            for run_id in (run_ids or ())
            if str(run_id).strip()
        }
        recovered: list[dict[str, Any]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                parameters: list[Any] = []
                requested_filter = ""
                status_filter = "status = 'running'"
                if requested:
                    placeholders = ", ".join("?" for _ in requested)
                    requested_filter = f"run_id IN ({placeholders})"
                    parameters.extend(sorted(requested))
                    # Exact recovery is idempotent. Another journal may have
                    # recovered this same row after the caller inspected it
                    # but before this transaction acquired the write lock.
                    # Include terminal interrupted rows so their persisted
                    # reason and timestamp can reconstruct the exact receipt.
                    status_filter = (
                        f"{requested_filter} AND ("
                        "status = 'running' OR "
                        "(status = 'failed' "
                        "AND error_text LIKE 'interrupted:%'))"
                    )
                rows = connection.execute(
                    f"""
                    SELECT run_id, projection_name, commit_id,
                           status, error_text, finished_at,
                           owner_host, owner_pid, owner_token
                    FROM projection_runs
                    WHERE {status_filter}
                    ORDER BY started_at, run_id
                    """,
                    parameters,
                ).fetchall()
                for row in rows:
                    run_id = str(row["run_id"])
                    if str(row["status"] or "") == "failed":
                        recovered.append(
                            {
                                "run_id": run_id,
                                "projection_name": str(
                                    row["projection_name"]
                                ),
                                "commit_id": str(row["commit_id"]),
                                "status": "failed",
                                "reason": str(row["error_text"] or ""),
                                "finished_at": str(
                                    row["finished_at"] or ""
                                ),
                            }
                        )
                        continue
                    explicit = run_id in requested
                    owner_host = str(
                        row["owner_host"] or ""
                    ).strip().casefold()
                    owner_pid = int(row["owner_pid"] or 0)
                    owner_token = str(row["owner_token"] or "").strip()
                    legacy_ownerless = bool(
                        not owner_host
                        and owner_pid <= 0
                        and not owner_token
                    )
                    owner_state, owner_reason = _owner_state(
                        owner_host,
                        owner_pid,
                        owner_token,
                    )
                    if owner_state != "dead" and not (
                        explicit and legacy_ownerless
                    ):
                        continue
                    reason = (
                        "interrupted: exact legacy ownerless run was "
                        "explicitly recovered"
                        if explicit and legacy_ownerless
                        else f"interrupted: {owner_reason}"
                    )
                    finished_at = _utc_now()
                    updated = connection.execute(
                        """
                        UPDATE projection_runs
                        SET status = 'failed', error_text = ?, finished_at = ?
                        WHERE run_id = ? AND status = 'running'
                          AND owner_host IS ? AND owner_pid IS ?
                          AND owner_token IS ?
                        """,
                        (
                            reason,
                            finished_at,
                            run_id,
                            row["owner_host"],
                            row["owner_pid"],
                            row["owner_token"],
                        ),
                    )
                    if updated.rowcount:
                        recovered.append(
                            {
                                "run_id": run_id,
                                "projection_name": str(
                                    row["projection_name"]
                                ),
                                "commit_id": str(row["commit_id"]),
                                "status": "failed",
                                "reason": reason,
                                "finished_at": finished_at,
                            }
                        )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return recovered

    def _execute(
        self,
        *,
        run_id: str,
        projection_name: str,
        commit_id: str,
        input_payload: Mapping[str, Any],
        projector: Callable[[Mapping[str, Any]], Any],
    ) -> dict[str, Any]:
        owner_host, owner_pid, owner_token = _current_owner()
        try:
            output = projector(input_payload)
            output_sha256 = stable_normalized_hash(output)
            reported_status = (
                str(output.get("status") or "").casefold()
                if isinstance(output, Mapping)
                else ""
            )
            run_status = (
                reported_status
                if reported_status in {"degraded", "failed"}
                else "succeeded"
            )
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                input_sha256 = connection.execute(
                    """
                    SELECT input_sha256 FROM projection_runs WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()
                if input_sha256 is None:
                    connection.rollback()
                    raise _ProjectionOwnershipLost(
                        run_id,
                        "projection run no longer exists",
                    )
                updated = connection.execute(
                    """
                    UPDATE projection_runs
                    SET status = ?, output_sha256 = ?,
                        finished_at = ?, error_text = ?
                    WHERE run_id = ? AND status = 'running'
                      AND owner_host = ? AND owner_pid = ?
                      AND owner_token = ?
                    """,
                    (
                        run_status,
                        output_sha256,
                        _utc_now(),
                        (
                            f"projector reported {run_status}"
                            if run_status != "succeeded"
                            else None
                        ),
                        run_id,
                        owner_host,
                        owner_pid,
                        owner_token,
                    ),
                )
                if updated.rowcount != 1:
                    connection.rollback()
                    raise _ProjectionOwnershipLost(
                        run_id,
                        "projection run lost ownership before completion",
                    )
                if run_status == "succeeded":
                    connection.execute(
                        """
                        INSERT INTO projection_outputs(
                            projection_name, commit_id, input_sha256,
                            output_sha256, output_json, generated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(projection_name, commit_id, input_sha256)
                        DO UPDATE SET
                            output_sha256 = excluded.output_sha256,
                            output_json = excluded.output_json,
                            generated_at = excluded.generated_at
                        """,
                        (
                            projection_name,
                            commit_id,
                            input_sha256["input_sha256"],
                            output_sha256,
                            self._stable_json(output),
                            _utc_now(),
                        ),
                    )
                connection.commit()
            return {
                "run_id": run_id,
                "projection_name": projection_name,
                "commit_id": commit_id,
                "status": run_status,
                "output_sha256": output_sha256,
                "output": output,
            }
        except _ProjectionOwnershipLost:
            raise
        except BaseException as error:
            self._best_effort_fail_owned_run(
                run_id,
                error,
                owner=(owner_host, owner_pid, owner_token),
            )
            if not isinstance(error, Exception):
                raise
            raise ProjectionRunError(run_id, str(error)) from error

    def run(
        self,
        projection_name: str,
        commit: Mapping[str, Any],
        projector: Callable[[Mapping[str, Any]], Any],
        *,
        force: bool = False,
        wait_timeout_seconds: float = PROJECTION_WAIT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        self._validate_projection_name(projection_name)
        self._drain_deferred_owned_failures()
        operation = str(commit.get("operation") or "accept")
        canon_status = str(commit.get("canon_status") or "")
        if not (
            canon_status == "accepted"
            or (operation == "retract" and canon_status == "retracted")
        ):
            raise ValueError(
                "derived projections require an accepted lifecycle commit"
            )
        commit_id = str(commit.get("commit_id") or "")
        if not commit_id:
            raise ValueError("accepted commit requires commit_id")
        input_sha256 = stable_normalized_hash(commit)
        wait_deadline = time.monotonic() + max(
            0.0,
            float(wait_timeout_seconds),
        )
        takeover_after_attempt: int | None = None
        takeover_retry_of: str | None = None
        while True:
            cached_result: dict[str, Any] | None = None
            following_run_id: str | None = None
            run_id: str | None = None
            claimed_owner: tuple[str, int, str] | None = None
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        successor = (
                            connection.execute(
                                """
                                SELECT run_id
                                FROM projection_runs
                                WHERE projection_name = ? AND commit_id = ?
                                  AND input_sha256 = ? AND attempt > ?
                                ORDER BY attempt DESC, started_at DESC,
                                         run_id DESC
                                LIMIT 1
                                """,
                                (
                                    projection_name,
                                    commit_id,
                                    input_sha256,
                                    takeover_after_attempt,
                                ),
                            ).fetchone()
                            if takeover_after_attempt is not None
                            else None
                        )
                        existing = (
                            connection.execute(
                                """
                                SELECT output_sha256, output_json
                                FROM projection_outputs
                                WHERE projection_name = ? AND commit_id = ?
                                  AND input_sha256 = ?
                                """,
                                (projection_name, commit_id, input_sha256),
                            ).fetchone()
                            if takeover_after_attempt is None and not force
                            else None
                        )
                        if successor is not None:
                            following_run_id = str(successor["run_id"])
                        elif existing is not None:
                            cached_result = {
                                "run_id": None,
                                "projection_name": projection_name,
                                "commit_id": commit_id,
                                "status": "cached",
                                "output_sha256": str(
                                    existing["output_sha256"]
                                ),
                                "output": json.loads(existing["output_json"]),
                            }
                        else:
                            running = connection.execute(
                                """
                                SELECT run_id
                                FROM projection_runs
                                WHERE projection_name = ? AND commit_id = ?
                                  AND input_sha256 = ? AND status = 'running'
                                ORDER BY attempt DESC, started_at DESC,
                                         run_id DESC
                                LIMIT 1
                                """,
                                (projection_name, commit_id, input_sha256),
                            ).fetchone()
                            if running is not None:
                                following_run_id = str(running["run_id"])
                            else:
                                attempt = 1
                                if takeover_after_attempt is not None:
                                    max_attempt = connection.execute(
                                        """
                                        SELECT COALESCE(MAX(attempt), 0)
                                               AS max_attempt
                                        FROM projection_runs
                                        WHERE projection_name = ?
                                          AND commit_id = ?
                                          AND input_sha256 = ?
                                        """,
                                        (
                                            projection_name,
                                            commit_id,
                                            input_sha256,
                                        ),
                                    ).fetchone()
                                    attempt = (
                                        int(max_attempt["max_attempt"]) + 1
                                    )
                                claimed_owner = _current_owner()
                                run_id = self._insert_run(
                                    connection,
                                    projection_name=projection_name,
                                    commit_id=commit_id,
                                    input_payload=commit,
                                    attempt=attempt,
                                    retry_of=takeover_retry_of,
                                    owner=claimed_owner,
                                )
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                if cached_result is not None:
                    return cached_result
                if following_run_id is not None:
                    waited, interrupted_attempt = self._wait_for_run_output(
                        following_run_id,
                        timeout_seconds=max(
                            0.0,
                            wait_deadline - time.monotonic(),
                        ),
                    )
                    if waited is not None:
                        return waited
                    takeover_after_attempt = interrupted_attempt
                    takeover_retry_of = following_run_id
                    continue
                if run_id is None:
                    raise RuntimeError(
                        "projection run claim did not select an owner"
                    )
                return self._execute(
                    run_id=run_id,
                    projection_name=projection_name,
                    commit_id=commit_id,
                    input_payload=commit,
                    projector=projector,
                )
            except BaseException as error:
                if run_id is not None and claimed_owner is not None:
                    self._best_effort_fail_owned_run(
                        run_id,
                        error,
                        owner=claimed_owner,
                    )
                raise

    def retry(
        self,
        run_id: str,
        projector: Callable[[Mapping[str, Any]], Any],
        *,
        wait_for_running: bool = False,
        wait_timeout_seconds: float = PROJECTION_WAIT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        self._drain_deferred_owned_failures()
        wait_deadline = time.monotonic() + max(
            0.0,
            float(wait_timeout_seconds),
        )
        takeover_after_attempt: int | None = None
        while True:
            following_run_id: str | None = None
            retry_run_id: str | None = None
            projection_name = ""
            commit_id = ""
            input_payload: Mapping[str, Any] | None = None
            claimed_owner: tuple[str, int, str] | None = None
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        failed = connection.execute(
                            "SELECT * FROM projection_runs WHERE run_id = ?",
                            (run_id,),
                        ).fetchone()
                        if failed is None:
                            raise KeyError(
                                f"projection run not found: {run_id}"
                            )
                        if failed["status"] not in {"failed", "degraded"}:
                            raise ValueError(
                                "only a failed or degraded projection run "
                                "can be retried"
                            )
                        successor = (
                            connection.execute(
                                """
                                SELECT run_id
                                FROM projection_runs
                                WHERE projection_name = ? AND commit_id = ?
                                  AND input_sha256 = ? AND attempt > ?
                                ORDER BY attempt DESC, started_at DESC,
                                         run_id DESC
                                LIMIT 1
                                """,
                                (
                                    failed["projection_name"],
                                    failed["commit_id"],
                                    failed["input_sha256"],
                                    takeover_after_attempt,
                                ),
                            ).fetchone()
                            if takeover_after_attempt is not None
                            else None
                        )
                        cached = (
                            connection.execute(
                                """
                                SELECT outputs.output_sha256,
                                       outputs.output_json
                                FROM projection_outputs AS outputs
                                WHERE outputs.projection_name = ?
                                  AND outputs.commit_id = ?
                                  AND outputs.input_sha256 = ?
                                  AND (
                                    outputs.generated_at > COALESCE(?, '')
                                    OR EXISTS(
                                        SELECT 1
                                        FROM projection_runs AS completed
                                        WHERE completed.projection_name =
                                              outputs.projection_name
                                          AND completed.commit_id =
                                              outputs.commit_id
                                          AND completed.input_sha256 =
                                              outputs.input_sha256
                                          AND completed.status = 'succeeded'
                                          AND completed.retry_of = ?
                                    )
                                  )
                                """,
                                (
                                    failed["projection_name"],
                                    failed["commit_id"],
                                    failed["input_sha256"],
                                    failed["finished_at"]
                                    or failed["started_at"],
                                    run_id,
                                ),
                            ).fetchone()
                            if successor is None
                            and takeover_after_attempt is None
                            else None
                        )
                        if successor is not None:
                            following_run_id = str(successor["run_id"])
                        elif cached is not None:
                            connection.commit()
                            return {
                                "run_id": None,
                                "projection_name": str(
                                    failed["projection_name"]
                                ),
                                "commit_id": str(failed["commit_id"]),
                                "status": "cached",
                                "output_sha256": str(
                                    cached["output_sha256"]
                                ),
                                "output": json.loads(
                                    cached["output_json"]
                                ),
                            }
                        else:
                            running = connection.execute(
                                """
                                SELECT run_id FROM projection_runs
                                WHERE projection_name = ? AND commit_id = ?
                                  AND input_sha256 = ?
                                  AND status = 'running'
                                ORDER BY attempt DESC, started_at DESC,
                                         run_id DESC
                                LIMIT 1
                                """,
                                (
                                    failed["projection_name"],
                                    failed["commit_id"],
                                    failed["input_sha256"],
                                ),
                            ).fetchone()
                            if running is not None:
                                if not wait_for_running:
                                    raise ValueError(
                                        "projection input already has a "
                                        "running retry: "
                                        + str(running["run_id"])
                                    )
                                following_run_id = str(running["run_id"])
                            else:
                                max_attempt = connection.execute(
                                    """
                                    SELECT COALESCE(MAX(attempt), 0)
                                           AS max_attempt
                                    FROM projection_runs
                                    WHERE projection_name = ?
                                      AND commit_id = ?
                                      AND input_sha256 = ?
                                    """,
                                    (
                                        failed["projection_name"],
                                        failed["commit_id"],
                                        failed["input_sha256"],
                                    ),
                                ).fetchone()
                                input_payload = json.loads(
                                    failed["input_json"]
                                )
                                projection_name = str(
                                    failed["projection_name"]
                                )
                                commit_id = str(failed["commit_id"])
                                claimed_owner = _current_owner()
                                retry_run_id = self._insert_run(
                                    connection,
                                    projection_name=projection_name,
                                    commit_id=commit_id,
                                    input_payload=input_payload,
                                    attempt=(
                                        int(max_attempt["max_attempt"]) + 1
                                    ),
                                    retry_of=run_id,
                                    owner=claimed_owner,
                                )
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                if following_run_id is not None:
                    waited, interrupted_attempt = self._wait_for_run_output(
                        following_run_id,
                        timeout_seconds=max(
                            0.0,
                            wait_deadline - time.monotonic(),
                        ),
                    )
                    if waited is not None:
                        return waited
                    takeover_after_attempt = interrupted_attempt
                    continue
                if retry_run_id is None or input_payload is None:
                    raise RuntimeError(
                        "projection retry claim did not select an owner"
                    )
                return self._execute(
                    run_id=retry_run_id,
                    projection_name=projection_name,
                    commit_id=commit_id,
                    input_payload=input_payload,
                    projector=projector,
                )
            except BaseException as error:
                if retry_run_id is not None and claimed_owner is not None:
                    self._best_effort_fail_owned_run(
                        retry_run_id,
                        error,
                        owner=claimed_owner,
                    )
                raise

    def replay(
        self,
        commit: Mapping[str, Any],
        projectors: Mapping[str, Callable[[Mapping[str, Any]], Any]],
        *,
        names: Iterable[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        selected = tuple(names or PROJECTION_NAMES)
        results: dict[str, dict[str, Any]] = {}
        for name in selected:
            self._validate_projection_name(name)
            if name not in projectors:
                raise KeyError(f"missing projector callback: {name}")
            results[name] = self.run(
                name,
                commit,
                projectors[name],
                force=True,
            )
        return results

    @staticmethod
    def _run_select(include_payload: bool) -> str:
        if include_payload:
            return "*"
        return """
            run_id, projection_name, commit_id, input_sha256,
            status, attempt, retry_of, output_sha256,
            CASE
                WHEN error_text IS NULL THEN NULL
                ELSE SUBSTR(error_text, 1, 512)
            END AS error_text,
            LENGTH(CAST(error_text AS BLOB)) AS error_bytes,
            CASE
                WHEN LENGTH(error_text) > 512 THEN 1
                ELSE 0
            END AS error_truncated,
            started_at, finished_at, owner_host, owner_pid, owner_token,
            LENGTH(CAST(input_json AS BLOB)) AS input_bytes
        """

    def runs(
        self,
        projection_name: str | None = None,
        *,
        include_payload: bool = True,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        bounded_limit = None if limit is None else max(0, int(limit))
        if bounded_limit == 0:
            return []
        columns = self._run_select(include_payload)
        direction = "DESC" if newest_first else "ASC"
        where = "WHERE projection_name = ?" if projection_name else ""
        parameters: list[Any] = [projection_name] if projection_name else []
        limit_sql = ""
        if bounded_limit is not None:
            limit_sql = " LIMIT ?"
            parameters.append(bounded_limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {columns}
                FROM projection_runs
                {where}
                ORDER BY started_at {direction}, run_id {direction}
                {limit_sql}
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def run_snapshot(
        self,
        projection_name: str | None = None,
        *,
        include_payload: bool = True,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        bounded_limit = None if limit is None else max(0, int(limit))
        if bounded_limit == 0:
            return [], self.run_count(projection_name)
        columns = self._run_select(include_payload)
        direction = "DESC" if newest_first else "ASC"
        where = "WHERE projection_name = ?" if projection_name else ""
        parameters: list[Any] = [projection_name] if projection_name else []
        limit_sql = ""
        if bounded_limit is not None:
            limit_sql = " LIMIT ?"
            parameters.append(bounded_limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {columns}, COUNT(*) OVER() AS snapshot_total_count
                FROM projection_runs
                {where}
                ORDER BY started_at {direction}, run_id {direction}
                {limit_sql}
                """,
                parameters,
            ).fetchall()
        total_count = (
            int(rows[0]["snapshot_total_count"])
            if rows
            else 0
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item.pop("snapshot_total_count", None)
            result.append(item)
        return result, total_count

    def inspect_run(
        self,
        run_id: str,
        *,
        include_payload: bool = False,
    ) -> dict[str, Any]:
        requested = str(run_id or "").strip()
        if not requested:
            raise ValueError("projection run inspection requires run_id")
        columns = self._run_select(include_payload)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT {columns}
                FROM projection_runs
                WHERE run_id = ?
                """,
                (requested,),
            ).fetchone()
        if row is None:
            raise KeyError(f"projection run not found: {requested}")
        return dict(row)

    def latest_succeeded_run(
        self,
        *,
        projection_name: str,
        commit_id: str,
        input_sha256: str,
        retry_of: str | None = None,
    ) -> dict[str, Any] | None:
        self._validate_projection_name(projection_name)
        columns = self._run_select(False)
        retry_filter = "AND retry_of = ?" if retry_of is not None else ""
        parameters: list[Any] = [
            projection_name,
            commit_id,
            input_sha256,
        ]
        if retry_of is not None:
            parameters.append(retry_of)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT {columns}
                FROM projection_runs
                WHERE projection_name = ?
                  AND commit_id = ?
                  AND input_sha256 = ?
                  AND status = 'succeeded'
                  {retry_filter}
                ORDER BY attempt DESC, started_at DESC, run_id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        return dict(row) if row is not None else None

    def run_count(self, projection_name: str | None = None) -> int:
        with self._connect() as connection:
            if projection_name:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM projection_runs
                    WHERE projection_name = ?
                    """,
                    (projection_name,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM projection_runs"
                ).fetchone()
        return int(row["count"])

    def prune_derived_runs(self, *, keep_successful_per_projection: int = 20) -> int:
        """Bound only derived run history; accepted commits live outside this DB."""

        keep = max(1, int(keep_successful_per_projection))
        deleted = 0
        with self._connect() as connection:
            for name in PROJECTION_NAMES:
                rows = connection.execute(
                    """
                    SELECT run_id FROM projection_runs
                    WHERE projection_name = ? AND status = 'succeeded'
                    ORDER BY finished_at DESC, run_id DESC
                    """,
                    (name,),
                ).fetchall()
                stale = [row["run_id"] for row in rows[keep:]]
                if stale:
                    connection.executemany(
                        "DELETE FROM projection_runs WHERE run_id = ?",
                        ((run_id,) for run_id in stale),
                    )
                    deleted += len(stale)
        return deleted
