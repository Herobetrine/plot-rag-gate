"""Public service API for proposal-only story initialization."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from .canonical import (
    canonical_hash,
    normalize_real_path,
    path_is_within,
    sha256_bytes,
    stable_id,
    utc_now,
)
from .engine import (
    apply_answers,
    build_proposal,
    create_initial_state,
    drive_state,
    public_session,
    response_for_state,
)
from .errors import PlotInitError
from .inventory import inventory_sources
from .remote_cache import (
    DEFAULT_MAX_AGE_SECONDS,
    DEFAULT_MAX_ENTRIES,
    MemoryRemoteResponseCache,
    RemoteResponseCache,
    SQLiteRemoteResponseCache,
)
from .storage import InitStorage


class PlotInitService:
    """Orchestrates sessions without importing or mutating the plot-state runtime."""

    def __init__(
        self,
        workspace_root: Path | str,
        *,
        database_path: Path | str | None = None,
        remote_cache_max_entries: int = DEFAULT_MAX_ENTRIES,
        remote_cache_max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
        self.database_path = (
            Path(database_path).expanduser().resolve(strict=False)
            if database_path is not None
            else self.workspace_root / ".plot-rag-init" / "init.sqlite3"
        )
        self.storage = InitStorage(self.database_path)
        self.remote_cache: RemoteResponseCache = SQLiteRemoteResponseCache(
            self.database_path,
            max_entries=remote_cache_max_entries,
            max_age_seconds=remote_cache_max_age_seconds,
        )

    def _bound_remote_cache(
        self,
        *,
        session_id: str,
        stage: str = "CLASSIFY_EXTRACT",
        one_shot: bool = False,
    ) -> RemoteResponseCache:
        if one_shot:
            return MemoryRemoteResponseCache(
                max_entries=self.remote_cache.max_entries,
                max_age_seconds=self.remote_cache.max_age_seconds,
                session_id=session_id,
                stage=stage,
            )
        return self.remote_cache.bind(session_id=session_id, stage=stage)

    def remote_cache_for_session(
        self,
        session_id: str,
        *,
        stage: str,
    ) -> RemoteResponseCache:
        """Return the cache adapter used by an existing remote-capable stage."""

        normalized_stage = str(stage or "").strip().upper()
        if normalized_stage not in {"CLASSIFY", "EXTRACT"}:
            raise PlotInitError(
                "INVALID_REMOTE_CACHE_STAGE",
                "remote cache session binding supports CLASSIFY or EXTRACT",
                stage=stage,
            )
        self.storage.load_session(session_id)
        return self._bound_remote_cache(
            session_id=session_id,
            stage=normalized_stage,
        )

    @staticmethod
    def _require_idempotency_key(value: str) -> str:
        key = str(value or "").strip()
        if not key:
            raise PlotInitError(
                "IDEMPOTENCY_KEY_REQUIRED",
                "mutating initialization operations require an idempotency key",
            )
        if len(key) > 256:
            raise PlotInitError(
                "INVALID_IDEMPOTENCY_KEY",
                "idempotency key exceeds 256 characters",
            )
        return key

    @staticmethod
    def _require_revision(value: Any, field: str) -> int:
        if type(value) is not int or value < 0:
            raise PlotInitError(
                "INVALID_REVISION",
                f"{field} must be a non-negative integer",
                field=field,
                received_type=type(value).__name__,
            )
        return value

    def _project_root(self, value: Path | str | None) -> Path | None:
        if value is None or not str(value).strip():
            return None
        project_root = Path(value).expanduser().resolve(strict=False)
        if not path_is_within(project_root, self.workspace_root):
            raise PlotInitError(
                "UNSAFE_PROJECT_ROOT",
                "initialization target must stay within the workspace root",
                workspace_root=str(self.workspace_root),
                project_root=str(project_root),
            )
        return project_root

    @staticmethod
    def _source_paths(
        sources: Iterable[Path | str] | None,
        *,
        project_root: Path | None,
        mode: str,
    ) -> list[Path]:
        values: list[Path] = []
        for raw in sources or []:
            path = Path(raw).expanduser().resolve(strict=False)
            if path not in values:
                values.append(path)
        if (
            not values
            and project_root is not None
            and project_root.exists()
            and mode in {"auto", "ingest", "hybrid"}
        ):
            values.append(project_root)
        return values

    @staticmethod
    def _read_seed_file(path: Path | str) -> Any:
        source = Path(path).expanduser().resolve(strict=True)
        raw = source.read_bytes()
        text: str | None = None
        for encoding in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise PlotInitError(
                "SEED_ENCODING_UNSUPPORTED",
                f"seed file is not UTF-8, GBK, or GB18030 text: {source}",
            )
        if source.suffix.casefold() == ".json":
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise PlotInitError(
                    "INVALID_SEED_JSON",
                    f"seed JSON line {exc.lineno}, column {exc.colno}: {exc.msg}",
                ) from exc
        return text

    @classmethod
    def _seed_value(cls, seed: Any, seed_file: Path | str | None) -> Any:
        if seed_file is None:
            return copy.deepcopy(seed)
        file_value = cls._read_seed_file(seed_file)
        if seed is None:
            return file_value
        if isinstance(seed, dict) and isinstance(file_value, dict):
            merged = copy.deepcopy(file_value)
            merged.update(copy.deepcopy(seed))
            return merged
        return f"{file_value}\n\n{seed}"

    @staticmethod
    def _storage_signature(path: Path) -> tuple[bool, int, int, str]:
        """Hash a SQLite source file without opening it through SQLite."""

        if not path.is_file():
            return False, 0, 0, ""
        stat = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return True, int(stat.st_size), int(stat.st_mtime_ns), digest.hexdigest()

    @classmethod
    def _canon_revision_from_snapshot(cls, state_database: Path) -> int:
        """Read canon revision from a private main/WAL snapshot.

        SQLite may create or mutate ``-wal``/``-shm`` files even for a
        ``mode=ro`` connection. Initialization dry-run must be byte-stable, so
        it never opens the project database itself.
        """

        source = state_database.resolve()
        source_paths = (
            source,
            Path(str(source) + "-wal"),
            Path(str(source) + "-shm"),
        )
        last_error: Exception | None = None
        for _attempt in range(3):
            before = tuple(cls._storage_signature(path) for path in source_paths)
            if not before[0][0]:
                return 0
            with tempfile.TemporaryDirectory(
                prefix="plot-rag-init-canon-guard-"
            ) as temporary:
                snapshot = Path(temporary) / source.name
                try:
                    shutil.copyfile(source, snapshot)
                    if before[1][0]:
                        shutil.copyfile(
                            source_paths[1],
                            Path(str(snapshot) + "-wal"),
                        )
                except OSError as exc:
                    last_error = exc
                    continue
                after = tuple(
                    cls._storage_signature(path) for path in source_paths
                )
                if before != after:
                    last_error = RuntimeError(
                        "state database changed while taking canon guard snapshot"
                    )
                    continue
                uri = snapshot.resolve().as_uri() + "?mode=ro"
                try:
                    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
                    try:
                        row = connection.execute(
                            "SELECT value FROM state_meta "
                            "WHERE key='active_canon_revision'"
                        ).fetchone()
                    finally:
                        connection.close()
                    return int(row[0]) if row is not None else 0
                except (sqlite3.Error, TypeError, ValueError) as exc:
                    last_error = exc
                    break
        if last_error is not None:
            raise PlotInitError(
                "CANON_GUARD_SNAPSHOT_FAILED",
                "could not read a stable canon revision snapshot",
                reason=str(last_error),
            ) from last_error
        return 0

    @staticmethod
    def _canon_guard(project_root: Path | None) -> dict[str, Any]:
        if project_root is None:
            return {
                "canon_revision": 0,
                "authority_config_hash": None,
                "current_projection_hash": None,
            }
        config_path = project_root / ".plot-rag" / "config.json"
        snapshot_path = project_root / ".plot-rag" / "state_snapshot.json"
        revision_path = project_root / ".plot-rag" / "canon_revision.json"
        state_database = project_root / ".plot-rag" / "state.sqlite3"
        canon_revision = 0
        if state_database.is_file():
            try:
                canon_revision = PlotInitService._canon_revision_from_snapshot(
                    state_database
                )
            except (OSError, PlotInitError):
                canon_revision = 0
        elif revision_path.is_file():
            try:
                payload = json.loads(revision_path.read_text(encoding="utf-8-sig"))
                if isinstance(payload, dict):
                    canon_revision = int(payload.get("canon_revision") or 0)
                else:
                    canon_revision = int(payload)
            except (ValueError, OSError, json.JSONDecodeError):
                canon_revision = 0
        elif config_path.is_file():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8-sig"))
                if isinstance(config, dict):
                    canon_revision = int(config.get("canon_revision") or 0)
            except (ValueError, OSError, json.JSONDecodeError):
                canon_revision = 0
        return {
            "canon_revision": canon_revision,
            "authority_config_hash": (
                sha256_bytes(config_path.read_bytes()) if config_path.is_file() else None
            ),
            "current_projection_hash": (
                sha256_bytes(snapshot_path.read_bytes())
                if snapshot_path.is_file()
                else None
            ),
        }

    def _request_material(
        self,
        *,
        project_root: Path | None,
        mode: str,
        target_profile: str,
        interaction_profile: str,
        seed: Any,
        source_paths: list[Path],
        expected_canon_revision: int,
        bundle_schema_version: str,
    ) -> dict[str, Any]:
        return {
            "workspace_root": normalize_real_path(self.workspace_root),
            "project_root": normalize_real_path(project_root) if project_root else None,
            "mode": mode,
            "target_profile": target_profile,
            "interaction_profile": interaction_profile,
            "seed": seed,
            "source_paths": [normalize_real_path(path) for path in source_paths],
            "expected_canon_revision": int(expected_canon_revision),
            "bundle_schema_version": str(bundle_schema_version),
        }

    @staticmethod
    def _ensure_revision(state: dict[str, Any], expected: int) -> None:
        expected = PlotInitService._require_revision(
            expected,
            "expected_session_revision",
        )
        actual = int(state["session_revision"])
        if actual != expected:
            raise PlotInitError(
                "SESSION_REVISION_CONFLICT",
                "initialization session revision changed",
                expected_session_revision=expected,
                actual_session_revision=actual,
            )

    def _staleness(self, state: dict[str, Any]) -> dict[str, Any]:
        project_root = (
            Path(state["project_root"]).resolve(strict=False)
            if state.get("project_root")
            else None
        )
        current_guard = self._canon_guard(project_root)
        expected_guard = state.get("canon_guard") or {}
        expected_canon_revision = int(state.get("expected_canon_revision") or 0)
        canon_changed = int(current_guard["canon_revision"]) != expected_canon_revision
        authority_changed = (
            expected_guard.get("authority_config_hash")
            != current_guard.get("authority_config_hash")
        )
        projection_changed = (
            expected_guard.get("current_projection_hash")
            != current_guard.get("current_projection_hash")
        )
        source_diff = {
            "added": [],
            "changed": [],
            "removed": [],
            "unchanged": [],
        }
        source_changed = False
        if state.get("source_paths"):
            inventory = inventory_sources(
                [Path(value) for value in state.get("source_paths") or []],
                previous_manifest=list(state.get("source_manifest") or []),
            )
            source_diff = inventory["source_diff"]
            source_changed = bool(
                source_diff["added"]
                or source_diff["changed"]
                or source_diff["removed"]
            )
        if canon_changed or authority_changed or projection_changed:
            status = "STALE_CANON"
        elif source_changed:
            status = "STALE_SOURCE"
        else:
            status = "CURRENT"
        return {
            "status": status,
            "source_changed": source_changed,
            "source_diff": source_diff,
            "canon_changed": canon_changed,
            "authority_config_changed": authority_changed,
            "current_projection_changed": projection_changed,
            "expected_canon_revision": expected_canon_revision,
            "actual_canon_revision": int(current_guard["canon_revision"]),
        }

    def dry_run(
        self,
        *,
        project_root: Path | str | None = None,
        mode: str = "auto",
        target_profile: str = "plot_ready",
        interaction_profile: str = "balanced",
        seed: Any = None,
        seed_file: Path | str | None = None,
        sources: Iterable[Path | str] | None = None,
        expected_canon_revision: int | None = None,
        bundle_schema_version: str = "auto",
    ) -> dict[str, Any]:
        """Build an in-memory report. This method never creates a DB, session, or file."""

        target = self._project_root(project_root)
        seed_value = self._seed_value(seed, seed_file)
        source_paths = self._source_paths(sources, project_root=target, mode=mode)
        guard = self._canon_guard(target)
        canon_revision = (
            self._require_revision(
                expected_canon_revision,
                "expected_canon_revision",
            )
            if expected_canon_revision is not None
            else int(guard["canon_revision"])
        )
        material = self._request_material(
            project_root=target,
            mode=mode,
            target_profile=target_profile,
            interaction_profile=interaction_profile,
            seed=seed_value,
            source_paths=source_paths,
            expected_canon_revision=canon_revision,
            bundle_schema_version=bundle_schema_version,
        )
        state = create_initial_state(
            session_id=stable_id("dryrun", material),
            workspace_root=self.workspace_root,
            project_root=target,
            mode=mode,
            target_profile=target_profile,
            interaction_profile=interaction_profile,
            seed=seed_value,
            source_paths=source_paths,
            expected_canon_revision=canon_revision,
            bundle_schema_version=bundle_schema_version,
            session_revision=0,
        )
        state["canon_guard"] = guard
        one_shot_cache = self._bound_remote_cache(
            session_id=state["session_id"],
            one_shot=True,
        )
        state, _ = drive_state(
            state,
            refresh_inventory=True,
            remote_cache=one_shot_cache,
        )
        response = response_for_state(
            state,
            operation="dry_run",
            include_bundle=True,
        )
        response.update(
            {
                "persisted": False,
                "database_touched": False,
                "database_path": str(self.database_path),
            }
        )
        return response

    def start(
        self,
        *,
        project_root: Path | str | None = None,
        mode: str = "auto",
        target_profile: str = "plot_ready",
        interaction_profile: str = "balanced",
        seed: Any = None,
        seed_file: Path | str | None = None,
        sources: Iterable[Path | str] | None = None,
        expected_canon_revision: int | None = None,
        bundle_schema_version: str = "auto",
        idempotency_key: str,
        session_id: str | None = None,
        host_session_id: str | None = None,
        host_turn_id: str | None = None,
    ) -> dict[str, Any]:
        key = self._require_idempotency_key(idempotency_key)
        target = self._project_root(project_root)
        seed_value = self._seed_value(seed, seed_file)
        source_paths = self._source_paths(sources, project_root=target, mode=mode)
        guard = self._canon_guard(target)
        canon_revision = (
            self._require_revision(
                expected_canon_revision,
                "expected_canon_revision",
            )
            if expected_canon_revision is not None
            else int(guard["canon_revision"])
        )
        material = self._request_material(
            project_root=target,
            mode=mode,
            target_profile=target_profile,
            interaction_profile=interaction_profile,
            seed=seed_value,
            source_paths=source_paths,
            expected_canon_revision=canon_revision,
            bundle_schema_version=bundle_schema_version,
        )
        if session_id:
            material["requested_session_id"] = session_id
        if host_session_id:
            material["host_session_id"] = str(host_session_id)
        if host_turn_id:
            material["host_turn_id"] = str(host_turn_id)
        request_hash = canonical_hash(material)
        scope = f"start:{normalize_real_path(self.workspace_root)}"
        replay = self.storage.lookup_idempotency(scope, key, request_hash)
        if replay is not None:
            return replay
        selected_session_id = (
            str(session_id).strip()
            if session_id
            else f"init-{uuid.uuid4().hex}"
        )
        state = create_initial_state(
            session_id=selected_session_id,
            workspace_root=self.workspace_root,
            project_root=target,
            mode=mode,
            target_profile=target_profile,
            interaction_profile=interaction_profile,
            seed=seed_value,
            source_paths=source_paths,
            expected_canon_revision=canon_revision,
            bundle_schema_version=bundle_schema_version,
            session_revision=1,
        )
        state["canon_guard"] = guard
        state["host_session_id"] = str(host_session_id or "")
        state["host_turn_id"] = str(host_turn_id or "")
        state, checkpoints = drive_state(
            state,
            refresh_inventory=True,
            remote_cache=self._bound_remote_cache(
                session_id=selected_session_id,
            ),
        )
        response = response_for_state(
            state,
            operation="start",
            include_bundle=True,
        )
        response["persisted"] = True
        response["database_path"] = str(self.database_path)
        return self.storage.create_session(
            state,
            checkpoints,
            scope=scope,
            idempotency_key=key,
            request_hash=request_hash,
            response=response,
        )

    def advance(
        self,
        session_id: str,
        *,
        expected_session_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        expected_session_revision = self._require_revision(
            expected_session_revision,
            "expected_session_revision",
        )
        key = self._require_idempotency_key(idempotency_key)
        request = {
            "session_id": session_id,
            "expected_session_revision": expected_session_revision,
        }
        request_hash = canonical_hash(request)
        scope = f"{session_id}:advance"
        replay = self.storage.lookup_idempotency(scope, key, request_hash)
        if replay is not None:
            return replay
        previous = self.storage.load_session(session_id)
        self._ensure_revision(previous, expected_session_revision)
        if previous.get("status") == "PROPOSAL_FROZEN":
            response = response_for_state(
                previous,
                operation="advance",
                idempotent=True,
                include_bundle=True,
            )
            response["staleness"] = self._staleness(previous)
            if response["staleness"]["status"] != "CURRENT":
                response["proposal_status"] = "PROPOSAL_FROZEN"
                response["status"] = response["staleness"]["status"]
            return response
        if previous.get("status") == "CANCELLED":
            raise PlotInitError(
                "SESSION_CANCELLED",
                "cancelled initialization session cannot be advanced",
            )
        staleness = self._staleness(previous)
        if staleness["status"] == "STALE_CANON":
            state = copy.deepcopy(previous)
            state["session_revision"] = int(previous["session_revision"]) + 1
            state["stage"] = "STALE_CANON"
            state["status"] = "STALE_CANON"
            state["updated_at"] = utc_now()
            checkpoint = {
                "checkpoint_id": stable_id(
                    "checkpoint",
                    session_id,
                    state["session_revision"],
                    "STALE_CANON",
                    staleness,
                ),
                "stage": "STALE_CANON",
                "status": "STALE_CANON",
                "reason": "canon guard changed during initialization",
                "source_snapshot_hash": state["source_snapshot_hash"],
                "dependency_hash": canonical_hash(staleness),
            }
            state["checkpoints"] = [checkpoint]
            response = response_for_state(
                state,
                operation="advance",
                include_bundle=True,
            )
            response["staleness"] = staleness
            return self.storage.save_session(
                state,
                [checkpoint],
                expected_previous_revision=int(previous["session_revision"]),
                operation="stale_canon",
                scope=scope,
                idempotency_key=key,
                request_hash=request_hash,
                response=response,
            )
        state = copy.deepcopy(previous)
        state["session_revision"] = int(previous["session_revision"]) + 1
        state["updated_at"] = utc_now()
        state, checkpoints = drive_state(
            state,
            refresh_inventory=True,
            remote_cache=self._bound_remote_cache(
                session_id=session_id,
            ),
        )
        response = response_for_state(
            state,
            operation="advance",
            include_bundle=True,
        )
        return self.storage.save_session(
            state,
            checkpoints,
            expected_previous_revision=int(previous["session_revision"]),
            operation="advance",
            scope=scope,
            idempotency_key=key,
            request_hash=request_hash,
            response=response,
        )

    def answer(
        self,
        session_id: str,
        answers: Mapping[str, Any],
        *,
        expected_session_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        expected_session_revision = self._require_revision(
            expected_session_revision,
            "expected_session_revision",
        )
        if not isinstance(answers, Mapping) or not answers:
            raise PlotInitError(
                "ANSWERS_REQUIRED",
                "answer operation requires a non-empty question-to-answer mapping",
            )
        key = self._require_idempotency_key(idempotency_key)
        request = {
            "session_id": session_id,
            "answers": dict(answers),
            "expected_session_revision": expected_session_revision,
        }
        request_hash = canonical_hash(request)
        scope = f"{session_id}:answer"
        replay = self.storage.lookup_idempotency(scope, key, request_hash)
        if replay is not None:
            return replay
        previous = self.storage.load_session(session_id)
        self._ensure_revision(previous, expected_session_revision)
        if previous.get("status") in {"PROPOSAL_FROZEN", "CANCELLED"}:
            raise PlotInitError(
                "PROPOSAL_IMMUTABLE"
                if previous.get("status") == "PROPOSAL_FROZEN"
                else "SESSION_CANCELLED",
                "initialization session no longer accepts answers",
            )
        staleness = self._staleness(previous)
        if staleness["status"] == "STALE_CANON":
            raise PlotInitError(
                "STALE_CANON",
                "canon guard changed; start a new initialization revision before answering",
                **staleness,
            )
        state = copy.deepcopy(previous)
        state["session_revision"] = int(previous["session_revision"]) + 1
        state["updated_at"] = utc_now()
        state, _ = apply_answers(state, answers)
        state, checkpoints = drive_state(
            state,
            refresh_inventory=True,
            remote_cache=self._bound_remote_cache(
                session_id=session_id,
            ),
        )
        response = response_for_state(
            state,
            operation="answer",
            include_bundle=True,
        )
        return self.storage.save_session(
            state,
            checkpoints,
            expected_previous_revision=int(previous["session_revision"]),
            operation="answer",
            scope=scope,
            idempotency_key=key,
            request_hash=request_hash,
            response=response,
        )

    def propose(
        self,
        session_id: str,
        *,
        expected_session_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        expected_session_revision = self._require_revision(
            expected_session_revision,
            "expected_session_revision",
        )
        key = self._require_idempotency_key(idempotency_key)
        request = {
            "session_id": session_id,
            "expected_session_revision": expected_session_revision,
        }
        request_hash = canonical_hash(request)
        scope = f"{session_id}:propose"
        replay = self.storage.lookup_idempotency(scope, key, request_hash)
        if replay is not None:
            return replay
        previous = self.storage.load_session(session_id)
        self._ensure_revision(previous, expected_session_revision)
        if previous.get("status") == "PROPOSAL_FROZEN":
            response = response_for_state(
                previous,
                operation="propose",
                idempotent=True,
                include_bundle=True,
            )
            response["staleness"] = self._staleness(previous)
            if response["staleness"]["status"] != "CURRENT":
                response["proposal_status"] = "PROPOSAL_FROZEN"
                response["status"] = response["staleness"]["status"]
            return response
        if previous.get("status") == "CANCELLED":
            raise PlotInitError(
                "SESSION_CANCELLED",
                "cancelled initialization session cannot freeze a proposal",
            )
        staleness = self._staleness(previous)
        if staleness["status"] == "STALE_CANON":
            raise PlotInitError(
                "STALE_CANON",
                "canon guard changed; proposal must be rebuilt from a fresh baseline",
                **staleness,
            )
        state = copy.deepcopy(previous)
        state["session_revision"] = int(previous["session_revision"]) + 1
        state["updated_at"] = utc_now()
        state, checkpoints = drive_state(
            state,
            refresh_inventory=True,
            remote_cache=self._bound_remote_cache(
                session_id=session_id,
            ),
        )
        if state.get("status") != "READY_TO_PROPOSE":
            raise PlotInitError(
                "NOT_READY_TO_PROPOSE",
                "initialization still needs input or review",
                stage=state.get("stage"),
                status=state.get("status"),
                questions=state.get("current_questions") or [],
            )
        state, proposal = build_proposal(state)
        frozen_checkpoint = {
            "checkpoint_id": stable_id(
                "checkpoint",
                state["session_id"],
                state["session_revision"],
                "PROPOSAL_FROZEN",
                proposal["proposal_id"],
            ),
            "stage": "PROPOSAL_FROZEN",
            "status": "PROPOSAL_FROZEN",
            "reason": "immutable proposal persisted; apply remains unavailable",
            "source_snapshot_hash": state["source_snapshot_hash"],
            "dependency_hash": canonical_hash(
                {
                    "package_hash": proposal["package_hash"],
                    "source_snapshot_hash": state["source_snapshot_hash"],
                }
            ),
        }
        checkpoints.append(frozen_checkpoint)
        state["checkpoints"] = checkpoints
        response = response_for_state(
            state,
            operation="propose",
            include_bundle=True,
        )
        return self.storage.save_session(
            state,
            checkpoints,
            expected_previous_revision=int(previous["session_revision"]),
            operation="propose",
            scope=scope,
            idempotency_key=key,
            request_hash=request_hash,
            response=response,
            proposal=proposal,
        )

    def cancel(
        self,
        session_id: str,
        *,
        expected_session_revision: int,
        idempotency_key: str,
        reason: str = "",
    ) -> dict[str, Any]:
        expected_session_revision = self._require_revision(
            expected_session_revision,
            "expected_session_revision",
        )
        key = self._require_idempotency_key(idempotency_key)
        request = {
            "session_id": session_id,
            "expected_session_revision": expected_session_revision,
            "reason": reason,
        }
        request_hash = canonical_hash(request)
        scope = f"{session_id}:cancel"
        replay = self.storage.lookup_idempotency(scope, key, request_hash)
        if replay is not None:
            return replay
        previous = self.storage.load_session(session_id)
        self._ensure_revision(previous, expected_session_revision)
        if previous.get("status") == "CANCELLED":
            return response_for_state(
                previous,
                operation="cancel",
                idempotent=True,
                include_bundle=True,
            )
        state = copy.deepcopy(previous)
        state["session_revision"] = int(previous["session_revision"]) + 1
        state["stage"] = "CANCELLED"
        state["status"] = "CANCELLED"
        state["current_questions"] = []
        state["cancel_reason"] = reason
        state["updated_at"] = utc_now()
        checkpoint = {
            "checkpoint_id": stable_id(
                "checkpoint",
                session_id,
                state["session_revision"],
                "CANCELLED",
                reason,
            ),
            "stage": "CANCELLED",
            "status": "CANCELLED",
            "reason": reason,
            "source_snapshot_hash": state["source_snapshot_hash"],
            "dependency_hash": canonical_hash(
                {"session_id": session_id, "reason": reason}
            ),
        }
        checkpoints = list(previous.get("checkpoints") or [])
        checkpoints.append(checkpoint)
        state["checkpoints"] = checkpoints
        response = response_for_state(
            state,
            operation="cancel",
            include_bundle=True,
        )
        return self.storage.save_session(
            state,
            checkpoints,
            expected_previous_revision=int(previous["session_revision"]),
            operation="cancel",
            scope=scope,
            idempotency_key=key,
            request_hash=request_hash,
            response=response,
        )

    def complete(
        self,
        proposal_id: str,
        *,
        commit_id: str,
        verification: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Mark a frozen session terminal only after accepted materialization verifies."""

        key = self._require_idempotency_key(idempotency_key)
        proposal = self.storage.load_proposal(proposal_id)
        session_ref = proposal.get("session_ref") or {}
        session_id = str(session_ref.get("session_id") or "")
        if not session_id:
            raise PlotInitError(
                "PROPOSAL_SESSION_MISSING",
                "initialization proposal does not reference its source session",
                proposal_id=proposal_id,
            )
        request = {
            "proposal_id": proposal_id,
            "commit_id": str(commit_id),
            "verification_hash": canonical_hash(dict(verification)),
        }
        request_hash = canonical_hash(request)
        scope = f"{session_id}:complete"
        previous = self.storage.load_session(session_id)
        if previous.get("status") == "COMPLETED":
            completion = previous.get("completion") or {}
            if str(completion.get("commit_id") or "") != str(commit_id):
                raise PlotInitError(
                    "SESSION_ALREADY_COMPLETED",
                    "initialization session is bound to a different accepted commit",
                    session_id=session_id,
                    existing_commit_id=completion.get("commit_id"),
                )
            response = response_for_state(
                previous,
                operation="complete",
                idempotent=True,
                include_bundle=True,
            )
            response["bootstrap_ready"] = True
            return response
        replay = self.storage.lookup_idempotency(scope, key, request_hash)
        if replay is not None:
            return replay
        if previous.get("status") != "PROPOSAL_FROZEN":
            raise PlotInitError(
                "PROPOSAL_NOT_FROZEN",
                "only a frozen initialization session can be completed",
                session_id=session_id,
                status=previous.get("status"),
            )
        if str(verification.get("status") or "") != "verified":
            raise PlotInitError(
                "INITIALIZATION_NOT_VERIFIED",
                "materialized initialization must verify before completion",
                proposal_id=proposal_id,
                commit_id=commit_id,
            )
        state = copy.deepcopy(previous)
        state["session_revision"] = int(previous["session_revision"]) + 1
        state["stage"] = "COMPLETED"
        state["status"] = "COMPLETED"
        state["bootstrap_ready"] = True
        state["current_questions"] = []
        state["completion"] = {
            "proposal_id": proposal_id,
            "commit_id": str(commit_id),
            "verification_hash": canonical_hash(dict(verification)),
            "completed_at": utc_now(),
        }
        state["updated_at"] = state["completion"]["completed_at"]
        checkpoint = {
            "checkpoint_id": stable_id(
                "checkpoint",
                session_id,
                state["session_revision"],
                "COMPLETED",
                proposal_id,
                commit_id,
            ),
            "stage": "COMPLETED",
            "status": "COMPLETED",
            "reason": "accepted initialization materialized and verified",
            "source_snapshot_hash": state["source_snapshot_hash"],
            "dependency_hash": canonical_hash(state["completion"]),
        }
        state["checkpoints"] = [checkpoint]
        response = response_for_state(
            state,
            operation="complete",
            include_bundle=True,
        )
        response["bootstrap_ready"] = True
        response["completion"] = copy.deepcopy(state["completion"])
        return self.storage.save_session(
            state,
            [checkpoint],
            expected_previous_revision=int(previous["session_revision"]),
            operation="complete",
            scope=scope,
            idempotency_key=key,
            request_hash=request_hash,
            response=response,
        )

    def inspect(
        self,
        session_id: str,
        *,
        view: str = "summary",
    ) -> dict[str, Any]:
        """Read a session without advancing it or appending a journal entry."""

        state = self.storage.load_session(session_id)
        allowed = {
            "summary",
            "sources",
            "conflicts",
            "gaps",
            "questions",
            "normalized",
            "diff",
            "proposal",
            "journal",
            "checkpoints",
            "all",
        }
        if view not in allowed:
            raise PlotInitError("INVALID_INSPECT_VIEW", f"unknown inspect view: {view}")
        base = {
            "status": state["status"],
            "operation": "inspect",
            "view": view,
            "session_id": state["session_id"],
            "session_revision": state["session_revision"],
            "stage": state["stage"],
            "read_only": True,
            "staleness": self._staleness(state),
        }
        if view == "summary":
            base["session"] = public_session(state, include_bundle=False)
        elif view == "sources":
            base["source_manifest"] = copy.deepcopy(state.get("source_manifest") or [])
            base["source_diff"] = copy.deepcopy(state.get("source_diff") or {})
            base["source_issues"] = copy.deepcopy(state.get("source_issues") or [])
        elif view == "conflicts":
            base["conflicts"] = copy.deepcopy(state.get("conflicts") or [])
        elif view == "gaps":
            base["gaps"] = copy.deepcopy(state.get("gaps") or [])
        elif view == "questions":
            base["questions"] = copy.deepcopy(state.get("current_questions") or [])
            base["decision_package_count"] = state.get("decision_package_count", 0)
        elif view == "normalized":
            base["bundle"] = copy.deepcopy(state.get("bundle"))
        elif view == "diff":
            bundle = state.get("bundle") or {}
            base["artifact_manifest"] = copy.deepcopy(
                bundle.get("artifact_manifest") or []
            )
        elif view == "proposal":
            base["proposal"] = copy.deepcopy(state.get("proposal"))
        elif view == "journal":
            base["journal"] = self.storage.journal(session_id)
        elif view == "checkpoints":
            base["checkpoints"] = self.storage.checkpoints(session_id)
        else:
            base["session"] = public_session(state, include_bundle=True)
            base["journal"] = self.storage.journal(session_id)
            base["checkpoints"] = self.storage.checkpoints(session_id)
        return base

    def list(
        self,
        *,
        project_root: Path | str | None = None,
        active_only: bool = False,
    ) -> dict[str, Any]:
        target = self._project_root(project_root)
        sessions = self.storage.list_sessions(
            project_root=str(target) if target else None,
            active_only=active_only,
        )
        return {
            "status": "ready",
            "operation": "list",
            "read_only": True,
            "sessions": sessions,
            "count": len(sessions),
        }

    def find_active_session(
        self,
        *,
        project_root: Path | str | None = None,
        host_session_id: str | None = None,
    ) -> dict[str, Any] | None:
        result = self.list(project_root=project_root, active_only=True)
        sessions = list(result["sessions"])
        if host_session_id:
            matching = [
                session
                for session in sessions
                if str(session.get("host_session_id") or "")
                == str(host_session_id)
            ]
            if matching:
                return matching[0]
            unbound = [
                session
                for session in sessions
                if not str(session.get("host_session_id") or "")
            ]
            if len(unbound) == 1 and len(sessions) == 1:
                return unbound[0]
            return None
        return sessions[0] if sessions else None
