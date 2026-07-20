"""Strict canonical lifecycle service.

The service has no automatic acceptance path.  Model/hook callers can only
save proposals.  A separate ``HostApprovalAuthority`` issues short-lived,
single-use approval tokens; only their SHA-256 hashes are persisted.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import hashlib
import json
import os
import secrets
import shutil
import socket
import sqlite3
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .advantages import (
    ADVANTAGE_BRANCH_LOCAL_EVENT_TYPES,
    ADVANTAGE_EVENT_TYPES,
    ADVANTAGE_META_ACTIVE_REVISION,
    ADVANTAGE_META_HASH,
    ADVANTAGE_META_HEAD_REVISION,
    ADVANTAGE_META_VERSION,
    ADVANTAGE_SCHEMA_VERSION,
    query_advantage_anchors as query_advantage_anchors_projection,
    query_advantage_context as query_advantage_context_projection,
    query_advantage_contexts as query_advantage_contexts_projection,
    query_advantage_contracts as query_advantage_contracts_projection,
    query_advantage_definition as query_advantage_definition_projection,
    query_advantage_definitions as query_advantage_definitions_projection,
    query_advantage_exposure as query_advantage_exposure_projection,
    query_advantage_knowledge as query_advantage_knowledge_projection,
    query_advantage_ledger as query_advantage_ledger_projection,
    query_advantage_modules as query_advantage_modules_projection,
    query_advantage_narrative_contract as query_advantage_narrative_contract_projection,
    query_advantage_progression as query_advantage_progression_projection,
    query_advantage_runtime as query_advantage_runtime_projection,
    read_advantage_projection_metadata,
    validate_advantage_event_sequence,
)
from .advantage_readable import refresh_advantage_readable_projection_safe
from .items import (
    ITEM_EVENT_TYPES,
    ITEM_PROJECTION_META_ACTIVE_REVISION,
    ITEM_PROJECTION_META_HASH,
    ITEM_PROJECTION_META_HEAD_REVISION,
    ITEM_PROJECTION_META_VERSION,
    assert_item_rollout_acceptance,
    detect_item_ability_bridge_attempts,
    inspect_item_event_sequence,
    load_item_rollout_policy,
    read_item_projection_metadata,
)
from .item_readable import refresh_item_readable_projection_safe
from .power_spec import (
    POWER_SPEC_ARTIFACT_STAGE,
    POWER_SPEC_PROPOSAL_KIND,
    PowerSpecImportError,
    preview_power_spec_import,
)
from .replay import (
    ReplayEngine,
    expand_correction_event,
    validate_correction_link_consistency,
    validate_event_branch_consistency,
)
from .schema import (
    LEGACY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    STATE_DATABASE_TABLES,
    SchemaVersionError,
    validate_schema_versions,
)
from .source_manifest import (
    SOURCE_MANIFEST_ACCEPT_OPERATION,
    SOURCE_MANIFEST_ARTIFACT_ID,
    SOURCE_MANIFEST_ARTIFACT_KIND,
    SOURCE_MANIFEST_ARTIFACT_STAGE,
    SOURCE_MANIFEST_BRANCH_ID,
    SOURCE_MANIFEST_PROPOSAL_KIND,
    SOURCE_MANIFEST_SOURCE_ROLE,
    current_manifest_snapshot,
    effective_active_manifest,
    insert_manifest_upserts,
    latest_active_manifest_proposal_id,
    manifest_status as source_manifest_projection_status,
    preview_manifest_plan,
    validate_frozen_manifest_change,
    validate_source_manifest_retract_files,
    validate_source_manifest_proposal_envelope,
)
from .store import ContinuityStore, sha256_file, utc_now
from .validators import (
    ContinuityError,
    canonical_json,
    changes_authority,
    normalize_event,
    normalize_source_role,
    normalize_stage,
    normalize_story_coordinate,
    normalize_text,
    parse_utc,
    stable_hash,
    validate_advantage_experience_contract_bindings,
    validate_positive_int,
    validate_proposal_metadata,
)

try:
    from ..sqlite_guard import (
        SQLiteComponentSchemaError,
        validate_sqlite_component_schema,
    )
except ImportError:  # Direct ``continuity`` import with scripts on sys.path.
    from sqlite_guard import (  # type: ignore[no-redef]
        SQLiteComponentSchemaError,
        validate_sqlite_component_schema,
    )


_LIFECYCLE_IDENTITY_FIELDS = frozenset(
    {
        "intent_contract_hash",
        "event_seed_manifest_hash",
        "experience_contract_hashes",
        "event_experience_control_revision",
        "event_seed_references",
    }
)
_ADVANTAGE_SIDECAR_RELATIVE_PATH = ".plot-rag/advantages.v1.json"
_ADVANTAGE_SIDECAR_LOGICAL_OWNER = "advantage_sidecar"


def _json_load(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    return json.loads(value)


def _database_storage_signature(path: Path) -> tuple[bool, int, int, str]:
    try:
        if not path.is_file():
            return False, 0, 0, ""
        metadata = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return (
            True,
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
            digest.hexdigest(),
        )
    except OSError:
        return False, -1, -1, "unstable"


@contextlib.contextmanager
def _open_private_database_snapshot(
    source: Path,
) -> Any:
    """Read a private SQLite snapshot without touching source WAL/SHM."""

    source = source.resolve()
    if not source.is_file():
        raise ContinuityError(
            "POWER_SPEC_STATE_NOT_CREATED",
            "power specification preview requires an existing continuity database",
            details={"path": str(source)},
        )
    source_paths = (
        source,
        Path(str(source) + "-wal"),
        Path(str(source) + "-shm"),
    )
    for _attempt in range(3):
        before = tuple(
            _database_storage_signature(path) for path in source_paths
        )
        if not before[0][0]:
            break
        with tempfile.TemporaryDirectory(
            prefix="plot-rag-power-spec-preview-"
        ) as temporary:
            snapshot = Path(temporary) / source.name
            try:
                shutil.copyfile(source, snapshot)
                if before[1][0]:
                    shutil.copyfile(
                        source_paths[1],
                        Path(str(snapshot) + "-wal"),
                    )
            except OSError:
                continue
            after = tuple(
                _database_storage_signature(path) for path in source_paths
            )
            if before != after:
                continue
            connection = sqlite3.connect(
                str(snapshot),
                timeout=30.0,
                isolation_level=None,
            )
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA busy_timeout = 30000")
                yield connection
            finally:
                connection.close()
            return
    raise ContinuityError(
        "POWER_SPEC_STATE_SNAPSHOT_UNSTABLE",
        "continuity database changed while creating a read-only PowerSpec snapshot",
        details={"path": str(source)},
    )


def _safe_relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ContinuityError(
            "UNSAFE_MATERIALIZATION_PATH",
            f"materialization path must stay relative: {value}",
        )
    normalized = Path(*[part for part in path.parts if part not in {"", "."}])
    if not normalized.parts:
        raise ContinuityError(
            "UNSAFE_MATERIALIZATION_PATH", "empty materialization path"
        )
    return normalized


def _is_reparse_path(path: Path) -> bool:
    """Treat POSIX symlinks and Windows reparse points as unsafe path hops."""

    if path.is_symlink():
        return True
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return False
    return bool(
        attributes
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    )


def _windows_process_probe(process_id: int) -> tuple[str, str | None]:
    """Return liveness and a PID-reuse-resistant birth token on Windows."""

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
            int(process_id),
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
    """Return liveness and a PID-reuse-resistant birth token from procfs."""

    stat_path = Path("/proc") / str(int(process_id)) / "stat"
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


def _materialization_process_probe(
    process_id: int,
) -> tuple[str, str | None]:
    """Probe a materialization owner without assuming uncertain means dead."""

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


def _materialization_owner_state(
    owner_host: str,
    owner_pid: int,
    owner_token: str,
) -> tuple[str, str]:
    """Classify an activation claim, including dead and PID-reused owners."""

    local_host = socket.gethostname().strip().casefold() or "localhost"
    if (
        not owner_host
        or owner_host.strip().casefold() != local_host
        or int(owner_pid) <= 0
    ):
        return "unknown", "materialization owner cannot be verified on this host"
    state, current_birth = _materialization_process_probe(int(owner_pid))
    if state == "dead":
        return (
            "dead",
            f"materialization owner process {owner_pid} is no longer running",
        )
    if state != "alive":
        return "unknown", "materialization owner process state is uncertain"
    try:
        token_payload = json.loads(str(owner_token))
    except (TypeError, ValueError, json.JSONDecodeError):
        token_payload = None
    if not isinstance(token_payload, Mapping):
        return "unknown", "materialization owner token is not verifiable"
    stored_birth = token_payload.get("birth")
    if not isinstance(stored_birth, str) or not stored_birth:
        return "unknown", "materialization owner has no verifiable birth token"
    if current_birth is None:
        return "unknown", "materialization owner birth token cannot be queried"
    if stored_birth == current_birth:
        return "alive", "materialization owner process is still active"
    if stored_birth.startswith(("windows-filetime:", "linux-start:")):
        return (
            "dead",
            f"materialization owner PID {owner_pid} was reused",
        )
    return "unknown", "materialization owner token format is not verifiable"


def _assert_no_reparse_path(
    path: Path,
    *,
    anchor: Path,
    code: str,
    label: str,
) -> None:
    """Reject an existing symlink/junction at or below an approved anchor."""

    lexical_anchor = Path(os.path.abspath(anchor))
    lexical_path = Path(os.path.abspath(path))
    try:
        relative = lexical_path.relative_to(lexical_anchor)
    except ValueError as exc:
        raise ContinuityError(
            code,
            f"{label} is outside its approved anchor",
            details={"path": str(lexical_path), "anchor": str(lexical_anchor)},
        ) from exc
    current = lexical_anchor
    candidates = [current]
    for part in relative.parts:
        current = current / part
        candidates.append(current)
    for candidate in candidates:
        if (
            candidate.exists() or candidate.is_symlink()
        ) and _is_reparse_path(candidate):
            raise ContinuityError(
                code,
                f"{label} crosses a symlink or junction",
                details={"path": str(candidate)},
            )


def _validated_materialization_backup(
    path: Path,
    *,
    anchor: Path,
    expected_hash: str,
    code: str,
    message: str,
) -> Path:
    """Return a private, single-link rollback backup or fail closed."""

    _assert_no_reparse_path(
        path,
        anchor=anchor,
        code="UNSAFE_BACKUP_PATH",
        label="materialization rollback backup",
    )
    resolved_anchor = anchor.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(resolved_anchor)
    except ValueError as exc:
        raise ContinuityError(
            "UNSAFE_BACKUP_PATH",
            "materialization backup escaped its approved run root",
            details={"path": str(path), "anchor": str(resolved_anchor)},
        ) from exc
    try:
        backup_stat = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ContinuityError(
            code,
            message,
            details={"backup_path": str(path)},
        ) from exc
    if (
        not stat.S_ISREG(backup_stat.st_mode)
        or int(getattr(backup_stat, "st_nlink", 1)) != 1
        or sha256_file(path) != str(expected_hash)
    ):
        raise ContinuityError(
            code,
            message,
            details={
                "backup_path": str(path),
                "link_count": int(getattr(backup_stat, "st_nlink", 1)),
            },
        )
    return resolved


class ContinuityService:
    """Proposal, canon commit, query, replay, and materialization API."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        db_path: str | Path | None = None,
    ) -> None:
        self.store = ContinuityStore(project_root, db_path=db_path)
        self.replay_engine = ReplayEngine(self.store)
        self.item_rollout_policy = load_item_rollout_policy(project_root)
        self._transaction_connection: contextvars.ContextVar[
            sqlite3.Connection | None
        ] = contextvars.ContextVar(
            f"continuity_transaction_connection_{id(self)}",
            default=None,
        )
        self._event_experience_service_instance: Any | None = None

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def atomic_write(self):
        """Share one rollback boundary across composed lifecycle operations.

        Stop-hook conversion resolves mentions and may need to register new
        proposal-local entities before the proposal itself is normalized and
        stored.  Reusing one connection makes those metadata writes atomic
        with proposal persistence instead of leaking them when a later
        validator rejects the proposal.
        """

        current = self._transaction_connection.get()
        if current is not None:
            yield current
            return
        with self.store.transaction() as connection:
            token = self._transaction_connection.set(connection)
            try:
                yield connection
            finally:
                self._transaction_connection.reset(token)

    @contextlib.contextmanager
    def _write_transaction(self):
        current = self._transaction_connection.get()
        if current is not None:
            yield current
            return
        with self.store.transaction() as connection:
            yield connection

    @staticmethod
    def _lower_sha256(value: Any, field: str) -> str:
        digest = str(value or "")
        if (
            len(digest) != 64
            or digest != digest.casefold()
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ContinuityError(
                "PREPARED_LIFECYCLE_IDENTITY_INVALID",
                f"{field} must be a lowercase SHA-256 digest",
                details={"field": field},
            )
        return digest

    @classmethod
    def _normalize_lifecycle_identity(
        cls,
        value: Any,
    ) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ContinuityError(
                "PREPARED_LIFECYCLE_IDENTITY_INVALID",
                "lifecycle_identity must be a JSON object",
            )
        raw = dict(value)
        if not raw:
            return {}
        unknown = sorted(set(raw) - _LIFECYCLE_IDENTITY_FIELDS)
        missing = sorted(_LIFECYCLE_IDENTITY_FIELDS - set(raw))
        if unknown or missing:
            raise ContinuityError(
                "PREPARED_LIFECYCLE_IDENTITY_INVALID",
                "lifecycle_identity fields are incomplete or unsupported",
                details={"missing": missing, "unknown": unknown},
            )

        control_revision = raw["event_experience_control_revision"]
        if type(control_revision) is not int or control_revision < 1:
            raise ContinuityError(
                "PREPARED_LIFECYCLE_IDENTITY_INVALID",
                "event_experience_control_revision must be an integer >= 1",
                details={"field": "event_experience_control_revision"},
            )

        contract_values = raw["experience_contract_hashes"]
        if (
            not isinstance(contract_values, Sequence)
            or isinstance(contract_values, (str, bytes, bytearray))
            or not contract_values
        ):
            raise ContinuityError(
                "PREPARED_LIFECYCLE_IDENTITY_INVALID",
                "experience_contract_hashes must be a non-empty array",
                details={"field": "experience_contract_hashes"},
            )
        contract_hashes = sorted(
            {
                cls._lower_sha256(
                    item,
                    "experience_contract_hashes[]",
                )
                for item in contract_values
            }
        )

        reference_values = raw["event_seed_references"]
        if (
            not isinstance(reference_values, Sequence)
            or isinstance(reference_values, (str, bytes, bytearray))
            or not reference_values
        ):
            raise ContinuityError(
                "PREPARED_LIFECYCLE_IDENTITY_INVALID",
                "event_seed_references must be a non-empty array",
                details={"field": "event_seed_references"},
            )
        references_by_id: dict[str, int] = {}
        for index, item in enumerate(reference_values):
            if not isinstance(item, Mapping):
                raise ContinuityError(
                    "PREPARED_LIFECYCLE_IDENTITY_INVALID",
                    "event_seed_references entries must be objects",
                    details={"index": index},
                )
            candidate = dict(item)
            if set(candidate) != {
                "event_seed_id",
                "event_seed_revision",
            }:
                raise ContinuityError(
                    "PREPARED_LIFECYCLE_IDENTITY_INVALID",
                    "event_seed_references entries must contain exactly "
                    "event_seed_id and event_seed_revision",
                    details={"index": index},
                )
            seed_id = str(candidate.get("event_seed_id") or "").strip()
            seed_revision = candidate.get("event_seed_revision")
            if not seed_id or len(seed_id) > 256:
                raise ContinuityError(
                    "PREPARED_LIFECYCLE_IDENTITY_INVALID",
                    "event_seed_id must be a non-empty bounded string",
                    details={"index": index},
                )
            if type(seed_revision) is not int or seed_revision < 1:
                raise ContinuityError(
                    "PREPARED_LIFECYCLE_IDENTITY_INVALID",
                    "event_seed_revision must be an integer >= 1",
                    details={"index": index},
                )
            previous = references_by_id.get(seed_id)
            if previous is not None and previous != seed_revision:
                raise ContinuityError(
                    "PREPARED_LIFECYCLE_IDENTITY_INVALID",
                    "one EventSeed cannot bind multiple revisions",
                    details={
                        "event_seed_id": seed_id,
                        "revisions": [previous, seed_revision],
                    },
                )
            references_by_id[seed_id] = seed_revision
        seed_references = [
            {
                "event_seed_id": seed_id,
                "event_seed_revision": seed_revision,
            }
            for seed_id, seed_revision in sorted(
                references_by_id.items(),
                key=lambda item: (item[0], item[1]),
            )
        ]
        return {
            "intent_contract_hash": cls._lower_sha256(
                raw["intent_contract_hash"],
                "intent_contract_hash",
            ),
            "event_seed_manifest_hash": cls._lower_sha256(
                raw["event_seed_manifest_hash"],
                "event_seed_manifest_hash",
            ),
            "experience_contract_hashes": contract_hashes,
            "event_experience_control_revision": control_revision,
            "event_seed_references": seed_references,
        }

    @staticmethod
    def _strict_proposal_content(
        proposal: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        proposal_values = dict(proposal)
        try:
            payload = json.loads(str(proposal_values["payload_json"]))
            events = json.loads(str(proposal_values["events_json"]))
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            RecursionError,
        ) as exc:
            raise ContinuityError(
                "PROPOSAL_CONTENT_CORRUPT",
                "proposal payload or events are not valid JSON",
            ) from exc
        if not isinstance(payload, dict) or not isinstance(events, list):
            raise ContinuityError(
                "PROPOSAL_CONTENT_CORRUPT",
                "proposal payload must be an object and events must be an array",
            )
        if any(not isinstance(event, Mapping) for event in events):
            raise ContinuityError(
                "PROPOSAL_CONTENT_CORRUPT",
                "proposal events must contain only JSON objects",
            )
        normalized_events = [dict(event) for event in events]
        try:
            actual_hash = stable_hash(
                {"payload": payload, "events": normalized_events},
                prefix="payload_",
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise ContinuityError(
                "PROPOSAL_CONTENT_CORRUPT",
                "proposal payload cannot be canonically hashed",
            ) from exc
        if actual_hash != str(proposal_values["payload_hash"]):
            raise ContinuityError(
                "PROPOSAL_CONTENT_HASH_MISMATCH",
                "proposal JSON no longer matches its immutable payload hash",
                details={
                    "proposal_id": str(
                        proposal_values.get("proposal_id") or ""
                    ),
                    "expected": str(proposal_values["payload_hash"]),
                    "actual": actual_hash,
                },
            )
        return payload, normalized_events

    @staticmethod
    def _expanded_validation_events(
        events: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Expose correction leaf events to cross-domain acceptance gates."""

        expanded_events: list[dict[str, Any]] = []
        for index, event in enumerate(events):
            event_type = str(
                event.get("event_type") or event.get("type") or "fact"
            )
            expanded = expand_correction_event(
                event_type,
                event,
                event_id=f"proposal-event:{index}",
            )
            if expanded is None:
                continue
            leaf_type, leaf = expanded
            if event_type in {
                "correction",
                "item_correction",
                "advantage_correction",
            }:
                leaf["event_type"] = leaf_type
                expanded_events.append(leaf)
            else:
                expanded_events.append(dict(event))
        return expanded_events

    @classmethod
    def _proposal_lifecycle_binding(
        cls,
        proposal: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        identity = cls._normalize_lifecycle_identity(
            payload.get("lifecycle_identity")
        )
        if not identity:
            return {}

        def required_text(field: str) -> str:
            text = str(payload.get(field) or "").strip()
            if not text:
                raise ContinuityError(
                    "PREPARED_LIFECYCLE_BINDING_INVALID",
                    f"{field} is required for lifecycle-bound proposals",
                    details={"field": field},
                )
            return text

        receipt_id = required_text("receipt_id")
        request_id = required_text("request_id")
        assistant_sha256 = cls._lower_sha256(
            payload.get("assistant_sha256"),
            "assistant_sha256",
        )
        prompt_hash = cls._lower_sha256(
            payload.get("prompt_hash"),
            "prompt_hash",
        )
        context_digest = cls._lower_sha256(
            payload.get("retrieved_context_digest"),
            "retrieved_context_digest",
        )
        prepared_revision = payload.get("prepared_canon_revision")
        if type(prepared_revision) is not int or prepared_revision < 0:
            raise ContinuityError(
                "PREPARED_LIFECYCLE_BINDING_INVALID",
                "prepared_canon_revision must be an integer >= 0",
                details={"field": "prepared_canon_revision"},
            )
        if prepared_revision != int(proposal["prepared_canon_revision"]):
            raise ContinuityError(
                "PREPARED_LIFECYCLE_BINDING_MISMATCH",
                "proposal row and payload bind different canon revisions",
                details={
                    "row": int(proposal["prepared_canon_revision"]),
                    "payload": prepared_revision,
                },
            )
        active_projection_hash = required_text("active_projection_hash")
        artifact_context = payload.get("artifact_context")
        if not isinstance(artifact_context, Mapping):
            raise ContinuityError(
                "PREPARED_LIFECYCLE_BINDING_INVALID",
                "artifact_context must be a JSON object",
            )
        artifact_context = dict(artifact_context)
        artifact_branch = str(
            artifact_context.get("branch_id")
            or proposal["branch_id"]
        ).strip()
        artifact_id = str(
            artifact_context.get("artifact_id")
            or proposal["artifact_id"]
        ).strip()
        artifact_revision = artifact_context.get("artifact_revision", 0)
        if (
            not artifact_branch
            or artifact_branch != str(proposal["branch_id"])
            or not artifact_id
            or artifact_id != str(proposal["artifact_id"])
            or type(artifact_revision) is not int
            or artifact_revision < 0
        ):
            raise ContinuityError(
                "PREPARED_LIFECYCLE_BINDING_MISMATCH",
                "artifact_context differs from the proposal identity",
                details={
                    "proposal_branch_id": str(proposal["branch_id"]),
                    "artifact_branch_id": artifact_branch,
                    "proposal_artifact_id": str(proposal["artifact_id"]),
                    "artifact_context_id": artifact_id,
                    "artifact_context_revision": artifact_revision,
                },
            )

        extraction_job_id = payload.get("extraction_job_id")
        job_binding_hash = payload.get("job_binding_hash")
        if bool(extraction_job_id) != bool(job_binding_hash):
            raise ContinuityError(
                "EXTRACTION_PROPOSAL_BINDING_MISMATCH",
                "extraction_job_id and job_binding_hash must appear together",
            )
        normalized_job_id: str | None = None
        normalized_job_hash: str | None = None
        if extraction_job_id:
            normalized_job_id = str(extraction_job_id).strip()
            if not normalized_job_id:
                raise ContinuityError(
                    "EXTRACTION_PROPOSAL_BINDING_MISMATCH",
                    "extraction_job_id must be a non-empty string",
                )
            normalized_job_hash = cls._lower_sha256(
                job_binding_hash,
                "job_binding_hash",
            )

        return {
            "receipt_id": receipt_id,
            "request_id": request_id,
            "assistant_sha256": assistant_sha256,
            "prompt_hash": prompt_hash,
            "retrieved_context_digest": context_digest,
            "prepared_canon_revision": prepared_revision,
            "active_projection_hash": active_projection_hash,
            "lifecycle_identity": identity,
            "lifecycle_identity_hash": stable_hash(
                identity,
                prefix="lifecycle_identity_",
            ),
            "event_artifact_identity": {
                "branch_id": artifact_branch,
                "artifact_id": artifact_id,
                "artifact_revision": artifact_revision,
            },
            "extraction_job_id": normalized_job_id,
            "job_binding_hash": normalized_job_hash,
        }

    def _event_experience_service(self) -> Any:
        service = self._event_experience_service_instance
        if service is not None:
            return service
        try:
            try:
                from scripts.event_experience import EventExperienceService
            except ImportError:
                from event_experience import EventExperienceService

            default_path = (
                self.store.project_root / ".plot-rag" / "state.sqlite3"
            ).resolve(strict=False)
            if self.store.db_path.resolve(strict=False) == default_path:
                service = EventExperienceService.for_project(
                    self.store.project_root
                )
            else:
                service = EventExperienceService(self.store.db_path)
        except Exception as exc:
            raise ContinuityError(
                str(
                    getattr(
                        exc,
                        "code",
                        "EVENT_EXPERIENCE_SERVICE_UNAVAILABLE",
                    )
                ),
                str(exc),
                details=dict(getattr(exc, "details", {}) or {}),
            ) from exc
        self._event_experience_service_instance = service
        return service

    def _preflight_event_experience_service(
        self,
        proposal_id: str,
        *,
        active_checks: bool,
    ) -> Any | None:
        """Construct the shared validator before a lifecycle write lock."""

        if not active_checks:
            return None
        with self.store.read_connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            not isinstance(payload, Mapping)
            or not isinstance(payload.get("lifecycle_identity"), Mapping)
            or not payload.get("lifecycle_identity")
        ):
            return None
        return self._event_experience_service()

    @classmethod
    def _decode_turn_lifecycle_identity(
        cls,
        value: Any,
    ) -> dict[str, Any]:
        try:
            decoded = (
                dict(value)
                if isinstance(value, Mapping)
                else json.loads(str(value or "{}"))
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContinuityError(
                "PREPARED_RECEIPT_CORRUPT",
                "prepared receipt lifecycle identity is not valid JSON",
            ) from exc
        return cls._normalize_lifecycle_identity(decoded)

    def _validate_receipt_lifecycle_binding(
        self,
        connection: sqlite3.Connection,
        proposal: Mapping[str, Any],
        payload: Mapping[str, Any],
        binding: Mapping[str, Any],
        *,
        allow_assistant_bind: bool,
    ) -> None:
        required_columns = {
            "receipt_id",
            "request_id",
            "prompt",
            "prompt_hash",
            "assistant_hash",
            "prepared_canon_revision",
            "active_projection_hash",
            "retrieved_context_digest",
            "lifecycle_identity_json",
        }
        available_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(turns)")
        }
        missing_columns = sorted(required_columns - available_columns)
        if missing_columns:
            raise ContinuityError(
                "PREPARED_RECEIPT_BINDING_MISSING",
                "prepared receipt lacks lifecycle identity columns",
                details={"missing_columns": missing_columns},
            )
        receipt = connection.execute(
            "SELECT * FROM turns WHERE receipt_id=?",
            (binding["receipt_id"],),
        ).fetchone()
        if receipt is None:
            raise ContinuityError(
                "PREPARED_RECEIPT_NOT_FOUND",
                "lifecycle-bound proposal requires its Prepare receipt",
                details={"receipt_id": binding["receipt_id"]},
            )

        mismatches: dict[str, dict[str, Any]] = {}
        expected_fields = {
            "request_id": binding["request_id"],
            "prompt_hash": binding["prompt_hash"],
            "prepared_canon_revision": binding[
                "prepared_canon_revision"
            ],
            "active_projection_hash": binding[
                "active_projection_hash"
            ],
            "retrieved_context_digest": binding[
                "retrieved_context_digest"
            ],
        }
        for field, expected in expected_fields.items():
            actual = receipt[field]
            if field == "prepared_canon_revision":
                actual = int(actual)
            else:
                actual = str(actual or "")
            if actual != expected:
                mismatches[field] = {
                    "expected": expected,
                    "actual": actual,
                }
        turn_identity = self._decode_turn_lifecycle_identity(
            receipt["lifecycle_identity_json"]
        )
        if turn_identity != binding["lifecycle_identity"]:
            mismatches["lifecycle_identity"] = {
                "expected": binding["lifecycle_identity"],
                "actual": turn_identity,
            }

        stored_prompt = str(receipt["prompt"] or "")
        if (
            not stored_prompt
            or hashlib.sha256(stored_prompt.encode("utf-8")).hexdigest()
            != str(receipt["prompt_hash"])
        ):
            mismatches["receipt_prompt_content"] = {
                "expected": str(receipt["prompt_hash"]),
                "actual": (
                    hashlib.sha256(stored_prompt.encode("utf-8")).hexdigest()
                    if stored_prompt
                    else ""
                ),
            }
        supplied_prompt = payload.get("prompt")
        if supplied_prompt is not None:
            if (
                not isinstance(supplied_prompt, str)
                or hashlib.sha256(
                    supplied_prompt.encode("utf-8")
                ).hexdigest()
                != binding["prompt_hash"]
            ):
                mismatches["payload_prompt_content"] = {
                    "expected": binding["prompt_hash"],
                    "actual": (
                        hashlib.sha256(
                            str(supplied_prompt).encode("utf-8")
                        ).hexdigest()
                    ),
                }

        supplied_assistant = payload.get("assistant_text")
        if supplied_assistant is not None:
            if (
                not isinstance(supplied_assistant, str)
                or hashlib.sha256(
                    supplied_assistant.encode("utf-8")
                ).hexdigest()
                != binding["assistant_sha256"]
            ):
                mismatches["assistant_text"] = {
                    "expected": binding["assistant_sha256"],
                    "actual": (
                        hashlib.sha256(
                            str(supplied_assistant).encode("utf-8")
                        ).hexdigest()
                    ),
                }

        stored_assistant_hash = str(receipt["assistant_hash"] or "")
        if not stored_assistant_hash and allow_assistant_bind:
            attested = (
                isinstance(supplied_assistant, str)
                and hashlib.sha256(
                    supplied_assistant.encode("utf-8")
                ).hexdigest()
                == binding["assistant_sha256"]
            )
            if not attested and binding.get("extraction_job_id"):
                job = connection.execute(
                    """
                    SELECT receipt_id, request_id, assistant_sha256,
                           job_binding_hash
                    FROM extraction_jobs
                    WHERE job_id=?
                    """,
                    (binding["extraction_job_id"],),
                ).fetchone()
                attested = bool(
                    job is not None
                    and str(job["receipt_id"]) == binding["receipt_id"]
                    and str(job["request_id"]) == binding["request_id"]
                    and str(job["assistant_sha256"])
                    == binding["assistant_sha256"]
                    and str(job["job_binding_hash"])
                    == binding["job_binding_hash"]
                )
            if attested:
                connection.execute(
                    "UPDATE turns SET assistant_hash=? WHERE receipt_id=?",
                    (
                        binding["assistant_sha256"],
                        binding["receipt_id"],
                    ),
                )
                stored_assistant_hash = binding["assistant_sha256"]
        if stored_assistant_hash != binding["assistant_sha256"]:
            mismatches["assistant_sha256"] = {
                "expected": binding["assistant_sha256"],
                "actual": stored_assistant_hash,
            }

        if mismatches:
            raise ContinuityError(
                "PREPARED_RECEIPT_BINDING_MISMATCH",
                "proposal does not match its immutable Prepare receipt",
                details={
                    "proposal_id": str(proposal["proposal_id"]),
                    "mismatches": mismatches,
                },
            )

    def _validate_active_projection_binding(
        self,
        connection: sqlite3.Connection,
        binding: Mapping[str, Any],
    ) -> None:
        prepared_revision = int(binding["prepared_canon_revision"])
        active_revision = self.store.get_meta_int(
            connection,
            "active_canon_revision",
        )
        row = connection.execute(
            """
            SELECT projection_hash
            FROM projection_runs
            WHERE projection_name='continuity'
              AND run_status='completed'
              AND source_active_revision=?
            ORDER BY created_at DESC, run_id DESC
            LIMIT 1
            """,
            (prepared_revision,),
        ).fetchone()
        actual_hash = str(row["projection_hash"]) if row is not None else ""
        if (
            active_revision != prepared_revision
            or actual_hash != binding["active_projection_hash"]
        ):
            raise ContinuityError(
                "PREPARED_PROJECTION_BINDING_MISMATCH",
                "canon revision or completed continuity projection changed",
                details={
                    "prepared_canon_revision": prepared_revision,
                    "active_canon_revision": active_revision,
                    "prepared_projection_hash": binding[
                        "active_projection_hash"
                    ],
                    "active_projection_hash": actual_hash,
                },
            )

    def _validate_event_experience_binding(
        self,
        connection: sqlite3.Connection,
        binding: Mapping[str, Any],
        event_experience_service: Any | None,
        events: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        if event_experience_service is None:
            raise ContinuityError(
                "EVENT_EXPERIENCE_SERVICE_UNAVAILABLE",
                "event-experience validator was not prepared before the "
                "lifecycle transaction",
            )
        identity = dict(binding["lifecycle_identity"])
        try:
            manifest = (
                event_experience_service
                .validate_locked_manifest_in_transaction(
                    connection,
                    identity["event_seed_references"],
                    expected_event_seed_manifest_hash=identity[
                        "event_seed_manifest_hash"
                    ],
                    expected_control_revision=identity[
                        "event_experience_control_revision"
                    ],
                )
            )
        except Exception as exc:
            raise ContinuityError(
                str(
                    getattr(
                        exc,
                        "code",
                        "EVENT_EXPERIENCE_VALIDATION_FAILED",
                    )
                ),
                str(exc),
                details=dict(getattr(exc, "details", {}) or {}),
            ) from exc

        contracts = list(manifest.get("contracts") or [])
        manifest_references = sorted(
            {
                (
                    str(item.get("event_seed_id") or ""),
                    int(item.get("event_seed_revision") or 0),
                )
                for item in contracts
                if isinstance(item, Mapping)
            }
        )
        expected_references = [
            (
                str(item["event_seed_id"]),
                int(item["event_seed_revision"]),
            )
            for item in identity["event_seed_references"]
        ]
        manifest_contract_hashes = sorted(
            {
                str(item.get("contract_hash") or "")
                for item in contracts
                if isinstance(item, Mapping)
            }
        )
        expected_artifact = dict(binding["event_artifact_identity"])
        artifact_mismatches = [
            {
                "event_seed_id": str(item.get("event_seed_id") or ""),
                "branch_id": str(item.get("branch_id") or ""),
                "artifact_id": str(item.get("artifact_id") or ""),
                "artifact_revision": int(
                    item.get("artifact_revision") or 0
                ),
            }
            for item in contracts
            if isinstance(item, Mapping)
            and (
                str(item.get("branch_id") or "")
                != expected_artifact["branch_id"]
                or str(item.get("artifact_id") or "")
                != expected_artifact["artifact_id"]
                or int(item.get("artifact_revision") or 0)
                != expected_artifact["artifact_revision"]
            )
        ]
        mismatches: dict[str, Any] = {}
        if (
            str(manifest.get("source_intent_contract_hash") or "")
            != identity["intent_contract_hash"]
        ):
            mismatches["intent_contract_hash"] = {
                "expected": identity["intent_contract_hash"],
                "actual": str(
                    manifest.get("source_intent_contract_hash") or ""
                ),
            }
        if manifest_references != expected_references:
            mismatches["event_seed_references"] = {
                "expected": expected_references,
                "actual": manifest_references,
            }
        if (
            manifest_contract_hashes
            != identity["experience_contract_hashes"]
        ):
            mismatches["experience_contract_hashes"] = {
                "expected": identity["experience_contract_hashes"],
                "actual": manifest_contract_hashes,
            }
        if artifact_mismatches:
            mismatches["event_artifact_identity"] = {
                "expected": expected_artifact,
                "actual": artifact_mismatches,
            }
        if mismatches:
            raise ContinuityError(
                "EVENT_EXPERIENCE_LIFECYCLE_BINDING_MISMATCH",
                "locked event-experience manifest differs from the proposal",
                details=mismatches,
            )
        validate_advantage_experience_contract_bindings(
            events,
            required=True,
            allowed_contract_bindings=[
                dict(item)
                for item in contracts
                if isinstance(item, Mapping)
            ],
        )

    @staticmethod
    def _validate_extraction_job_binding(
        connection: sqlite3.Connection,
        proposal: Mapping[str, Any],
        payload: Mapping[str, Any],
        binding: Mapping[str, Any],
    ) -> None:
        job_id = binding.get("extraction_job_id")
        if not job_id:
            return
        job = connection.execute(
            "SELECT * FROM extraction_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise ContinuityError(
                "EXTRACTION_PROPOSAL_JOB_NOT_FINALIZED",
                "proposal extraction job does not exist",
                details={"job_id": job_id},
            )
        if (
            str(job["job_status"]) != "succeeded"
            or str(job["result_kind"]) != "proposal"
        ):
            raise ContinuityError(
                "EXTRACTION_PROPOSAL_JOB_NOT_FINALIZED",
                "proposal extraction job has not finalized a proposal result",
                details={
                    "job_id": job_id,
                    "job_status": str(job["job_status"]),
                    "result_kind": str(job["result_kind"]),
                },
            )
        if str(job["result_proposal_id"] or "") != str(
            proposal["proposal_id"]
        ):
            raise ContinuityError(
                "EXTRACTION_PROPOSAL_BINDING_MISMATCH",
                "extraction job finalized a different proposal",
                details={
                    "job_id": job_id,
                    "expected_proposal_id": str(proposal["proposal_id"]),
                    "actual_proposal_id": str(
                        job["result_proposal_id"] or ""
                    ),
                },
            )
        try:
            try:
                from scripts.extraction_jobs import ExtractionJobQueue
            except ImportError:
                from extraction_jobs import ExtractionJobQueue

            decoded_job = ExtractionJobQueue._assert_persisted_binding(job)
            expected_payload = (
                ExtractionJobQueue._proposal_binding_for_job(decoded_job)
            )
        except Exception as exc:
            raise ContinuityError(
                "EXTRACTION_PROPOSAL_BINDING_MISMATCH",
                "extraction job immutable binding is corrupt",
                details={
                    "job_id": job_id,
                    "source_code": str(
                        getattr(exc, "code", type(exc).__name__)
                    ),
                },
            ) from exc
        mismatches = {
            key: {"expected": expected, "actual": payload.get(key)}
            for key, expected in expected_payload.items()
            if payload.get(key) != expected
        }
        if str(proposal["branch_id"]) != str(decoded_job["branch_id"]):
            mismatches["branch_id"] = {
                "expected": str(decoded_job["branch_id"]),
                "actual": str(proposal["branch_id"]),
            }
        if mismatches:
            raise ContinuityError(
                "EXTRACTION_PROPOSAL_BINDING_MISMATCH",
                "proposal differs from its immutable extraction job",
                details={"job_id": job_id, "mismatches": mismatches},
            )

    @staticmethod
    def _idempotency_lookup(
        connection: sqlite3.Connection,
        namespace: str,
        key: str | None,
        request_hash: str,
    ) -> dict[str, Any] | None:
        if not key:
            return None
        row = connection.execute(
            """
            SELECT request_hash, response_json
            FROM idempotency_records
            WHERE namespace=? AND idempotency_key=?
            """,
            (namespace, key),
        ).fetchone()
        if row is None:
            return None
        if str(row["request_hash"]) != request_hash:
            raise ContinuityError(
                "IDEMPOTENCY_CONFLICT",
                "the idempotency key was already used for a different request",
                details={"namespace": namespace, "idempotency_key": key},
            )
        return dict(_json_load(str(row["response_json"]), {}))

    @staticmethod
    def _idempotency_store(
        connection: sqlite3.Connection,
        namespace: str,
        key: str | None,
        request_hash: str,
        response: Mapping[str, Any],
    ) -> None:
        if not key:
            return
        connection.execute(
            """
            INSERT INTO idempotency_records(
                namespace, idempotency_key, request_hash,
                response_json, created_at
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (
                namespace,
                key,
                request_hash,
                canonical_json(dict(response)),
                utc_now(),
            ),
        )

    def schema_status(self) -> dict[str, Any]:
        backup = self.store.ensure_schema()
        with self.store.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT key, value
                FROM state_meta
                WHERE key IN (
                    'schema_version',
                    'continuity_schema_version',
                    'head_canon_revision',
                    'active_canon_revision'
                )
                ORDER BY key
                """
            ).fetchall()
            item_metadata = read_item_projection_metadata(connection)
            advantage_metadata = read_advantage_projection_metadata(connection)
        return {
            "db_path": str(self.store.db_path),
            "meta": {str(row["key"]): str(row["value"]) for row in rows},
            "item_projection_hash": str(
                item_metadata.get(ITEM_PROJECTION_META_HASH) or ""
            ),
            "advantage_projection_hash": str(
                advantage_metadata.get(ADVANTAGE_META_HASH) or ""
            ),
            "advantage_projection_schema_version": int(
                advantage_metadata.get(ADVANTAGE_META_VERSION) or 0
            ),
            "migration_backup": str(backup) if backup else None,
        }

    def get_canon_revisions(self) -> dict[str, int]:
        with self.store.read_connection() as connection:
            return {
                "head": self.store.get_meta_int(
                    connection, "head_canon_revision"
                ),
                "active": self.store.get_meta_int(
                    connection, "active_canon_revision"
                ),
            }

    # ------------------------------------------------------------------
    # Entity registry, aliases, and mention resolution
    # ------------------------------------------------------------------
    def register_entity(
        self,
        entity_type: str,
        canonical_name: str,
        *,
        entity_id: str | None = None,
        aliases: Sequence[str] = (),
        attributes: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        entity_type = normalize_text(entity_type or "unknown")
        name = str(canonical_name or "").strip()
        if not name:
            raise ContinuityError(
                "ENTITY_NAME_REQUIRED", "canonical entity name is required"
            )
        normalized_name = normalize_text(name)
        resolved_id = entity_id or stable_hash(
            [entity_type, normalized_name], prefix="entity_"
        )
        now = utc_now()
        with self._write_transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM entities
                WHERE entity_id=?
                   OR (entity_type=? AND normalized_name=?)
                """,
                (resolved_id, entity_type, normalized_name),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["entity_type"]) != entity_type
                    or str(existing["normalized_name"]) != normalized_name
                ):
                    raise ContinuityError(
                        "ENTITY_ID_CONFLICT",
                        "entity id already belongs to another entity",
                        details={"entity_id": resolved_id},
                    )
                resolved_id = str(existing["entity_id"])
            else:
                connection.execute(
                    """
                    INSERT INTO entities(
                        entity_id, entity_type, canonical_name,
                        normalized_name, attributes_json, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved_id,
                        entity_type,
                        name,
                        normalized_name,
                        canonical_json(dict(attributes or {})),
                        now,
                        now,
                    ),
                )
            for alias in aliases:
                self._add_alias_in_transaction(
                    connection,
                    resolved_id,
                    alias,
                    confidence=1.0,
                    status="confirmed",
                    source_ref="register_entity",
                )
            row = connection.execute(
                "SELECT * FROM entities WHERE entity_id=?", (resolved_id,)
            ).fetchone()
            return self._entity_response(connection, row)

    @staticmethod
    def _entity_response(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        aliases = [
            {
                "alias": alias["alias_text"],
                "confidence": float(alias["confidence"]),
                "status": alias["alias_status"],
            }
            for alias in connection.execute(
                """
                SELECT alias_text, confidence, alias_status
                FROM entity_aliases
                WHERE entity_id=?
                ORDER BY alias_status, confidence DESC, normalized_alias
                """,
                (row["entity_id"],),
            )
        ]
        return {
            "entity_id": str(row["entity_id"]),
            "entity_type": str(row["entity_type"]),
            "canonical_name": str(row["canonical_name"]),
            "attributes": _json_load(str(row["attributes_json"]), {}),
            "aliases": aliases,
        }

    @staticmethod
    def _add_alias_in_transaction(
        connection: sqlite3.Connection,
        entity_id: str,
        alias: str,
        *,
        confidence: float,
        status: str,
        source_ref: str,
    ) -> str:
        alias_text = str(alias or "").strip()
        if not alias_text:
            raise ContinuityError(
                "ALIAS_REQUIRED", "entity alias cannot be empty"
            )
        if status not in {"confirmed", "candidate", "rejected"}:
            raise ContinuityError(
                "INVALID_ALIAS_STATUS", f"unsupported alias status: {status}"
            )
        if not 0.0 <= float(confidence) <= 1.0:
            raise ContinuityError(
                "INVALID_CONFIDENCE", "alias confidence must be between 0 and 1"
            )
        if (
            connection.execute(
                "SELECT 1 FROM entities WHERE entity_id=?", (entity_id,)
            ).fetchone()
            is None
        ):
            raise ContinuityError(
                "ENTITY_NOT_FOUND", f"unknown entity: {entity_id}"
            )
        normalized = normalize_text(alias_text)
        alias_id = stable_hash([entity_id, normalized], prefix="alias_")
        connection.execute(
            """
            INSERT INTO entity_aliases(
                alias_id, entity_id, alias_text, normalized_alias,
                confidence, alias_status, source_ref, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_id, normalized_alias) DO UPDATE
            SET confidence=excluded.confidence,
                alias_status=excluded.alias_status,
                source_ref=excluded.source_ref
            """,
            (
                alias_id,
                entity_id,
                alias_text,
                normalized,
                float(confidence),
                status,
                source_ref,
                utc_now(),
            ),
        )
        return alias_id

    def add_alias(
        self,
        entity_id: str,
        alias: str,
        *,
        confidence: float = 1.0,
        status: str = "confirmed",
        source_ref: str = "host",
    ) -> dict[str, Any]:
        with self._write_transaction() as connection:
            alias_id = self._add_alias_in_transaction(
                connection,
                entity_id,
                alias,
                confidence=confidence,
                status=status,
                source_ref=source_ref,
            )
            return {
                "alias_id": alias_id,
                "entity_id": entity_id,
                "alias": alias,
                "status": status,
            }

    def resolve_mention(
        self,
        mention: str,
        *,
        artifact_id: str | None = None,
        context_entity_ids: Sequence[str] = (),
        persist: bool = True,
    ) -> dict[str, Any]:
        mention_text = str(mention or "").strip()
        normalized = normalize_text(mention_text)
        if not normalized:
            raise ContinuityError(
                "MENTION_REQUIRED", "mention text cannot be empty"
            )
        pronouns = {
            "他",
            "她",
            "它",
            "他们",
            "她们",
            "其",
            "此人",
            "that person",
            "he",
            "she",
            "they",
            "it",
        }
        with self._write_transaction() as connection:
            candidates: list[dict[str, Any]] = []
            if normalized in pronouns and len(context_entity_ids) == 1:
                row = connection.execute(
                    "SELECT * FROM entities WHERE entity_id=?",
                    (context_entity_ids[0],),
                ).fetchone()
                if row is not None:
                    candidates = [
                        {
                            "entity_id": str(row["entity_id"]),
                            "canonical_name": str(row["canonical_name"]),
                            "confidence": 1.0,
                            "match": "context_pronoun",
                        }
                    ]
            if not candidates:
                rows = connection.execute(
                    """
                    SELECT entity_id, canonical_name, 1.0 AS confidence,
                           'canonical_name' AS match
                    FROM entities
                    WHERE normalized_name=?
                    UNION ALL
                    SELECT e.entity_id, e.canonical_name, a.confidence,
                           'alias' AS match
                    FROM entity_aliases AS a
                    JOIN entities AS e ON e.entity_id=a.entity_id
                    WHERE a.normalized_alias=?
                      AND a.alias_status='confirmed'
                    ORDER BY confidence DESC, entity_id
                    """,
                    (normalized, normalized),
                ).fetchall()
                seen: set[str] = set()
                for row in rows:
                    entity_id = str(row["entity_id"])
                    if entity_id in seen:
                        continue
                    seen.add(entity_id)
                    candidates.append(
                        {
                            "entity_id": entity_id,
                            "canonical_name": str(row["canonical_name"]),
                            "confidence": float(row["confidence"]),
                            "match": str(row["match"]),
                        }
                    )
            if len(candidates) == 1:
                status = "RESOLVED"
                entity_id = candidates[0]["entity_id"]
            elif candidates:
                status = "AMBIGUOUS"
                entity_id = None
            else:
                status = "UNRESOLVED"
                entity_id = None
            result = {
                "mention": mention_text,
                "status": status,
                "entity_id": entity_id,
                "candidates": candidates,
            }
            if persist:
                context = {"context_entity_ids": list(context_entity_ids)}
                resolution_id = stable_hash(
                    [artifact_id, mention_text, context, candidates],
                    prefix="mention_",
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO mention_resolutions(
                        resolution_id, artifact_id, mention_text,
                        normalized_mention, entity_id, resolution_status,
                        candidates_json, context_json, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolution_id,
                        artifact_id,
                        mention_text,
                        normalized,
                        entity_id,
                        status,
                        canonical_json(candidates),
                        canonical_json(context),
                        utc_now(),
                    ),
                )
                result["resolution_id"] = resolution_id
            return result

    # ------------------------------------------------------------------
    # Proposal lifecycle
    # ------------------------------------------------------------------
    def _item_proposal_diagnostics(
        self,
        events: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        item_events = [
            event
            for event in events
            if str(event.get("event_type") or "") in ITEM_EVENT_TYPES
        ]
        if not item_events and self.item_rollout_policy.power_binding_bridge:
            return []

        current = self._transaction_connection.get()
        if current is not None:
            report = inspect_item_event_sequence(
                current,
                events,
                rollout_policy=self.item_rollout_policy,
            )
            bridge_attempts = detect_item_ability_bridge_attempts(
                current,
                events,
            )
        else:
            with self.store.read_connection() as connection:
                report = inspect_item_event_sequence(
                    connection,
                    events,
                    rollout_policy=self.item_rollout_policy,
                )
                bridge_attempts = detect_item_ability_bridge_attempts(
                    connection,
                    events,
                )

        diagnostics: list[dict[str, Any]] = []
        if item_events and not (
            self.item_rollout_policy.strict_runtime_validation
        ):
            diagnostics.append(
                {
                    "code": "ITEM_STRICT_RUNTIME_SHADOW_ONLY",
                    "severity": "warning",
                    "message": (
                        "v4 item reducer ran in shadow mode; authority accept "
                        "requires items.strict_runtime_validation=true"
                    ),
                    "details": {
                        "policy": self.item_rollout_policy.as_dict(),
                        "event_count": len(item_events),
                        "shadow_status": report["status"],
                    },
                }
            )
        if (
            bridge_attempts
            and not self.item_rollout_policy.power_binding_bridge
        ):
            diagnostics.append(
                {
                    "code": "ITEM_POWER_BINDING_BRIDGE_DISABLED",
                    "severity": "warning",
                    "message": (
                        "item-to-ability bridge creation or activation is "
                        "disabled for this project"
                    ),
                    "details": {
                        "policy": self.item_rollout_policy.as_dict(),
                        "attempts": bridge_attempts,
                    },
                }
            )
        for diagnostic in report["diagnostics"]:
            if (
                diagnostic["code"]
                == "ITEM_POWER_BINDING_BRIDGE_DISABLED"
                and bridge_attempts
                and not self.item_rollout_policy.power_binding_bridge
            ):
                continue
            diagnostics.append(
                {
                    "code": "ITEM_SHADOW_DIAGNOSTIC",
                    "severity": "warning",
                    "message": str(diagnostic["message"]),
                    "details": {
                        "source_code": diagnostic["code"],
                        "event_index": diagnostic["event_index"],
                        "event_type": diagnostic["event_type"],
                        "source_details": diagnostic["details"],
                        "policy": self.item_rollout_policy.as_dict(),
                    },
                }
            )
        return diagnostics

    @staticmethod
    def _advantage_events(
        events: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            dict(event)
            for event in events
            if str(event.get("event_type") or "") in ADVANTAGE_EVENT_TYPES
        ]

    @classmethod
    def _validate_advantage_events(
        cls,
        connection: sqlite3.Connection,
        events: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any] | None:
        """Strictly dry-run Advantage deltas against accepted state."""

        advantage_events = cls._advantage_events(events)
        if not advantage_events:
            return None
        for index, event in enumerate(advantage_events):
            declared_schema = str(event.get("schema_version") or "")
            if declared_schema != ADVANTAGE_SCHEMA_VERSION:
                raise ContinuityError(
                    "ADVANTAGE_SCHEMA_VERSION_MISMATCH",
                    "advantage events must declare plot-rag-advantage/v1",
                    details={
                        "event_index": index,
                        "declared": declared_schema or None,
                        "supported": ADVANTAGE_SCHEMA_VERSION,
                    },
                )
        return validate_advantage_event_sequence(
            connection,
            advantage_events,
        )

    def save_proposal(
        self,
        *,
        events: Sequence[Mapping[str, Any]],
        payload: Mapping[str, Any] | None = None,
        artifact_id: str | None = None,
        artifact_kind: str = "story",
        artifact_stage: str | None = None,
        branch_id: str = "main",
        chapter_no: int | None = None,
        scene_index: int | None = None,
        artifact_revision: int | None = None,
        prepared_canon_revision: int | None = None,
        source_role: str | None = None,
        issues: Sequence[Mapping[str, Any]] = (),
        proposal_kind: str = "story_delta",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Save a model/hook delta as proposed-only state.

        This API never creates a canon commit and never rebuilds authority
        projections, which makes it suitable for Stop-hook routing.
        """

        stage = normalize_stage(artifact_stage)
        branch = str(branch_id or "main").strip() or "main"
        for index, raw_event in enumerate(events):
            if not isinstance(raw_event, Mapping):
                raise ContinuityError(
                    "INVALID_EVENT",
                    "event must be an object",
                    details={"event_index": index},
                )
            validate_event_branch_consistency(
                raw_event,
                branch,
                event_id=f"proposal-input:{index}",
            )
            expand_correction_event(
                str(
                    raw_event.get("event_type")
                    or raw_event.get("type")
                    or "fact"
                ),
                raw_event,
                event_id=f"proposal-input:{index}",
            )
        chapter = validate_positive_int(
            chapter_no, "chapter_no", allow_none=True, minimum=1
        )
        scene = validate_positive_int(
            scene_index, "scene_index", allow_none=True, minimum=0
        )
        artifact_revision = validate_positive_int(
            artifact_revision,
            "artifact_revision",
            allow_none=True,
            minimum=1,
        )
        prepared_canon_revision = validate_positive_int(
            prepared_canon_revision,
            "prepared_canon_revision",
            allow_none=True,
            minimum=0,
        )
        role = normalize_source_role(
            source_role
            or (
                "outline"
                if stage == "outline"
                else "canon"
                if stage in {"final", "published"}
                else "setting"
                if stage == "bootstrap"
                else "draft"
            )
        )
        normalized_events = [
            normalize_event(
                event,
                artifact_stage=stage,
                branch_id=branch,
                chapter_no=chapter,
                scene_index=scene,
            )
            for event in events
        ]
        for index, normalized_event in enumerate(normalized_events):
            validate_event_branch_consistency(
                normalized_event,
                branch,
                event_id=f"proposal-normalized:{index}",
            )
            validate_correction_link_consistency(
                normalized_event,
                event_id=f"proposal-normalized:{index}",
            )
        expanded_validation_events = self._expanded_validation_events(
            normalized_events
        )
        effective_issues = [
            dict(issue) for issue in issues
        ] + self._item_proposal_diagnostics(expanded_validation_events)
        normalized_proposal_kind = normalize_text(
            str(proposal_kind or "story_delta")
        )
        contains_power_spec = any(
            event.get("event_type") == "power_spec"
            for event in expanded_validation_events
        )
        contains_non_spec = any(
            event.get("event_type") != "power_spec"
            for event in expanded_validation_events
        )
        if contains_power_spec and (
            normalized_proposal_kind != "power_spec_change"
            or contains_non_spec
        ):
            raise ContinuityError(
                "POWER_SPEC_PROPOSAL_REQUIRED",
                "power specification changes require a dedicated power_spec_change proposal",
                details={
                    "proposal_kind": normalized_proposal_kind,
                    "contains_non_spec_events": contains_non_spec,
                },
            )
        if normalized_proposal_kind == "power_spec_change" and (
            not contains_power_spec or contains_non_spec
        ):
            raise ContinuityError(
                "POWER_SPEC_PROPOSAL_REQUIRED",
                "power_spec_change proposals may contain only power_spec events",
            )
        normalized_payload = dict(payload or {})
        if "lifecycle_identity" in normalized_payload:
            normalized_payload["lifecycle_identity"] = (
                self._normalize_lifecycle_identity(
                    normalized_payload.get("lifecycle_identity")
                )
            )
        if normalized_payload.get("lifecycle_identity"):
            validate_advantage_experience_contract_bindings(
                normalized_events,
                required=True,
            )
        provisional_request = {
            "artifact_id": artifact_id,
            "artifact_kind": artifact_kind,
            "artifact_stage": stage,
            "branch_id": branch,
            "chapter_no": chapter,
            "scene_index": scene,
            "artifact_revision": artifact_revision,
            "source_role": role,
            "proposal_kind": normalized_proposal_kind,
            "payload": normalized_payload,
            "events": normalized_events,
            "issues": effective_issues,
        }
        request_hash = stable_hash(
            provisional_request, prefix="proposal_request_"
        )
        with self._write_transaction() as connection:
            retry = self._idempotency_lookup(
                connection,
                "save_proposal",
                idempotency_key,
                request_hash,
            )
            if retry is not None:
                return retry
            # Idempotency is checked before any state-dependent dry-run.
            # A retry must return the immutable first response even when the
            # accepted Advantage/item state has changed since the request was
            # originally saved (for example, charges were consumed meanwhile).
            self._validate_advantage_events(
                connection,
                expanded_validation_events,
            )

            active_revision = self.store.get_meta_int(
                connection, "active_canon_revision"
            )
            prepared_revision = (
                active_revision
                if prepared_canon_revision is None
                else prepared_canon_revision
            )
            resolved_artifact_id = artifact_id or stable_hash(
                [
                    artifact_kind,
                    normalized_proposal_kind,
                    branch,
                    chapter,
                    normalized_payload,
                ],
                prefix="artifact_",
            )
            pending_proposal = {
                "proposal_id": "",
                "artifact_id": resolved_artifact_id,
                "artifact_stage": stage,
                "branch_id": branch,
                "chapter_no": chapter,
                "scene_index": scene,
                "artifact_revision": artifact_revision or 1,
                "prepared_canon_revision": prepared_revision,
            }
            lifecycle_binding = self._proposal_lifecycle_binding(
                pending_proposal,
                normalized_payload,
            )
            if lifecycle_binding:
                self._validate_receipt_lifecycle_binding(
                    connection,
                    pending_proposal,
                    normalized_payload,
                    lifecycle_binding,
                    allow_assistant_bind=True,
                )
                self._validate_active_projection_binding(
                    connection,
                    lifecycle_binding,
                )
            payload_hash = stable_hash(
                {
                    "payload": normalized_payload,
                    "events": normalized_events,
                },
                prefix="payload_",
            )

            duplicate = connection.execute(
                """
                SELECT p.*
                FROM proposals AS p
                WHERE p.artifact_id=?
                  AND p.artifact_stage=?
                  AND p.branch_id=?
                  AND p.chapter_no IS ?
                  AND p.scene_index IS ?
                  AND p.prepared_canon_revision=?
                  AND p.payload_hash=?
                  AND p.events_json=?
                ORDER BY p.artifact_revision DESC
                LIMIT 1
                """,
                (
                    resolved_artifact_id,
                    stage,
                    branch,
                    chapter,
                    scene,
                    prepared_revision,
                    payload_hash,
                    canonical_json(normalized_events),
                ),
            ).fetchone()
            if duplicate is not None:
                response = self._proposal_response(connection, duplicate)
                self._idempotency_store(
                    connection,
                    "save_proposal",
                    idempotency_key,
                    request_hash,
                    response,
                )
                return response

            revision = artifact_revision
            if revision is None:
                revision = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(artifact_revision), 0) + 1
                        FROM artifacts
                        WHERE artifact_id=? AND branch_id=?
                        """,
                        (resolved_artifact_id, branch),
                    ).fetchone()[0]
                )
            revision = validate_positive_int(
                revision,
                "artifact_revision",
                allow_none=False,
                minimum=1,
            )
            validate_proposal_metadata(
                artifact_stage=stage,
                canon_status="proposed",
                branch_id=branch,
                artifact_revision=revision,
                chapter_no=chapter,
                scene_index=scene,
                source_role=role,
            )

            issue_rows: list[dict[str, Any]] = []
            quarantined = False
            for index, issue in enumerate(effective_issues):
                severity = normalize_text(str(issue.get("severity") or "error"))
                if severity not in {"info", "warning", "error", "critical"}:
                    severity = "error"
                quarantined = quarantined or severity in {"error", "critical"}
                issue_rows.append(
                    {
                        "issue_code": str(
                            issue.get("code") or f"ISSUE_{index + 1}"
                        ),
                        "severity": severity,
                        "message": str(issue.get("message") or ""),
                        "details": dict(issue.get("details") or {}),
                    }
                )

            proposal_content = {
                **provisional_request,
                "artifact_id": resolved_artifact_id,
                "artifact_revision": revision,
                "prepared_canon_revision": prepared_revision,
            }
            proposal_id = stable_hash(
                proposal_content, prefix="proposal_"
            )
            artifact_version_id = stable_hash(
                [resolved_artifact_id, branch, revision],
                prefix="artifact_version_",
            )
            now = utc_now()
            try:
                connection.execute(
                    """
                    INSERT INTO artifacts(
                        artifact_version_id, artifact_id, artifact_kind,
                        artifact_stage, canon_status, branch_id, chapter_no,
                        scene_index, artifact_revision, source_role,
                        content_hash, content_json, active, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        artifact_version_id,
                        resolved_artifact_id,
                        artifact_kind,
                        stage,
                        branch,
                        chapter,
                        scene,
                        revision,
                        role,
                        payload_hash,
                        canonical_json(normalized_payload),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ContinuityError(
                    "ARTIFACT_REVISION_CONFLICT",
                    "artifact revision already exists with different content",
                    details={
                        "artifact_id": resolved_artifact_id,
                        "branch_id": branch,
                        "artifact_revision": revision,
                    },
                ) from exc
            connection.execute(
                """
                INSERT INTO proposals(
                    proposal_id, artifact_version_id, artifact_id,
                    artifact_stage, canon_status, branch_id, chapter_no,
                    scene_index, artifact_revision, prepared_canon_revision,
                    source_role, proposal_kind, payload_hash, payload_json,
                    events_json, validation_status, status_reason,
                    accepted_commit_id, created_at, updated_at
                ) VALUES(
                    ?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, '', NULL, ?, ?
                )
                """,
                (
                    proposal_id,
                    artifact_version_id,
                    resolved_artifact_id,
                    stage,
                    branch,
                    chapter,
                    scene,
                    revision,
                    prepared_revision,
                    role,
                    proposal_kind,
                    payload_hash,
                    canonical_json(normalized_payload),
                    canonical_json(normalized_events),
                    "quarantined" if quarantined else "valid",
                    now,
                    now,
                ),
            )
            for issue in issue_rows:
                issue_id = stable_hash(
                    [proposal_id, issue], prefix="proposal_issue_"
                )
                connection.execute(
                    """
                    INSERT INTO proposal_issues(
                        issue_id, proposal_id, issue_code, severity,
                        message, details_json, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        issue_id,
                        proposal_id,
                        issue["issue_code"],
                        issue["severity"],
                        issue["message"],
                        canonical_json(issue["details"]),
                        now,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            response = self._proposal_response(connection, row)
            self._idempotency_store(
                connection,
                "save_proposal",
                idempotency_key,
                request_hash,
                response,
            )
            return response

    @staticmethod
    def _proposal_response(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        issues = [
            {
                "issue_id": str(issue["issue_id"]),
                "code": str(issue["issue_code"]),
                "severity": str(issue["severity"]),
                "message": str(issue["message"]),
                "details": _json_load(str(issue["details_json"]), {}),
            }
            for issue in connection.execute(
                """
                SELECT * FROM proposal_issues
                WHERE proposal_id=?
                ORDER BY severity DESC, issue_code, issue_id
                """,
                (row["proposal_id"],),
            )
        ]
        return {
            "proposal_id": str(row["proposal_id"]),
            "artifact_id": str(row["artifact_id"]),
            "artifact_stage": str(row["artifact_stage"]),
            "canon_status": str(row["canon_status"]),
            "branch_id": str(row["branch_id"]),
            "chapter_no": row["chapter_no"],
            "scene_index": row["scene_index"],
            "artifact_revision": int(row["artifact_revision"]),
            "prepared_canon_revision": int(row["prepared_canon_revision"]),
            "proposal_kind": str(row["proposal_kind"]),
            "payload_hash": str(row["payload_hash"]),
            "payload": _json_load(str(row["payload_json"]), {}),
            "events": _json_load(str(row["events_json"]), []),
            "validation_status": str(row["validation_status"]),
            "status_reason": str(row["status_reason"]),
            "accepted_commit_id": row["accepted_commit_id"],
            "issues": issues,
        }

    def inspect_proposal(self, proposal_id: str) -> dict[str, Any]:
        with self.store.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise ContinuityError(
                    "PROPOSAL_NOT_FOUND", f"unknown proposal: {proposal_id}"
                )
            return self._proposal_response(connection, row)

    def list_proposals(
        self,
        *,
        canon_status: str | None = None,
        branch_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if canon_status:
            clauses.append("canon_status=?")
            params.append(canon_status)
        if branch_id:
            clauses.append("branch_id=?")
            params.append(branch_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.store.read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM proposals
                {where}
                ORDER BY created_at DESC, proposal_id
                """,
                params,
            ).fetchall()
            return [self._proposal_response(connection, row) for row in rows]

    def reject_proposal(
        self,
        proposal_id: str,
        *,
        reason: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        request_hash = stable_hash(
            {"proposal_id": proposal_id, "reason": reason},
            prefix="reject_request_",
        )
        with self.store.transaction() as connection:
            retry = self._idempotency_lookup(
                connection,
                "reject_proposal",
                idempotency_key,
                request_hash,
            )
            if retry is not None:
                return retry
            row = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise ContinuityError(
                    "PROPOSAL_NOT_FOUND", f"unknown proposal: {proposal_id}"
                )
            status = str(row["canon_status"])
            if status == "rejected":
                response = self._proposal_response(connection, row)
            elif status != "proposed":
                raise ContinuityError(
                    "INVALID_PROPOSAL_TRANSITION",
                    f"cannot reject a {status} proposal",
                )
            else:
                now = utc_now()
                connection.execute(
                    """
                    UPDATE proposals
                    SET canon_status='rejected', status_reason=?, updated_at=?
                    WHERE proposal_id=?
                    """,
                    (reason, now, proposal_id),
                )
                connection.execute(
                    """
                    UPDATE artifacts
                    SET canon_status='rejected', updated_at=?
                    WHERE artifact_version_id=?
                    """,
                    (now, row["artifact_version_id"]),
                )
                updated = connection.execute(
                    "SELECT * FROM proposals WHERE proposal_id=?",
                    (proposal_id,),
                ).fetchone()
                response = self._proposal_response(connection, updated)
            self._idempotency_store(
                connection,
                "reject_proposal",
                idempotency_key,
                request_hash,
                response,
            )
            return response

    # ------------------------------------------------------------------
    # Strict acceptance and retraction
    # ------------------------------------------------------------------
    @staticmethod
    def _load_grant(
        connection: sqlite3.Connection,
        approval_id: str,
    ) -> tuple[sqlite3.Row, str]:
        token_hash = stable_hash(approval_id, prefix="grant_token_")
        row = connection.execute(
            "SELECT * FROM approval_grants WHERE token_hash=?",
            (token_hash,),
        ).fetchone()
        if row is None:
            raise ContinuityError(
                "APPROVAL_GRANT_NOT_FOUND",
                "approval grant is missing or invalid",
            )
        return row, token_hash

    @staticmethod
    def _event_entity_references(event: Mapping[str, Any]) -> set[str]:
        keys = {
            "entity_id",
            "source_entity_id",
            "target_entity_id",
            "actor_entity_id",
            "from_location_entity_id",
            "to_location_entity_id",
            "item_entity_id",
            "from_owner_entity_id",
            "to_owner_entity_id",
            "owner_entity_id",
            "ability_entity_id",
            "believer_entity_id",
            "observer_entity_id",
            "subject_entity_id",
            "spec_entity_id",
            "system_entity_id",
            "track_entity_id",
            "from_rank_entity_id",
            "to_rank_entity_id",
            "rank_edge_entity_id",
            "resource_entity_id",
            "target_resource_entity_id",
            "conversion_rule_entity_id",
            "status_entity_id",
            "source_entity_id",
            "qualification_entity_id",
            "source_resource_entity_id",
            "source_system_entity_id",
            "target_system_entity_id",
            "from_legal_owner_entity_id",
            "to_legal_owner_entity_id",
            "from_custodian_entity_id",
            "to_custodian_entity_id",
            "from_carrier_entity_id",
            "to_carrier_entity_id",
            "from_location_entity_id",
            "to_location_entity_id",
            "from_access_controller_entity_id",
            "to_access_controller_entity_id",
        }
        refs = {
            str(value)
            for key, value in event.items()
            if key in keys and value
        }
        for list_field in (
            "ability_entity_ids",
            "from_rank_entity_ids",
            "granted_ability_ids",
        ):
            refs.update(
                str(value)
                for value in event.get(list_field) or []
                if value
            )
        if event.get("event_type") == "power_spec":
            definition = dict(event.get("definition") or {})
            refs.update(
                ContinuityService._event_entity_references(
                    {
                        **definition,
                        "event_type": "_power_spec_definition",
                    }
                )
            )
        if event.get("event_type") == "item_spec":
            definition = dict(event.get("definition") or {})
            refs.update(
                ContinuityService._event_entity_references(
                    {
                        **definition,
                        "event_type": "_item_spec_definition",
                    }
                )
            )
        if event.get("event_type") in {
            "correction",
            "item_correction",
            "advantage_correction",
        }:
            expanded = expand_correction_event(
                str(event.get("event_type")),
                event,
            )
            if expanded is not None:
                _leaf_type, leaf = expanded
                refs.update(
                    ContinuityService._event_entity_references(leaf)
                )
        return refs

    @staticmethod
    def _event_endpoint_contract(
        event: Mapping[str, Any],
    ) -> list[tuple[str, tuple[str, ...], bool]]:
        """Return ``(field, allowed types, is_list)`` endpoint contracts."""

        event_type = str(event.get("event_type") or "")
        actor_types = ("character", "group", "summon")
        contracts: list[tuple[str, tuple[str, ...], bool]] = []
        if event_type in {
            "correction",
            "item_correction",
            "advantage_correction",
        }:
            expanded = expand_correction_event(event_type, event)
            return (
                []
                if expanded is None
                else ContinuityService._event_endpoint_contract(expanded[1])
            )
        if event_type == "ability":
            contracts.extend(
                (
                    ("owner_entity_id", actor_types, False),
                    ("ability_entity_id", ("ability",), False),
                )
            )
        elif event_type == "progression":
            contracts.extend(
                (
                    ("actor_entity_id", actor_types, False),
                    ("track_entity_id", ("progression_track",), False),
                    ("from_rank_entity_id", ("rank_node",), False),
                    ("to_rank_entity_id", ("rank_node",), False),
                    ("rank_edge_entity_id", ("rank_edge",), False),
                )
            )
        elif event_type == "resource":
            contracts.extend(
                (
                    ("actor_entity_id", actor_types, False),
                    ("resource_entity_id", ("resource_pool",), False),
                    (
                        "target_resource_entity_id",
                        ("resource_pool",),
                        False,
                    ),
                    (
                        "conversion_rule_entity_id",
                        ("conversion_rule",),
                        False,
                    ),
                )
            )
        elif event_type == "status_effect":
            contracts.extend(
                (
                    ("actor_entity_id", actor_types, False),
                    ("status_entity_id", ("status_effect",), False),
                )
            )
        elif event_type == "power_binding":
            contracts.extend(
                (
                    ("actor_entity_id", actor_types, False),
                    (
                        "source_entity_id",
                        (
                            "item",
                            "ability",
                            "bloodline",
                            "contract",
                            "faction",
                            "role",
                            "system",
                            "power_system",
                            "summon",
                        ),
                        False,
                    ),
                    ("ability_entity_ids", ("ability",), True),
                )
            )
        elif event_type == "qualification":
            contracts.extend(
                (
                    ("actor_entity_id", actor_types, False),
                    (
                        "qualification_entity_id",
                        ("qualification",),
                        False,
                    ),
                )
            )
        elif event_type == "power_observation":
            contracts.extend(
                (
                    ("observer_entity_id", actor_types, False),
                    ("subject_entity_id", actor_types, False),
                    ("ability_entity_id", ("ability",), False),
                )
            )
        elif event_type == "power_spec":
            spec_type = str(event.get("spec_type") or "")
            expected_spec_type = {
                "power_system": "power_system",
                "progression_track": "progression_track",
                "rank_node": "rank_node",
                "rank_edge": "rank_edge",
                "ability_definition": "ability",
                "resource_definition": "resource_pool",
                "status_definition": "status_effect",
                "qualification_definition": "qualification",
                "counter_rule": "counter_rule",
                "bridge_rule": "bridge_rule",
                "conversion_rule": "conversion_rule",
            }.get(spec_type)
            if expected_spec_type:
                contracts.append(
                    ("spec_entity_id", (expected_spec_type,), False)
                )
            definition = dict(event.get("definition") or {})
            contracts.extend(
                (
                    ("system_entity_id", ("power_system",), False),
                    (
                        "track_entity_id",
                        ("progression_track",),
                        False,
                    ),
                    ("from_rank_entity_ids", ("rank_node",), True),
                    ("to_rank_entity_id", ("rank_node",), False),
                    (
                        "source_resource_entity_id",
                        ("resource_pool",),
                        False,
                    ),
                    (
                        "target_resource_entity_id",
                        ("resource_pool",),
                        False,
                    ),
                    (
                        "source_system_entity_id",
                        ("power_system",),
                        False,
                    ),
                    (
                        "target_system_entity_id",
                        ("power_system",),
                        False,
                    ),
                )
            )
            return [
                (field, types, is_list)
                for field, types, is_list in contracts
                if (
                    field == "spec_entity_id"
                    and event.get(field) is not None
                )
                or definition.get(field) is not None
            ]
        elif event_type == "item_spec":
            spec_type = str(event.get("spec_type") or "")
            definition = dict(event.get("definition") or {})
            if spec_type == "item_definition":
                contracts.append(("item_entity_id", ("item",), False))
            elif spec_type == "function_definition":
                contracts.append(
                    ("granted_ability_ids", ("ability",), True)
                )
            return [
                (field, types, is_list)
                for field, types, is_list in contracts
                if event.get(field) is not None
                or definition.get(field) is not None
            ]
        elif event_type == "item_instance":
            contracts.append(("item_entity_id", ("item",), False))
        elif event_type == "item_custody":
            location_types = (
                "location",
                "region",
                "place",
                "building",
                "vehicle",
                "world",
                "zone",
            )
            contracts.extend(
                (
                    ("from_legal_owner_entity_id", actor_types, False),
                    ("to_legal_owner_entity_id", actor_types, False),
                    ("from_custodian_entity_id", actor_types, False),
                    ("to_custodian_entity_id", actor_types, False),
                    ("from_carrier_entity_id", actor_types, False),
                    ("to_carrier_entity_id", actor_types, False),
                    (
                        "from_access_controller_entity_id",
                        actor_types,
                        False,
                    ),
                    (
                        "to_access_controller_entity_id",
                        actor_types,
                        False,
                    ),
                    ("from_location_entity_id", location_types, False),
                    ("to_location_entity_id", location_types, False),
                )
            )
        elif event_type == "item_runtime":
            contracts.append(("actor_entity_id", actor_types, False))
        elif event_type == "item_use":
            contracts.append(("actor_entity_id", actor_types, False))
        elif event_type == "item_observation":
            contracts.append(("observer_entity_id", actor_types, False))
        return [
            (field, types, is_list)
            for field, types, is_list in contracts
            if event.get(field) is not None
        ]

    @staticmethod
    def _validate_entity_endpoints(
        connection: sqlite3.Connection,
        events: Sequence[Mapping[str, Any]],
    ) -> None:
        proposed_entities: dict[str, str] = {}
        for event in events:
            expanded = expand_correction_event(
                str(event.get("event_type") or "fact"),
                event,
            )
            if expanded is None:
                continue
            leaf_type, leaf = expanded
            if leaf_type == "entity" and leaf.get("entity_id"):
                proposed_entities[str(leaf["entity_id"])] = normalize_text(
                    str(leaf.get("entity_type") or "unknown")
                )
        refs: set[str] = set()
        for event in events:
            refs.update(ContinuityService._event_entity_references(event))
        existing = {
            str(row["entity_id"]): normalize_text(str(row["entity_type"]))
            for row in connection.execute(
                "SELECT entity_id, entity_type FROM entities"
            ).fetchall()
        }
        entity_types = {**existing, **proposed_entities}
        missing = sorted(refs - set(entity_types))
        if missing:
            raise ContinuityError(
                "UNKNOWN_EVENT_ENTITY",
                "event endpoints must resolve to registered entities",
                details={"entity_ids": missing},
            )
        for event in events:
            expanded = expand_correction_event(
                str(event.get("event_type") or "fact"),
                event,
            )
            if expanded is None:
                continue
            _leaf_type, endpoint_event = expanded
            definition = dict(endpoint_event.get("definition") or {})
            for field, allowed, is_list in (
                ContinuityService._event_endpoint_contract(endpoint_event)
            ):
                raw_value = (
                    endpoint_event.get(field)
                    if field == "spec_entity_id"
                    or endpoint_event.get(field) is not None
                    else definition.get(field)
                )
                values = list(raw_value or []) if is_list else [raw_value]
                for value in values:
                    if value is None or str(value).strip() == "":
                        continue
                    entity_id = str(value)
                    actual = entity_types.get(entity_id, "unknown")
                    if actual != "unknown" and actual not in allowed:
                        is_item_event = str(
                            endpoint_event.get("event_type") or ""
                        ).startswith("item_")
                        raise ContinuityError(
                            (
                                "ITEM_ENTITY_TYPE_MISMATCH"
                                if is_item_event
                                else "POWER_ENTITY_TYPE_MISMATCH"
                            ),
                            (
                                "item event endpoint has an incompatible entity type"
                                if is_item_event
                                else "power event endpoint has an incompatible entity type"
                            ),
                            details={
                                "event_type": endpoint_event.get(
                                    "event_type"
                                ),
                                "field": field,
                                "entity_id": entity_id,
                                "expected_entity_types": list(allowed),
                                "actual_entity_type": actual,
                            },
                        )

    @staticmethod
    def _validate_event_links(
        connection: sqlite3.Connection,
        proposal: sqlite3.Row,
        events: Sequence[Mapping[str, Any]],
    ) -> None:
        existing = {
            str(row["event_id"]): {
                "branch_id": str(row["branch_id"]),
                "changes_authority": bool(row["changes_authority"]),
            }
            for row in connection.execute(
                """
                SELECT e.event_id, e.branch_id, c.changes_authority
                FROM continuity_events AS e
                JOIN canon_commits AS c ON c.commit_id=e.commit_id
                WHERE c.operation='accept'
                """
            )
        }
        proposal_branch = str(proposal["branch_id"])
        proposal_authority = changes_authority(
            str(proposal["artifact_stage"]),
            proposal_branch,
        )
        for index, event in enumerate(events):
            validate_event_branch_consistency(
                event,
                proposal_branch,
                event_id=f"{proposal['proposal_id']}:{index}",
            )
            validate_correction_link_consistency(
                event,
                event_id=f"{proposal['proposal_id']}:{index}",
            )
            for field in ("supersedes", "retracts", "caused_by"):
                targets = [str(target) for target in event.get(field) or []]
                if len(targets) != len(set(targets)):
                    raise ContinuityError(
                        "INVALID_EVENT_LINK",
                        f"{field} contains duplicate event ids",
                        details={"field": field, "event_index": index},
                    )
                missing = sorted(set(targets) - set(existing))
                if missing:
                    raise ContinuityError(
                        "EVENT_LINK_TARGET_NOT_FOUND",
                        f"{field} references unknown accepted event(s)",
                        details={"field": field, "event_ids": missing},
                    )
                if field not in {"supersedes", "retracts"}:
                    continue
                for target in targets:
                    target_info = existing[target]
                    if target_info["branch_id"] != proposal_branch:
                        raise ContinuityError(
                            "EVENT_LINK_BRANCH_MISMATCH",
                            f"{field} cannot cross continuity branches",
                            details={
                                "field": field,
                                "event_id": target,
                                "proposal_branch_id": proposal_branch,
                                "target_branch_id": target_info["branch_id"],
                            },
                        )
                    if (
                        target_info["changes_authority"]
                        != proposal_authority
                    ):
                        raise ContinuityError(
                            "EVENT_LINK_AUTHORITY_MISMATCH",
                            f"{field} cannot cross authority planes",
                            details={
                                "field": field,
                                "event_id": target,
                                "proposal_changes_authority": (
                                    proposal_authority
                                ),
                                "target_changes_authority": target_info[
                                    "changes_authority"
                                ],
                            },
                        )

    @staticmethod
    def _validate_invariants(
        connection: sqlite3.Connection,
        proposal: sqlite3.Row,
        events: Sequence[Mapping[str, Any]],
    ) -> None:
        semantic_events = ContinuityService._expanded_validation_events(
            events
        )
        ContinuityService._validate_entity_endpoints(connection, events)
        ContinuityService._validate_event_links(
            connection,
            proposal,
            events,
        )
        proposal_kind = str(proposal["proposal_kind"])
        has_power_spec = any(
            event.get("event_type") == "power_spec"
            for event in semantic_events
        )
        if has_power_spec and (
            proposal_kind != "power_spec_change"
            or any(
                event.get("event_type") != "power_spec"
                for event in semantic_events
            )
        ):
            raise ContinuityError(
                "POWER_SPEC_PROPOSAL_REQUIRED",
                "power specification events require an isolated power_spec_change proposal",
            )

        if has_power_spec:
            spec_tables = {
                "power_system": ("power_system_specs", "spec_entity_id"),
                "progression_track": (
                    "progression_tracks",
                    "track_entity_id",
                ),
                "rank_node": ("rank_nodes", "rank_entity_id"),
                "rank_edge": ("rank_edges", "edge_entity_id"),
                "ability_definition": (
                    "ability_definitions",
                    "ability_entity_id",
                ),
                "resource_definition": (
                    "resource_definitions",
                    "resource_entity_id",
                ),
                "status_definition": (
                    "status_definitions",
                    "status_entity_id",
                ),
                "qualification_definition": (
                    "qualification_definitions",
                    "qualification_entity_id",
                ),
                "counter_rule": ("counter_rules", "rule_entity_id"),
                "bridge_rule": ("bridge_rules", "rule_entity_id"),
                "conversion_rule": (
                    "conversion_rules",
                    "rule_entity_id",
                ),
            }
            known_specs: dict[str, set[str]] = {}
            for spec_type, (table, column) in spec_tables.items():
                known_specs[spec_type] = {
                    str(row[0])
                    for row in connection.execute(
                        f"SELECT {column} FROM {table}"
                    )
                }
            for event in semantic_events:
                spec_type = str(event["spec_type"])
                spec_id = str(event["spec_entity_id"])
                action = str(event["action"])
                exists = spec_id in known_specs[spec_type]
                if action == "define":
                    known_specs[spec_type].add(spec_id)
                elif not exists:
                    raise ContinuityError(
                        "POWER_SPEC_NOT_FOUND",
                        "power specification amend/deprecate requires an accepted definition",
                        details={
                            "spec_type": spec_type,
                            "spec_entity_id": spec_id,
                            "action": action,
                        },
                    )

            conversion_definitions: dict[str, dict[str, Any]] = {
                str(row["rule_entity_id"]): dict(
                    _json_load(str(row["definition_json"]), {})
                )
                for row in connection.execute(
                    """
                    SELECT rule_entity_id, definition_json
                    FROM conversion_rules
                    WHERE rule_status!='deprecated'
                    """
                )
            }
            for event in semantic_events:
                if event.get("spec_type") != "conversion_rule":
                    continue
                rule_id = str(event["spec_entity_id"])
                if event.get("action") == "deprecate":
                    conversion_definitions.pop(rule_id, None)
                else:
                    previous = conversion_definitions.get(rule_id, {})
                    conversion_definitions[rule_id] = {
                        **previous,
                        **dict(event.get("definition") or {}),
                    }

            graph: dict[str, list[tuple[str, float, str]]] = {}
            for rule_id, definition in conversion_definitions.items():
                source = str(
                    definition.get("source_resource_entity_id") or ""
                )
                target = str(
                    definition.get("target_resource_entity_id") or ""
                )
                try:
                    ratio = float(definition.get("ratio", 0))
                    fixed_cost = float(definition.get("fixed_cost", 0))
                except (TypeError, ValueError):
                    continue
                if (
                    not source
                    or not target
                    or ratio <= 0
                    or fixed_cost > 0
                ):
                    continue
                graph.setdefault(source, []).append(
                    (target, ratio, rule_id)
                )
            nodes = sorted(
                set(graph)
                | {
                    target
                    for edges in graph.values()
                    for target, _, _ in edges
                }
            )
            for start in nodes:
                stack: list[
                    tuple[str, float, tuple[str, ...], frozenset[str]]
                ] = [(start, 1.0, (), frozenset({start}))]
                while stack:
                    node, product, rule_path, visited = stack.pop()
                    for target, ratio, rule_id in graph.get(node, []):
                        next_product = product * ratio
                        next_path = (*rule_path, rule_id)
                        if target == start and next_product > 1.0 + 1e-12:
                            raise ContinuityError(
                                "POWER_CONVERSION_ARBITRAGE",
                                "conversion rules contain a zero-cost net-gain cycle",
                                details={
                                    "resource_entity_id": start,
                                    "rule_entity_ids": list(next_path),
                                    "net_ratio": next_product,
                                },
                            )
                        if target in visited or len(next_path) >= len(nodes):
                            continue
                        stack.append(
                            (
                                target,
                                next_product,
                                next_path,
                                visited | {target},
                            )
                        )

        authoritative = changes_authority(
            str(proposal["artifact_stage"]), str(proposal["branch_id"])
        )
        if not authoritative:
            return

        locations: dict[str, dict[str, Any]] = {
            str(row["actor_entity_id"]): {
                "location": row["location_entity_id"],
                "chapter_no": row["chapter_no"],
                "scene_index": row["scene_index"],
            }
            for row in connection.execute("SELECT * FROM location_state")
        }
        unique_items: dict[str, str | None] = {
            str(row["item_entity_id"]): (
                str(row["owner_entity_id"])
                if row["owner_entity_id"] is not None
                else None
            )
            for row in connection.execute(
                "SELECT * FROM inventory_state WHERE is_unique=1"
            )
        }
        ability_ownership: dict[tuple[str, str], dict[str, Any]] = {}
        for row in connection.execute("SELECT * FROM actor_ability_state"):
            ability_ownership[
                (
                    str(row["owner_entity_id"]),
                    str(row["ability_entity_id"]),
                )
            ] = {
                **dict(_json_load(str(row["ownership_json"]), {})),
                "acquired": bool(row["acquired"]),
            }
        # A just-migrated database may still be queried before its first v5
        # replay.  Preserve the active legacy compatibility rows as a fallback.
        for row in connection.execute("SELECT * FROM ability_state"):
            key = (
                str(row["owner_entity_id"]),
                str(row["ability_entity_id"]),
            )
            ability_ownership.setdefault(
                key,
                {
                    **dict(_json_load(str(row["state_json"]), {})),
                    "acquired": True,
                },
            )
        ability_runtime = {
            (
                str(row["owner_entity_id"]),
                str(row["ability_entity_id"]),
            ): dict(_json_load(str(row["runtime_json"]), {}))
            for row in connection.execute(
                "SELECT * FROM ability_runtime_state"
            )
        }
        progression_state = {
            (
                str(row["actor_entity_id"]),
                str(row["track_entity_id"]),
            ): {
                **dict(_json_load(str(row["state_json"]), {})),
                "rank_entity_id": row["rank_entity_id"],
            }
            for row in connection.execute(
                "SELECT * FROM actor_progression_state"
            )
        }
        resource_state = {
            (
                str(row["actor_entity_id"]),
                str(row["resource_entity_id"]),
            ): {
                **dict(_json_load(str(row["state_json"]), {})),
                "balance": float(row["balance"]),
                "reserved": float(row["reserved"]),
            }
            for row in connection.execute(
                "SELECT * FROM actor_resource_state"
            )
        }
        status_state = {
            (
                str(row["actor_entity_id"]),
                str(row["status_entity_id"]),
            ): {
                **dict(_json_load(str(row["state_json"]), {})),
                "active": bool(row["active"]),
                "stacks": int(row["stacks"]),
            }
            for row in connection.execute("SELECT * FROM actor_status_state")
        }
        binding_state = {
            (
                str(row["actor_entity_id"]),
                str(row["binding_id"]),
            ): {
                **dict(_json_load(str(row["state_json"]), {})),
                "active": bool(row["active"]),
                "source_entity_id": str(row["source_entity_id"]),
                "ability_entity_ids": list(
                    _json_load(str(row["ability_entity_ids_json"]), [])
                ),
            }
            for row in connection.execute("SELECT * FROM power_bindings")
        }
        qualification_state = {
            (
                str(row["actor_entity_id"]),
                str(row["qualification_entity_id"]),
            ): {
                **dict(_json_load(str(row["state_json"]), {})),
                "active": bool(row["active"]),
                "quantity": float(row["quantity"]),
            }
            for row in connection.execute(
                "SELECT * FROM qualification_state"
            )
        }
        ability_definitions = {
            str(row["ability_entity_id"]): dict(
                _json_load(str(row["definition_json"]), {})
            )
            for row in connection.execute(
                """
                SELECT ability_entity_id, definition_json
                FROM ability_definitions
                WHERE definition_status!='deprecated'
                """
            )
        }
        resource_definitions = {
            str(row["resource_entity_id"]): dict(
                _json_load(str(row["definition_json"]), {})
            )
            for row in connection.execute(
                """
                SELECT resource_entity_id, definition_json
                FROM resource_definitions
                WHERE definition_status!='deprecated'
                """
            )
        }
        rank_edges = {
            str(row["edge_entity_id"]): {
                **dict(_json_load(str(row["definition_json"]), {})),
                "track_entity_id": row["track_entity_id"],
                "from_rank_entity_ids": list(
                    _json_load(str(row["from_rank_ids_json"]), [])
                ),
                "to_rank_entity_id": row["to_rank_entity_id"],
                "status": str(row["edge_status"]),
            }
            for row in connection.execute(
                "SELECT * FROM rank_edges WHERE edge_status!='deprecated'"
            )
        }
        conversion_rules = {
            str(row["rule_entity_id"]): dict(
                _json_load(str(row["definition_json"]), {})
            )
            for row in connection.execute(
                """
                SELECT rule_entity_id, definition_json
                FROM conversion_rules
                WHERE rule_status!='deprecated'
                """
            )
        }
        qualification_definitions = {
            str(row["qualification_entity_id"])
            for row in connection.execute(
                """
                SELECT qualification_entity_id
                FROM qualification_definitions
                WHERE definition_status='active'
                """
            )
        }
        required_costs: dict[tuple[str, str], float] = {}
        applied_spends: dict[tuple[str, str], float] = {}

        def compare_coordinates(
            actual: Mapping[str, Any] | None,
            expected: Mapping[str, Any] | None,
            *,
            code: str = "POWER_STORY_COORDINATE_UNKNOWN",
        ) -> int:
            if not actual or not expected:
                raise ContinuityError(
                    "POWER_STORY_COORDINATE_UNKNOWN",
                    "power timing requires comparable story coordinates",
                    details={"actual": actual, "expected": expected},
                )
            actual_calendar = str(actual.get("calendar_id") or "")
            expected_calendar = str(expected.get("calendar_id") or "")
            if (
                not actual_calendar
                or not expected_calendar
                or actual_calendar != expected_calendar
                or actual.get("ordinal") is None
                or expected.get("ordinal") is None
            ):
                raise ContinuityError(
                    "POWER_STORY_COORDINATE_UNKNOWN",
                    "story coordinates use different or incomplete calendars",
                    details={"actual": actual, "expected": expected},
                )
            actual_ordinal = actual["ordinal"]
            expected_ordinal = expected["ordinal"]
            if (
                type(actual_ordinal) is not int
                or type(expected_ordinal) is not int
            ):
                raise ContinuityError(
                    "POWER_STORY_COORDINATE_UNKNOWN",
                    "story coordinate ordinals must be integers",
                    details={"actual": actual, "expected": expected},
                )
            if actual_ordinal < expected_ordinal and code != (
                "POWER_STORY_COORDINATE_UNKNOWN"
            ):
                raise ContinuityError(
                    code,
                    "the story coordinate has not reached the required point",
                    details={"actual": dict(actual), "expected": dict(expected)},
                )
            return (actual_ordinal > expected_ordinal) - (
                actual_ordinal < expected_ordinal
            )

        def active_status(
            actor: str,
            status_id: str,
            coordinate: Mapping[str, Any] | None,
        ) -> bool:
            state = status_state.get((actor, status_id), {})
            if not state.get("active"):
                return False
            expires = state.get("expires_coordinate")
            if expires:
                if coordinate is None:
                    raise ContinuityError(
                        "POWER_STORY_COORDINATE_UNKNOWN",
                        "status expiry cannot be evaluated without story time",
                        details={
                            "actor_entity_id": actor,
                            "status_entity_id": status_id,
                        },
                    )
                return compare_coordinates(coordinate, expires) < 0
            return True

        def validate_prerequisites(
            actor: str,
            prerequisites: Mapping[str, Any] | None,
            event: Mapping[str, Any],
        ) -> None:
            rules = dict(prerequisites or {})
            missing: list[dict[str, Any]] = []
            for ability_id in rules.get("ability_entity_ids") or []:
                if not ability_ownership.get(
                    (actor, str(ability_id)), {}
                ).get("acquired"):
                    missing.append(
                        {"kind": "ability", "entity_id": ability_id}
                    )
            for qualification_id in (
                rules.get("qualification_entity_ids") or []
            ):
                qualification = qualification_state.get(
                    (actor, str(qualification_id)), {}
                )
                qualification_active = bool(
                    qualification.get("active")
                ) and float(qualification.get("quantity", 0)) > 0
                expires = qualification.get("expires_coordinate")
                if qualification_active and expires:
                    coordinate = event.get("story_coordinate")
                    if coordinate is None:
                        raise ContinuityError(
                            "POWER_STORY_COORDINATE_UNKNOWN",
                            "qualification expiry cannot be evaluated without story time",
                            details={
                                "actor_entity_id": actor,
                                "qualification_entity_id": qualification_id,
                            },
                        )
                    qualification_active = (
                        compare_coordinates(coordinate, expires) < 0
                    )
                if not qualification_active:
                    missing.append(
                        {
                            "kind": "qualification",
                            "entity_id": qualification_id,
                        }
                    )
            for binding_id in rules.get("binding_ids") or []:
                if not binding_state.get(
                    (actor, str(binding_id)), {}
                ).get("active"):
                    missing.append(
                        {"kind": "binding", "binding_id": binding_id}
                    )
            for requirement in rules.get("progression") or []:
                if not isinstance(requirement, Mapping):
                    continue
                track_id = str(requirement.get("track_entity_id") or "")
                rank_id = str(requirement.get("rank_entity_id") or "")
                current_rank = progression_state.get(
                    (actor, track_id), {}
                ).get("rank_entity_id")
                if not track_id or not rank_id or str(current_rank) != rank_id:
                    missing.append(
                        {
                            "kind": "progression",
                            "track_entity_id": track_id,
                            "required_rank_entity_id": rank_id,
                            "actual_rank_entity_id": current_rank,
                        }
                    )
            coordinate = event.get("story_coordinate")
            for status_id in rules.get("required_status_entity_ids") or []:
                if not active_status(actor, str(status_id), coordinate):
                    missing.append(
                        {"kind": "status", "entity_id": status_id}
                    )
            for status_id in rules.get("forbidden_status_entity_ids") or []:
                if active_status(actor, str(status_id), coordinate):
                    missing.append(
                        {
                            "kind": "forbidden_status",
                            "entity_id": status_id,
                        }
                    )
            if missing:
                raise ContinuityError(
                    "POWER_PREREQUISITE_UNMET",
                    "one or more power prerequisites are not satisfied",
                    details={
                        "actor_entity_id": actor,
                        "missing": missing,
                    },
                )
            allowed_locations = list(
                rules.get("location_entity_ids") or []
            )
            if allowed_locations:
                actual_location = locations.get(actor, {}).get("location")
                if actual_location not in allowed_locations:
                    raise ContinuityError(
                        "POWER_CONTEXT_CONDITION_UNMET",
                        "the actor is not at an allowed power-action location",
                        details={
                            "actor_entity_id": actor,
                            "actual_location_entity_id": actual_location,
                            "allowed_location_entity_ids": allowed_locations,
                        },
                    )
            minimum_coordinate = rules.get("minimum_story_coordinate")
            if minimum_coordinate:
                compare_coordinates(
                    event.get("story_coordinate"),
                    minimum_coordinate,
                    code="POWER_CONTEXT_CONDITION_UNMET",
                )

        def require_costs(
            actor: str,
            costs: Any,
        ) -> None:
            for cost in costs or []:
                if not isinstance(cost, Mapping):
                    continue
                resource_id = str(
                    cost.get("resource_entity_id") or ""
                ).strip()
                if not resource_id:
                    continue
                amount = float(cost.get("amount", 0))
                if amount <= 0:
                    continue
                key = (actor, resource_id)
                required_costs[key] = required_costs.get(key, 0.0) + amount

        for event in semantic_events:
            if (
                event.get("scope") != "current"
                or event.get("narrative_mode") == "flashback"
            ):
                continue
            event_type = event.get("event_type")
            if event_type == "correction":
                event = dict(event.get("replacement") or {})
                event_type = event.get("event_type")
            if event_type == "movement":
                actor = str(event["actor_entity_id"])
                current = locations.get(actor)
                origin = event.get("from_location_entity_id")
                destination = event.get("to_location_entity_id")
                chapter = event.get("chapter_no")
                scene = event.get("scene_index")
                if (
                    current
                    and origin
                    and current["location"] is not None
                    and str(current["location"]) != str(origin)
                ):
                    raise ContinuityError(
                        "MOVEMENT_ORIGIN_CONFLICT",
                        "movement origin does not match the accepted location",
                        details={
                            "actor_entity_id": actor,
                            "expected": current["location"],
                            "actual": origin,
                        },
                    )
                if (
                    current
                    and current["location"] is not None
                    and destination is not None
                    and str(current["location"]) != str(destination)
                    and current["chapter_no"] == chapter
                    and current["scene_index"] == scene
                    and not origin
                ):
                    raise ContinuityError(
                        "CONFLICTING_LOCATION",
                        "same-scene location change needs an explicit movement origin",
                        details={
                            "actor_entity_id": actor,
                            "existing_location": current["location"],
                            "new_location": destination,
                            "chapter_no": chapter,
                            "scene_index": scene,
                        },
                    )
                locations[actor] = {
                    "location": destination,
                    "chapter_no": chapter,
                    "scene_index": scene,
                }
            elif (
                event_type in {"fact", "state"}
                and str(event.get("field")) == "location"
            ):
                actor = str(event["entity_id"])
                destination = event.get("value")
                current = locations.get(actor)
                if current and current["location"] != destination:
                    raise ContinuityError(
                        "LOCATION_REQUIRES_MOVEMENT",
                        "conflicting current location must use a movement event",
                        details={"actor_entity_id": actor},
                    )
                locations[actor] = {
                    "location": destination,
                    "chapter_no": event.get("chapter_no"),
                    "scene_index": event.get("scene_index"),
                }
            elif event_type == "inventory" and bool(event.get("unique")):
                item = str(event["item_entity_id"])
                action = str(event["action"])
                current_owner = unique_items.get(item)
                from_owner = event.get("from_owner_entity_id")
                to_owner = event.get("to_owner_entity_id")
                if action == "transfer":
                    if current_owner is not None and str(from_owner) != str(
                        current_owner
                    ):
                        raise ContinuityError(
                            "INVENTORY_TRANSFER_OWNER_MISMATCH",
                            "unique item transfer does not start at its current owner",
                            details={
                                "item_entity_id": item,
                                "current_owner": current_owner,
                                "from_owner": from_owner,
                            },
                        )
                    unique_items[item] = str(to_owner)
                elif action in {"acquire", "set"}:
                    if (
                        current_owner is not None
                        and str(current_owner) != str(to_owner)
                    ):
                        raise ContinuityError(
                            "UNIQUE_ITEM_DOUBLE_OWNER",
                            "a unique item cannot have two canon owners",
                            details={
                                "item_entity_id": item,
                                "current_owner": current_owner,
                                "new_owner": to_owner,
                            },
                        )
                    unique_items[item] = str(to_owner)
                elif action in {"lose", "consume"}:
                    if from_owner and current_owner and str(from_owner) != str(
                        current_owner
                    ):
                        raise ContinuityError(
                            "INVENTORY_TRANSFER_OWNER_MISMATCH",
                            "unique item removal names a non-owner",
                        )
                    unique_items[item] = None
            elif event_type == "ability":
                owner = str(event["owner_entity_id"])
                ability = str(event["ability_entity_id"])
                action = str(event["action"])
                key = (owner, ability)
                ownership = dict(ability_ownership.get(key, {}))
                runtime = dict(ability_runtime.get(key, {}))
                acquired = bool(ownership.get("acquired"))
                state_patch = dict(event.get("state") or {})
                if action in {"gain", "set", "unlock"}:
                    acquired = bool(state_patch.get("acquired", True))
                    ownership.update(state_patch)
                    ownership["acquired"] = acquired
                    runtime.setdefault("available", acquired)
                elif action in {
                    "use",
                    "cooldown",
                    "breakthrough",
                    "upgrade",
                    "charge",
                    "activate",
                    "deactivate",
                    "refresh",
                    "lose",
                } and not acquired:
                    raise ContinuityError(
                        "POWER_ABILITY_NOT_ACQUIRED",
                        "an ability action requires active ownership",
                        details={
                            "owner_entity_id": owner,
                            "ability_entity_id": ability,
                            "action": action,
                        },
                    )
                if action in {"breakthrough", "upgrade"}:
                    ownership.update(state_patch)
                    ownership["acquired"] = True
                elif action == "use":
                    definition = ability_definitions.get(ability, {})
                    validate_prerequisites(
                        owner,
                        event.get("prerequisites")
                        or definition.get("prerequisites"),
                        event,
                    )
                    required_binding = definition.get(
                        "source_binding_id"
                    )
                    if required_binding and not binding_state.get(
                        (owner, str(required_binding)), {}
                    ).get("active"):
                        raise ContinuityError(
                            "POWER_PREREQUISITE_UNMET",
                            "the ability source binding is inactive",
                            details={
                                "owner_entity_id": owner,
                                "ability_entity_id": ability,
                                "binding_id": required_binding,
                            },
                        )
                    cooldown_until = runtime.get("cooldown_until")
                    if cooldown_until:
                        compare_coordinates(
                            event.get("story_coordinate"),
                            cooldown_until,
                            code="POWER_COOLDOWN_ACTIVE",
                        )
                        runtime.pop("cooldown_until", None)
                        runtime["available"] = True
                    runtime.update(state_patch)
                    runtime["last_used_at"] = event.get(
                        "story_coordinate"
                    )
                    previous_use_count = runtime.get("use_count", 0)
                    if (
                        type(previous_use_count) is not int
                        or previous_use_count < 0
                    ):
                        raise ContinuityError(
                            "POWER_RUNTIME_STATE_INVALID",
                            "ability runtime use_count must be a non-negative integer",
                            details={
                                "owner_entity_id": owner,
                                "ability_entity_id": ability,
                            },
                        )
                    runtime["use_count"] = previous_use_count + 1
                    if event.get("cooldown_until"):
                        cooldown_until = event["cooldown_until"]
                        if event.get("story_coordinate"):
                            if (
                                compare_coordinates(
                                    cooldown_until,
                                    event.get("story_coordinate"),
                                )
                                <= 0
                            ):
                                raise ContinuityError(
                                    "POWER_STORY_COORDINATE_UNKNOWN",
                                    "cooldown_until must follow the use coordinate",
                                )
                        runtime["cooldown_until"] = cooldown_until
                        runtime["available"] = False
                    require_costs(
                        owner,
                        event.get("resource_costs")
                        or definition.get("resource_costs"),
                    )
                elif action == "cooldown":
                    cooldown_until = event.get("cooldown_until") or (
                        state_patch.get("cooldown_until")
                    )
                    if not cooldown_until:
                        raise ContinuityError(
                            "POWER_STORY_COORDINATE_UNKNOWN",
                            "cooldown requires cooldown_until",
                        )
                    if not event.get("story_coordinate"):
                        raise ContinuityError(
                            "POWER_STORY_COORDINATE_UNKNOWN",
                            "cooldown requires a current story coordinate",
                        )
                    if (
                        compare_coordinates(
                            cooldown_until,
                            event.get("story_coordinate"),
                        )
                        <= 0
                    ):
                        raise ContinuityError(
                            "POWER_STORY_COORDINATE_UNKNOWN",
                            "cooldown_until must follow the current coordinate",
                        )
                    runtime.update(state_patch)
                    runtime["cooldown_until"] = cooldown_until
                    runtime["available"] = False
                elif action == "charge":
                    runtime.update(state_patch)
                    runtime["charges"] = float(
                        runtime.get("charges", 0)
                    ) + float(state_patch.get("amount", 1))
                elif action == "activate":
                    runtime.update(state_patch)
                    runtime["active"] = True
                elif action == "deactivate":
                    runtime.update(state_patch)
                    runtime["active"] = False
                elif action == "refresh":
                    runtime.update(state_patch)
                    if event.get("cooldown_until"):
                        runtime["cooldown_until"] = event[
                            "cooldown_until"
                        ]
                elif action == "lose":
                    ownership["acquired"] = False
                    ownership["lost_at"] = event.get("story_coordinate")
                    runtime["available"] = False
                    runtime["active"] = False
                ability_ownership[key] = ownership
                ability_runtime[key] = runtime
            elif event_type == "progression":
                actor = str(event["actor_entity_id"])
                track = str(event["track_entity_id"])
                key = (actor, track)
                action = str(event["action"])
                current = dict(progression_state.get(key, {}))
                current_rank = current.get("rank_entity_id")
                expected_from = event.get("from_rank_entity_id")
                target_rank = str(event["to_rank_entity_id"])
                if expected_from and str(current_rank) != str(expected_from):
                    raise ContinuityError(
                        "POWER_PREREQUISITE_UNMET",
                        "progression from-rank does not match accepted state",
                        details={
                            "actor_entity_id": actor,
                            "track_entity_id": track,
                            "expected_rank_entity_id": expected_from,
                            "actual_rank_entity_id": current_rank,
                        },
                    )
                if action == "initialize":
                    if current_rank is not None:
                        raise ContinuityError(
                            "POWER_PREREQUISITE_UNMET",
                            "progression track is already initialized",
                            details={
                                "actor_entity_id": actor,
                                "track_entity_id": track,
                                "rank_entity_id": current_rank,
                            },
                        )
                    edge = None
                else:
                    if current_rank is None:
                        raise ContinuityError(
                            "POWER_PREREQUISITE_UNMET",
                            "progression transition requires an initialized track",
                            details={
                                "actor_entity_id": actor,
                                "track_entity_id": track,
                            },
                        )
                    edge_id = str(
                        event.get("rank_edge_entity_id") or ""
                    )
                    candidates = [
                        (candidate_id, definition)
                        for candidate_id, definition in rank_edges.items()
                        if str(definition.get("track_entity_id") or "")
                        == track
                        and str(
                            definition.get("to_rank_entity_id") or ""
                        )
                        == target_rank
                        and str(current_rank)
                        in {
                            str(item)
                            for item in definition.get(
                                "from_rank_entity_ids", []
                            )
                        }
                    ]
                    if edge_id:
                        edge = rank_edges.get(edge_id)
                        if edge is None:
                            candidates = []
                        else:
                            candidates = [(edge_id, edge)]
                    if len(candidates) != 1:
                        raise ContinuityError(
                            "POWER_TRANSITION_EDGE_MISSING",
                            "progression transition requires one accepted rank edge",
                            details={
                                "actor_entity_id": actor,
                                "track_entity_id": track,
                                "from_rank_entity_id": current_rank,
                                "to_rank_entity_id": target_rank,
                                "rank_edge_entity_id": edge_id or None,
                                "matching_edge_count": len(candidates),
                            },
                        )
                    edge_id, edge = candidates[0]
                    if (
                        str(edge.get("track_entity_id") or "") != track
                        or str(edge.get("to_rank_entity_id") or "")
                        != target_rank
                        or str(current_rank)
                        not in {
                            str(item)
                            for item in edge.get(
                                "from_rank_entity_ids", []
                            )
                        }
                    ):
                        raise ContinuityError(
                            "POWER_TRANSITION_EDGE_MISSING",
                            "rank edge endpoints do not match the transition",
                        )
                    validate_prerequisites(
                        actor,
                        edge.get("prerequisites"),
                        event,
                    )
                    require_costs(actor, edge.get("resource_costs"))
                    current["rank_edge_entity_id"] = edge_id
                current.update(dict(event.get("state") or {}))
                current.update(
                    {
                        "rank_entity_id": target_rank,
                        "from_rank_entity_id": current_rank,
                        "action": action,
                        "story_coordinate": event.get("story_coordinate"),
                    }
                )
                progression_state[key] = current
            elif event_type == "resource":
                actor = str(event["actor_entity_id"])
                resource = str(event["resource_entity_id"])
                key = (actor, resource)
                action = str(event["action"])
                amount = float(event["amount"])
                current = dict(
                    resource_state.get(
                        key, {"balance": 0.0, "reserved": 0.0}
                    )
                )
                balance = float(current.get("balance", 0))
                reserved = float(current.get("reserved", 0))
                if action in {"initialize", "set"}:
                    balance = amount
                    reserved = min(
                        float(
                            (event.get("state") or {}).get("reserved", 0)
                        ),
                        balance,
                    )
                elif action in {"gain", "recover"}:
                    definition = resource_definitions.get(resource, {})
                    if (
                        not event.get("source")
                        and not event.get("caused_by")
                        and not (
                            action == "recover"
                            and definition.get("passive_recovery")
                        )
                    ):
                        raise ContinuityError(
                            "POWER_RESOURCE_SOURCE_REQUIRED",
                            "resource gain/recovery requires an explicit source",
                            details={
                                "actor_entity_id": actor,
                                "resource_entity_id": resource,
                                "action": action,
                            },
                        )
                    balance += amount
                elif action == "spend":
                    from_reserved = bool(event.get("from_reserved", False))
                    available = (
                        balance if from_reserved else balance - reserved
                    )
                    if available + 1e-12 < amount:
                        raise ContinuityError(
                            "POWER_RESOURCE_INSUFFICIENT",
                            "resource spend exceeds the available balance",
                            details={
                                "actor_entity_id": actor,
                                "resource_entity_id": resource,
                                "balance": balance,
                                "reserved": reserved,
                                "requested": amount,
                            },
                        )
                    balance -= amount
                    if from_reserved:
                        reserved = max(0.0, reserved - amount)
                    applied_spends[key] = (
                        applied_spends.get(key, 0.0) + amount
                    )
                elif action == "reserve":
                    if balance - reserved + 1e-12 < amount:
                        raise ContinuityError(
                            "POWER_RESOURCE_INSUFFICIENT",
                            "resource reservation exceeds the available balance",
                        )
                    reserved += amount
                elif action == "release":
                    if reserved + 1e-12 < amount:
                        raise ContinuityError(
                            "POWER_RESOURCE_INSUFFICIENT",
                            "resource release exceeds the reserved balance",
                        )
                    reserved -= amount
                elif action == "convert":
                    rule_id = str(event["conversion_rule_entity_id"])
                    rule = conversion_rules.get(rule_id)
                    target_resource = str(
                        event["target_resource_entity_id"]
                    )
                    if (
                        rule is None
                        or str(
                            rule.get("source_resource_entity_id") or ""
                        )
                        != resource
                        or str(
                            rule.get("target_resource_entity_id") or ""
                        )
                        != target_resource
                    ):
                        raise ContinuityError(
                            "POWER_INTERACTION_UNKNOWN",
                            "resource conversion requires a matching accepted rule",
                            details={
                                "conversion_rule_entity_id": rule_id,
                                "source_resource_entity_id": resource,
                                "target_resource_entity_id": target_resource,
                            },
                        )
                    if balance - reserved + 1e-12 < amount:
                        raise ContinuityError(
                            "POWER_RESOURCE_INSUFFICIENT",
                            "resource conversion exceeds the available balance",
                        )
                    ratio = float(rule.get("ratio", 0))
                    if ratio <= 0:
                        raise ContinuityError(
                            "POWER_INTERACTION_UNKNOWN",
                            "conversion rule has no positive ratio",
                        )
                    target_amount = float(
                        event.get("target_amount", amount * ratio)
                    )
                    if abs(target_amount - amount * ratio) > 1e-9:
                        raise ContinuityError(
                            "POWER_INTERACTION_UNKNOWN",
                            "resource conversion amount does not match the accepted ratio",
                        )
                    balance -= amount
                    target_key = (actor, target_resource)
                    target_state = dict(
                        resource_state.get(
                            target_key,
                            {"balance": 0.0, "reserved": 0.0},
                        )
                    )
                    target_state["balance"] = float(
                        target_state.get("balance", 0)
                    ) + target_amount
                    resource_state[target_key] = target_state
                definition = resource_definitions.get(resource, {})
                minimum = float(
                    definition.get(
                        "minimum_balance",
                        -float("inf")
                        if definition.get("allow_debt")
                        else 0,
                    )
                )
                maximum = definition.get("maximum_balance")
                if balance < minimum - 1e-12 or (
                    maximum is not None
                    and balance > float(maximum) + 1e-12
                ):
                    raise ContinuityError(
                        "POWER_RESOURCE_INSUFFICIENT",
                        "resource state violates its accepted bounds",
                        details={
                            "actor_entity_id": actor,
                            "resource_entity_id": resource,
                            "balance": balance,
                            "minimum_balance": minimum,
                            "maximum_balance": maximum,
                        },
                    )
                current.update(dict(event.get("state") or {}))
                current.update(
                    {
                        "balance": balance,
                        "reserved": reserved,
                        "action": action,
                        "story_coordinate": event.get("story_coordinate"),
                    }
                )
                resource_state[key] = current
            elif event_type == "status_effect":
                actor = str(event["actor_entity_id"])
                status = str(event["status_entity_id"])
                key = (actor, status)
                action = str(event["action"])
                current = dict(status_state.get(key, {}))
                active = bool(current.get("active"))
                if action in {"stack", "refresh", "remove", "expire"} and (
                    not active
                ):
                    raise ContinuityError(
                        "POWER_PREREQUISITE_UNMET",
                        "status action requires an active status",
                        details={
                            "actor_entity_id": actor,
                            "status_entity_id": status,
                            "action": action,
                        },
                    )
                if action == "apply":
                    stacks = int(event.get("stacks", 1))
                    active = True
                elif action == "stack":
                    stacks = int(current.get("stacks", 0)) + int(
                        event.get("stacks", 1)
                    )
                elif action == "refresh":
                    stacks = int(
                        event.get("stacks", current.get("stacks", 1))
                    )
                else:
                    stacks = 0
                    active = False
                expires = event.get("expires_coordinate") or current.get(
                    "expires_coordinate"
                )
                if expires:
                    if not event.get("story_coordinate"):
                        raise ContinuityError(
                            "POWER_STORY_COORDINATE_UNKNOWN",
                            "status expiry requires an application coordinate",
                        )
                    if (
                        compare_coordinates(
                            expires, event.get("story_coordinate")
                        )
                        <= 0
                    ):
                        raise ContinuityError(
                            "POWER_STORY_COORDINATE_UNKNOWN",
                            "status expiry must follow its start/refresh coordinate",
                        )
                current.update(dict(event.get("state") or {}))
                current.update(
                    {
                        "active": active,
                        "stacks": stacks,
                        "expires_coordinate": expires,
                        "action": action,
                        "story_coordinate": event.get("story_coordinate"),
                    }
                )
                status_state[key] = current
            elif event_type == "power_binding":
                actor = str(event["actor_entity_id"])
                binding_id = str(event["binding_id"])
                key = (actor, binding_id)
                action = str(event["action"])
                current = dict(binding_state.get(key, {}))
                active = bool(current.get("active"))
                activating = action in {
                    "bind",
                    "equip",
                    "contract",
                    "summon",
                }
                if not activating and not active:
                    raise ContinuityError(
                        "POWER_PREREQUISITE_UNMET",
                        "binding removal/suppression requires an active binding",
                        details={
                            "actor_entity_id": actor,
                            "binding_id": binding_id,
                            "action": action,
                        },
                    )
                if activating and event.get("unique"):
                    slot_key = str(event.get("slot_key") or binding_id)
                    conflict = next(
                        (
                            other_id
                            for (other_actor, other_id), other in (
                                binding_state.items()
                            )
                            if other_actor == actor
                            and other_id != binding_id
                            and other.get("active")
                            and str(other.get("slot_key") or other_id)
                            == slot_key
                            and other.get("unique")
                        ),
                        None,
                    )
                    if conflict:
                        raise ContinuityError(
                            "POWER_PREREQUISITE_UNMET",
                            "a unique power-binding slot is already occupied",
                            details={
                                "actor_entity_id": actor,
                                "slot_key": slot_key,
                                "existing_binding_id": conflict,
                            },
                        )
                current.update(dict(event.get("state") or {}))
                current.update(
                    {
                        "active": activating,
                        "source_entity_id": event["source_entity_id"],
                        "ability_entity_ids": list(
                            event.get("ability_entity_ids") or []
                        ),
                        "slot_key": event.get("slot_key"),
                        "unique": bool(event.get("unique")),
                        "action": action,
                        "story_coordinate": event.get("story_coordinate"),
                    }
                )
                binding_state[key] = current
            elif event_type == "qualification":
                actor = str(event["actor_entity_id"])
                qualification = str(event["qualification_entity_id"])
                if qualification not in qualification_definitions:
                    raise ContinuityError(
                        "POWER_RUNTIME_DEFINITION_MISSING",
                        "qualification runtime event requires an active accepted definition",
                        details={
                            "spec_type": "qualification_definition",
                            "spec_entity_id": qualification,
                            "actor_entity_id": actor,
                        },
                    )
                key = (actor, qualification)
                action = str(event["action"])
                amount = float(event.get("quantity", 1))
                current = dict(qualification_state.get(key, {}))
                quantity = float(current.get("quantity", 0))
                if action == "grant":
                    quantity += amount
                elif action == "consume":
                    if quantity + 1e-12 < amount:
                        raise ContinuityError(
                            "POWER_PREREQUISITE_UNMET",
                            "qualification consumption exceeds the active quantity",
                            details={
                                "actor_entity_id": actor,
                                "qualification_entity_id": qualification,
                                "available": quantity,
                                "requested": amount,
                            },
                        )
                    quantity -= amount
                elif action in {"revoke", "expire"}:
                    if quantity <= 0:
                        raise ContinuityError(
                            "POWER_PREREQUISITE_UNMET",
                            "qualification is not active",
                        )
                    quantity = max(0.0, quantity - amount)
                current.update(dict(event.get("state") or {}))
                current.update(
                    {
                        "active": quantity > 0,
                        "quantity": quantity,
                        "expires_coordinate": event.get(
                            "expires_coordinate"
                        ),
                        "action": action,
                        "story_coordinate": event.get("story_coordinate"),
                    }
                )
                qualification_state[key] = current

        missing_costs = [
            {
                "actor_entity_id": actor,
                "resource_entity_id": resource,
                "required": required,
                "applied": applied_spends.get((actor, resource), 0.0),
            }
            for (actor, resource), required in sorted(required_costs.items())
            if applied_spends.get((actor, resource), 0.0) + 1e-12
            < required
        ]
        if missing_costs:
            raise ContinuityError(
                "POWER_COST_NOT_APPLIED",
                "immediate power costs must be written in the same accepted transaction",
                details={"missing_costs": missing_costs},
            )

    @staticmethod
    def _validate_initialization_power_spec_binding(
        connection: sqlite3.Connection,
        proposal: sqlite3.Row,
        payload: Mapping[str, Any],
        package: Mapping[str, Any],
    ) -> None:
        power_spec_package = dict(package.get("power_spec_package") or {})
        requires_power_spec = bool(
            package.get("requires_power_spec_acceptance")
            or power_spec_package.get("events")
        )
        if not requires_power_spec:
            return

        parent_initialization_proposal_id = str(
            package.get("proposal_id")
            or proposal["artifact_id"]
            or proposal["proposal_id"]
        )
        base_details = {
            "parent_initialization_proposal_id": (
                parent_initialization_proposal_id
            ),
            "initialization_canon_proposal_id": str(
                proposal["proposal_id"]
            ),
        }
        if not power_spec_package:
            raise ContinuityError(
                "POWER_SPEC_PACKAGE_MISSING",
                "initialization requires a power specification package",
                details=base_details,
            )

        declared_package_hash = str(
            power_spec_package.get("package_hash") or ""
        )
        hash_payload = dict(power_spec_package)
        hash_payload.pop("package_hash", None)
        actual_package_hash = stable_hash(hash_payload)
        if (
            not declared_package_hash
            or declared_package_hash != actual_package_hash
        ):
            raise ContinuityError(
                "POWER_SPEC_PACKAGE_HASH_MISMATCH",
                "power specification package hash is invalid",
                details={
                    **base_details,
                    "expected_package_hash": declared_package_hash or None,
                    "actual_package_hash": actual_package_hash,
                },
            )

        power_spec_binding = dict(payload.get("power_spec_binding") or {})
        required_binding_fields = {
            "proposal_id",
            "commit_id",
            "package_hash",
            "power_package_hash",
            "projection_hash",
            "active_canon_revision",
        }
        missing_binding_fields = sorted(
            field
            for field in required_binding_fields
            if field not in power_spec_binding
            or (
                field not in {"power_package_hash"}
                and (
                    power_spec_binding.get(field) is None
                    or str(power_spec_binding.get(field)).strip() == ""
                )
            )
        )
        if missing_binding_fields:
            raise ContinuityError(
                "POWER_SPEC_ACCEPTANCE_REQUIRED",
                "initialization requires an accepted power specification commit",
                details={
                    **base_details,
                    "missing_binding_fields": missing_binding_fields,
                    "expected_package_hash": declared_package_hash,
                },
            )

        spec_proposal_id = str(power_spec_binding["proposal_id"])
        spec_commit_id = str(power_spec_binding["commit_id"])
        binding_details = {
            **base_details,
            "power_spec_proposal_id": spec_proposal_id,
            "power_spec_commit_id": spec_commit_id,
        }
        bound_package_hash = str(
            power_spec_binding.get("package_hash") or ""
        )
        requested_package_hash = str(
            power_spec_binding.get("requested_package_hash")
            or declared_package_hash
        )
        if requested_package_hash != declared_package_hash:
            raise ContinuityError(
                "POWER_SPEC_BINDING_MISMATCH",
                "power specification binding names a different requested package",
                details={
                    **binding_details,
                    "expected_package_hash": declared_package_hash,
                    "actual_package_hash": requested_package_hash,
                },
            )

        expected_power_package_hash = str(
            power_spec_package.get("power_package_hash") or ""
        )
        bound_power_package_hash = str(
            power_spec_binding.get("power_package_hash") or ""
        )
        if bound_power_package_hash != expected_power_package_hash:
            raise ContinuityError(
                "POWER_SPEC_BINDING_MISMATCH",
                "power specification binding names a different power package",
                details={
                    **binding_details,
                    "expected_power_package_hash": (
                        expected_power_package_hash
                    ),
                    "actual_power_package_hash": bound_power_package_hash,
                },
            )

        row = connection.execute(
            """
            SELECT
                c.*,
                p.proposal_kind,
                p.canon_status AS proposal_canon_status,
                p.payload_json AS proposal_payload_json,
                p.events_json AS proposal_events_json,
                a.active AS artifact_active
            FROM canon_commits AS c
            JOIN proposals AS p ON p.proposal_id=c.proposal_id
            JOIN artifacts AS a
              ON a.artifact_version_id=p.artifact_version_id
            WHERE c.commit_id=?
            """,
            (spec_commit_id,),
        ).fetchone()
        if row is None:
            raise ContinuityError(
                "POWER_SPEC_COMMIT_NOT_FOUND",
                "power specification binding references an unknown commit",
                details=binding_details,
            )
        if str(row["proposal_id"]) != spec_proposal_id:
            raise ContinuityError(
                "POWER_SPEC_BINDING_MISMATCH",
                "power specification commit belongs to another proposal",
                details={
                    **binding_details,
                    "actual_power_spec_proposal_id": str(
                        row["proposal_id"]
                    ),
                },
            )
        if (
            str(row["operation"]) != "accept"
            or str(row["proposal_kind"]) != "power_spec_change"
            or str(row["proposal_canon_status"]) != "accepted"
            or not bool(row["artifact_active"])
            or not bool(row["changes_authority"])
        ):
            raise ContinuityError(
                "POWER_SPEC_COMMIT_INACTIVE",
                "power specification commit is not an active accepted definition",
                details={
                    **binding_details,
                    "operation": str(row["operation"]),
                    "proposal_kind": str(row["proposal_kind"]),
                    "canon_status": str(row["proposal_canon_status"]),
                    "artifact_active": bool(row["artifact_active"]),
                    "changes_authority": bool(row["changes_authority"]),
                },
            )

        prepared_revision = int(proposal["prepared_canon_revision"])
        committed_revision = int(row["active_revision_after"])
        bound_revision = int(power_spec_binding["active_canon_revision"])
        if (
            prepared_revision < committed_revision
            or bound_revision != committed_revision
        ):
            raise ContinuityError(
                "POWER_SPEC_REVISION_MISMATCH",
                "initialization must be prepared at or after the active bound power specification commit",
                details={
                    **binding_details,
                    "initialization_prepared_canon_revision": (
                        prepared_revision
                    ),
                    "spec_commit_active_canon_revision": committed_revision,
                    "bound_active_canon_revision": bound_revision,
                },
            )

        spec_commit_projection_hash = str(row["projection_hash"] or "")
        bound_projection_hash = str(
            power_spec_binding.get("projection_hash") or ""
        )
        if (
            not spec_commit_projection_hash
            or bound_projection_hash != spec_commit_projection_hash
        ):
            raise ContinuityError(
                "POWER_SPEC_PROJECTION_HASH_MISMATCH",
                "power specification binding projection hash is invalid",
                details={
                    **binding_details,
                    "expected_projection_hash": spec_commit_projection_hash,
                    "actual_projection_hash": bound_projection_hash,
                },
            )

        spec_payload = dict(
            _json_load(str(row["proposal_payload_json"]), {})
        )
        accepted_spec_package = dict(
            spec_payload.get("lifecycle_package")
            or spec_payload.get("power_spec_package")
            or {}
        )
        accepted_package_hash = str(
            accepted_spec_package.get("package_hash")
            or spec_payload.get("package_hash")
            or ""
        )
        accepted_power_package_hash = str(
            accepted_spec_package.get("power_package_hash")
            or spec_payload.get("power_package_hash")
            or ""
        )
        exact_power_package = bool(expected_power_package_hash) and (
            accepted_power_package_hash == expected_power_package_hash
        )
        same_lifecycle_package = (
            accepted_package_hash == declared_package_hash
            and accepted_power_package_hash == expected_power_package_hash
        )
        if (
            bound_package_hash != accepted_package_hash
            or not (exact_power_package or same_lifecycle_package)
        ):
            raise ContinuityError(
                "POWER_SPEC_BINDING_MISMATCH",
                "accepted power specification payload differs from initialization",
                details={
                    **binding_details,
                    "expected_package_hash": accepted_package_hash,
                    "actual_package_hash": bound_package_hash,
                    "accepted_package_hash": accepted_package_hash,
                    "requested_package_hash": requested_package_hash,
                    "expected_power_package_hash": (
                        expected_power_package_hash
                    ),
                    "actual_power_package_hash": (
                        accepted_power_package_hash
                    ),
                },
            )

        projection_tables = {
            "power_system": (
                "power_system_specs",
                "spec_entity_id",
                "spec_status",
            ),
            "progression_track": (
                "progression_tracks",
                "track_entity_id",
                "track_status",
            ),
            "rank_node": (
                "rank_nodes",
                "rank_entity_id",
                "rank_status",
            ),
            "rank_edge": (
                "rank_edges",
                "edge_entity_id",
                "edge_status",
            ),
            "ability_definition": (
                "ability_definitions",
                "ability_entity_id",
                "definition_status",
            ),
            "resource_definition": (
                "resource_definitions",
                "resource_entity_id",
                "definition_status",
            ),
            "status_definition": (
                "status_definitions",
                "status_entity_id",
                "definition_status",
            ),
            "qualification_definition": (
                "qualification_definitions",
                "qualification_entity_id",
                "definition_status",
            ),
            "counter_rule": (
                "counter_rules",
                "rule_entity_id",
                "rule_status",
            ),
            "bridge_rule": (
                "bridge_rules",
                "rule_entity_id",
                "rule_status",
            ),
            "conversion_rule": (
                "conversion_rules",
                "rule_entity_id",
                "rule_status",
            ),
        }
        commit_events = {
            (
                str(event["payload"].get("spec_type") or ""),
                str(event["payload"].get("spec_entity_id") or ""),
            ): event
            for event in (
                {
                    "event_id": str(event_row["event_id"]),
                    "payload": dict(
                        _json_load(str(event_row["payload_json"]), {})
                    ),
                }
                for event_row in connection.execute(
                    """
                    SELECT event_id, payload_json
                    FROM continuity_events
                    WHERE commit_id=? AND event_type='power_spec'
                    ORDER BY event_ordinal
                    """,
                    (spec_commit_id,),
                )
            )
        }
        expected_specs = {
            (
                str(event.get("spec_type") or ""),
                str(event.get("spec_entity_id") or ""),
            )
            for event in power_spec_package.get("events") or []
            if isinstance(event, Mapping)
            and event.get("event_type") == "power_spec"
        }
        missing_commit_events = sorted(expected_specs - set(commit_events))
        if missing_commit_events:
            raise ContinuityError(
                "POWER_SPEC_BINDING_MISMATCH",
                "accepted power specification commit is missing package events",
                details={
                    **binding_details,
                    "missing_spec_events": [
                        {
                            "spec_type": spec_type,
                            "spec_entity_id": spec_entity_id,
                        }
                        for spec_type, spec_entity_id in missing_commit_events
                    ],
                },
            )

        missing_projection: list[dict[str, Any]] = []
        for spec_type, spec_entity_id in sorted(expected_specs):
            table_contract = projection_tables.get(spec_type)
            if table_contract is None:
                missing_projection.append(
                    {
                        "spec_type": spec_type,
                        "spec_entity_id": spec_entity_id,
                        "reason": "unsupported_spec_type",
                    }
                )
                continue
            table, id_column, status_column = table_contract
            projected = connection.execute(
                f"""
                SELECT {status_column} AS definition_status,
                       source_event_id, definition_json
                FROM {table}
                WHERE {id_column}=?
                """,
                (spec_entity_id,),
            ).fetchone()
            commit_event = commit_events[(spec_type, spec_entity_id)]
            expected_status = (
                "deprecated"
                if commit_event["payload"].get("action") == "deprecate"
                else "active"
            )
            if (
                projected is None
                or str(projected["source_event_id"])
                != str(commit_event["event_id"])
                or str(projected["definition_status"]) != expected_status
            ):
                missing_projection.append(
                    {
                        "spec_type": spec_type,
                        "spec_entity_id": spec_entity_id,
                        "expected_source_event_id": commit_event["event_id"],
                        "actual_source_event_id": (
                            str(projected["source_event_id"])
                            if projected is not None
                            else None
                        ),
                        "expected_status": expected_status,
                        "actual_status": (
                            str(projected["definition_status"])
                            if projected is not None
                            else None
                        ),
                    }
                )
        if missing_projection:
            raise ContinuityError(
                "POWER_SPEC_PROJECTION_MISSING",
                "accepted power specification definitions are not active in projection",
                details={
                    **binding_details,
                    "package_hash": declared_package_hash,
                    "definitions": missing_projection,
                },
            )

        runtime_definition_refs: set[tuple[str, str]] = set()
        for event in package.get("events") or []:
            if not isinstance(event, Mapping):
                continue
            event_type = str(event.get("event_type") or "")
            if event_type == "ability" and event.get("ability_entity_id"):
                runtime_definition_refs.add(
                    (
                        "ability_definition",
                        str(event["ability_entity_id"]),
                    )
                )
            elif event_type == "progression":
                if event.get("track_entity_id"):
                    runtime_definition_refs.add(
                        (
                            "progression_track",
                            str(event["track_entity_id"]),
                        )
                    )
                for field in (
                    "from_rank_entity_id",
                    "to_rank_entity_id",
                ):
                    if event.get(field):
                        runtime_definition_refs.add(
                            ("rank_node", str(event[field]))
                        )
                if event.get("rank_edge_entity_id"):
                    runtime_definition_refs.add(
                        (
                            "rank_edge",
                            str(event["rank_edge_entity_id"]),
                        )
                    )
            elif event_type == "resource":
                for field in (
                    "resource_entity_id",
                    "target_resource_entity_id",
                ):
                    if event.get(field):
                        runtime_definition_refs.add(
                            (
                                "resource_definition",
                                str(event[field]),
                            )
                        )
                if event.get("conversion_rule_entity_id"):
                    runtime_definition_refs.add(
                        (
                            "conversion_rule",
                            str(event["conversion_rule_entity_id"]),
                        )
                    )
            elif event_type in {
                "status",
                "status_effect",
            } and event.get("status_entity_id"):
                runtime_definition_refs.add(
                    (
                        "status_definition",
                        str(event["status_entity_id"]),
                    )
                )
            elif event_type == "power_binding":
                runtime_definition_refs.update(
                    ("ability_definition", str(ability_id))
                    for ability_id in event.get("ability_entity_ids") or []
                    if ability_id
                )
            elif (
                event_type == "qualification"
                and event.get("qualification_entity_id")
            ):
                runtime_definition_refs.add(
                    (
                        "qualification_definition",
                        str(event["qualification_entity_id"]),
                    )
                )

        missing_runtime_definitions: list[dict[str, Any]] = []
        for spec_type, spec_entity_id in sorted(runtime_definition_refs):
            table, id_column, status_column = projection_tables[spec_type]
            projected = connection.execute(
                f"""
                SELECT {status_column} AS definition_status
                FROM {table}
                WHERE {id_column}=?
                """,
                (spec_entity_id,),
            ).fetchone()
            if (
                projected is None
                or str(projected["definition_status"]) != "active"
            ):
                missing_runtime_definitions.append(
                    {
                        "spec_type": spec_type,
                        "spec_entity_id": spec_entity_id,
                        "actual_status": (
                            str(projected["definition_status"])
                            if projected is not None
                            else None
                        ),
                    }
                )
        if missing_runtime_definitions:
            raise ContinuityError(
                "POWER_RUNTIME_DEFINITION_MISSING",
                "initialization runtime state references missing power definitions",
                details={
                    **binding_details,
                    "definitions": missing_runtime_definitions,
                },
            )

    @staticmethod
    def _validate_initialization_target_paths(
        proposal: sqlite3.Row,
        binding: Mapping[str, Any],
    ) -> None:
        if str(proposal["proposal_kind"]) != "initialization_bundle":
            return
        raw_target_root = Path(
            str(binding.get("target_project_real_path") or "")
        ).expanduser()
        target_root_path = Path(os.path.abspath(raw_target_root))
        _assert_no_reparse_path(
            target_root_path,
            anchor=Path(target_root_path.anchor),
            code="UNSAFE_MATERIALIZATION_PATH",
            label="initialization target root",
        )
        if target_root_path.exists() and not target_root_path.is_dir():
            raise ContinuityError(
                "TARGET_TYPE_CONFLICT",
                "initialization target root is not a directory",
                details={"path": str(target_root_path)},
            )
        target_root = target_root_path.resolve()
        for item in binding.get("target_old_new_hashes") or []:
            if not isinstance(item, Mapping) or not item.get("path"):
                continue
            relative = _safe_relative_path(str(item["path"]))
            target_path = target_root / relative
            _assert_no_reparse_path(
                target_path,
                anchor=target_root,
                code="UNSAFE_MATERIALIZATION_PATH",
                label="initialization materialization target",
            )
            current = target_root
            for part in relative.parts[:-1]:
                current = current / part
                if current.exists() and not current.is_dir():
                    raise ContinuityError(
                        "TARGET_TYPE_CONFLICT",
                        "initialization target parent is not a directory",
                        details={"path": relative.as_posix()},
                    )
            target = target_path.resolve(strict=False)
            if target_root != target and target_root not in target.parents:
                raise ContinuityError(
                    "UNSAFE_MATERIALIZATION_PATH",
                    f"path escapes target root: {relative.as_posix()}",
                )
            if target_path.exists() and not target_path.is_file():
                raise ContinuityError(
                    "TARGET_TYPE_CONFLICT",
                    "materialization target is not a regular file",
                    details={"path": relative.as_posix()},
                )
            actual = sha256_file(target_path) if target_path.is_file() else None
            expected = item.get("expected_old_hash")
            if actual != expected:
                raise ContinuityError(
                    "TARGET_HASH_CONFLICT",
                    "materialization target changed after proposal review",
                    details={
                        "path": relative.as_posix(),
                        "expected": expected,
                        "actual": actual,
                    },
                )

    @staticmethod
    def _validated_initialization_advantage_sidecar(
        bundle: Mapping[str, Any],
        lifecycle_package: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        artifacts = [
            dict(item)
            for item in bundle.get("artifact_manifest") or []
            if isinstance(item, Mapping)
        ]
        provenance = bundle.get("provenance")
        references = [
            dict(item)
            for item in (
                provenance.get("advantage_sidecars") or []
                if isinstance(provenance, Mapping)
                else []
            )
            if isinstance(item, Mapping)
        ]
        declared = bool(references) or any(
            str(item.get("logical_owner") or "")
            == _ADVANTAGE_SIDECAR_LOGICAL_OWNER
            or str(item.get("path") or "").replace("\\", "/")
            == _ADVANTAGE_SIDECAR_RELATIVE_PATH
            for item in artifacts
        )
        if not declared:
            return None
        try:
            try:
                from scripts.plot_init import (
                    advantage_package_from_artifact_manifest,
                    recompute_advantage_package_hash,
                    validate_advantage_package,
                )
            except ImportError:
                from plot_init import (
                    advantage_package_from_artifact_manifest,
                    recompute_advantage_package_hash,
                    validate_advantage_package,
                )

            loaded = advantage_package_from_artifact_manifest(artifacts)
            if loaded is None:
                raise ContinuityError(
                    "ADVANTAGE_SIDECAR_MISSING",
                    "initialization declares Advantage data without a sidecar artifact",
                )
            sidecar, actual_reference = loaded
            validated = validate_advantage_package(sidecar)
            canonical_package_hash = recompute_advantage_package_hash(
                validated
            )
            declared_package_hash = str(validated.get("package_hash") or "")
            if (
                not declared_package_hash
                or declared_package_hash != canonical_package_hash
            ):
                raise ContinuityError(
                    "ADVANTAGE_PACKAGE_HASH_MISMATCH",
                    "Advantage sidecar package hash does not match canonical content",
                    details={
                        "expected": declared_package_hash or None,
                        "actual": canonical_package_hash,
                    },
                )
        except ContinuityError:
            raise
        except Exception as exc:
            raise ContinuityError(
                str(getattr(exc, "code", "ADVANTAGE_SIDECAR_INVALID")),
                str(getattr(exc, "message", exc)),
                details=dict(getattr(exc, "details", {}) or {}),
            ) from exc

        if (
            str(actual_reference.get("path") or "").replace("\\", "/")
            != _ADVANTAGE_SIDECAR_RELATIVE_PATH
        ):
            raise ContinuityError(
                "ADVANTAGE_SIDECAR_PATH_MISMATCH",
                "Advantage sidecar path differs from the v1 contract",
                details={"path": actual_reference.get("path")},
            )
        comparable = (
            "schema_version",
            "path",
            "artifact_id",
            "package_hash",
            "content_hash",
        )
        for reference in references:
            expected = {
                field: str(reference.get(field) or "")
                for field in comparable
            }
            actual = {
                field: str(actual_reference.get(field) or "")
                for field in comparable
            }
            if expected != actual:
                raise ContinuityError(
                    "ADVANTAGE_SIDECAR_REFERENCE_MISMATCH",
                    "initialization Advantage reference differs from its artifact",
                    details={"expected": expected, "actual": actual},
                )
        package = dict(lifecycle_package or {})
        bound_package_hash = str(package.get("advantage_package_hash") or "")
        if bound_package_hash and bound_package_hash != canonical_package_hash:
            raise ContinuityError(
                "ADVANTAGE_SIDECAR_PACKAGE_HASH_MISMATCH",
                "lifecycle package binds a different Advantage package hash",
                details={
                    "expected": bound_package_hash,
                    "actual": canonical_package_hash,
                },
            )
        bound_reference = package.get("advantage_sidecar")
        if isinstance(bound_reference, Mapping):
            expected = {
                field: str(bound_reference.get(field) or "")
                for field in comparable
            }
            actual = {
                field: str(actual_reference.get(field) or "")
                for field in comparable
            }
            if expected != actual:
                raise ContinuityError(
                    "ADVANTAGE_SIDECAR_REFERENCE_MISMATCH",
                    "lifecycle package Advantage reference differs from its artifact",
                    details={"expected": expected, "actual": actual},
                )
        return {
            "package": dict(validated),
            "package_hash": canonical_package_hash,
            "reference": dict(actual_reference),
        }

    @staticmethod
    def _plain_advantage_lifecycle_content(
        bundle: Mapping[str, Any],
        sidecar: Mapping[str, Any],
        *,
        proposal_id: str,
        branch_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        try:
            try:
                from scripts.plot_init import proposal_to_lifecycle_package
                from scripts.plot_init.canonical import canonical_hash
            except ImportError:
                from plot_init import proposal_to_lifecycle_package
                from plot_init.canonical import canonical_hash

            reference = dict(sidecar["reference"])
            artifacts = [
                copy.deepcopy(dict(item))
                for item in bundle.get("artifact_manifest") or []
                if isinstance(item, Mapping)
                and (
                    str(item.get("logical_owner") or "")
                    == _ADVANTAGE_SIDECAR_LOGICAL_OWNER
                    or str(item.get("path") or "").replace("\\", "/")
                    == _ADVANTAGE_SIDECAR_RELATIVE_PATH
                )
            ]
            provenance = bundle.get("provenance")
            claims = (
                copy.deepcopy(list(provenance.get("claims") or []))
                if isinstance(provenance, Mapping)
                else []
            )
            synthetic_bundle: dict[str, Any] = {
                "schema_version": (
                    str(bundle.get("schema_version"))
                    if str(bundle.get("schema_version") or "")
                    in {"plot-rag-init/v1", "plot-rag-init/v2"}
                    else "plot-rag-init/v1"
                ),
                "meta": {
                    "proposal_only": True,
                    "advantage_sidecar": copy.deepcopy(reference),
                },
                "world_model": {},
                "actor_system": {},
                "story_engine": {},
                "serialization_contract": {},
                "entities": [],
                "source_manifest": [],
                "provenance": {
                    "claims": claims,
                    "advantage_sidecars": [copy.deepcopy(reference)],
                },
                "artifact_manifest": artifacts,
            }
            package_hash = canonical_hash(
                synthetic_bundle,
                extra_volatile_keys=(
                    "real_path",
                    "normalized_real_path",
                    "unified_diff",
                ),
                strip_default_volatile=True,
            )
            synthetic_bundle["bundle_hash"] = package_hash
            frozen = {
                "schema_version": 1,
                "proposal_id": proposal_id,
                "package_hash": package_hash,
                "status": "PROPOSAL_FROZEN",
                "target_project_real_path": str(
                    bundle.get("target_project_real_path") or ""
                ),
                "source_manifest_hash": canonical_hash([]),
                "bundle": synthetic_bundle,
                "apply_plan": {
                    "requires_approval_grant": True,
                    "authorized_operations_required": [
                        "accept_initialization",
                        "materialize",
                    ],
                    "artifacts": artifacts,
                    "advantage_sidecar": copy.deepcopy(reference),
                    "executed": False,
                },
            }
            lifecycle = proposal_to_lifecycle_package(frozen)
        except ContinuityError:
            raise
        except Exception as exc:
            raise ContinuityError(
                str(getattr(exc, "code", "INITIALIZATION_ADAPTER_FAILED")),
                str(getattr(exc, "message", exc)),
                details=dict(getattr(exc, "details", {}) or {}),
            ) from exc

        def bind_branch(event: Mapping[str, Any]) -> dict[str, Any]:
            bound = copy.deepcopy(dict(event))
            bound["branch_id"] = branch_id
            replacement = bound.get("replacement")
            if (
                str(bound.get("event_type") or "")
                == "advantage_correction"
                and isinstance(replacement, Mapping)
            ):
                bound["replacement"] = bind_branch(replacement)
            return bound

        generated_events = [
            bind_branch(event)
            for event in lifecycle.get("events") or []
            if isinstance(event, Mapping)
            and str(event.get("event_type") or "") in ADVANTAGE_EVENT_TYPES
        ]
        generated_entities = [
            copy.deepcopy(dict(entity))
            for entity in lifecycle.get("entities") or []
            if isinstance(entity, Mapping) and entity.get("entity_id")
        ]
        if branch_id != "main":
            generated_events = [
                event
                for event in generated_events
                if str(event.get("event_type") or "")
                in ADVANTAGE_BRANCH_LOCAL_EVENT_TYPES
                or (
                    str(event.get("event_type") or "")
                    == "advantage_module"
                    and str(event.get("action") or "")
                    in {
                        "unlock",
                        "enable",
                        "lock",
                        "suppress",
                        "deprecate",
                    }
                )
            ]
            generated_entities = []
        return generated_entities, generated_events

    @staticmethod
    def _validate_initialization_inputs(
        connection: sqlite3.Connection,
        proposal: sqlite3.Row,
        binding: Mapping[str, Any],
    ) -> None:
        if str(proposal["proposal_kind"]) != "initialization_bundle":
            return
        ContinuityService._require_main_initialization_branch(
            proposal["branch_id"]
        )
        payload = dict(_json_load(str(proposal["payload_json"]), {}))
        package = dict(payload.get("lifecycle_package") or {})
        ContinuityService._validated_initialization_advantage_sidecar(
            dict(payload.get("bundle") or {}),
            package,
        )
        ContinuityService._validate_initialization_power_spec_binding(
            connection,
            proposal,
            payload,
            package,
        )
        manifest = list(package.get("source_manifest") or [])
        expected_manifest_hash = str(
            binding.get("source_manifest_hash") or ""
        )
        actual_manifest_hash = stable_hash(
            manifest, prefix="source_manifest_"
        )
        if expected_manifest_hash != actual_manifest_hash:
            raise ContinuityError(
                "SOURCE_MANIFEST_HASH_MISMATCH",
                "source manifest differs from the approval binding",
            )
        for item in manifest:
            if not isinstance(item, Mapping):
                continue
            declared_real_path = item.get("real_path") or item.get(
                "normalized_real_path"
            )
            raw_path = declared_real_path or item.get("path")
            if not raw_path:
                continue
            source_path = Path(str(raw_path))
            # Relative paths without an inventory real_path may describe a
            # generated target rather than an existing ingest source.
            if not source_path.is_absolute() and not declared_real_path:
                continue
            source_path = source_path.expanduser().resolve()
            if not source_path.is_file():
                raise ContinuityError(
                    "SOURCE_MANIFEST_MISSING",
                    "an approved initialization source is missing",
                    details={"path": str(source_path)},
                )
            expected_hash = str(
                item.get("content_hash") or item.get("sha256") or ""
            )
            actual_hash = sha256_file(source_path)
            if expected_hash and actual_hash != expected_hash:
                raise ContinuityError(
                    "SOURCE_MANIFEST_DRIFT",
                    "an initialization source changed after proposal freeze",
                    details={
                        "path": str(source_path),
                        "expected": expected_hash,
                        "actual": actual_hash,
                    },
                )

        ContinuityService._validate_initialization_target_paths(
            proposal,
            binding,
        )

    @staticmethod
    def _insert_proposed_entities(
        connection: sqlite3.Connection,
        events: Sequence[Mapping[str, Any]],
    ) -> None:
        now = utc_now()
        for raw_event in events:
            expanded = expand_correction_event(
                str(raw_event.get("event_type") or "fact"),
                raw_event,
            )
            if expanded is None:
                continue
            event_type, event = expanded
            if event_type != "entity":
                continue
            entity_id = str(event["entity_id"])
            entity_type = normalize_text(str(event.get("entity_type") or "unknown"))
            name = str(event.get("canonical_name") or entity_id)
            normalized_name = normalize_text(name)
            existing = connection.execute(
                "SELECT * FROM entities WHERE entity_id=?", (entity_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO entities(
                        entity_id, entity_type, canonical_name,
                        normalized_name, attributes_json, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entity_id,
                        entity_type,
                        name,
                        normalized_name,
                        canonical_json(dict(event.get("attributes") or {})),
                        now,
                        now,
                    ),
                )
            for alias in event.get("aliases") or []:
                ContinuityService._add_alias_in_transaction(
                    connection,
                    entity_id,
                    str(alias),
                    confidence=1.0,
                    status="confirmed",
                    source_ref="accepted_entity_event",
                )

    @staticmethod
    def _insert_event_links(
        connection: sqlite3.Connection,
        *,
        commit_id: str,
        event_ids: Sequence[str],
        events: Sequence[Mapping[str, Any]],
    ) -> None:
        now = utc_now()
        for index, event in enumerate(events):
            source_event_id = event_ids[index]
            for field, link_type in (
                ("supersedes", "supersedes"),
                ("retracts", "retracts"),
                ("caused_by", "caused_by"),
            ):
                for target in event.get(field) or []:
                    link_id = stable_hash(
                        [commit_id, source_event_id, target, link_type],
                        prefix="event_link_",
                    )
                    connection.execute(
                        """
                        INSERT INTO event_links(
                            link_id, source_commit_id, source_event_id,
                            target_event_id, link_type, created_at
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (
                            link_id,
                            commit_id,
                            source_event_id,
                            target,
                            link_type,
                            now,
                        ),
                    )

    @staticmethod
    def _insert_automatic_supersession(
        connection: sqlite3.Connection,
        *,
        proposal: sqlite3.Row,
        commit_id: str,
    ) -> None:
        authority_change = changes_authority(
            str(proposal["artifact_stage"]),
            str(proposal["branch_id"]),
        )
        # A prior supersession link is not evidence that the target is still
        # inactive: its source proposal/event may have been retracted or
        # superseded later.  Compute the effective inactive set from the
        # immutable link ledger after this commit's explicit links have been
        # inserted, then link every older *currently active* event.  This
        # allows a later artifact revision to supersede a fact restored by
        # retracting an earlier correction.
        authority_inactive, branch_inactive = (
            ReplayEngine._inactive_event_sets(connection)
        )
        inactive = (
            authority_inactive
            if authority_change
            else branch_inactive.get(str(proposal["branch_id"]), set())
        )
        old_events = connection.execute(
            """
            SELECT e.event_id
            FROM continuity_events AS e
            JOIN canon_commits AS c ON c.commit_id=e.commit_id
            WHERE c.operation='accept'
              AND c.artifact_id=?
              AND c.branch_id=?
              AND c.changes_authority=?
              AND c.artifact_revision < ?
            ORDER BY c.artifact_revision, e.event_ordinal
            """,
            (
                proposal["artifact_id"],
                proposal["branch_id"],
                int(authority_change),
                proposal["artifact_revision"],
            ),
        ).fetchall()
        old_events = [
            row
            for row in old_events
            if str(row["event_id"]) not in inactive
        ]
        now = utc_now()
        for row in old_events:
            target = str(row["event_id"])
            link_id = stable_hash(
                [commit_id, target, "artifact_revision_supersedes"],
                prefix="event_link_",
            )
            connection.execute(
                """
                INSERT INTO event_links(
                    link_id, source_commit_id, source_event_id,
                    target_event_id, link_type, created_at
                ) VALUES(?, ?, NULL, ?, 'supersedes', ?)
                """,
                (link_id, commit_id, target, now),
            )

    @staticmethod
    def _insert_initialization_side_effects(
        connection: sqlite3.Connection,
        proposal: sqlite3.Row,
        commit_id: str,
        *,
        approved_target_root: str,
    ) -> str | None:
        if str(proposal["proposal_kind"]) != "initialization_bundle":
            return None
        payload = dict(_json_load(str(proposal["payload_json"]), {}))
        bundle = dict(payload.get("bundle") or payload)
        package = dict(payload.get("lifecycle_package") or {})
        manifest = (
            package.get("source_manifest")
            or bundle.get("source_manifest")
            or []
        )
        if isinstance(manifest, Mapping):
            manifest = list(manifest.values())
        now = utc_now()
        for index, item in enumerate(manifest):
            if not isinstance(item, Mapping):
                continue
            source_path = str(item.get("path") or item.get("source_path") or "")
            content_hash = str(
                item.get("content_hash") or item.get("sha256") or ""
            )
            if not source_path or not content_hash:
                continue
            source_id = str(
                item.get("source_id")
                or stable_hash(
                    [source_path, content_hash], prefix="source_"
                )
            )
            entry_id = stable_hash(
                [commit_id, source_id], prefix="manifest_entry_"
            )
            connection.execute(
                """
                INSERT INTO accepted_source_manifest(
                    manifest_entry_id, commit_id, source_id, source_path,
                    content_hash, source_role, manifest_status,
                    metadata_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    entry_id,
                    commit_id,
                    source_id,
                    source_path,
                    content_hash,
                    normalize_source_role(item.get("source_role")),
                    canonical_json(
                        {
                            key: value
                            for key, value in item.items()
                            if key
                            not in {
                                "source_id",
                                "path",
                                "source_path",
                                "content_hash",
                                "sha256",
                                "source_role",
                            }
                        }
                    ),
                    now,
                ),
            )
        plan = package.get("materialization_plan") or bundle.get(
            "materialization_plan"
        ) or {"artifacts": bundle.get("artifact_manifest") or []}
        artifacts = (
            plan.get("artifacts")
            or plan.get("files")
            or bundle.get("artifact_manifest")
            or []
        )
        for index, item in enumerate(artifacts):
            if not isinstance(item, Mapping):
                continue
            source_path = str(item.get("path") or "").strip()
            content_hash = str(
                item.get("proposed_new_hash")
                or item.get("new_hash")
                or item.get("content_hash")
                or ""
            ).strip()
            if not source_path or not content_hash:
                continue
            if connection.execute(
                """
                SELECT 1 FROM accepted_source_manifest
                WHERE commit_id=? AND source_path=? AND content_hash=?
                LIMIT 1
                """,
                (commit_id, source_path, content_hash),
            ).fetchone():
                continue
            normalized_path = source_path.replace("\\", "/")
            source_role = item.get("source_role")
            if not source_role:
                folded = normalized_path.casefold()
                if folded.startswith("正文/"):
                    source_role = "canon"
                elif folded.startswith("剧情/"):
                    source_role = "outline"
                elif folded.startswith("资料/"):
                    source_role = "reference"
                else:
                    source_role = "setting"
            source_id = stable_hash(
                ["materialized", source_path, content_hash],
                prefix="source_",
            )
            entry_id = stable_hash(
                [commit_id, source_id], prefix="manifest_entry_"
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO accepted_source_manifest(
                    manifest_entry_id, commit_id, source_id, source_path,
                    content_hash, source_role, manifest_status,
                    metadata_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    entry_id,
                    commit_id,
                    source_id,
                    source_path,
                    content_hash,
                    normalize_source_role(source_role),
                    canonical_json(
                        {
                            "origin": "materialized_artifact",
                            "artifact_id": item.get("artifact_id"),
                            "operation": item.get("operation"),
                            "manifest_index": index,
                        }
                    ),
                    now,
                ),
            )
        target_root = str(Path(approved_target_root).expanduser().resolve())
        run_id = stable_hash([commit_id, "materialize"], prefix="materialize_")
        connection.execute(
            """
            INSERT OR IGNORE INTO materialization_runs(
                run_id, commit_id, target_root, run_status, plan_json,
                staging_path, created_at, updated_at
            ) VALUES(?, ?, ?, 'pending', ?, '', ?, ?)
            """,
            (
                run_id,
                commit_id,
                target_root,
                canonical_json(plan),
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO materialization_journal(
                journal_id, run_id, step_no, step_name,
                step_status, details_json, created_at
            ) VALUES(?, ?, 1, 'db_commit', 'completed', ?, ?)
            """,
            (
                stable_hash([run_id, 1], prefix="materialize_journal_"),
                run_id,
                canonical_json({"commit_id": commit_id}),
                now,
            ),
        )
        return run_id

    def _consume_lifecycle_grant(
        self,
        *,
        proposal_id: str,
        approval_id: str,
        expected_canon_revision: int,
        operation: str,
        reason: str = "",
    ) -> dict[str, Any]:
        expected_canon_revision = validate_positive_int(
            expected_canon_revision,
            "expected_canon_revision",
            allow_none=False,
            minimum=0,
        )
        event_experience_service = (
            self._preflight_event_experience_service(
                proposal_id,
                active_checks=operation == "accept",
            )
        )
        with self.store.transaction() as connection:
            grant, token_hash = self._load_grant(connection, approval_id)
            binding = dict(_json_load(str(grant["binding_json"]), {}))
            binding_hash = stable_hash(binding, prefix="grant_binding_")
            if binding_hash != str(grant["binding_hash"]):
                raise ContinuityError(
                    "APPROVAL_BINDING_CORRUPT",
                    "stored grant binding hash does not match its payload",
                )
            request_hash = stable_hash(
                {
                    "proposal_id": proposal_id,
                    "token_hash": token_hash,
                    "binding_hash": binding_hash,
                    "expected_canon_revision": expected_canon_revision,
                    "operation": operation,
                    "reason": reason,
                },
                prefix="accepted_request_",
            )
            if str(grant["proposal_id"]) != proposal_id:
                raise ContinuityError(
                    "APPROVAL_PROPOSAL_MISMATCH",
                    "grant is bound to a different proposal",
                )
            proposal = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if proposal is None:
                raise ContinuityError(
                    "PROPOSAL_NOT_FOUND", f"unknown proposal: {proposal_id}"
                )
            payload, verified_events = self._strict_proposal_content(proposal)
            if grant["consumed_request_hash"] is not None:
                if str(grant["consumed_request_hash"]) != request_hash:
                    raise ContinuityError(
                        "APPROVAL_GRANT_CONSUMED",
                        "approval grant was already consumed by another request",
                    )
                commit = connection.execute(
                    "SELECT * FROM canon_commits WHERE commit_id=?",
                    (grant["accepted_commit_id"],),
                ).fetchone()
                if commit is None:
                    raise ContinuityError(
                        "COMMIT_NOT_FOUND",
                        "consumed grant references a missing commit",
                    )
                return self._commit_response(connection, commit, retry=True)

            if parse_utc(str(grant["expires_at"])) <= datetime.now(timezone.utc):
                raise ContinuityError(
                    "APPROVAL_GRANT_EXPIRED", "approval grant has expired"
                )
            operations = set(
                _json_load(str(grant["authorized_operations_json"]), [])
            )
            required_operation = operation
            if (
                operation == "accept"
                and str(proposal["proposal_kind"]) == "initialization_bundle"
            ):
                required_operation = "accept_initialization"
            elif (
                operation == "accept"
                and str(proposal["proposal_kind"]) == "power_spec_change"
            ):
                required_operation = "accept_power_spec"
            elif (
                operation == "accept"
                and str(proposal["proposal_kind"])
                == SOURCE_MANIFEST_PROPOSAL_KIND
            ):
                required_operation = SOURCE_MANIFEST_ACCEPT_OPERATION
            if required_operation not in operations:
                raise ContinuityError(
                    "APPROVAL_OPERATION_NOT_AUTHORIZED",
                    f"grant does not authorize {required_operation}",
                )
            if int(grant["expected_canon_revision"]) != expected_canon_revision:
                raise ContinuityError(
                    "APPROVAL_REVISION_MISMATCH",
                    "request revision differs from the grant binding",
                )

            lifecycle_binding = self._proposal_lifecycle_binding(
                proposal,
                payload,
            )
            if lifecycle_binding:
                lifecycle_mismatches = {
                    field: {
                        "binding": binding.get(field),
                        "actual": value,
                    }
                    for field, value in lifecycle_binding.items()
                    if binding.get(field) != value
                }
                if lifecycle_mismatches:
                    raise ContinuityError(
                        "APPROVAL_BINDING_MISMATCH",
                        "grant differs from the proposal lifecycle identity",
                        details={"mismatches": lifecycle_mismatches},
                    )
            elif "lifecycle_identity" in binding:
                raise ContinuityError(
                    "APPROVAL_BINDING_MISMATCH",
                    "grant carries lifecycle identity for a legacy proposal",
                )

            for field in (
                "proposal_id",
                "artifact_stage",
                "branch_id",
                "chapter_no",
                "artifact_revision",
                "prepared_canon_revision",
                "payload_hash",
            ):
                actual = proposal[field]
                if binding.get(field) != actual:
                    raise ContinuityError(
                        "APPROVAL_BINDING_MISMATCH",
                        f"grant binding differs at {field}",
                        details={
                            "field": field,
                            "binding": binding.get(field),
                            "actual": actual,
                        },
                    )
            active_before = self.store.get_meta_int(
                connection, "active_canon_revision"
            )
            head_before = self.store.get_meta_int(
                connection, "head_canon_revision"
            )
            if active_before != expected_canon_revision:
                raise ContinuityError(
                    "CANON_REVISION_CONFLICT",
                    "active canon revision changed after preparation",
                    details={
                        "expected": expected_canon_revision,
                        "actual": active_before,
                    },
                )

            if operation == "accept":
                if str(proposal["canon_status"]) != "proposed":
                    raise ContinuityError(
                        "INVALID_PROPOSAL_TRANSITION",
                        f"cannot accept a {proposal['canon_status']} proposal",
                    )
                if str(proposal["validation_status"]) != "valid":
                    raise ContinuityError(
                        "PROPOSAL_QUARANTINED",
                        "proposal has unresolved validation issues",
                    )
                events = list(verified_events)
                expanded_validation_events = (
                    self._expanded_validation_events(events)
                )
                authority_change = changes_authority(
                    str(proposal["artifact_stage"]),
                    str(proposal["branch_id"]),
                )
                assert_item_rollout_acceptance(
                    connection,
                    expanded_validation_events,
                    rollout_policy=self.item_rollout_policy,
                    changes_authority=authority_change,
                )
                self._validate_advantage_events(
                    connection,
                    expanded_validation_events,
                )
                if lifecycle_binding:
                    self._validate_receipt_lifecycle_binding(
                        connection,
                        proposal,
                        payload,
                        lifecycle_binding,
                        allow_assistant_bind=False,
                    )
                    self._validate_active_projection_binding(
                        connection,
                        lifecycle_binding,
                    )
                    self._validate_event_experience_binding(
                        connection,
                        lifecycle_binding,
                        event_experience_service,
                        verified_events,
                    )
                    self._validate_extraction_job_binding(
                        connection,
                        proposal,
                        payload,
                        lifecycle_binding,
                    )
                self._validate_initialization_inputs(
                    connection,
                    proposal,
                    binding,
                )
                if (
                    str(proposal["proposal_kind"])
                    == SOURCE_MANIFEST_PROPOSAL_KIND
                ):
                    validate_source_manifest_proposal_envelope(
                        connection,
                        proposal,
                        payload,
                        verified_events,
                    )
                    manifest_change = dict(
                        payload.get("source_manifest_change") or {}
                    )
                    validate_frozen_manifest_change(
                        connection,
                        self.store.project_root,
                        manifest_change,
                    )
                    manifest_binding = {
                        "source_manifest_plan_hash": manifest_change.get(
                            "plan_hash"
                        ),
                        "source_manifest_base_hash": manifest_change.get(
                            "base_manifest_hash"
                        ),
                        "source_manifest_target_hash": manifest_change.get(
                            "target_manifest_hash"
                        ),
                    }
                    mismatches = {
                        field: {
                            "binding": binding.get(field),
                            "actual": value,
                        }
                        for field, value in manifest_binding.items()
                        if binding.get(field) != value
                    }
                    if mismatches:
                        raise ContinuityError(
                            "APPROVAL_BINDING_MISMATCH",
                            "grant differs from the frozen source manifest plan",
                            details={"mismatches": mismatches},
                        )
                self._validate_invariants(connection, proposal, events)
            elif operation == "retract":
                if str(proposal["canon_status"]) != "accepted":
                    raise ContinuityError(
                        "INVALID_PROPOSAL_TRANSITION",
                        f"cannot retract a {proposal['canon_status']} proposal",
                    )
                events = []
                accepted = connection.execute(
                    """
                    SELECT * FROM canon_commits
                    WHERE proposal_id=? AND operation='accept'
                    """,
                    (proposal_id,),
                ).fetchone()
                if accepted is None:
                    raise ContinuityError(
                        "ACCEPTED_COMMIT_NOT_FOUND",
                        "accepted proposal has no immutable commit",
                    )
                authority_change = bool(accepted["changes_authority"])
                if (
                    str(proposal["proposal_kind"])
                    == SOURCE_MANIFEST_PROPOSAL_KIND
                ):
                    if (
                        latest_active_manifest_proposal_id(connection)
                        != proposal_id
                    ):
                        raise ContinuityError(
                            "SOURCE_MANIFEST_RETRACT_NOT_LATEST",
                            "only the current source manifest migration may be retracted",
                            details={"proposal_id": proposal_id},
                        )
                    validate_source_manifest_retract_files(
                        self.store.project_root,
                        dict(
                            payload.get("source_manifest_change")
                            or {}
                        ),
                    )
            else:
                raise ContinuityError(
                    "INVALID_LIFECYCLE_OPERATION",
                    f"unsupported lifecycle operation: {operation}",
                )

            head_after = head_before + 1
            active_after = head_after if authority_change else active_before
            commit_id = stable_hash(
                [
                    proposal_id,
                    binding_hash,
                    head_before,
                    operation,
                    reason,
                ],
                prefix="canon_commit_",
            )
            now = utc_now()
            acceptance_source = {
                "issuer": str(grant["issuer"]),
                "channel": str(grant["channel"]),
                "binding_hash": binding_hash,
                "operation": operation,
            }
            connection.execute(
                """
                INSERT INTO canon_commits(
                    commit_id, proposal_id, operation, artifact_id,
                    artifact_stage, branch_id, chapter_no, scene_index,
                    artifact_revision, head_revision_before,
                    head_revision_after, active_revision_before,
                    active_revision_after, changes_authority,
                    accepted_request_hash, grant_token_hash, payload_hash,
                    acceptance_source_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    commit_id,
                    proposal_id,
                    operation,
                    proposal["artifact_id"],
                    proposal["artifact_stage"],
                    proposal["branch_id"],
                    proposal["chapter_no"],
                    proposal["scene_index"],
                    proposal["artifact_revision"],
                    head_before,
                    head_after,
                    active_before,
                    active_after,
                    int(authority_change),
                    request_hash,
                    token_hash,
                    proposal["payload_hash"],
                    canonical_json(acceptance_source),
                    now,
                ),
            )

            materialization_run_id: str | None = None
            if operation == "accept":
                self._insert_proposed_entities(connection, events)
                event_ids: list[str] = []
                for index, event in enumerate(events):
                    event_id = stable_hash(
                        [commit_id, index, event],
                        prefix="story_event_",
                    )
                    event_ids.append(event_id)
                    validate_event_branch_consistency(
                        event,
                        str(proposal["branch_id"]),
                        event_id=event_id,
                    )
                    expanded = expand_correction_event(
                        str(event.get("event_type") or "fact"),
                        event,
                        event_id=event_id,
                    )
                    replacement = (
                        dict(event) if expanded is None else expanded[1]
                    )
                    connection.execute(
                        """
                        INSERT INTO continuity_events(
                            event_id, commit_id, event_ordinal, event_type,
                            scope, branch_id, artifact_id, artifact_revision,
                            chapter_no, scene_index, story_time, narrative_mode,
                            entity_id, subject_entity_id, target_entity_id,
                            payload_json, evidence_json, created_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            commit_id,
                            index,
                            event["event_type"],
                            replacement.get("scope", event.get("scope")),
                            proposal["branch_id"],
                            proposal["artifact_id"],
                            proposal["artifact_revision"],
                            replacement.get(
                                "chapter_no", proposal["chapter_no"]
                            ),
                            replacement.get(
                                "scene_index", proposal["scene_index"]
                            ),
                            replacement.get("story_time"),
                            replacement.get("narrative_mode", "linear"),
                            replacement.get("entity_id")
                            or replacement.get("actor_entity_id")
                            or replacement.get("owner_entity_id")
                            or replacement.get("observer_entity_id")
                            or replacement.get("spec_entity_id")
                            or replacement.get("believer_entity_id"),
                            replacement.get("source_entity_id")
                            or replacement.get("actor_entity_id")
                            or replacement.get("to_owner_entity_id")
                            or replacement.get("owner_entity_id")
                            or replacement.get("subject_entity_id")
                            or replacement.get("observer_entity_id")
                            or replacement.get("spec_entity_id")
                            or replacement.get("believer_entity_id"),
                            replacement.get("target_entity_id")
                            or replacement.get("to_location_entity_id")
                            or replacement.get("item_entity_id")
                            or replacement.get("ability_entity_id")
                            or replacement.get("track_entity_id")
                            or replacement.get("to_rank_entity_id")
                            or replacement.get("resource_entity_id")
                            or replacement.get("status_entity_id")
                            or replacement.get("qualification_entity_id")
                            or replacement.get("source_entity_id")
                            or replacement.get("spec_entity_id"),
                            canonical_json(event),
                            canonical_json(event.get("evidence") or {}),
                            now,
                        ),
                    )
                self._insert_event_links(
                    connection,
                    commit_id=commit_id,
                    event_ids=event_ids,
                    events=events,
                )
                self._insert_automatic_supersession(
                    connection,
                    proposal=proposal,
                    commit_id=commit_id,
                )
                connection.execute(
                    """
                    UPDATE artifacts
                    SET active=0, updated_at=?
                    WHERE artifact_id=? AND branch_id=?
                      AND artifact_revision < ?
                    """,
                    (
                        now,
                        proposal["artifact_id"],
                        proposal["branch_id"],
                        proposal["artifact_revision"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE artifacts
                    SET canon_status='accepted', active=1, updated_at=?
                    WHERE artifact_version_id=?
                    """,
                    (now, proposal["artifact_version_id"]),
                )
                connection.execute(
                    """
                    UPDATE proposals
                    SET canon_status='accepted', accepted_commit_id=?,
                        updated_at=?
                    WHERE proposal_id=?
                    """,
                    (commit_id, now, proposal_id),
                )
                materialization_run_id = (
                    self._insert_initialization_side_effects(
                        connection,
                        proposal,
                        commit_id,
                        approved_target_root=str(
                            binding["target_project_real_path"]
                        ),
                    )
                )
                if (
                    str(proposal["proposal_kind"])
                    == SOURCE_MANIFEST_PROPOSAL_KIND
                ):
                    insert_manifest_upserts(
                        connection,
                        proposal,
                        commit_id,
                    )
            else:
                targets = connection.execute(
                    """
                    SELECT e.event_id
                    FROM continuity_events AS e
                    JOIN canon_commits AS c ON c.commit_id=e.commit_id
                    WHERE c.proposal_id=? AND c.operation='accept'
                    ORDER BY e.event_ordinal
                    """,
                    (proposal_id,),
                ).fetchall()
                for target_row in targets:
                    target = str(target_row["event_id"])
                    link_id = stable_hash(
                        [commit_id, target, "retracts"],
                        prefix="event_link_",
                    )
                    connection.execute(
                        """
                        INSERT INTO event_links(
                            link_id, source_commit_id, source_event_id,
                            target_event_id, link_type, created_at
                        ) VALUES(?, ?, NULL, ?, 'retracts', ?)
                        """,
                        (link_id, commit_id, target, now),
                    )
                connection.execute(
                    """
                    UPDATE artifacts
                    SET canon_status='retracted', active=0, updated_at=?
                    WHERE artifact_version_id=?
                    """,
                    (now, proposal["artifact_version_id"]),
                )
                connection.execute(
                    """
                    UPDATE proposals
                    SET canon_status='retracted', status_reason=?, updated_at=?
                    WHERE proposal_id=?
                    """,
                    (reason, now, proposal_id),
                )
                prior_artifact = connection.execute(
                    """
                    SELECT a.artifact_version_id
                    FROM artifacts AS a
                    JOIN proposals AS p
                      ON p.artifact_version_id=a.artifact_version_id
                    WHERE a.artifact_id=?
                      AND a.branch_id=?
                      AND a.artifact_revision < ?
                      AND p.canon_status='accepted'
                    ORDER BY a.artifact_revision DESC
                    LIMIT 1
                    """,
                    (
                        proposal["artifact_id"],
                        proposal["branch_id"],
                        proposal["artifact_revision"],
                    ),
                ).fetchone()
                if prior_artifact is not None:
                    connection.execute(
                        """
                        UPDATE artifacts
                        SET active=1, updated_at=?
                        WHERE artifact_version_id=?
                        """,
                        (now, prior_artifact["artifact_version_id"]),
                    )

            connection.execute(
                """
                UPDATE approval_grants
                SET consumed_request_hash=?, accepted_commit_id=?,
                    consumed_at=?
                WHERE token_hash=?
                """,
                (request_hash, commit_id, now, token_hash),
            )
            self.store.set_meta_int(
                connection, "head_canon_revision", head_after
            )
            self.store.set_meta_int(
                connection, "active_canon_revision", active_after
            )
            replay_result = self.replay_engine.rebuild_in_transaction(
                connection
            )
            connection.execute(
                "UPDATE canon_commits SET projection_hash=? WHERE commit_id=?",
                (replay_result["projection_hash"], commit_id),
            )
            commit = connection.execute(
                "SELECT * FROM canon_commits WHERE commit_id=?", (commit_id,)
            ).fetchone()
            response = self._commit_response(connection, commit, retry=False)
            response["materialization_run_id"] = materialization_run_id
            return response

    def _materialize_accepted_advantage_sidecar(
        self,
        commit_id: str,
    ) -> dict[str, Any] | None:
        """Atomically publish the grant-bound Advantage initialization sidecar.

        Advantage replay consumes only accepted typed events, but the frozen
        sidecar remains the human/tooling artifact.  Initialization acceptance
        therefore publishes this one internal artifact immediately, without
        requiring a second materialization command.  The approval binding
        still owns the target root, path and old/new hashes.
        """

        with self.store.read_connection() as connection:
            row = connection.execute(
                """
                SELECT p.payload_json, c.grant_token_hash
                FROM canon_commits AS c
                JOIN proposals AS p ON p.proposal_id=c.proposal_id
                WHERE c.commit_id=? AND c.operation='accept'
                """,
                (commit_id,),
            ).fetchone()
            if row is None:
                raise ContinuityError(
                    "COMMIT_NOT_FOUND",
                    f"unknown accepted commit: {commit_id}",
                )
            grant_row = connection.execute(
                """
                SELECT binding_json, binding_hash
                FROM approval_grants
                WHERE token_hash=?
                """,
                (row["grant_token_hash"],),
            ).fetchone()
        payload = dict(_json_load(str(row["payload_json"]), {}))
        bundle = dict(payload.get("bundle") or {})
        artifacts = [
            dict(item)
            for item in bundle.get("artifact_manifest") or []
            if isinstance(item, Mapping)
            and (
                str(item.get("logical_owner") or "")
                == _ADVANTAGE_SIDECAR_LOGICAL_OWNER
                or str(item.get("path") or "").replace("\\", "/")
                == _ADVANTAGE_SIDECAR_RELATIVE_PATH
            )
        ]
        if not artifacts:
            return None
        if len(artifacts) != 1:
            raise ContinuityError(
                "ADVANTAGE_SIDECAR_DUPLICATE",
                "accepted initialization contains multiple Advantage sidecars",
            )
        validated_sidecar = (
            self._validated_initialization_advantage_sidecar(
                bundle,
                dict(payload.get("lifecycle_package") or {}),
            )
        )
        if validated_sidecar is None:
            raise ContinuityError(
                "ADVANTAGE_SIDECAR_MISSING",
                "accepted initialization Advantage sidecar is unavailable",
            )
        if grant_row is None:
            raise ContinuityError(
                "APPROVAL_GRANT_NOT_FOUND",
                "accepted Advantage sidecar has no approval binding",
            )
        binding = dict(_json_load(str(grant_row["binding_json"]), {}))
        if stable_hash(binding, prefix="grant_binding_") != str(
            grant_row["binding_hash"]
        ):
            raise ContinuityError(
                "APPROVAL_BINDING_CORRUPT",
                "stored Advantage sidecar approval binding is corrupt",
            )

        artifact = artifacts[0]
        relative = _safe_relative_path(str(artifact.get("path") or ""))
        if relative.as_posix() != _ADVANTAGE_SIDECAR_RELATIVE_PATH:
            raise ContinuityError(
                "ADVANTAGE_SIDECAR_PATH_MISMATCH",
                "Advantage sidecar path differs from the v1 contract",
                details={"path": relative.as_posix()},
            )
        content = artifact.get("proposed_content")
        if not isinstance(content, str) or not content:
            raise ContinuityError(
                "ADVANTAGE_SIDECAR_CONTENT_MISSING",
                "frozen Advantage sidecar bytes are not available",
            )
        data = content.encode("utf-8")
        proposed_hash = hashlib.sha256(data).hexdigest()
        declared_hash = str(
            artifact.get("proposed_new_hash")
            or artifact.get("content_hash")
            or ""
        )
        if not declared_hash or proposed_hash != declared_hash:
            raise ContinuityError(
                "ADVANTAGE_SIDECAR_CONTENT_HASH_MISMATCH",
                "frozen Advantage sidecar bytes differ from the artifact hash",
                details={
                    "expected": declared_hash,
                    "actual": proposed_hash,
                },
            )
        sidecar = dict(validated_sidecar["package"])
        if str(sidecar.get("schema_version") or "") != ADVANTAGE_SCHEMA_VERSION:
            raise ContinuityError(
                "ADVANTAGE_SIDECAR_SCHEMA_UNSUPPORTED",
                "frozen Advantage sidecar schema differs from runtime v1",
            )
        package_hash = str(validated_sidecar["package_hash"])
        declared_package_hashes = {
            str(value)
            for value in (
                artifact.get("advantage_package_hash"),
                (payload.get("lifecycle_package") or {}).get(
                    "advantage_package_hash"
                )
                if isinstance(payload.get("lifecycle_package"), Mapping)
                else None,
            )
            if value
        }
        provenance = bundle.get("provenance")
        if isinstance(provenance, Mapping):
            for reference in provenance.get("advantage_sidecars") or []:
                if not isinstance(reference, Mapping):
                    continue
                if str(reference.get("path") or "").replace(
                    "\\", "/"
                ) != relative.as_posix():
                    continue
                reference_hash = str(reference.get("content_hash") or "")
                if reference_hash and reference_hash != proposed_hash:
                    raise ContinuityError(
                        "ADVANTAGE_SIDECAR_REFERENCE_MISMATCH",
                        "Advantage sidecar reference content hash drifted",
                    )
                if reference.get("package_hash"):
                    declared_package_hashes.add(
                        str(reference["package_hash"])
                    )
        if (
            not package_hash
            or not declared_package_hashes
            or declared_package_hashes != {package_hash}
        ):
            raise ContinuityError(
                "ADVANTAGE_SIDECAR_PACKAGE_HASH_MISMATCH",
                "Advantage sidecar package hash differs from the frozen proposal",
                details={
                    "package_hash": package_hash,
                    "declared": sorted(declared_package_hashes),
                },
            )

        authorized_paths = {
            _safe_relative_path(str(path)).as_posix()
            for path in binding.get("authorized_paths") or []
        }
        if relative.as_posix() not in authorized_paths:
            raise ContinuityError(
                "MATERIALIZATION_PATH_NOT_AUTHORIZED",
                "Advantage sidecar path is outside the approval binding",
                details={"path": relative.as_posix()},
            )
        authorized_hashes = {
            _safe_relative_path(str(item["path"])).as_posix(): {
                "expected_old_hash": item.get("expected_old_hash"),
                "proposed_new_hash": item.get("proposed_new_hash"),
            }
            for item in binding.get("target_old_new_hashes") or []
            if isinstance(item, Mapping) and item.get("path")
        }
        bound_hashes = authorized_hashes.get(relative.as_posix())
        if (
            bound_hashes is None
            or str(bound_hashes.get("proposed_new_hash") or "")
            != proposed_hash
            or bound_hashes.get("expected_old_hash")
            != artifact.get("expected_old_hash")
        ):
            raise ContinuityError(
                "MATERIALIZATION_HASH_NOT_AUTHORIZED",
                "Advantage sidecar hashes differ from the approval binding",
                details={"path": relative.as_posix()},
            )

        root_path = Path(
            os.path.abspath(
                Path(
                    str(
                        binding.get("target_project_real_path")
                        or payload.get("target_project_real_path")
                        or self.store.project_root
                    )
                ).expanduser()
            )
        )
        _assert_no_reparse_path(
            root_path,
            anchor=Path(root_path.anchor),
            code="UNSAFE_MATERIALIZATION_PATH",
            label="Advantage sidecar target root",
        )
        root = root_path.resolve()
        target = root / relative
        _assert_no_reparse_path(
            target,
            anchor=root,
            code="UNSAFE_MATERIALIZATION_PATH",
            label="Advantage sidecar target",
        )
        if target.exists() and not target.is_file():
            raise ContinuityError(
                "TARGET_PATH_INVALID",
                "Advantage sidecar target is not a regular file",
                details={"path": str(target)},
            )
        expected_old_hash = artifact.get("expected_old_hash")

        def current_hash() -> str | None:
            return sha256_file(target) if target.is_file() else None

        def mark_manifest_active() -> None:
            with self.store.transaction() as connection:
                connection.execute(
                    """
                    UPDATE accepted_source_manifest
                    SET manifest_status='active'
                    WHERE commit_id=? AND source_path=? AND content_hash=?
                    """,
                    (
                        commit_id,
                        relative.as_posix(),
                        proposed_hash,
                    ),
                )

        actual_old_hash = current_hash()
        if actual_old_hash == proposed_hash:
            mark_manifest_active()
            return {
                "status": "already_materialized",
                "path": relative.as_posix(),
                "package_hash": package_hash,
                "content_hash": proposed_hash,
            }
        if (
            (expected_old_hash is None and actual_old_hash is not None)
            or (
                expected_old_hash is not None
                and actual_old_hash != str(expected_old_hash)
            )
        ):
            raise ContinuityError(
                "TARGET_HASH_CONFLICT",
                "Advantage sidecar target changed after proposal review",
                details={
                    "path": relative.as_posix(),
                    "expected": expected_old_hash,
                    "actual": actual_old_hash,
                },
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        _assert_no_reparse_path(
            target.parent,
            anchor=root,
            code="UNSAFE_MATERIALIZATION_PATH",
            label="Advantage sidecar parent",
        )
        temporary = target.parent / (
            f".{target.name}.{commit_id}.{secrets.token_hex(8)}.tmp"
        )
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if sha256_file(temporary) != proposed_hash:
                raise ContinuityError(
                    "STAGING_HASH_MISMATCH",
                    "staged Advantage sidecar hash mismatch",
                )
            latest_hash = current_hash()
            if latest_hash == proposed_hash:
                mark_manifest_active()
                return {
                    "status": "already_materialized",
                    "path": relative.as_posix(),
                    "package_hash": package_hash,
                    "content_hash": proposed_hash,
                }
            if (
                (expected_old_hash is None and latest_hash is not None)
                or (
                    expected_old_hash is not None
                    and latest_hash != str(expected_old_hash)
                )
            ):
                raise ContinuityError(
                    "TARGET_HASH_CONFLICT",
                    "Advantage sidecar target changed during atomic publish",
                    details={
                        "path": relative.as_posix(),
                        "expected": expected_old_hash,
                        "actual": latest_hash,
                    },
                )
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        if current_hash() != proposed_hash:
            raise ContinuityError(
                "MATERIALIZATION_ACTIVATION_FAILED",
                "Advantage sidecar atomic publish produced unexpected bytes",
            )
        mark_manifest_active()
        return {
            "status": "materialized",
            "path": relative.as_posix(),
            "package_hash": package_hash,
            "content_hash": proposed_hash,
        }

    def accept_proposal(
        self,
        proposal_id: str,
        *,
        approval_id: str,
        expected_canon_revision: int,
    ) -> dict[str, Any]:
        response = self._consume_lifecycle_grant(
            proposal_id=proposal_id,
            approval_id=approval_id,
            expected_canon_revision=expected_canon_revision,
            operation="accept",
        )
        response["advantage_sidecar_materialization"] = (
            self._materialize_accepted_advantage_sidecar(
                str(response["commit_id"])
            )
        )
        response["readable_item_projection"] = (
            refresh_item_readable_projection_safe(self.store)
        )
        response["readable_advantage_projection"] = (
            refresh_advantage_readable_projection_safe(self.store)
        )
        return response

    def retract_proposal(
        self,
        proposal_id: str,
        *,
        approval_id: str,
        expected_canon_revision: int,
        reason: str,
    ) -> dict[str, Any]:
        response = self._consume_lifecycle_grant(
            proposal_id=proposal_id,
            approval_id=approval_id,
            expected_canon_revision=expected_canon_revision,
            operation="retract",
            reason=reason,
        )
        response["readable_item_projection"] = (
            refresh_item_readable_projection_safe(self.store)
        )
        response["readable_advantage_projection"] = (
            refresh_advantage_readable_projection_safe(self.store)
        )
        return response

    @staticmethod
    def _commit_response(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        retry: bool,
    ) -> dict[str, Any]:
        events = [
            {
                "event_id": str(event["event_id"]),
                "event_type": str(event["event_type"]),
                "scope": str(event["scope"]),
                "chapter_no": event["chapter_no"],
                "scene_index": event["scene_index"],
                "payload": _json_load(str(event["payload_json"]), {}),
            }
            for event in connection.execute(
                """
                SELECT * FROM continuity_events
                WHERE commit_id=?
                ORDER BY event_ordinal
                """,
                (row["commit_id"],),
            )
        ]
        materialization = connection.execute(
            """
            SELECT run_id
            FROM materialization_runs
            WHERE commit_id=?
            """,
            (row["commit_id"],),
        ).fetchone()
        item_metadata = read_item_projection_metadata(connection)
        advantage_metadata = read_advantage_projection_metadata(connection)
        return {
            "commit_id": str(row["commit_id"]),
            "proposal_id": str(row["proposal_id"]),
            "operation": str(row["operation"]),
            "artifact_id": str(row["artifact_id"]),
            "artifact_stage": str(row["artifact_stage"]),
            "branch_id": str(row["branch_id"]),
            "chapter_no": row["chapter_no"],
            "scene_index": row["scene_index"],
            "artifact_revision": int(row["artifact_revision"]),
            "head_canon_revision": int(row["head_revision_after"]),
            "active_canon_revision": int(row["active_revision_after"]),
            "changes_authority": bool(row["changes_authority"]),
            "projection_hash": str(row["projection_hash"]),
            "item_projection_hash": str(
                item_metadata.get(ITEM_PROJECTION_META_HASH) or ""
            ),
            "item_projection_schema_version": int(
                item_metadata.get(ITEM_PROJECTION_META_VERSION) or 0
            ),
            "advantage_projection_hash": str(
                advantage_metadata.get(ADVANTAGE_META_HASH) or ""
            ),
            "advantage_projection_schema_version": int(
                advantage_metadata.get(ADVANTAGE_META_VERSION) or 0
            ),
            "acceptance_source": _json_load(
                str(row["acceptance_source_json"]), {}
            ),
            "events": events,
            # Keep the short retry flag used by the initialization/lifecycle
            # receipt contract.  ``idempotent_retry`` remains the descriptive
            # compatibility field used by older callers.
            "retry": retry,
            "idempotent_retry": retry,
            "materialization_run_id": (
                str(materialization["run_id"])
                if materialization is not None
                else None
            ),
        }

    def inspect_commit(self, commit_id: str) -> dict[str, Any]:
        with self.store.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM canon_commits WHERE commit_id=?", (commit_id,)
            ).fetchone()
            if row is None:
                raise ContinuityError(
                    "COMMIT_NOT_FOUND", f"unknown commit: {commit_id}"
                )
            return self._commit_response(connection, row, retry=False)

    def list_active_accepted_commits(
        self,
        *,
        authority_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Return active accepted artifact commits in canonical order."""

        authority_clause = "AND c.changes_authority=1" if authority_only else ""
        with self.store.read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT c.*
                FROM canon_commits AS c
                JOIN proposals AS p ON p.proposal_id=c.proposal_id
                JOIN artifacts AS a
                  ON a.artifact_version_id=p.artifact_version_id
                WHERE c.operation='accept'
                  AND p.canon_status='accepted'
                  AND a.active=1
                  {authority_clause}
                ORDER BY c.active_revision_after, c.head_revision_after,
                         c.commit_id
                """
            ).fetchall()
            return [
                self._commit_response(connection, row, retry=False)
                for row in rows
            ]

    # ------------------------------------------------------------------
    # Initialization bundle acceptance
    # ------------------------------------------------------------------
    @staticmethod
    def _require_main_initialization_branch(value: Any) -> str:
        branch_id = str(value or "main").strip() or "main"
        if branch_id != "main":
            raise ContinuityError(
                "INITIALIZATION_BRANCH_UNSUPPORTED",
                "initialization bundles may only bootstrap the main branch",
                details={"branch_id": branch_id},
            )
        return branch_id

    @staticmethod
    def _initialization_package(
        value: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return ``(lifecycle_package, raw_bundle)``.

        A v0.4.5 ``PROPOSAL_FROZEN`` envelope is preferred because its
        package/source hashes and apply plan are immutable.  Plain bundle
        dictionaries remain supported for programmatic bootstrap callers.
        """

        candidate = dict(value)
        nested_bundle = candidate.get("bundle")
        branch_source = (
            nested_bundle
            if isinstance(nested_bundle, Mapping)
            else candidate
        )
        declared_branch = branch_source.get("branch_id")
        if declared_branch is None:
            declared_branch = candidate.get("branch_id")
        ContinuityService._require_main_initialization_branch(declared_branch)
        if (
            candidate.get("status") == "PROPOSAL_FROZEN"
            and isinstance(candidate.get("bundle"), Mapping)
        ):
            try:
                try:
                    from scripts.plot_init import (
                        proposal_to_lifecycle_package,
                    )
                except ImportError:
                    from plot_init import proposal_to_lifecycle_package

                package = proposal_to_lifecycle_package(candidate)
            except Exception as exc:
                code = str(
                    getattr(exc, "code", "INITIALIZATION_ADAPTER_FAILED")
                )
                raise ContinuityError(
                    code,
                    str(getattr(exc, "message", exc)),
                    details=dict(getattr(exc, "details", {}) or {}),
                ) from exc
            normalized_package = dict(package)
            raw_bundle = dict(candidate["bundle"])
            ContinuityService._validated_initialization_advantage_sidecar(
                raw_bundle,
                normalized_package,
            )
            return normalized_package, raw_bundle
        if (
            str(candidate.get("schema_version") or "").endswith(
                "init-package-v1"
            )
            and isinstance(candidate.get("events"), list)
        ):
            raw_bundle = dict(candidate.get("bundle") or {})
            ContinuityService._validated_initialization_advantage_sidecar(
                raw_bundle,
                candidate,
            )
            return candidate, raw_bundle

        raw_bundle = copy.deepcopy(candidate)
        package_hash = str(
            raw_bundle.get("package_hash")
            or raw_bundle.get("bundle_hash")
            or stable_hash(
                raw_bundle, prefix="initialization_bundle_"
            )
        )
        package = {
            "schema_version": "plot-rag-lifecycle/init-package-v1",
            "proposal_id": str(
                raw_bundle.get("proposal_id")
                or raw_bundle.get("bundle_id")
                or stable_hash(
                    ["initialization", package_hash],
                    prefix="init_proposal_",
                )
            ),
            "package_hash": package_hash,
            "target_project_real_path": str(
                raw_bundle.get("target_project_real_path") or ""
            ),
            "source_manifest": list(
                raw_bundle.get("source_manifest") or []
            ),
            "materialization_plan": raw_bundle.get(
                "materialization_plan"
            )
            or {"artifacts": raw_bundle.get("artifact_manifest") or []},
            "entities": raw_bundle.get("entities") or [],
            "events": raw_bundle.get("events")
            or raw_bundle.get("proposed_canon_deltas")
            or raw_bundle.get("canon_deltas")
            or [],
        }
        advantage_sidecar = (
            ContinuityService._validated_initialization_advantage_sidecar(
                raw_bundle,
                package,
            )
        )
        if advantage_sidecar is not None:
            raw_bundle.pop("package_hash", None)
            raw_bundle.pop("bundle_hash", None)
            if not raw_bundle.get("proposal_id") and not raw_bundle.get(
                "bundle_id"
            ):
                package["proposal_id"] = stable_hash(
                    [
                        "initialization",
                        stable_hash(
                            raw_bundle,
                            prefix="initialization_bundle_",
                        ),
                    ],
                    prefix="init_proposal_",
                )
            branch_id = ContinuityService._require_main_initialization_branch(
                raw_bundle.get("branch_id")
            )
            generated_entities, generated_events = (
                ContinuityService._plain_advantage_lifecycle_content(
                    raw_bundle,
                    advantage_sidecar,
                    proposal_id=str(package["proposal_id"]),
                    branch_id=branch_id,
                )
            )
            explicit_entities = raw_bundle.get("entities") or []
            if isinstance(explicit_entities, Mapping):
                explicit_entities = [
                    {"entity_id": entity_id, **dict(entity)}
                    for entity_id, entity in explicit_entities.items()
                    if isinstance(entity, Mapping)
                ]
            merged_entities: dict[str, dict[str, Any]] = {
                str(entity["entity_id"]): copy.deepcopy(dict(entity))
                for entity in generated_entities
                if entity.get("entity_id")
            }
            for entity in explicit_entities:
                if isinstance(entity, Mapping) and entity.get("entity_id"):
                    merged_entities[str(entity["entity_id"])] = copy.deepcopy(
                        dict(entity)
                    )
            explicit_events = (
                raw_bundle.get("events")
                or raw_bundle.get("proposed_canon_deltas")
                or raw_bundle.get("canon_deltas")
                or []
            )
            if isinstance(explicit_events, Mapping):
                explicit_events = list(explicit_events.values())
            merged_events: list[dict[str, Any]] = []
            seen_event_ids: set[str] = set()
            for event in [*generated_events, *explicit_events]:
                if not isinstance(event, Mapping):
                    continue
                normalized = copy.deepcopy(dict(event))
                event_id = str(normalized.get("event_id") or "")
                if event_id and event_id in seen_event_ids:
                    continue
                if event_id:
                    seen_event_ids.add(event_id)
                merged_events.append(normalized)
            package["entities"] = sorted(
                merged_entities.values(),
                key=lambda entity: str(entity["entity_id"]),
            )
            package["events"] = merged_events
            package["advantage_sidecar"] = advantage_sidecar["reference"]
            package["advantage_package_hash"] = advantage_sidecar[
                "package_hash"
            ]
            package["requires_advantage_acceptance"] = True
            hash_payload = copy.deepcopy(package)
            hash_payload.pop("package_hash", None)
            hash_payload.pop("proposal_id", None)
            package["package_hash"] = stable_hash(
                hash_payload,
                prefix="initialization_bundle_",
            )
        return package, raw_bundle

    @staticmethod
    def initialization_bundle_events(
        bundle: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Deterministically convert an InitializationBundle dict to events."""

        package, raw_bundle = ContinuityService._initialization_package(bundle)
        bundle = {
            **raw_bundle,
            "entities": package.get("entities")
            or raw_bundle.get("entities")
            or [],
            "events": package.get("events")
            or raw_bundle.get("events")
            or [],
        }
        events: list[dict[str, Any]] = []
        explicit = (
            bundle.get("events")
            or bundle.get("proposed_canon_deltas")
            or bundle.get("canon_deltas")
            or []
        )
        if isinstance(explicit, Mapping):
            explicit = list(explicit.values())
        for delta in explicit:
            if not isinstance(delta, Mapping):
                continue
            if delta.get("event_type") or delta.get("type"):
                events.append(dict(delta))
            else:
                subject = (
                    delta.get("entity_id")
                    or delta.get("subject_entity_id")
                    or delta.get("subject")
                )
                predicate = delta.get("field") or delta.get("predicate")
                if subject and predicate and "value" in delta:
                    events.append(
                        {
                            "event_type": "fact",
                            "entity_id": str(subject),
                            "field": str(predicate),
                            "value": delta["value"],
                            "scope": delta.get("scope", "current"),
                            "chapter_no": delta.get("chapter_no"),
                            "scene_index": delta.get("scene_index"),
                            "story_time": delta.get("story_time"),
                            "evidence": delta.get("evidence") or {},
                        }
                    )

        entities = bundle.get("entities") or []
        if isinstance(entities, Mapping):
            entities = [
                {"entity_id": entity_id, **dict(value)}
                for entity_id, value in entities.items()
                if isinstance(value, Mapping)
            ]
        existing_entity_events = {
            str(event.get("entity_id"))
            for event in events
            if event.get("event_type") == "entity"
        }
        for item in entities:
            if not isinstance(item, Mapping):
                continue
            name = str(
                item.get("canonical_name") or item.get("name") or ""
            ).strip()
            entity_type = str(item.get("entity_type") or item.get("type") or "unknown")
            entity_id = str(
                item.get("entity_id")
                or stable_hash(
                    [normalize_text(entity_type), normalize_text(name)],
                    prefix="entity_",
                )
            )
            if entity_id in existing_entity_events:
                continue
            events.insert(
                0,
                {
                    "event_type": "entity",
                    "entity_id": entity_id,
                    "entity_type": entity_type,
                    "canonical_name": name or entity_id,
                    "aliases": list(item.get("aliases") or []),
                    "attributes": dict(item.get("attributes") or {}),
                    "scope": "timeless",
                },
            )
            existing_entity_events.add(entity_id)

        typed_sections = {
            "relationships": "relation",
            "relations": "relation",
            "movements": "movement",
            "inventory_events": "inventory",
            "inventory": "inventory",
            "abilities": "ability",
            "beliefs": "belief",
            "open_loops": "open_loop",
            "current_state": "state",
            "world_rules": "world_rule",
        }
        for section, event_type in typed_sections.items():
            values = bundle.get(section) or []
            if isinstance(values, Mapping):
                values = list(values.values())
            for value in values:
                if not isinstance(value, Mapping):
                    continue
                candidate = {"event_type": event_type, **dict(value)}
                if event_type == "world_rule":
                    candidate.setdefault(
                        "field",
                        candidate.get("rule_id")
                        or candidate.get("name")
                        or "world_rule",
                    )
                    candidate.setdefault(
                        "value",
                        {
                            key: item
                            for key, item in value.items()
                            if key not in {"field", "scope"}
                        },
                    )
                    candidate.setdefault("scope", "timeless")
                events.append(candidate)

        return events

    def save_initialization_bundle(
        self,
        bundle: Mapping[str, Any],
        *,
        artifact_id: str | None = None,
        artifact_revision: int | None = None,
        prepared_canon_revision: int | None = None,
        power_spec_binding: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        package, normalized_bundle = self._initialization_package(bundle)
        package_hash = str(package["package_hash"])
        resolved_artifact_id = artifact_id or str(
            package.get("proposal_id")
            or normalized_bundle.get("bundle_id")
            or stable_hash(
                ["initialization", package_hash],
                prefix="artifact_init_",
            )
        )
        proposal_payload = {
            "bundle": normalized_bundle,
            "lifecycle_package": package,
            "package_hash": package_hash,
            "target_project_real_path": str(
                package.get("target_project_real_path")
                or normalized_bundle.get("target_project_real_path")
                or self.store.project_root
            ),
        }
        if power_spec_binding is not None:
            proposal_payload["power_spec_binding"] = dict(
                power_spec_binding
            )
        bundle_meta = normalized_bundle.get("meta")
        bundle_prepared_revision = (
            bundle_meta.get("expected_canon_revision")
            if isinstance(bundle_meta, Mapping)
            else None
        )
        return self.save_proposal(
            events=self.initialization_bundle_events(bundle),
            payload=proposal_payload,
            artifact_id=resolved_artifact_id,
            artifact_kind="initialization_bundle",
            artifact_stage="bootstrap",
            branch_id=str(normalized_bundle.get("branch_id") or "main"),
            chapter_no=None,
            scene_index=None,
            artifact_revision=artifact_revision,
            prepared_canon_revision=(
                prepared_canon_revision
                if prepared_canon_revision is not None
                else bundle_prepared_revision
            ),
            source_role="setting",
            proposal_kind="initialization_bundle",
            idempotency_key=idempotency_key,
        )

    def apply_initialization_bundle(
        self,
        bundle: Mapping[str, Any],
        *,
        approval_id: str,
        expected_canon_revision: int,
        proposal_id: str | None = None,
        prepared_canon_revision: int | None = None,
        power_spec_binding: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        saved = self.save_initialization_bundle(
            bundle,
            prepared_canon_revision=prepared_canon_revision,
            power_spec_binding=power_spec_binding,
            idempotency_key=idempotency_key,
        )
        resolved_proposal = proposal_id or str(saved["proposal_id"])
        if resolved_proposal != saved["proposal_id"]:
            inspected = self.inspect_proposal(resolved_proposal)
            if inspected["payload_hash"] != saved["payload_hash"]:
                raise ContinuityError(
                    "INITIALIZATION_BUNDLE_MISMATCH",
                    "bundle differs from the grant-bound proposal",
                )
        return self.accept_proposal(
            resolved_proposal,
            approval_id=approval_id,
            expected_canon_revision=expected_canon_revision,
        )

    # ------------------------------------------------------------------
    # Query and deterministic replay
    # ------------------------------------------------------------------
    @staticmethod
    def _decode_fact_row(
        row: sqlite3.Row,
        *,
        provisional: bool = False,
    ) -> dict[str, Any]:
        columns = set(row.keys())
        return {
            "fact_key": str(row["fact_key"]),
            "fact_type": str(row["fact_type"]),
            "scope": str(row["scope"]),
            "entity_id": row["entity_id"],
            "subject_entity_id": row["subject_entity_id"],
            "target_entity_id": row["target_entity_id"],
            "field": str(row["field_name"]),
            "value": _json_load(str(row["value_json"]), None),
            "source_event_id": str(row["source_event_id"]),
            "chapter_no": (
                row["chapter_no"]
                if "chapter_no" in columns
                else row["valid_from_chapter"]
            ),
            "scene_index": (
                row["scene_index"]
                if "scene_index" in columns
                else row["valid_from_scene"]
            ),
            "story_time": row["story_time"],
            "provisional": provisional,
        }

    @staticmethod
    def _point_leq(
        chapter: int | None,
        scene: int | None,
        target_chapter: int,
        target_scene: int,
    ) -> bool:
        if chapter is None:
            return True
        return (chapter, scene if scene is not None else -1) <= (
            target_chapter,
            target_scene,
        )

    @staticmethod
    def _point_lt(
        target_chapter: int,
        target_scene: int,
        chapter: int | None,
        scene: int | None,
    ) -> bool:
        if chapter is None:
            return True
        return (target_chapter, target_scene) < (
            chapter,
            scene if scene is not None else -1,
        )

    def query_facts(
        self,
        *,
        entity_id: str | None = None,
        fact_type: str | None = None,
        scope: str | None = None,
        chapter_no: int | None = None,
        scene_index: int | None = None,
        include_timeless: bool = True,
        include_historical: bool = False,
        branch_id: str | None = None,
        include_provisional: bool = False,
    ) -> dict[str, Any]:
        chapter_no = validate_positive_int(
            chapter_no,
            "chapter_no",
            allow_none=True,
            minimum=0,
        )
        scene_index = validate_positive_int(
            scene_index,
            "scene_index",
            allow_none=True,
            minimum=0,
        )
        target_scene = scene_index if scene_index is not None else 2**31 - 1
        facts: list[dict[str, Any]] = []
        with self.store.read_connection() as connection:
            if scope == "planned":
                planned_rows = connection.execute(
                    """
                    SELECT
                        fact_key, fact_type, 'planned' AS scope,
                        entity_id, subject_entity_id, target_entity_id,
                        field_name, value_json, source_event_id,
                        chapter_no, scene_index, story_time
                    FROM planned_facts
                    ORDER BY fact_type, entity_id, field_name, fact_key
                    """
                ).fetchall()
                facts.extend(
                    self._decode_fact_row(row)
                    for row in planned_rows
                    if chapter_no is None
                    or self._point_leq(
                        row["chapter_no"],
                        row["scene_index"],
                        chapter_no,
                        target_scene,
                    )
                )
            elif chapter_no is None:
                rows = connection.execute(
                    """
                    SELECT * FROM canon_facts
                    ORDER BY fact_type, entity_id, field_name, fact_key
                    """
                ).fetchall()
                facts.extend(
                    self._decode_fact_row(row)
                    for row in rows
                    if scope is None or str(row["scope"]) == scope
                )
            else:
                version_rows = connection.execute(
                    """
                    SELECT * FROM fact_versions
                    ORDER BY fact_key, updated_order
                    """
                ).fetchall()
                chosen: dict[str, sqlite3.Row] = {}
                historical: list[sqlite3.Row] = []
                for row in version_rows:
                    row_scope = str(row["scope"])
                    if row_scope == "historical":
                        if include_historical and self._point_leq(
                            row["valid_from_chapter"],
                            row["valid_from_scene"],
                            chapter_no,
                            target_scene,
                        ):
                            historical.append(row)
                        continue
                    if not self._point_leq(
                        row["valid_from_chapter"],
                        row["valid_from_scene"],
                        chapter_no,
                        target_scene,
                    ):
                        continue
                    if not self._point_lt(
                        chapter_no,
                        target_scene,
                        row["valid_to_chapter"],
                        row["valid_to_scene"],
                    ):
                        continue
                    chosen[str(row["fact_key"])] = row
                facts.extend(
                    self._decode_fact_row(row)
                    for row in chosen.values()
                )
                facts.extend(
                    self._decode_fact_row(row) for row in historical
                )

            if include_timeless:
                timeless_rows = connection.execute(
                    """
                    SELECT
                        fact_key, fact_type, 'timeless' AS scope,
                        entity_id, subject_entity_id, target_entity_id,
                        field_name, value_json, source_event_id,
                        NULL AS chapter_no, NULL AS scene_index,
                        NULL AS story_time
                    FROM timeless_facts
                    ORDER BY fact_type, entity_id, field_name, fact_key
                    """
                ).fetchall()
                facts.extend(
                    self._decode_fact_row(row) for row in timeless_rows
                )

            if include_provisional:
                if not branch_id:
                    raise ContinuityError(
                        "BRANCH_REQUIRED",
                        "include_provisional requires an explicit branch_id",
                    )
                branch_rows = connection.execute(
                    """
                    SELECT
                        fact_key, fact_type, scope, entity_id,
                        subject_entity_id, target_entity_id, field_name,
                        value_json, source_event_id, chapter_no, scene_index,
                        story_time
                    FROM branch_facts
                    WHERE branch_id=?
                    ORDER BY fact_type, entity_id, field_name, fact_key
                    """,
                    (branch_id,),
                ).fetchall()
                facts.extend(
                    self._decode_fact_row(row, provisional=True)
                    for row in branch_rows
                )

            filtered = [
                fact
                for fact in facts
                if (entity_id is None or fact["entity_id"] == entity_id)
                and (fact_type is None or fact["fact_type"] == fact_type)
                and (
                    scope is None
                    or fact["scope"] == scope
                    or (
                        include_timeless
                        and fact["scope"] == "timeless"
                        and scope in {None, "current", "timeless"}
                    )
                )
            ]
            revisions = {
                "head": self.store.get_meta_int(
                    connection, "head_canon_revision"
                ),
                "active": self.store.get_meta_int(
                    connection, "active_canon_revision"
                ),
            }
        return {
            "facts": sorted(
                filtered,
                key=lambda fact: (
                    bool(fact["provisional"]),
                    fact["scope"],
                    fact["fact_type"],
                    str(fact["entity_id"]),
                    fact["field"],
                    fact["fact_key"],
                ),
            ),
            "chapter_no": chapter_no,
            "scene_index": scene_index,
            "branch_id": branch_id,
            "revisions": revisions,
        }

    def query_relations(
        self,
        entity_id: str,
        *,
        chapter_no: int | None = None,
        scene_index: int | None = None,
        scope: str | None = None,
        branch_id: str | None = None,
        include_provisional: bool = False,
    ) -> dict[str, Any]:
        result = self.query_facts(
            fact_type="relation",
            scope=scope,
            chapter_no=chapter_no,
            scene_index=scene_index,
            branch_id=branch_id,
            include_provisional=include_provisional,
        )
        relation_facts = [
            fact
            for fact in result["facts"]
            if entity_id
            in {fact["subject_entity_id"], fact["target_entity_id"]}
        ]
        result["facts"] = relation_facts
        result["relations"] = relation_facts
        return result

    @staticmethod
    def _power_projection_meta(
        connection: sqlite3.Connection,
        store: ContinuityStore,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT projection_hash
            FROM projection_runs
            WHERE projection_name='continuity'
              AND run_status='completed'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
        return {
            "revisions": {
                "head": store.get_meta_int(
                    connection, "head_canon_revision"
                ),
                "active": store.get_meta_int(
                    connection, "active_canon_revision"
                ),
            },
            "projection_hash": (
                str(row["projection_hash"]) if row is not None else None
            ),
        }

    def list_power_systems(
        self,
        *,
        include_deprecated: bool = False,
    ) -> dict[str, Any]:
        with self.store.read_connection() as connection:
            systems = []
            for row in connection.execute(
                """
                SELECT *
                FROM power_system_specs
                WHERE spec_status!='deprecated' OR ?=1
                ORDER BY spec_entity_id
                """,
                (int(include_deprecated),),
            ):
                system_id = str(row["spec_entity_id"])
                definition = dict(
                    _json_load(str(row["definition_json"]), {})
                )
                tracks = [
                    {
                        "track_entity_id": str(track["track_entity_id"]),
                        "track_kind": str(track["track_kind"]),
                        "status": str(track["track_status"]),
                        "definition": _json_load(
                            str(track["definition_json"]), {}
                        ),
                        "source_event_id": str(track["source_event_id"]),
                    }
                    for track in connection.execute(
                        """
                        SELECT *
                        FROM progression_tracks
                        WHERE system_entity_id=?
                          AND (track_status!='deprecated' OR ?=1)
                        ORDER BY track_entity_id
                        """,
                        (system_id, int(include_deprecated)),
                    )
                ]
                systems.append(
                    {
                        "system_entity_id": system_id,
                        "status": str(row["spec_status"]),
                        "definition": definition,
                        "tracks": tracks,
                        "source_event_id": str(row["source_event_id"]),
                    }
                )
            return {
                "systems": systems,
                **self._power_projection_meta(connection, self.store),
            }

    def query_power_state(
        self,
        entity_id: str | None = None,
        *,
        ability_entity_id: str | None = None,
        track_entity_id: str | None = None,
        resource_entity_id: str | None = None,
        include_inactive: bool = False,
        include_history: bool = True,
    ) -> dict[str, Any]:
        """Return typed power projections by actor or reverse ability lookup."""

        with self.store.read_connection() as connection:
            actor_clause = "" if entity_id is None else "AND actor_entity_id=?"
            owner_clause = "" if entity_id is None else "AND owner_entity_id=?"
            actor_params: list[Any] = [] if entity_id is None else [entity_id]
            owner_params: list[Any] = [] if entity_id is None else [entity_id]

            progression = [
                {
                    "progression_key": str(row["progression_key"]),
                    "actor_entity_id": str(row["actor_entity_id"]),
                    "track_entity_id": str(row["track_entity_id"]),
                    "rank_entity_id": row["rank_entity_id"],
                    "state": _json_load(str(row["state_json"]), {}),
                    "story_coordinate": _json_load(
                        str(row["story_coordinate_json"]), {}
                    ),
                    "source_event_id": str(row["source_event_id"]),
                }
                for row in connection.execute(
                    f"""
                    SELECT * FROM actor_progression_state
                    WHERE (? IS NULL OR track_entity_id=?)
                    {actor_clause}
                    ORDER BY actor_entity_id, track_entity_id
                    """,
                    [
                        track_entity_id,
                        track_entity_id,
                        *actor_params,
                    ],
                )
            ]
            resources = [
                {
                    "resource_key": str(row["resource_key"]),
                    "actor_entity_id": str(row["actor_entity_id"]),
                    "resource_entity_id": str(row["resource_entity_id"]),
                    "balance": float(row["balance"]),
                    "reserved": float(row["reserved"]),
                    "available": float(row["balance"])
                    - float(row["reserved"]),
                    "state": _json_load(str(row["state_json"]), {}),
                    "story_coordinate": _json_load(
                        str(row["story_coordinate_json"]), {}
                    ),
                    "source_event_id": str(row["source_event_id"]),
                }
                for row in connection.execute(
                    f"""
                    SELECT * FROM actor_resource_state
                    WHERE (? IS NULL OR resource_entity_id=?)
                    {actor_clause}
                    ORDER BY actor_entity_id, resource_entity_id
                    """,
                    [
                        resource_entity_id,
                        resource_entity_id,
                        *actor_params,
                    ],
                )
            ]
            ability_rows = connection.execute(
                f"""
                SELECT a.*, r.available, r.runtime_json,
                       r.story_coordinate_json AS runtime_coordinate_json,
                       r.source_event_id AS runtime_source_event_id
                FROM actor_ability_state AS a
                LEFT JOIN ability_runtime_state AS r
                  ON r.ability_key=a.ability_key
                WHERE (? IS NULL OR a.ability_entity_id=?)
                  AND (a.acquired=1 OR ?=1)
                  {owner_clause.replace('owner_entity_id', 'a.owner_entity_id')}
                ORDER BY a.owner_entity_id, a.ability_entity_id
                """,
                [
                    ability_entity_id,
                    ability_entity_id,
                    int(include_inactive),
                    *owner_params,
                ],
            ).fetchall()
            abilities = [
                {
                    "ability_key": str(row["ability_key"]),
                    "owner_entity_id": str(row["owner_entity_id"]),
                    "ability_entity_id": str(row["ability_entity_id"]),
                    "acquired": bool(row["acquired"]),
                    "available": (
                        bool(row["available"])
                        if row["available"] is not None
                        else bool(row["acquired"])
                    ),
                    "ownership": _json_load(
                        str(row["ownership_json"]), {}
                    ),
                    "runtime": _json_load(
                        str(row["runtime_json"] or "{}"), {}
                    ),
                    "source_event_id": str(row["source_event_id"]),
                    "runtime_source_event_id": (
                        str(row["runtime_source_event_id"])
                        if row["runtime_source_event_id"] is not None
                        else None
                    ),
                }
                for row in ability_rows
            ]
            statuses = [
                {
                    "status_key": str(row["status_key"]),
                    "actor_entity_id": str(row["actor_entity_id"]),
                    "status_entity_id": str(row["status_entity_id"]),
                    "active": bool(row["active"]),
                    "stacks": int(row["stacks"]),
                    "state": _json_load(str(row["state_json"]), {}),
                    "source_event_id": str(row["source_event_id"]),
                }
                for row in connection.execute(
                    f"""
                    SELECT * FROM actor_status_state
                    WHERE (active=1 OR ?=1)
                    {actor_clause}
                    ORDER BY actor_entity_id, status_entity_id
                    """,
                    [int(include_inactive), *actor_params],
                )
            ]
            bindings = [
                {
                    "binding_key": str(row["binding_key"]),
                    "binding_id": str(row["binding_id"]),
                    "actor_entity_id": str(row["actor_entity_id"]),
                    "source_entity_id": str(row["source_entity_id"]),
                    "binding_kind": str(row["binding_kind"]),
                    "active": bool(row["active"]),
                    "ability_entity_ids": _json_load(
                        str(row["ability_entity_ids_json"]), []
                    ),
                    "state": _json_load(str(row["state_json"]), {}),
                    "source_event_id": str(row["source_event_id"]),
                }
                for row in connection.execute(
                    f"""
                    SELECT * FROM power_bindings
                    WHERE (active=1 OR ?=1)
                    {actor_clause}
                    ORDER BY actor_entity_id, binding_id
                    """,
                    [int(include_inactive), *actor_params],
                )
            ]
            qualifications = [
                {
                    "qualification_key": str(row["qualification_key"]),
                    "actor_entity_id": str(row["actor_entity_id"]),
                    "qualification_entity_id": str(
                        row["qualification_entity_id"]
                    ),
                    "active": bool(row["active"]),
                    "quantity": float(row["quantity"]),
                    "state": _json_load(str(row["state_json"]), {}),
                    "source_event_id": str(row["source_event_id"]),
                }
                for row in connection.execute(
                    f"""
                    SELECT * FROM qualification_state
                    WHERE (active=1 OR ?=1)
                    {actor_clause}
                    ORDER BY actor_entity_id, qualification_entity_id
                    """,
                    [int(include_inactive), *actor_params],
                )
            ]
            observations = [
                {
                    "observation_key": str(row["observation_key"]),
                    "observer_entity_id": str(row["observer_entity_id"]),
                    "subject_entity_id": row["subject_entity_id"],
                    "ability_entity_id": row["ability_entity_id"],
                    "action": str(row["observation_action"]),
                    "knowledge_plane": str(row["knowledge_plane"]),
                    "observation": _json_load(
                        str(row["observation_json"]), {}
                    ),
                    "source_event_id": str(row["source_event_id"]),
                }
                for row in connection.execute(
                    """
                    SELECT * FROM power_observations
                    WHERE (? IS NULL OR observer_entity_id=?
                           OR subject_entity_id=?)
                      AND (? IS NULL OR ability_entity_id=?)
                    ORDER BY updated_order, observation_key
                    """,
                    (
                        entity_id,
                        entity_id,
                        entity_id,
                        ability_entity_id,
                        ability_entity_id,
                    ),
                )
            ]
            history = []
            if include_history:
                history = [
                    {
                        "source_event_id": str(row["source_event_id"]),
                        "owner_entity_id": str(row["owner_entity_id"]),
                        "ability_entity_id": str(
                            row["ability_entity_id"]
                        ),
                        "action": str(row["action"]),
                        "runtime": _json_load(
                            str(row["runtime_json"]), {}
                        ),
                        "story_coordinate": _json_load(
                            str(row["story_coordinate_json"]), {}
                        ),
                        "chapter_no": row["chapter_no"],
                        "scene_index": row["scene_index"],
                    }
                    for row in connection.execute(
                        """
                        SELECT * FROM ability_use_history
                        WHERE (? IS NULL OR owner_entity_id=?)
                          AND (? IS NULL OR ability_entity_id=?)
                        ORDER BY updated_order, source_event_id
                        """,
                        (
                            entity_id,
                            entity_id,
                            ability_entity_id,
                            ability_entity_id,
                        ),
                    )
                ]
            if entity_id is None and ability_entity_id is not None:
                holder_ids = {
                    item["owner_entity_id"] for item in abilities
                }
                progression = [
                    item
                    for item in progression
                    if item["actor_entity_id"] in holder_ids
                ]
                resources = [
                    item
                    for item in resources
                    if item["actor_entity_id"] in holder_ids
                ]
                statuses = [
                    item
                    for item in statuses
                    if item["actor_entity_id"] in holder_ids
                ]
                bindings = [
                    item
                    for item in bindings
                    if item["actor_entity_id"] in holder_ids
                ]
                qualifications = [
                    item
                    for item in qualifications
                    if item["actor_entity_id"] in holder_ids
                ]
            return {
                "entity_id": entity_id,
                "ability_entity_id": ability_entity_id,
                "progression": progression,
                "resources": resources,
                "abilities": abilities,
                "statuses": statuses,
                "bindings": bindings,
                "qualifications": qualifications,
                "observations": observations,
                "ability_history": history,
                **self._power_projection_meta(connection, self.store),
            }

    def query_progression_path(
        self,
        entity_id: str,
        *,
        track_entity_id: str | None = None,
        target_rank_entity_id: str | None = None,
    ) -> dict[str, Any]:
        with self.store.read_connection() as connection:
            current_rows = connection.execute(
                """
                SELECT * FROM actor_progression_state
                WHERE actor_entity_id=?
                  AND (? IS NULL OR track_entity_id=?)
                ORDER BY track_entity_id
                """,
                (entity_id, track_entity_id, track_entity_id),
            ).fetchall()
            tracks = []
            for current in current_rows:
                track_id = str(current["track_entity_id"])
                current_rank = current["rank_entity_id"]
                edges = [
                    {
                        "edge_entity_id": str(row["edge_entity_id"]),
                        "from_rank_entity_ids": _json_load(
                            str(row["from_rank_ids_json"]), []
                        ),
                        "to_rank_entity_id": row["to_rank_entity_id"],
                        "definition": _json_load(
                            str(row["definition_json"]), {}
                        ),
                        "source_event_id": str(row["source_event_id"]),
                    }
                    for row in connection.execute(
                        """
                        SELECT * FROM rank_edges
                        WHERE track_entity_id=?
                          AND edge_status!='deprecated'
                        ORDER BY edge_entity_id
                        """,
                        (track_id,),
                    )
                ]
                path: list[dict[str, Any]] | None = None
                if target_rank_entity_id is not None:
                    queue: list[
                        tuple[str | None, list[dict[str, Any]]]
                    ] = [(current_rank, [])]
                    visited = {str(current_rank)}
                    while queue:
                        rank, candidate_path = queue.pop(0)
                        if str(rank) == str(target_rank_entity_id):
                            path = candidate_path
                            break
                        for edge in edges:
                            if str(rank) not in {
                                str(item)
                                for item in edge[
                                    "from_rank_entity_ids"
                                ]
                            }:
                                continue
                            target = str(edge["to_rank_entity_id"])
                            if target in visited:
                                continue
                            visited.add(target)
                            queue.append(
                                (target, [*candidate_path, edge])
                            )
                tracks.append(
                    {
                        "track_entity_id": track_id,
                        "current_rank_entity_id": current_rank,
                        "target_rank_entity_id": target_rank_entity_id,
                        "status": (
                            "not_requested"
                            if target_rank_entity_id is None
                            else "reachable"
                            if path is not None
                            else "unreachable"
                        ),
                        "path": path,
                        "edges": edges,
                        "source_event_id": str(
                            current["source_event_id"]
                        ),
                    }
                )
            return {
                "entity_id": entity_id,
                "tracks": tracks,
                **self._power_projection_meta(connection, self.store),
            }

    def explain_power_action(
        self,
        entity_id: str,
        *,
        ability_id: str,
        action: str = "use",
        story_coordinate: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        story_coordinate = normalize_story_coordinate(
            story_coordinate,
            "story_coordinate",
        )
        state = self.query_power_state(
            entity_id,
            ability_entity_id=ability_id,
            include_inactive=True,
            include_history=False,
        )
        ability = next(
            (
                item
                for item in state["abilities"]
                if item["ability_entity_id"] == ability_id
            ),
            None,
        )
        reasons: list[dict[str, Any]] = []
        if ability is None or not ability["acquired"]:
            reasons.append(
                {
                    "code": "POWER_ABILITY_NOT_ACQUIRED",
                    "message": "ability ownership is inactive",
                }
            )
        runtime = dict((ability or {}).get("runtime") or {})
        cooldown_until = runtime.get("cooldown_until")
        if cooldown_until:
            if (
                not story_coordinate
                or story_coordinate.get("calendar_id")
                != cooldown_until.get("calendar_id")
                or story_coordinate.get("ordinal") is None
                or cooldown_until.get("ordinal") is None
            ):
                reasons.append(
                    {
                        "code": "POWER_STORY_COORDINATE_UNKNOWN",
                        "message": "cooldown cannot be compared",
                        "cooldown_until": cooldown_until,
                    }
                )
            elif int(story_coordinate["ordinal"]) < int(
                cooldown_until["ordinal"]
            ):
                reasons.append(
                    {
                        "code": "POWER_COOLDOWN_ACTIVE",
                        "message": "ability cooldown is still active",
                        "cooldown_until": cooldown_until,
                    }
                )
        with self.store.read_connection() as connection:
            definition_row = connection.execute(
                """
                SELECT definition_json, source_event_id
                FROM ability_definitions
                WHERE ability_entity_id=?
                  AND definition_status!='deprecated'
                """,
                (ability_id,),
            ).fetchone()
            definition = (
                dict(
                    _json_load(str(definition_row["definition_json"]), {})
                )
                if definition_row is not None
                else {}
            )
            resource_by_id = {
                item["resource_entity_id"]: item for item in state["resources"]
            }
            for cost in definition.get("resource_costs") or []:
                if not isinstance(cost, Mapping):
                    continue
                resource_id = str(
                    cost.get("resource_entity_id") or ""
                )
                required = float(cost.get("amount", 0))
                available = float(
                    resource_by_id.get(resource_id, {}).get(
                        "available", 0
                    )
                )
                if available + 1e-12 < required:
                    reasons.append(
                        {
                            "code": "POWER_RESOURCE_INSUFFICIENT",
                            "resource_entity_id": resource_id,
                            "required": required,
                            "available": available,
                        }
                    )
            binding_id = definition.get("source_binding_id")
            if binding_id and not any(
                item["binding_id"] == binding_id and item["active"]
                for item in state["bindings"]
            ):
                reasons.append(
                    {
                        "code": "POWER_PREREQUISITE_UNMET",
                        "message": "required source binding is inactive",
                        "binding_id": binding_id,
                    }
                )
            prerequisites = dict(
                definition.get("prerequisites") or {}
            )
            owned_ability_ids = {
                item["ability_entity_id"]
                for item in state["abilities"]
                if item["acquired"]
            }
            for required_ability in (
                prerequisites.get("ability_entity_ids") or []
            ):
                if required_ability not in owned_ability_ids:
                    reasons.append(
                        {
                            "code": "POWER_PREREQUISITE_UNMET",
                            "kind": "ability",
                            "ability_entity_id": required_ability,
                        }
                    )
            active_qualifications = {
                item["qualification_entity_id"]: item
                for item in state["qualifications"]
                if item["active"] and item["quantity"] > 0
            }
            for required_qualification in (
                prerequisites.get("qualification_entity_ids") or []
            ):
                if required_qualification not in active_qualifications:
                    reasons.append(
                        {
                            "code": "POWER_PREREQUISITE_UNMET",
                            "kind": "qualification",
                            "qualification_entity_id": (
                                required_qualification
                            ),
                        }
                    )
            active_bindings = {
                item["binding_id"]
                for item in state["bindings"]
                if item["active"]
            }
            for required_binding in prerequisites.get("binding_ids") or []:
                if required_binding not in active_bindings:
                    reasons.append(
                        {
                            "code": "POWER_PREREQUISITE_UNMET",
                            "kind": "binding",
                            "binding_id": required_binding,
                        }
                    )
            progression_by_track = {
                item["track_entity_id"]: item["rank_entity_id"]
                for item in state["progression"]
            }
            for requirement in prerequisites.get("progression") or []:
                if not isinstance(requirement, Mapping):
                    continue
                required_track = str(
                    requirement.get("track_entity_id") or ""
                )
                required_rank = str(
                    requirement.get("rank_entity_id") or ""
                )
                actual_rank = progression_by_track.get(required_track)
                if str(actual_rank) != required_rank:
                    reasons.append(
                        {
                            "code": "POWER_PREREQUISITE_UNMET",
                            "kind": "progression",
                            "track_entity_id": required_track,
                            "required_rank_entity_id": required_rank,
                            "actual_rank_entity_id": actual_rank,
                        }
                    )
            active_status_ids = {
                item["status_entity_id"]
                for item in state["statuses"]
                if item["active"]
            }
            for required_status in (
                prerequisites.get("required_status_entity_ids") or []
            ):
                if required_status not in active_status_ids:
                    reasons.append(
                        {
                            "code": "POWER_PREREQUISITE_UNMET",
                            "kind": "status",
                            "status_entity_id": required_status,
                        }
                    )
            for forbidden_status in (
                prerequisites.get("forbidden_status_entity_ids") or []
            ):
                if forbidden_status in active_status_ids:
                    reasons.append(
                        {
                            "code": "POWER_PREREQUISITE_UNMET",
                            "kind": "forbidden_status",
                            "status_entity_id": forbidden_status,
                        }
                    )
            allowed_locations = list(
                prerequisites.get("location_entity_ids") or []
            )
            if allowed_locations:
                location_row = connection.execute(
                    """
                    SELECT location_entity_id
                    FROM location_state
                    WHERE actor_entity_id=?
                    """,
                    (entity_id,),
                ).fetchone()
                actual_location = (
                    location_row["location_entity_id"]
                    if location_row is not None
                    else None
                )
                if actual_location not in allowed_locations:
                    reasons.append(
                        {
                            "code": "POWER_CONTEXT_CONDITION_UNMET",
                            "actual_location_entity_id": actual_location,
                            "allowed_location_entity_ids": (
                                allowed_locations
                            ),
                        }
                    )
            minimum_coordinate = prerequisites.get(
                "minimum_story_coordinate"
            )
            if minimum_coordinate:
                minimum_ordinal = minimum_coordinate.get("ordinal")
                story_ordinal = (
                    story_coordinate.get("ordinal")
                    if story_coordinate
                    else None
                )
                if (
                    not story_coordinate
                    or story_coordinate.get("calendar_id")
                    != minimum_coordinate.get("calendar_id")
                    or type(story_ordinal) is not int
                    or type(minimum_ordinal) is not int
                ):
                    reasons.append(
                        {
                            "code": "POWER_STORY_COORDINATE_UNKNOWN",
                            "minimum_story_coordinate": minimum_coordinate,
                        }
                    )
                elif story_ordinal < minimum_ordinal:
                    reasons.append(
                        {
                            "code": "POWER_CONTEXT_CONDITION_UNMET",
                            "minimum_story_coordinate": minimum_coordinate,
                        }
                    )
            return {
                "entity_id": entity_id,
                "ability_id": ability_id,
                "action": action,
                "story_coordinate": dict(story_coordinate or {}),
                "executable": not reasons,
                "status": "executable" if not reasons else "blocked",
                "reasons": reasons,
                "ability": ability,
                "definition": definition,
                "definition_source_event_id": (
                    str(definition_row["source_event_id"])
                    if definition_row is not None
                    else None
                ),
                **self._power_projection_meta(connection, self.store),
            }

    def compare_power_conditions(
        self,
        left_id: str,
        right_id: str,
        *,
        conditions: Mapping[str, Any] | None = None,
        knowledge_plane: str = "objective",
    ) -> dict[str, Any]:
        """Return evidence-bearing condition vectors without inventing a winner."""

        left = self.query_power_state(left_id, include_history=False)
        right = self.query_power_state(right_id, include_history=False)
        dimensions = (
            "progression",
            "resources",
            "abilities",
            "statuses",
            "bindings",
            "qualifications",
        )
        left_vector = {key: left[key] for key in dimensions}
        right_vector = {key: right[key] for key in dimensions}
        evidence: list[dict[str, Any]] = []
        for side, vector in (("left", left_vector), ("right", right_vector)):
            for dimension in dimensions:
                for item in vector[dimension]:
                    source_event_id = item.get("source_event_id")
                    if not source_event_id:
                        continue
                    evidence.append(
                        {
                            "side": side,
                            "dimension": dimension,
                            "source_event_id": str(source_event_id),
                        }
                    )
        evidence.sort(
            key=lambda item: (
                item["side"],
                item["dimension"],
                item["source_event_id"],
            )
        )
        populated = sum(
            bool(vector[dimension])
            for vector in (left_vector, right_vector)
            for dimension in dimensions
        )
        confidence = round(populated / (len(dimensions) * 2), 6)
        normalized_conditions = dict(conditions or {})
        claim_basis = {
            "left_entity_id": left_id,
            "right_entity_id": right_id,
            "knowledge_plane": knowledge_plane,
            "conditions": normalized_conditions,
            "active_canon_revision": left["revisions"]["active"],
            "projection_hash": left["projection_hash"],
        }
        return {
            "claim_id": stable_hash(
                claim_basis,
                prefix="comparison_claim_",
            ),
            "claim_type": "comparison_claim",
            "derivation": "query_time",
            "persisted": False,
            "left_entity_id": left_id,
            "right_entity_id": right_id,
            "knowledge_plane": knowledge_plane,
            "conditions": normalized_conditions,
            "status": "conditional_only",
            "winner": None,
            "left": left_vector,
            "right": right_vector,
            "evidence": evidence,
            "source_event_ids": sorted(
                {item["source_event_id"] for item in evidence}
            ),
            "confidence": confidence,
            "confidence_basis": {
                "kind": "accepted_dimension_coverage",
                "populated_dimensions": populated,
                "possible_dimensions": len(dimensions) * 2,
            },
            "revisions": left["revisions"],
            "projection_hash": left["projection_hash"],
        }

    # ------------------------------------------------------------------
    # Schema-v6 item projection queries
    # ------------------------------------------------------------------
    @staticmethod
    def _decode_item_projection_row(
        row: sqlite3.Row | Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for field, fallback in (
            ("definition_json", {}),
            ("instance_json", {}),
            ("batch_json", {}),
            ("binding_json", {}),
            ("state_json", {}),
            ("story_coordinate_json", {}),
            ("cooldown_until_json", None),
            ("delta_json", {}),
            ("before_json", {}),
            ("after_json", {}),
            ("observation_json", {}),
            ("evidence_json", {}),
        ):
            if field in result:
                result[field.removesuffix("_json")] = _json_load(
                    result.pop(field),
                    fallback,
                )
        for field in (
            "enabled",
            "sealed",
            "damaged",
            "destroyed",
            "active",
        ):
            if field in result and result[field] is not None:
                result[field] = bool(result[field])
        return result

    @staticmethod
    def _item_projection_meta(
        connection: sqlite3.Connection,
        store: ContinuityStore,
    ) -> dict[str, Any]:
        head = store.get_meta_int(connection, "head_canon_revision")
        active = store.get_meta_int(connection, "active_canon_revision")
        metadata = read_item_projection_metadata(connection)
        projection = connection.execute(
            """
            SELECT projection_hash
            FROM projection_runs
            WHERE projection_name='continuity'
              AND source_head_revision=?
              AND source_active_revision=?
              AND run_status='completed'
            ORDER BY completed_at DESC, run_id DESC
            LIMIT 1
            """,
            (head, active),
        ).fetchone()
        if projection is None:
            projection = connection.execute(
                """
                SELECT projection_hash
                FROM canon_commits
                WHERE head_revision_after=?
                ORDER BY commit_id DESC
                LIMIT 1
                """,
                (head,),
            ).fetchone()
        legacy_projection_hash = (
            str(projection["projection_hash"])
            if projection is not None
            else stable_hash(
                ReplayEngine._projection_payload(connection),
                prefix="projection_",
            )
        )
        return {
            "revisions": {"head": head, "active": active},
            "projection_hash": legacy_projection_hash,
            "item_projection_hash": str(
                metadata.get(ITEM_PROJECTION_META_HASH) or ""
            ),
            "item_projection_schema_version": int(
                metadata.get(ITEM_PROJECTION_META_VERSION) or 0
            ),
            "item_projection_source_revisions": {
                "head": int(
                    metadata.get(ITEM_PROJECTION_META_HEAD_REVISION) or 0
                ),
                "active": int(
                    metadata.get(ITEM_PROJECTION_META_ACTIVE_REVISION) or 0
                ),
            },
        }

    @classmethod
    def _item_subject_query_row(
        cls,
        connection: sqlite3.Connection,
        subject_type: str,
        subject_id: str,
    ) -> dict[str, Any]:
        if subject_type == "item_instance":
            instance_row = connection.execute(
                """
                SELECT * FROM item_instances
                WHERE item_instance_id=?
                """,
                (subject_id,),
            ).fetchone()
            if instance_row is None:
                raise ContinuityError(
                    "ITEM_INSTANCE_NOT_FOUND",
                    f"unknown item instance: {subject_id}",
                )
            instance = cls._decode_item_projection_row(instance_row)
            definition_id = str(instance_row["item_definition_id"])
            runtime = cls._decode_item_projection_row(
                connection.execute(
                    """
                    SELECT * FROM item_runtime_state
                    WHERE item_instance_id=?
                    """,
                    (subject_id,),
                ).fetchone()
            )
            subject = {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "instance": instance,
                "stack": None,
                "runtime": runtime,
            }
        else:
            stack_row = connection.execute(
                "SELECT * FROM item_stacks WHERE stack_id=?",
                (subject_id,),
            ).fetchone()
            if stack_row is None:
                raise ContinuityError(
                    "ITEM_STACK_NOT_FOUND",
                    f"unknown item stack: {subject_id}",
                )
            definition_id = str(stack_row["item_definition_id"])
            subject = {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "instance": None,
                "stack": cls._decode_item_projection_row(stack_row),
                "runtime": None,
            }
        definition = cls._decode_item_projection_row(
            connection.execute(
                """
                SELECT * FROM item_definitions
                WHERE item_definition_id=?
                """,
                (definition_id,),
            ).fetchone()
        )
        custody = cls._decode_item_projection_row(
            connection.execute(
                """
                SELECT * FROM item_custody_state
                WHERE subject_type=? AND subject_id=?
                """,
                (subject_type, subject_id),
            ).fetchone()
        )
        return {
            **subject,
            "definition": definition,
            "custody": custody,
        }

    def query_item_definition(
        self,
        item_definition_id: str,
    ) -> dict[str, Any]:
        definition_id = str(item_definition_id or "").strip()
        with self.store.read_connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM item_definitions
                WHERE item_definition_id=?
                """,
                (definition_id,),
            ).fetchone()
            if row is None:
                raise ContinuityError(
                    "ITEM_DEFINITION_NOT_FOUND",
                    f"unknown item definition: {definition_id}",
                )
            functions = [
                self._decode_item_projection_row(item)
                for item in connection.execute(
                    """
                    SELECT * FROM item_function_definitions
                    WHERE item_definition_id=?
                    ORDER BY function_id
                    """,
                    (definition_id,),
                )
            ]
            bindings = [
                self._decode_item_projection_row(item)
                for item in connection.execute(
                    """
                    SELECT * FROM item_function_bindings
                    WHERE item_definition_id=?
                    ORDER BY binding_id
                    """,
                    (definition_id,),
                )
            ]
            instances = [
                self._decode_item_projection_row(item)
                for item in connection.execute(
                    """
                    SELECT * FROM item_instances
                    WHERE item_definition_id=?
                    ORDER BY item_instance_id
                    """,
                    (definition_id,),
                )
            ]
            stacks = [
                self._decode_item_projection_row(item)
                for item in connection.execute(
                    """
                    SELECT * FROM item_stacks
                    WHERE item_definition_id=?
                    ORDER BY stack_id
                    """,
                    (definition_id,),
                )
            ]
            return {
                "definition": self._decode_item_projection_row(row),
                "functions": functions,
                "bindings": bindings,
                "instances": instances,
                "stacks": stacks,
                **self._item_projection_meta(connection, self.store),
            }

    def query_item_instance(
        self,
        item_instance_id: str,
    ) -> dict[str, Any]:
        instance_id = str(item_instance_id or "").strip()
        with self.store.read_connection() as connection:
            subject = self._item_subject_query_row(
                connection, "item_instance", instance_id
            )
            function_runtime = [
                self._decode_item_projection_row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM item_function_runtime_state
                    WHERE item_instance_id=?
                    ORDER BY function_id
                    """,
                    (instance_id,),
                )
            ]
            bindings = [
                self._decode_item_projection_row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM item_function_bindings
                    WHERE item_instance_id=?
                       OR (
                            item_definition_id=?
                            AND binding_status='active'
                       )
                    ORDER BY binding_id
                    """,
                    (
                        instance_id,
                        subject["instance"]["item_definition_id"],
                    ),
                )
            ]
            return {
                **subject,
                "function_runtime": function_runtime,
                "bindings": bindings,
                **self._item_projection_meta(connection, self.store),
            }

    def query_item_function(
        self,
        function_id: str,
        *,
        item_instance_id: str | None = None,
        stack_id: str | None = None,
    ) -> dict[str, Any]:
        if item_instance_id and stack_id:
            raise ContinuityError(
                "ITEM_SUBJECT_AMBIGUOUS",
                "query_item_function accepts one instance or stack filter",
            )
        normalized_function = str(function_id or "").strip()
        with self.store.read_connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM item_function_definitions
                WHERE function_id=?
                """,
                (normalized_function,),
            ).fetchone()
            if row is None:
                raise ContinuityError(
                    "ITEM_FUNCTION_NOT_FOUND",
                    f"unknown item function: {normalized_function}",
                )
            parameters: list[Any] = [normalized_function]
            clauses = ["function_id=?"]
            if item_instance_id:
                clauses.append(
                    "(item_instance_id=? OR item_definition_id=(SELECT "
                    "item_definition_id FROM item_instances "
                    "WHERE item_instance_id=?))"
                )
                parameters.extend([item_instance_id, item_instance_id])
            elif stack_id:
                clauses.append(
                    "(stack_id=? OR item_definition_id=(SELECT "
                    "item_definition_id FROM item_stacks WHERE stack_id=?))"
                )
                parameters.extend([stack_id, stack_id])
            bindings = [
                self._decode_item_projection_row(binding)
                for binding in connection.execute(
                    f"""
                    SELECT * FROM item_function_bindings
                    WHERE {" AND ".join(clauses)}
                    ORDER BY binding_id
                    """,
                    tuple(parameters),
                )
            ]
            runtime: list[dict[str, Any]] = []
            if item_instance_id:
                runtime_row = self._decode_item_projection_row(
                    connection.execute(
                        """
                        SELECT * FROM item_function_runtime_state
                        WHERE item_instance_id=? AND function_id=?
                        """,
                        (item_instance_id, normalized_function),
                    ).fetchone()
                )
                if runtime_row is not None:
                    runtime.append(runtime_row)
            return {
                "function": self._decode_item_projection_row(row),
                "bindings": bindings,
                "runtime": runtime,
                **self._item_projection_meta(connection, self.store),
            }

    def query_item_runtime(
        self,
        item_instance_id: str,
    ) -> dict[str, Any]:
        instance_id = str(item_instance_id or "").strip()
        with self.store.read_connection() as connection:
            instance = connection.execute(
                """
                SELECT * FROM item_instances
                WHERE item_instance_id=?
                """,
                (instance_id,),
            ).fetchone()
            if instance is None:
                raise ContinuityError(
                    "ITEM_INSTANCE_NOT_FOUND",
                    f"unknown item instance: {instance_id}",
                )
            runtime = self._decode_item_projection_row(
                connection.execute(
                    """
                    SELECT * FROM item_runtime_state
                    WHERE item_instance_id=?
                    """,
                    (instance_id,),
                ).fetchone()
            )
            function_runtime = [
                self._decode_item_projection_row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM item_function_runtime_state
                    WHERE item_instance_id=?
                    ORDER BY function_id
                    """,
                    (instance_id,),
                )
            ]
            return {
                "item_instance_id": instance_id,
                "runtime": runtime,
                "function_runtime": function_runtime,
                **self._item_projection_meta(connection, self.store),
            }

    def query_item_custody(
        self,
        *,
        subject_type: str,
        subject_id: str,
    ) -> dict[str, Any]:
        normalized_type = str(subject_type or "").strip()
        if normalized_type not in {"item_instance", "item_stack"}:
            raise ContinuityError(
                "ITEM_SUBJECT_REQUIRED",
                "subject_type must be item_instance or item_stack",
            )
        normalized_id = str(subject_id or "").strip()
        with self.store.read_connection() as connection:
            subject = self._item_subject_query_row(
                connection, normalized_type, normalized_id
            )
            return {
                **subject,
                **self._item_projection_meta(connection, self.store),
            }

    def query_actor_inventory(
        self,
        actor_entity_id: str,
    ) -> dict[str, Any]:
        actor = str(actor_entity_id or "").strip()
        with self.store.read_connection() as connection:
            entity = connection.execute(
                "SELECT entity_id FROM entities WHERE entity_id=?",
                (actor,),
            ).fetchone()
            if entity is None:
                raise ContinuityError(
                    "UNKNOWN_EVENT_ENTITY",
                    f"unknown actor entity: {actor}",
                )
            custody_rows = list(
                connection.execute(
                    """
                    SELECT * FROM item_custody_state
                    WHERE legal_owner_entity_id=?
                       OR custodian_entity_id=?
                       OR carrier_entity_id=?
                       OR access_controller_entity_id=?
                    ORDER BY subject_type, subject_id
                    """,
                    (actor, actor, actor, actor),
                )
            )
            subjects = {
                (
                    str(row["subject_type"]),
                    str(row["subject_id"]),
                ): self._item_subject_query_row(
                    connection,
                    str(row["subject_type"]),
                    str(row["subject_id"]),
                )
                for row in custody_rows
            }

            def facet(field: str) -> list[dict[str, Any]]:
                return [
                    subjects[
                        (
                            str(row["subject_type"]),
                            str(row["subject_id"]),
                        )
                    ]
                    for row in custody_rows
                    if str(row[field] or "") == actor
                ]

            stored = [
                subjects[
                    (
                        str(row["subject_type"]),
                        str(row["subject_id"]),
                    )
                ]
                for row in custody_rows
                if str(row["custody_status"]) == "stored"
            ]
            equipped = [
                self._item_subject_query_row(
                    connection,
                    "item_instance",
                    str(row["item_instance_id"]),
                )
                for row in connection.execute(
                    """
                    SELECT item_instance_id
                    FROM item_runtime_state
                    WHERE equipped_by_entity_id=? AND destroyed=0
                    ORDER BY item_instance_id
                    """,
                    (actor,),
                )
            ]
            typed_entity_ids = {
                str(row["item_entity_id"])
                for row in connection.execute(
                    """
                    SELECT item_entity_id FROM item_definitions
                    WHERE item_entity_id IS NOT NULL
                      AND item_status NOT IN (
                        'legacy_unmodeled', 'legacy_self_instance'
                      )
                    UNION
                    SELECT item_entity_id FROM item_instances
                    WHERE item_entity_id IS NOT NULL
                      AND instance_status NOT IN ('destroyed', 'consumed')
                    """
                )
            }
            legacy_inventory = [
                self._decode_item_projection_row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM inventory_state
                    WHERE owner_entity_id=?
                      AND item_status NOT IN ('consumed', 'lost')
                    ORDER BY item_entity_id, inventory_key
                    """,
                    (actor,),
                )
                if str(row["item_entity_id"]) not in typed_entity_ids
            ]
            return {
                "actor_entity_id": actor,
                "owned": facet("legal_owner_entity_id"),
                "custodied": facet("custodian_entity_id"),
                "carried": facet("carrier_entity_id"),
                "stored": stored,
                "equipped": equipped,
                "legacy_inventory": legacy_inventory,
                **self._item_projection_meta(connection, self.store),
            }

    def query_item_history(
        self,
        *,
        item_instance_id: str | None = None,
        stack_id: str | None = None,
        actor_entity_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        if item_instance_id and stack_id:
            raise ContinuityError(
                "ITEM_SUBJECT_AMBIGUOUS",
                "history accepts one instance or stack filter",
            )
        normalized_limit = validate_positive_int(
            limit,
            "limit",
            allow_none=False,
            minimum=1,
        )
        clauses: list[str] = []
        parameters: list[Any] = []
        if item_instance_id:
            clauses.append("h.item_instance_id=?")
            parameters.append(str(item_instance_id))
        if stack_id:
            clauses.append("h.stack_id=?")
            parameters.append(str(stack_id))
        if actor_entity_id:
            clauses.append("h.actor_entity_id=?")
            parameters.append(str(actor_entity_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(normalized_limit)
        with self.store.read_connection() as connection:
            rows = [
                self._decode_item_projection_row(row)
                for row in connection.execute(
                    f"""
                    SELECT h.*, e.scope, e.branch_id,
                           e.evidence_json
                    FROM item_use_history AS h
                    JOIN continuity_events AS e
                      ON e.event_id=h.source_event_id
                    {where}
                    ORDER BY h.updated_order DESC, h.source_event_id DESC
                    LIMIT ?
                    """,
                    tuple(parameters),
                )
            ]
            return {
                "history": rows,
                **self._item_projection_meta(connection, self.store),
            }

    def query_item_observations(
        self,
        *,
        item_instance_id: str | None = None,
        stack_id: str | None = None,
        observer_entity_id: str | None = None,
        knowledge_plane: str | None = None,
        visibility: str = "generation",
        limit: int = 100,
    ) -> dict[str, Any]:
        if item_instance_id and stack_id:
            raise ContinuityError(
                "ITEM_SUBJECT_AMBIGUOUS",
                "observations accept one instance or stack filter",
            )
        normalized_limit = validate_positive_int(
            limit,
            "limit",
            allow_none=False,
            minimum=1,
        )
        normalized_visibility = str(
            visibility or "generation"
        ).strip().casefold()
        if normalized_visibility not in {"generation", "inspection", "raw"}:
            raise ContinuityError(
                "ITEM_VISIBILITY_MODE_INVALID",
                f"unsupported item visibility mode: {visibility}",
            )
        clauses: list[str] = []
        parameters: list[Any] = []
        filters = (
            ("o.item_instance_id", item_instance_id),
            ("o.stack_id", stack_id),
            ("o.knowledge_plane", knowledge_plane),
        )
        for column, value in filters:
            if value is not None:
                clauses.append(f"{column}=?")
                parameters.append(str(value))
        if normalized_visibility == "generation":
            clauses.append("o.knowledge_plane<>'author_plan'")
            observer = str(observer_entity_id or "").strip()
            if observer:
                clauses.append(
                    "("
                    "(o.knowledge_plane='actor_belief' "
                    "AND o.observer_entity_id=?) "
                    "OR "
                    "o.knowledge_plane<>'actor_belief'"
                    ")"
                )
                parameters.append(observer)
            else:
                clauses.append("o.knowledge_plane<>'actor_belief'")
        elif observer_entity_id is not None:
            clauses.append("o.observer_entity_id=?")
            parameters.append(str(observer_entity_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(normalized_limit)
        with self.store.read_connection() as connection:
            rows = [
                self._decode_item_projection_row(row)
                for row in connection.execute(
                    f"""
                    SELECT o.*, e.scope, e.branch_id,
                           e.evidence_json
                    FROM item_observations AS o
                    JOIN continuity_events AS e
                      ON e.event_id=o.source_event_id
                    {where}
                    ORDER BY o.updated_order DESC, o.observation_key DESC
                    LIMIT ?
                    """,
                    tuple(parameters),
                )
            ]
            return {
                "observations": rows,
                "visibility": normalized_visibility,
                **self._item_projection_meta(connection, self.store),
            }

    @staticmethod
    def _advantage_projection_meta(
        connection: sqlite3.Connection,
        store: ContinuityStore,
    ) -> dict[str, Any]:
        metadata = read_advantage_projection_metadata(connection)
        return {
            "advantage_projection_hash": str(
                metadata.get(ADVANTAGE_META_HASH) or ""
            ),
            "advantage_projection_schema_version": int(
                metadata.get(ADVANTAGE_META_VERSION) or 0
            ),
            "advantage_projection_source_revisions": {
                "head": int(
                    metadata.get(ADVANTAGE_META_HEAD_REVISION)
                    or store.get_meta_int(
                        connection, "head_canon_revision"
                    )
                ),
                "active": int(
                    metadata.get(ADVANTAGE_META_ACTIVE_REVISION)
                    or store.get_meta_int(
                        connection, "active_canon_revision"
                    )
                ),
            },
        }

    def query_advantage_definition(
        self,
        advantage_id: str,
    ) -> dict[str, Any]:
        normalized_id = str(advantage_id or "").strip()
        with self.store.read_connection() as connection:
            definition = query_advantage_definition_projection(
                connection,
                normalized_id,
            )
            if definition is None:
                raise ContinuityError(
                    "ADVANTAGE_NOT_FOUND",
                    f"unknown advantage: {normalized_id}",
                )
            return {
                "definition": definition,
                "anchors": query_advantage_anchors_projection(
                    connection,
                    normalized_id,
                ),
                "narrative_contract": (
                    query_advantage_narrative_contract_projection(
                        connection,
                        normalized_id,
                    )
                ),
                **self._advantage_projection_meta(
                    connection,
                    self.store,
                ),
            }

    def query_advantage_definitions(
        self,
        *,
        owner_entity_id: str | None = None,
        status: str | None = None,
        profile: str | None = None,
        branch_id: str | None = None,
        generation_visible_only: bool = True,
    ) -> dict[str, Any]:
        with self.store.read_connection() as connection:
            definitions = query_advantage_definitions_projection(
                connection,
                owner_entity_id=owner_entity_id,
                status=status,
                profile=profile,
                branch_id=branch_id,
                generation_visible_only=bool(generation_visible_only),
            )
            return {
                "definitions": definitions,
                "count": len(definitions),
                **self._advantage_projection_meta(
                    connection,
                    self.store,
                ),
            }

    def query_advantage_runtime(
        self,
        advantage_id: str,
        *,
        branch_id: str = "main",
    ) -> dict[str, Any]:
        normalized_id = str(advantage_id or "").strip()
        with self.store.read_connection() as connection:
            definition = query_advantage_definition_projection(
                connection,
                normalized_id,
            )
            if definition is None:
                raise ContinuityError(
                    "ADVANTAGE_NOT_FOUND",
                    f"unknown advantage: {normalized_id}",
                )
            return {
                "advantage_id": normalized_id,
                "branch_id": str(branch_id or "main"),
                "runtime": query_advantage_runtime_projection(
                    connection,
                    normalized_id,
                    branch_id=str(branch_id or "main"),
                ),
                **self._advantage_projection_meta(
                    connection,
                    self.store,
                ),
            }

    def query_advantage_modules(
        self,
        advantage_id: str,
        *,
        enabled_only: bool = False,
    ) -> dict[str, Any]:
        normalized_id = str(advantage_id or "").strip()
        with self.store.read_connection() as connection:
            modules = query_advantage_modules_projection(
                connection,
                normalized_id,
                enabled_only=bool(enabled_only),
            )
            return {
                "advantage_id": normalized_id,
                "modules": modules,
                "count": len(modules),
                **self._advantage_projection_meta(
                    connection,
                    self.store,
                ),
            }

    def query_advantage_ledger(
        self,
        advantage_id: str,
        *,
        limit: int = 50,
        entry_kind: str | None = None,
        branch_id: str | None = None,
        visibility: str = "generation",
    ) -> dict[str, Any]:
        normalized_limit = validate_positive_int(
            limit,
            "limit",
            allow_none=False,
            minimum=1,
        )
        normalized_id = str(advantage_id or "").strip()
        with self.store.read_connection() as connection:
            entries = query_advantage_ledger_projection(
                connection,
                normalized_id,
                limit=normalized_limit,
                entry_kind=entry_kind,
                branch_id=branch_id,
                visibility=visibility,
            )
            return {
                "advantage_id": normalized_id,
                "visibility": str(visibility or "generation").strip().casefold(),
                "ledger": entries,
                "count": len(entries),
                **self._advantage_projection_meta(
                    connection,
                    self.store,
                ),
            }

    def query_advantage_knowledge(
        self,
        advantage_id: str,
        *,
        knowledge_plane: str | None = None,
        observer_entity_id: str | None = None,
        include_noncanon: bool = False,
        visibility: str = "generation",
        reveal_stage: str | None = None,
        visible_reveal_stages: Sequence[str] | None = None,
        story_cursor: Mapping[str, Any] | None = None,
        chapter_no: int | None = None,
        scene_index: int | None = None,
        visible_module_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        normalized_id = str(advantage_id or "").strip()
        normalized_visibility = str(
            visibility or "generation"
        ).strip().casefold()
        with self.store.read_connection() as connection:
            knowledge = query_advantage_knowledge_projection(
                connection,
                normalized_id,
                knowledge_plane=knowledge_plane,
                observer_entity_id=observer_entity_id,
                include_noncanon=bool(include_noncanon),
                visibility=normalized_visibility,
                reveal_stage=reveal_stage,
                visible_reveal_stages=visible_reveal_stages,
                story_cursor=story_cursor,
                chapter_no=chapter_no,
                scene_index=scene_index,
                visible_module_ids=visible_module_ids,
            )
            return {
                "advantage_id": normalized_id,
                "visibility": normalized_visibility,
                "knowledge": knowledge,
                "count": len(knowledge),
                **self._advantage_projection_meta(
                    connection,
                    self.store,
                ),
            }

    def query_advantage_progression(
        self,
        advantage_id: str,
        *,
        branch_id: str = "main",
    ) -> dict[str, Any]:
        normalized_id = str(advantage_id or "").strip()
        with self.store.read_connection() as connection:
            return {
                **query_advantage_progression_projection(
                    connection,
                    normalized_id,
                    branch_id=str(branch_id or "main"),
                ),
                **self._advantage_projection_meta(
                    connection,
                    self.store,
                ),
            }

    def query_advantage_exposure(
        self,
        advantage_id: str,
        *,
        branch_id: str = "main",
    ) -> dict[str, Any]:
        normalized_id = str(advantage_id or "").strip()
        with self.store.read_connection() as connection:
            return {
                **query_advantage_exposure_projection(
                    connection,
                    normalized_id,
                    branch_id=str(branch_id or "main"),
                ),
                **self._advantage_projection_meta(
                    connection,
                    self.store,
                ),
            }

    def query_advantage_contracts(
        self,
        advantage_id: str,
        *,
        active_only: bool = True,
    ) -> dict[str, Any]:
        normalized_id = str(advantage_id or "").strip()
        with self.store.read_connection() as connection:
            contracts = query_advantage_contracts_projection(
                connection,
                normalized_id,
                active_only=bool(active_only),
            )
            return {
                "advantage_id": normalized_id,
                "contracts": contracts,
                "count": len(contracts),
                **self._advantage_projection_meta(
                    connection,
                    self.store,
                ),
            }

    def query_special_item_context(
        self,
        advantage_id: str | None = None,
        *,
        owner_entity_id: str | None = None,
        branch_id: str = "main",
        knowledge_plane: str | None = None,
        observer_entity_id: str | None = None,
        ledger_limit: int = 10,
        visibility: str = "generation",
        reveal_stage: str | None = None,
        visible_reveal_stages: Sequence[str] | None = None,
        story_cursor: Mapping[str, Any] | None = None,
        chapter_no: int | None = None,
        scene_index: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        normalized_limit = validate_positive_int(
            ledger_limit,
            "ledger_limit",
            allow_none=False,
            minimum=1,
        )
        normalized_context_limit = validate_positive_int(
            limit,
            "limit",
            allow_none=True,
            minimum=1,
        )
        normalized_id = str(advantage_id or "").strip()
        normalized_visibility = str(
            visibility or "generation"
        ).strip().casefold()
        with self.store.read_connection() as connection:
            if normalized_id:
                context = query_advantage_context_projection(
                    connection,
                    normalized_id,
                    branch_id=str(branch_id or "main"),
                    knowledge_plane=knowledge_plane,
                    observer_entity_id=observer_entity_id,
                    ledger_limit=normalized_limit,
                    visibility=normalized_visibility,
                    reveal_stage=reveal_stage,
                    visible_reveal_stages=visible_reveal_stages,
                    story_cursor=story_cursor,
                    chapter_no=chapter_no,
                    scene_index=scene_index,
                )
                contexts = (
                    [context]
                    if isinstance(context.get("definition"), Mapping)
                    else []
                )
            else:
                contexts = query_advantage_contexts_projection(
                    connection,
                    owner_entity_id=owner_entity_id,
                    branch_id=str(branch_id or "main"),
                    knowledge_plane=knowledge_plane,
                    observer_entity_id=observer_entity_id,
                    ledger_limit=normalized_limit,
                    visibility=normalized_visibility,
                    reveal_stage=reveal_stage,
                    visible_reveal_stages=visible_reveal_stages,
                    story_cursor=story_cursor,
                    chapter_no=chapter_no,
                    scene_index=scene_index,
                    limit=normalized_context_limit,
                )
            return {
                "advantage_id": normalized_id or None,
                "owner_entity_id": owner_entity_id,
                "visibility": normalized_visibility,
                "contexts": contexts,
                "count": len(contexts),
                **self._advantage_projection_meta(
                    connection,
                    self.store,
                ),
            }

    def replay(self) -> dict[str, Any]:
        response = self.replay_engine.rebuild()
        response["readable_item_projection"] = (
            refresh_item_readable_projection_safe(self.store)
        )
        response["readable_advantage_projection"] = (
            refresh_advantage_readable_projection_safe(self.store)
        )
        return response

    def projection_hash(self) -> str:
        with self.store.read_connection() as connection:
            row = connection.execute(
                """
                SELECT projection_hash
                FROM projection_runs
                WHERE projection_name='continuity'
                  AND run_status='completed'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return self.replay()["projection_hash"]
            return str(row["projection_hash"])

    # ------------------------------------------------------------------
    # Standalone PowerSpec import lifecycle
    # ------------------------------------------------------------------
    @staticmethod
    def _power_spec_import_failure(
        error: PowerSpecImportError,
    ) -> ContinuityError:
        return ContinuityError(
            str(error.code),
            str(error),
            details=dict(error.details),
        )

    def preview_power_spec_change(
        self,
        power_spec: Mapping[str, Any],
        *,
        expected_canon_revision: int,
    ) -> dict[str, Any]:
        """Compile a standalone PowerSpec against one accepted revision.

        This method is read-only.  It validates the aggregate and deterministic
        lifecycle package without registering entities or saving a proposal.
        """

        expected = validate_positive_int(
            expected_canon_revision,
            "expected_canon_revision",
            allow_none=False,
            minimum=0,
        )
        state_path = self.store.db_path
        if not state_path.is_file():
            raise ContinuityError(
                "POWER_SPEC_STATE_NOT_CREATED",
                "power specification preview requires an existing continuity database",
                details={"path": str(state_path)},
            )
        with _open_private_database_snapshot(state_path) as connection:
            try:
                tables = validate_sqlite_component_schema(
                    connection,
                    component="continuity state",
                    meta_table="state_meta",
                    version_key="schema_version",
                    supported_version=LEGACY_SCHEMA_VERSION,
                    owned_tables=STATE_DATABASE_TABLES,
                    allowed_tables=STATE_DATABASE_TABLES,
                )
                missing_tables = sorted(STATE_DATABASE_TABLES - tables)
                if missing_tables:
                    raise ContinuityError(
                        "POWER_SPEC_STATE_SCHEMA_INCOMPLETE",
                        "power specification preview requires the complete continuity schema",
                        details={
                            "path": str(state_path),
                            "missing_tables": missing_tables,
                        },
                    )
                legacy_row = connection.execute(
                    "SELECT value FROM state_meta WHERE key='schema_version'"
                ).fetchone()
                continuity_row = connection.execute(
                    "SELECT value FROM state_meta "
                    "WHERE key='continuity_schema_version'"
                ).fetchone()
                legacy_version = (
                    int(legacy_row[0]) if legacy_row is not None else 0
                )
                continuity_version = (
                    int(continuity_row[0])
                    if continuity_row is not None
                    else 0
                )
                validate_schema_versions(
                    user_tables_present=bool(tables),
                    legacy_version=legacy_version,
                    continuity_version=continuity_version,
                )
                if (
                    legacy_version != LEGACY_SCHEMA_VERSION
                    or continuity_version != SCHEMA_VERSION
                ):
                    raise ContinuityError(
                        "POWER_SPEC_STATE_SCHEMA_UNSUPPORTED",
                        "power specification preview requires the current continuity schema",
                        details={
                            "path": str(state_path),
                            "legacy_version": legacy_version,
                            "continuity_version": continuity_version,
                            "supported_legacy_version": (
                                LEGACY_SCHEMA_VERSION
                            ),
                            "supported_continuity_version": SCHEMA_VERSION,
                        },
                    )
                active = self.store.get_meta_int(
                    connection,
                    "active_canon_revision",
                )
                head = self.store.get_meta_int(
                    connection,
                    "head_canon_revision",
                )
                if active != expected:
                    raise ContinuityError(
                        "CANON_REVISION_CONFLICT",
                        "power specification preview must bind the active canon revision",
                        details={"expected": expected, "actual": active},
                    )
            except (
                SQLiteComponentSchemaError,
                SchemaVersionError,
            ) as error:
                raise ContinuityError(
                    str(error.code),
                    str(error),
                    details={"path": str(state_path)},
                ) from error
            except sqlite3.Error as error:
                raise ContinuityError(
                    "POWER_SPEC_STATE_SCHEMA_INVALID",
                    "power specification preview requires an initialized continuity schema",
                    details={
                        "path": str(state_path),
                        "reason": str(error),
                    },
                ) from error
        try:
            response = preview_power_spec_import(power_spec)
        except PowerSpecImportError as error:
            raise self._power_spec_import_failure(error) from error
        return {
            **response,
            "canon_revisions": {
                "head": head,
                "active": active,
            },
            "expected_canon_revision": expected,
        }

    def propose_power_spec_change(
        self,
        power_spec: Mapping[str, Any],
        *,
        expected_canon_revision: int,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Register entities and freeze a standalone PowerSpec proposal."""

        preview = self.preview_power_spec_change(
            power_spec,
            expected_canon_revision=expected_canon_revision,
        )
        package = dict(preview["lifecycle_package"])
        with self.atomic_write() as connection:
            active = self.store.get_meta_int(
                connection,
                "active_canon_revision",
            )
            if active != int(expected_canon_revision):
                raise ContinuityError(
                    "CANON_REVISION_CONFLICT",
                    "power specification proposal must bind the active canon revision",
                    details={
                        "expected": int(expected_canon_revision),
                        "actual": active,
                    },
                )
            for entity in package.get("entities") or []:
                if not isinstance(entity, Mapping):
                    raise ContinuityError(
                        "POWER_SPEC_ENTITY_INVALID",
                        "power specification entity must be an object",
                    )
                self.register_entity(
                    str(entity.get("entity_type") or "concept"),
                    str(
                        entity.get("canonical_name")
                        or entity.get("entity_id")
                        or "power entity"
                    ),
                    entity_id=str(entity.get("entity_id") or "") or None,
                    aliases=tuple(entity.get("aliases") or ()),
                    attributes=dict(entity.get("attributes") or {}),
                )
            proposal = self.save_proposal(
                events=list(package.get("events") or []),
                payload={
                    "lifecycle_package": package,
                    "package_hash": str(package.get("package_hash") or ""),
                    "power_package_hash": str(
                        package.get("power_package_hash") or ""
                    ),
                },
                artifact_id=str(package.get("proposal_id") or "") or None,
                artifact_kind="power_spec",
                artifact_stage=POWER_SPEC_ARTIFACT_STAGE,
                branch_id="main",
                chapter_no=None,
                scene_index=None,
                prepared_canon_revision=expected_canon_revision,
                source_role="setting",
                proposal_kind=POWER_SPEC_PROPOSAL_KIND,
                idempotency_key=idempotency_key,
            )
        return {
            "status": "proposed",
            "proposal": proposal,
            "preview": preview,
            "required_operation": str(
                package.get("required_operation") or "accept_power_spec"
            ),
        }

    # ------------------------------------------------------------------
    # Accepted source manifest and materialization saga
    # ------------------------------------------------------------------
    def preview_source_manifest_change(
        self,
        plan: Mapping[str, Any],
        *,
        expected_canon_revision: int,
    ) -> dict[str, Any]:
        expected = validate_positive_int(
            expected_canon_revision,
            "expected_canon_revision",
            allow_none=False,
            minimum=0,
        )
        with self.store.read_connection() as connection:
            active = self.store.get_meta_int(
                connection,
                "active_canon_revision",
            )
            if active != expected:
                raise ContinuityError(
                    "CANON_REVISION_CONFLICT",
                    "source manifest preview must bind the active canon revision",
                    details={"expected": expected, "actual": active},
                )
            response = preview_manifest_plan(
                connection,
                self.store.project_root,
                plan,
                expected_canon_revision=expected,
            )
        response["canon_revisions"] = self.get_canon_revisions()
        return response

    def propose_source_manifest_change(
        self,
        plan: Mapping[str, Any],
        *,
        expected_canon_revision: int,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        preview = self.preview_source_manifest_change(
            plan,
            expected_canon_revision=expected_canon_revision,
        )
        proposal = self.save_proposal(
            events=[],
            payload={
                "source_manifest_change": dict(preview["migration"]),
            },
            artifact_id=SOURCE_MANIFEST_ARTIFACT_ID,
            artifact_kind=SOURCE_MANIFEST_ARTIFACT_KIND,
            artifact_stage=SOURCE_MANIFEST_ARTIFACT_STAGE,
            branch_id=SOURCE_MANIFEST_BRANCH_ID,
            prepared_canon_revision=expected_canon_revision,
            source_role=SOURCE_MANIFEST_SOURCE_ROLE,
            proposal_kind=SOURCE_MANIFEST_PROPOSAL_KIND,
            idempotency_key=idempotency_key,
        )
        return {
            "status": "proposed",
            "proposal": proposal,
            "preview": preview,
        }

    def source_manifest_status(self) -> dict[str, Any]:
        with self.store.read_connection() as connection:
            response = source_manifest_projection_status(connection)
            response["canon_revisions"] = {
                "head": self.store.get_meta_int(
                    connection,
                    "head_canon_revision",
                ),
                "active": self.store.get_meta_int(
                    connection,
                    "active_canon_revision",
                ),
            }
            return response

    def get_effective_source_manifest(self) -> list[dict[str, Any]]:
        """Return one deterministic read-only active source version per path."""

        with self.store.read_connection() as connection:
            return list(current_manifest_snapshot(connection)["entries"])

    def get_current_source_manifest_snapshot(self) -> dict[str, Any]:
        """Return the accepted current source view without fallback writes."""

        with self.store.read_connection() as connection:
            return current_manifest_snapshot(connection)

    def get_accepted_source_manifest(
        self,
        *,
        include_pending: bool = False,
    ) -> list[dict[str, Any]]:
        with self.store.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM accepted_source_manifest
                WHERE manifest_status='active' OR ?=1
                ORDER BY source_path, source_id
                """,
                (int(include_pending),),
            ).fetchall()
            return [
                {
                    "manifest_entry_id": str(row["manifest_entry_id"]),
                    "source_id": str(row["source_id"]),
                    "path": str(row["source_path"]),
                    "content_hash": str(row["content_hash"]),
                    "source_role": str(row["source_role"]),
                    "status": str(row["manifest_status"]),
                    "commit_id": str(row["commit_id"]),
                    "metadata": _json_load(str(row["metadata_json"]), {}),
                }
                for row in rows
            ]

    @staticmethod
    def _next_materialization_step(
        connection: sqlite3.Connection,
        run_id: str,
    ) -> int:
        return int(
            connection.execute(
                """
                SELECT COALESCE(MAX(step_no), 0) + 1
                FROM materialization_journal
                WHERE run_id=?
                """,
                (run_id,),
            ).fetchone()[0]
        )

    @staticmethod
    def _journal_materialization(
        connection: sqlite3.Connection,
        run_id: str,
        step_name: str,
        step_status: str,
        details: Mapping[str, Any],
    ) -> None:
        step_no = ContinuityService._next_materialization_step(
            connection, run_id
        )
        connection.execute(
            """
            INSERT INTO materialization_journal(
                journal_id, run_id, step_no, step_name,
                step_status, details_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stable_hash([run_id, step_no], prefix="materialize_journal_"),
                run_id,
                step_no,
                step_name,
                step_status,
                canonical_json(dict(details)),
                utc_now(),
            ),
        )

    @staticmethod
    def _new_materialization_activation_owner() -> tuple[str, int, str]:
        """Return an exact, durable identity for one activation attempt."""

        owner_pid = os.getpid()
        process_state, birth_token = _materialization_process_probe(owner_pid)
        return (
            socket.gethostname().strip().casefold() or "localhost",
            owner_pid,
            canonical_json(
                {
                    "birth": (
                        birth_token
                        if process_state == "alive" and birth_token
                        else None
                    ),
                    "nonce": secrets.token_hex(32),
                    "version": 1,
                }
            ),
        )

    @staticmethod
    def _require_materialization_activation_claim(
        connection: sqlite3.Connection,
        run_id: str,
        owner: tuple[str, int, str],
    ) -> None:
        owner_host, owner_pid, owner_token = owner
        row = connection.execute(
            """
            SELECT owner_host, owner_pid, owner_token
            FROM materialization_activation_claims
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
        if (
            row is not None
            and str(row["owner_host"]) == owner_host
            and int(row["owner_pid"]) == owner_pid
            and str(row["owner_token"]) == owner_token
        ):
            return
        raise ContinuityError(
            "MATERIALIZATION_ACTIVATION_OWNERSHIP_LOST",
            "materialization activation is no longer owned by this executor",
            details={
                "run_id": run_id,
                "owner_host": owner_host,
                "owner_pid": owner_pid,
                "claimed_host": (
                    str(row["owner_host"]) if row is not None else ""
                ),
                "claimed_pid": (
                    int(row["owner_pid"]) if row is not None else 0
                ),
            },
        )

    def _assert_materialization_activation_owner(
        self,
        run_id: str,
        owner: tuple[str, int, str],
    ) -> None:
        with self.store.read_connection() as connection:
            self._require_materialization_activation_claim(
                connection,
                run_id,
                owner,
            )

    @staticmethod
    def _release_materialization_activation_claim(
        connection: sqlite3.Connection,
        run_id: str,
        owner: tuple[str, int, str],
        *,
        required: bool,
    ) -> bool:
        owner_host, owner_pid, owner_token = owner
        cursor = connection.execute(
            """
            DELETE FROM materialization_activation_claims
            WHERE run_id=?
              AND owner_host=? AND owner_pid=? AND owner_token=?
            """,
            (run_id, owner_host, owner_pid, owner_token),
        )
        released = cursor.rowcount == 1
        if required and not released:
            ContinuityService._require_materialization_activation_claim(
                connection,
                run_id,
                owner,
            )
            raise ContinuityError(
                "MATERIALIZATION_ACTIVATION_OWNERSHIP_LOST",
                "materialization activation claim was not released",
                details={"run_id": run_id},
            )
        return released

    def materialization_status(self, commit_id: str) -> dict[str, Any]:
        with self.store.read_connection() as connection:
            run = connection.execute(
                "SELECT * FROM materialization_runs WHERE commit_id=?",
                (commit_id,),
            ).fetchone()
            if run is None:
                raise ContinuityError(
                    "MATERIALIZATION_RUN_NOT_FOUND",
                    f"commit has no materialization run: {commit_id}",
                )
            files = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT relative_path, expected_old_hash,
                           proposed_new_hash, actual_hash, file_status
                    FROM materialization_files
                    WHERE run_id=?
                    ORDER BY relative_path
                    """,
                    (run["run_id"],),
                )
            ]
            journal = [
                {
                    "step_no": int(row["step_no"]),
                    "step": str(row["step_name"]),
                    "status": str(row["step_status"]),
                    "details": _json_load(str(row["details_json"]), {}),
                }
                for row in connection.execute(
                    """
                    SELECT * FROM materialization_journal
                    WHERE run_id=?
                    ORDER BY step_no
                    """,
                    (run["run_id"],),
                )
            ]
            return {
                "run_id": str(run["run_id"]),
                "commit_id": commit_id,
                "target_root": str(run["target_root"]),
                "status": str(run["run_status"]),
                "plan": _json_load(str(run["plan_json"]), {}),
                "staging_path": str(run["staging_path"]),
                "files": files,
                "journal": journal,
                "completion_receipt": _json_load(
                    str(run["completion_receipt_json"]), {}
                ),
                "error": str(run["error"]),
            }

    def stage_materialization(
        self,
        commit_id: str,
        *,
        files: Mapping[str, str | bytes | Mapping[str, Any]] | None = None,
        target_root: str | Path | None = None,
    ) -> dict[str, Any]:
        """Write approved generated files to staging, never over source files."""

        status = self.materialization_status(commit_id)
        run_id = str(status["run_id"])
        approved_root_path = Path(
            os.path.abspath(
                Path(
                    status["target_root"] or self.store.project_root
                ).expanduser()
            )
        )
        _assert_no_reparse_path(
            approved_root_path,
            anchor=Path(approved_root_path.anchor),
            code="UNSAFE_MATERIALIZATION_PATH",
            label="materialization target root",
        )
        approved_root = approved_root_path.resolve()
        requested_root_path = Path(
            os.path.abspath(
                Path(target_root or approved_root_path).expanduser()
            )
        )
        _assert_no_reparse_path(
            requested_root_path,
            anchor=Path(requested_root_path.anchor),
            code="UNSAFE_MATERIALIZATION_PATH",
            label="requested materialization target root",
        )
        root = requested_root_path.resolve()
        if root != approved_root:
            raise ContinuityError(
                "MATERIALIZATION_TARGET_MISMATCH",
                "target root differs from the approval grant binding",
                details={
                    "approved": str(approved_root),
                    "requested": str(root),
                },
            )
        with self.store.read_connection() as authorization_connection:
            grant_row = authorization_connection.execute(
                """
                SELECT g.binding_json, g.binding_hash
                FROM canon_commits AS c
                JOIN approval_grants AS g
                  ON g.token_hash=c.grant_token_hash
                WHERE c.commit_id=?
                """,
                (commit_id,),
            ).fetchone()
            if grant_row is None:
                raise ContinuityError(
                    "APPROVAL_GRANT_NOT_FOUND",
                    "materialization commit has no approval binding",
                )
            grant_binding = dict(
                _json_load(str(grant_row["binding_json"]), {})
            )
            if stable_hash(
                grant_binding, prefix="grant_binding_"
            ) != str(grant_row["binding_hash"]):
                raise ContinuityError(
                    "APPROVAL_BINDING_CORRUPT",
                    "stored materialization grant binding is corrupt",
                )
        if "materialize" not in set(
            grant_binding.get("authorized_operations") or []
        ):
            raise ContinuityError(
                "APPROVAL_OPERATION_NOT_AUTHORIZED",
                "approval grant does not authorize materialization",
            )
        authorized_paths = {
            _safe_relative_path(str(path)).as_posix()
            for path in grant_binding.get("authorized_paths") or []
        }
        authorized_hashes = {
            _safe_relative_path(str(item["path"])).as_posix(): {
                "expected_old_hash": item.get("expected_old_hash"),
                "proposed_new_hash": item.get("proposed_new_hash"),
            }
            for item in grant_binding.get("target_old_new_hashes") or []
            if isinstance(item, Mapping) and item.get("path")
        }
        missing_authorization: list[str] = []
        if not authorized_paths:
            missing_authorization.append("authorized_paths")
        if not authorized_hashes:
            missing_authorization.append("target_old_new_hashes")
        if missing_authorization:
            raise ContinuityError(
                "MATERIALIZATION_AUTHORIZATION_REQUIRED",
                "materialization approval must bind explicit paths and hashes",
                details={"missing": missing_authorization},
            )
        if files is None:
            with self.store.read_connection() as proposal_connection:
                payload_row = proposal_connection.execute(
                    """
                    SELECT p.payload_json
                    FROM canon_commits AS c
                    JOIN proposals AS p ON p.proposal_id=c.proposal_id
                    WHERE c.commit_id=?
                    """,
                    (commit_id,),
                ).fetchone()
            if payload_row is None:
                raise ContinuityError(
                    "COMMIT_NOT_FOUND", f"unknown commit: {commit_id}"
                )
            proposal_payload = dict(
                _json_load(str(payload_row["payload_json"]), {})
            )
            raw_bundle = dict(proposal_payload.get("bundle") or {})
            derived_files: dict[str, Mapping[str, Any]] = {}
            for item in raw_bundle.get("artifact_manifest") or []:
                if (
                    not isinstance(item, Mapping)
                    or not item.get("path")
                    or item.get("operation") == "noop"
                ):
                    continue
                derived_files[str(item["path"])] = {
                    "content": item.get("proposed_content") or "",
                    "expected_old_hash": item.get("expected_old_hash"),
                }
            files = derived_files
        if not files:
            raise ContinuityError(
                "MATERIALIZATION_FILES_REQUIRED",
                "no approved files are available for staging",
            )
        staging_path = (
            self.store.project_root
            / ".plot-rag"
            / "staging"
            / run_id
        )
        _assert_no_reparse_path(
            staging_path,
            anchor=self.store.project_root,
            code="UNSAFE_STAGING_PATH",
            label="materialization staging path",
        )
        staging_path.mkdir(parents=True, exist_ok=True)
        _assert_no_reparse_path(
            staging_path,
            anchor=self.store.project_root,
            code="UNSAFE_STAGING_PATH",
            label="materialization staging path",
        )
        staging = staging_path.resolve()
        project_root = self.store.project_root.resolve()
        if staging != project_root and project_root not in staging.parents:
            raise ContinuityError(
                "UNSAFE_STAGING_PATH",
                "materialization staging path escaped the project",
                details={"staging": str(staging)},
            )
        prepared: list[dict[str, Any]] = []
        for raw_path, value in sorted(files.items()):
            relative = _safe_relative_path(raw_path)
            if relative.as_posix() not in authorized_paths:
                raise ContinuityError(
                    "MATERIALIZATION_PATH_NOT_AUTHORIZED",
                    "file path is outside the approval grant",
                    details={"path": relative.as_posix()},
                )
            if isinstance(value, Mapping):
                content = value.get("content", "")
                expected_old_hash = value.get("expected_old_hash")
            else:
                content = value
                expected_old_hash = None
            data = content if isinstance(content, bytes) else str(content).encode(
                "utf-8"
            )
            proposed_hash = hashlib.sha256(data).hexdigest()
            approved_hashes = authorized_hashes.get(relative.as_posix())
            if approved_hashes is None or not approved_hashes.get(
                "proposed_new_hash"
            ):
                raise ContinuityError(
                    "MATERIALIZATION_HASH_NOT_AUTHORIZED",
                    "file path has no exact approved output hash",
                    details={"path": relative.as_posix()},
                )
            approved_old_hash = approved_hashes.get("expected_old_hash")
            approved_new_hash = str(approved_hashes["proposed_new_hash"])
            if (
                expected_old_hash is not None
                and expected_old_hash != approved_old_hash
            ):
                raise ContinuityError(
                    "MATERIALIZATION_OLD_HASH_NOT_AUTHORIZED",
                    "caller old hash differs from the approval grant",
                    details={"path": relative.as_posix()},
                )
            expected_old_hash = approved_old_hash
            if proposed_hash != approved_new_hash:
                raise ContinuityError(
                    "MATERIALIZATION_NEW_HASH_NOT_AUTHORIZED",
                    "generated bytes differ from the approved new hash",
                    details={
                        "path": relative.as_posix(),
                        "expected": approved_new_hash,
                        "actual": proposed_hash,
                    },
                )
            target = (root / relative).resolve()
            if root != target and root not in target.parents:
                raise ContinuityError(
                    "UNSAFE_MATERIALIZATION_PATH",
                    f"path escapes target root: {raw_path}",
                )
            current_hash = sha256_file(target) if target.is_file() else None
            if expected_old_hash is None and target.exists():
                raise ContinuityError(
                    "TARGET_ALREADY_EXISTS",
                    "existing target needs an exact expected_old_hash",
                    details={"path": raw_path, "actual_hash": current_hash},
                )
            if (
                expected_old_hash is not None
                and current_hash != str(expected_old_hash)
            ):
                raise ContinuityError(
                    "TARGET_HASH_CONFLICT",
                    "target changed after proposal review",
                    details={
                        "path": raw_path,
                        "expected": expected_old_hash,
                        "actual": current_hash,
                    },
                )
            staged_path = staging / relative
            _assert_no_reparse_path(
                staged_path,
                anchor=staging,
                code="UNSAFE_STAGING_PATH",
                label="staged materialization file",
            )
            staged_path.parent.mkdir(parents=True, exist_ok=True)
            _assert_no_reparse_path(
                staged_path,
                anchor=staging,
                code="UNSAFE_STAGING_PATH",
                label="staged materialization file",
            )
            staged_path = staged_path.resolve()
            if staging != staged_path and staging not in staged_path.parents:
                raise ContinuityError(
                    "UNSAFE_STAGING_PATH",
                    "staged file escaped its approved root",
                    details={"path": relative.as_posix()},
                )
            staged_path.write_bytes(data)
            if sha256_file(staged_path) != proposed_hash:
                raise ContinuityError(
                    "STAGING_HASH_MISMATCH",
                    f"staged file hash mismatch: {raw_path}",
                )
            prepared.append(
                {
                    "relative_path": relative.as_posix(),
                    "expected_old_hash": expected_old_hash,
                    "proposed_new_hash": proposed_hash,
                }
            )
        with self.store.transaction() as connection:
            run = connection.execute(
                "SELECT * FROM materialization_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ContinuityError(
                    "MATERIALIZATION_RUN_NOT_FOUND", run_id
                )
            for item in prepared:
                connection.execute(
                    """
                    INSERT INTO materialization_files(
                        run_id, relative_path, expected_old_hash,
                        proposed_new_hash, actual_hash, file_status
                    ) VALUES(?, ?, ?, ?, ?, 'staged')
                    ON CONFLICT(run_id, relative_path) DO UPDATE
                    SET expected_old_hash=excluded.expected_old_hash,
                        proposed_new_hash=excluded.proposed_new_hash,
                        actual_hash=excluded.actual_hash,
                        file_status='staged'
                    """,
                    (
                        run_id,
                        item["relative_path"],
                        item["expected_old_hash"],
                        item["proposed_new_hash"],
                        item["proposed_new_hash"],
                    ),
                )
            now = utc_now()
            connection.execute(
                """
                UPDATE materialization_runs
                SET target_root=?, staging_path=?, run_status='staged',
                    completion_receipt_json='{}', error='',
                    updated_at=?, completed_at=NULL
                WHERE run_id=?
                """,
                (str(root), str(staging), now, run_id),
            )
            self._journal_materialization(
                connection,
                run_id,
                "staging",
                "completed",
                {"files": prepared, "target_root": str(root)},
            )
        return self.materialization_status(commit_id)

    def activate_materialization(self, commit_id: str) -> dict[str, Any]:
        """Activate staged files with old-hash CAS and recoverable checkpoints."""

        status = self.materialization_status(commit_id)
        if status["status"] == "completed":
            return status
        if status["status"] not in {
            "staged",
            "activating",
            "ready",
            "awaiting_manifest",
            "failed",
        }:
            raise ContinuityError(
                "MATERIALIZATION_NOT_STAGED",
                f"run is in {status['status']} state",
            )
        run_id = str(status["run_id"])
        project_root = self.store.project_root.resolve()
        root_path = Path(
            os.path.abspath(Path(status["target_root"]).expanduser())
        )
        staging_path = Path(
            os.path.abspath(Path(status["staging_path"]).expanduser())
        )

        def inside(path: Path, container: Path) -> bool:
            return path == container or container in path.parents

        with self.store.read_connection() as authorization_connection:
            grant_row = authorization_connection.execute(
                """
                SELECT g.binding_json, g.binding_hash
                FROM canon_commits AS c
                JOIN approval_grants AS g
                  ON g.token_hash=c.grant_token_hash
                WHERE c.commit_id=?
                """,
                (commit_id,),
            ).fetchone()
        if grant_row is None:
            raise ContinuityError(
                "APPROVAL_GRANT_NOT_FOUND",
                "materialization commit has no approval binding",
            )
        grant_binding = dict(
            _json_load(str(grant_row["binding_json"]), {})
        )
        if stable_hash(
            grant_binding, prefix="grant_binding_"
        ) != str(grant_row["binding_hash"]):
            raise ContinuityError(
                "APPROVAL_BINDING_CORRUPT",
                "stored materialization grant binding is corrupt",
            )
        if "materialize" not in set(
            grant_binding.get("authorized_operations") or []
        ):
            raise ContinuityError(
                "APPROVAL_OPERATION_NOT_AUTHORIZED",
                "approval grant does not authorize materialization",
            )
        authorized_paths = {
            _safe_relative_path(str(path)).as_posix()
            for path in grant_binding.get("authorized_paths") or []
        }
        authorized_hashes = {
            _safe_relative_path(str(item["path"])).as_posix(): {
                "expected_old_hash": item.get("expected_old_hash"),
                "proposed_new_hash": item.get("proposed_new_hash"),
            }
            for item in grant_binding.get("target_old_new_hashes") or []
            if isinstance(item, Mapping) and item.get("path")
        }
        if not authorized_paths or not authorized_hashes:
            raise ContinuityError(
                "MATERIALIZATION_AUTHORIZATION_REQUIRED",
                "materialization approval must bind explicit paths and hashes",
            )
        approved_root_path = Path(
            str(grant_binding.get("target_project_real_path") or "")
        ).expanduser()
        if os.path.normcase(os.path.abspath(root_path)) != os.path.normcase(
            os.path.abspath(approved_root_path)
        ):
            raise ContinuityError(
                "MATERIALIZATION_TARGET_MISMATCH",
                "materialization run target differs from the approval binding",
                details={
                    "approved": str(approved_root_path),
                    "run_target": str(root_path),
                },
            )
        _assert_no_reparse_path(
            root_path,
            anchor=Path(root_path.anchor),
            code="UNSAFE_MATERIALIZATION_PATH",
            label="materialization target root",
        )
        root = root_path.resolve()

        staging_base_path = project_root / ".plot-rag" / "staging"
        expected_staging_path = staging_base_path / run_id
        if os.path.normcase(os.path.abspath(staging_path)) != os.path.normcase(
            os.path.abspath(expected_staging_path)
        ):
            raise ContinuityError(
                "UNSAFE_STAGING_PATH",
                "materialization staging path differs from its approved run path",
                details={
                    "staging": str(staging_path),
                    "expected": str(expected_staging_path),
                },
            )
        _assert_no_reparse_path(
            staging_path,
            anchor=project_root,
            code="UNSAFE_STAGING_PATH",
            label="materialization staging path",
        )
        staging_base = staging_base_path.resolve()
        staging = staging_path.resolve()
        expected_staging = expected_staging_path.resolve()
        if (
            not inside(staging_base, project_root)
            or staging != expected_staging
            or not inside(staging, staging_base)
            or not staging.is_dir()
        ):
            raise ContinuityError(
                "UNSAFE_STAGING_PATH",
                "materialization staging path escaped its approved root",
                details={
                    "staging": str(staging),
                    "expected": str(expected_staging),
                },
            )
        if not root.is_dir():
            raise ContinuityError(
                "MATERIALIZATION_TARGET_MISSING",
                "materialization target root is not a directory",
                details={"target_root": str(root)},
            )

        backup_base_path = (
            project_root / ".plot-rag" / "backups" / "materialize"
        )
        _assert_no_reparse_path(
            backup_base_path,
            anchor=project_root,
            code="UNSAFE_BACKUP_PATH",
            label="materialization backup root",
        )
        backup_base = backup_base_path.resolve()
        if not inside(backup_base, project_root):
            raise ContinuityError(
                "UNSAFE_BACKUP_PATH",
                "materialization backup root escaped the project",
                details={"backup_root": str(backup_base)},
            )
        backup_root_path = backup_base_path / run_id
        _assert_no_reparse_path(
            backup_root_path,
            anchor=project_root,
            code="UNSAFE_BACKUP_PATH",
            label="materialization backup run",
        )
        backup_root = backup_root_path.resolve()
        if not inside(backup_root, backup_base):
            raise ContinuityError(
                "UNSAFE_BACKUP_PATH",
                "materialization backup run escaped its approved root",
                details={"backup_root": str(backup_root)},
            )

        prepared: list[dict[str, Any]] = []
        applied: list[dict[str, Any]] = []
        rollback_states: dict[str, tuple[str | None, str]] = {}
        created_directories: set[Path] = set()
        activation_owner = self._new_materialization_activation_owner()
        owner_host, owner_pid, owner_token = activation_owner
        claim_acquired = False
        recovered_dead_claim = False
        with self.store.transaction() as connection:
            run = connection.execute(
                """
                SELECT run_status
                FROM materialization_runs
                WHERE run_id=? AND commit_id=?
                """,
                (run_id, commit_id),
            ).fetchone()
            if run is None:
                raise ContinuityError(
                    "MATERIALIZATION_RUN_NOT_FOUND",
                    f"commit has no materialization run: {commit_id}",
                )
            current_status = str(run["run_status"])
            if current_status != "completed":
                if current_status not in {
                    "staged",
                    "activating",
                    "ready",
                    "awaiting_manifest",
                    "failed",
                }:
                    raise ContinuityError(
                        "MATERIALIZATION_NOT_STAGED",
                        f"run is in {current_status} state",
                    )
                existing_claim = connection.execute(
                    """
                    SELECT owner_host, owner_pid, owner_token, claimed_at
                    FROM materialization_activation_claims
                    WHERE run_id=?
                    """,
                    (run_id,),
                ).fetchone()
                if existing_claim is not None:
                    claimed_host = str(existing_claim["owner_host"])
                    claimed_pid = int(existing_claim["owner_pid"])
                    claimed_token = str(existing_claim["owner_token"])
                    owner_state, owner_reason = _materialization_owner_state(
                        claimed_host,
                        claimed_pid,
                        claimed_token,
                    )
                    if owner_state == "dead":
                        cursor = connection.execute(
                            """
                            DELETE FROM materialization_activation_claims
                            WHERE run_id=? AND owner_host=?
                              AND owner_pid=? AND owner_token=?
                            """,
                            (
                                run_id,
                                claimed_host,
                                claimed_pid,
                                claimed_token,
                            ),
                        )
                        if cursor.rowcount == 1:
                            self._journal_materialization(
                                connection,
                                run_id,
                                "activation_claim",
                                "recovered",
                                {
                                    "owner_host": claimed_host,
                                    "owner_pid": claimed_pid,
                                    "claimed_at": str(
                                        existing_claim["claimed_at"]
                                    ),
                                    "reason": owner_reason,
                                },
                            )
                            existing_claim = None
                            recovered_dead_claim = True
                if existing_claim is None:
                    if current_status == "failed":
                        rollback_failed = connection.execute(
                            """
                            SELECT 1
                            FROM materialization_files
                            WHERE run_id=? AND file_status='rollback_failed'
                            LIMIT 1
                            """,
                            (run_id,),
                        ).fetchone()
                        if rollback_failed is not None:
                            raise ContinuityError(
                                "MATERIALIZATION_ROLLBACK_INCOMPLETE",
                                "materialization has files that were not "
                                "restored safely",
                                details={"run_id": run_id},
                            )
                    connection.execute(
                        """
                        INSERT INTO materialization_activation_claims(
                            run_id, owner_host, owner_pid, owner_token, claimed_at
                        ) VALUES(?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            owner_host,
                            owner_pid,
                            owner_token,
                            utc_now(),
                        ),
                    )
                    cursor = connection.execute(
                        """
                        UPDATE materialization_runs
                        SET run_status='activating', error='', updated_at=?
                        WHERE run_id=? AND run_status=?
                          AND EXISTS (
                              SELECT 1
                              FROM materialization_activation_claims
                              WHERE run_id=? AND owner_host=?
                                AND owner_pid=? AND owner_token=?
                          )
                        """,
                        (
                            utc_now(),
                            run_id,
                            current_status,
                            run_id,
                            owner_host,
                            owner_pid,
                            owner_token,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ContinuityError(
                            "MATERIALIZATION_ACTIVATION_OWNERSHIP_LOST",
                            "materialization activation claim did not win "
                            "the run-state compare-and-swap",
                            details={"run_id": run_id},
                        )
                    self._journal_materialization(
                        connection,
                        run_id,
                        "activation",
                        "started",
                        {
                            "owner_host": owner_host,
                            "owner_pid": owner_pid,
                        },
                    )
                    claim_acquired = True

        if not claim_acquired:
            return self.materialization_status(commit_id)

        try:
            status = self.materialization_status(commit_id)
            # Preflight every path and hash before the first filesystem swap.
            for file_row in status["files"]:
                relative = _safe_relative_path(
                    str(file_row["relative_path"])
                )
                relative_path = relative.as_posix()
                if relative_path not in authorized_paths:
                    raise ContinuityError(
                        "MATERIALIZATION_PATH_NOT_AUTHORIZED",
                        "materialization file is outside the approval grant",
                        details={"path": relative_path},
                    )
                approved_hashes = authorized_hashes.get(relative_path)
                if (
                    approved_hashes is None
                    or not approved_hashes.get("proposed_new_hash")
                    or file_row["expected_old_hash"]
                    != approved_hashes.get("expected_old_hash")
                    or str(file_row["proposed_new_hash"])
                    != str(approved_hashes.get("proposed_new_hash"))
                ):
                    raise ContinuityError(
                        "MATERIALIZATION_HASH_NOT_AUTHORIZED",
                        "materialization file hashes differ from the approval grant",
                        details={"path": relative_path},
                    )
                target_path = root / relative
                staged_path = staging / relative
                _assert_no_reparse_path(
                    target_path,
                    anchor=root,
                    code="UNSAFE_MATERIALIZATION_PATH",
                    label="materialization target",
                )
                _assert_no_reparse_path(
                    staged_path,
                    anchor=staging,
                    code="UNSAFE_STAGING_PATH",
                    label="staged materialization file",
                )
                target = target_path.resolve()
                staged = staged_path.resolve()
                if not inside(target, root):
                    raise ContinuityError(
                        "UNSAFE_MATERIALIZATION_PATH",
                        "materialization target escaped its approved root",
                        details={"path": relative.as_posix()},
                    )
                if not inside(staged, staging):
                    raise ContinuityError(
                        "UNSAFE_STAGING_PATH",
                        "staged file escaped its approved staging root",
                        details={"path": relative.as_posix()},
                    )
                if target.exists() and not target.is_file():
                    raise ContinuityError(
                        "TARGET_TYPE_CONFLICT",
                        "materialization target is not a regular file",
                        details={"path": relative.as_posix()},
                    )
                expected = file_row["expected_old_hash"]
                proposed = str(file_row["proposed_new_hash"])
                current = sha256_file(target) if target.is_file() else None
                already_activated = current == proposed
                if not already_activated and current != expected:
                    raise ContinuityError(
                        "TARGET_HASH_CONFLICT",
                        "target changed before activation",
                        details={
                            "path": relative.as_posix(),
                            "expected": expected,
                            "actual": current,
                        },
                    )
                if not already_activated and (
                    not staged.is_file()
                    or sha256_file(staged) != proposed
                ):
                    raise ContinuityError(
                        "STAGING_HASH_MISMATCH",
                        f"staged file unavailable: {relative.as_posix()}",
                    )
                prepared.append(
                    {
                        "relative": relative,
                        "target": target,
                        "staged": staged,
                        "expected": expected,
                        "proposed": proposed,
                        "already_activated": already_activated,
                        "recovered_applied": (
                            recovered_dead_claim
                            and already_activated
                            and expected != proposed
                        ),
                        # The approved baseline, not the current post-crash
                        # target, determines whether rollback needs a backup.
                        "old_existed": expected is not None,
                        "file_status": str(file_row["file_status"]),
                    }
                )

            # Prepare every backup and swap copy before replacing any target.
            backup_root_path.mkdir(parents=True, exist_ok=True)
            _assert_no_reparse_path(
                backup_root_path,
                anchor=project_root,
                code="UNSAFE_BACKUP_PATH",
                label="materialization backup run",
            )
            backup_root = backup_root_path.resolve()
            if not inside(backup_root, backup_base):
                raise ContinuityError(
                    "UNSAFE_BACKUP_PATH",
                    "materialization backup root changed during activation",
                    details={"backup_root": str(backup_root)},
                )
            for entry in prepared:
                self._assert_materialization_activation_owner(
                    run_id,
                    activation_owner,
                )
                if entry["already_activated"]:
                    if not entry["recovered_applied"]:
                        continue
                    relative = entry["relative"]
                    if entry["old_existed"]:
                        backup_path = backup_root_path / relative
                        _assert_no_reparse_path(
                            backup_path,
                            anchor=backup_root_path,
                            code="UNSAFE_BACKUP_PATH",
                            label="materialization recovery backup",
                        )
                        entry["backup"] = backup_path.resolve(strict=False)
                        # Treat a recovered swap as applied before validating
                        # its rollback evidence.  If the backup is missing or
                        # corrupt, the failure path marks this file
                        # ``rollback_failed`` instead of pretending the run
                        # was restored.
                        applied.append(entry)
                        try:
                            backup = _validated_materialization_backup(
                                backup_path,
                                anchor=backup_root_path,
                                expected_hash=str(entry["expected"]),
                                code=(
                                    "MATERIALIZATION_RECOVERY_EVIDENCE_MISSING"
                                ),
                                message=(
                                    "an activated file lacks its approved "
                                    "rollback backup"
                                ),
                            )
                        except ContinuityError as exc:
                            raise ContinuityError(
                                "MATERIALIZATION_RECOVERY_EVIDENCE_MISSING",
                                "an activated file lacks its approved rollback "
                                "backup",
                                details={
                                    "path": relative.as_posix(),
                                    "file_status": entry["file_status"],
                                    "backup_path": str(backup_path),
                                },
                            ) from exc
                        entry["backup"] = backup
                    else:
                        # A recovered create has no old bytes to preserve; its
                        # rollback action is an exact-hash-guarded unlink.
                        applied.append(entry)
                    continue
                relative = entry["relative"]
                target = entry["target"]
                staged = entry["staged"]
                if entry["old_existed"]:
                    backup_path = backup_root_path / relative
                    _assert_no_reparse_path(
                        backup_path,
                        anchor=backup_root_path,
                        code="UNSAFE_BACKUP_PATH",
                        label="materialization backup file",
                    )
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    _assert_no_reparse_path(
                        backup_path,
                        anchor=backup_root_path,
                        code="UNSAFE_BACKUP_PATH",
                        label="materialization backup file",
                    )
                    created_stat: os.stat_result | None = None
                    try:
                        try:
                            with (
                                target.open("rb") as source_stream,
                                backup_path.open("xb") as backup_stream,
                            ):
                                created_stat = os.fstat(backup_stream.fileno())
                                shutil.copyfileobj(source_stream, backup_stream)
                                backup_stream.flush()
                                os.fsync(backup_stream.fileno())
                        except FileExistsError:
                            pass
                        backup = _validated_materialization_backup(
                            backup_path,
                            anchor=backup_root_path,
                            expected_hash=str(entry["expected"]),
                            code="BACKUP_HASH_MISMATCH",
                            message=(
                                "materialization backup does not match the "
                                "approved old hash or is not private"
                            ),
                        )
                    except BaseException:
                        if created_stat is not None:
                            try:
                                current_stat = backup_path.stat(
                                    follow_symlinks=False
                                )
                            except OSError:
                                pass
                            else:
                                if os.path.samestat(
                                    created_stat,
                                    current_stat,
                                ):
                                    backup_path.unlink(missing_ok=True)
                        raise
                    entry["backup"] = backup
                swap = (
                    staged.parent
                    / f".{staged.name}.{run_id}.activation"
                ).resolve()
                if not inside(swap, staging):
                    raise ContinuityError(
                        "UNSAFE_STAGING_PATH",
                        "activation swap escaped its staging root",
                        details={"path": relative.as_posix()},
                    )
                if swap.exists():
                    if not swap.is_file():
                        raise ContinuityError(
                            "STAGING_TYPE_CONFLICT",
                            "activation swap is not a regular file",
                            details={"path": relative.as_posix()},
                        )
                    swap.unlink()
                shutil.copy2(staged, swap)
                swap_stat = swap.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(swap_stat.st_mode)
                    or int(getattr(swap_stat, "st_nlink", 1)) != 1
                    or sha256_file(swap) != entry["proposed"]
                ):
                    raise ContinuityError(
                        "STAGING_HASH_MISMATCH",
                        "activation swap is not a private regular file with "
                        "the approved output hash",
                        details={"path": relative.as_posix()},
                    )
                entry["swap"] = swap
                entry["swap_stat"] = swap_stat

            for entry in prepared:
                self._assert_materialization_activation_owner(
                    run_id,
                    activation_owner,
                )
                relative = entry["relative"]
                proposed = entry["proposed"]
                if entry["already_activated"]:
                    actual = proposed
                else:
                    # Re-resolve immediately before replace to catch a parent
                    # symlink or junction swapped in after staging/preflight.
                    target_lexical = root / relative
                    staged_lexical = staging / relative
                    _assert_no_reparse_path(
                        target_lexical,
                        anchor=root,
                        code="UNSAFE_MATERIALIZATION_PATH",
                        label="materialization target",
                    )
                    _assert_no_reparse_path(
                        staged_lexical,
                        anchor=staging,
                        code="UNSAFE_STAGING_PATH",
                        label="staged materialization file",
                    )
                    parent = target_lexical.parent
                    while parent != root and inside(parent, root):
                        if parent.exists():
                            break
                        created_directories.add(parent)
                        parent = parent.parent
                    target_lexical.parent.mkdir(parents=True, exist_ok=True)
                    _assert_no_reparse_path(
                        target_lexical,
                        anchor=root,
                        code="UNSAFE_MATERIALIZATION_PATH",
                        label="materialization target",
                    )
                    target = target_lexical.resolve()
                    staged = staged_lexical.resolve()
                    swap = Path(entry["swap"]).resolve()
                    _assert_no_reparse_path(
                        Path(entry["swap"]),
                        anchor=staging,
                        code="UNSAFE_STAGING_PATH",
                        label="activation swap",
                    )
                    if (
                        target != entry["target"]
                        or not inside(target, root)
                    ):
                        raise ContinuityError(
                            "UNSAFE_MATERIALIZATION_PATH",
                            "materialization target changed after preflight",
                            details={"path": relative.as_posix()},
                        )
                    if (
                        staged != entry["staged"]
                        or not inside(staged, staging)
                        or swap != entry["swap"]
                        or not inside(swap, staging)
                    ):
                        raise ContinuityError(
                            "UNSAFE_STAGING_PATH",
                            "staging path changed after preflight",
                            details={"path": relative.as_posix()},
                        )
                    current_swap_stat = swap.stat(follow_symlinks=False)
                    if (
                        not os.path.samestat(
                            entry["swap_stat"],
                            current_swap_stat,
                        )
                        or not stat.S_ISREG(current_swap_stat.st_mode)
                        or int(getattr(current_swap_stat, "st_nlink", 1)) != 1
                        or sha256_file(swap) != proposed
                    ):
                        raise ContinuityError(
                            "STAGING_HASH_MISMATCH",
                            "activation swap changed identity, link count, "
                            "or content after preflight",
                            details={"path": relative.as_posix()},
                        )
                    current = (
                        sha256_file(target) if target.is_file() else None
                    )
                    if current != entry["expected"]:
                        raise ContinuityError(
                            "TARGET_HASH_CONFLICT",
                            "target changed after activation preflight",
                            details={
                                "path": relative.as_posix(),
                                "expected": entry["expected"],
                                "actual": current,
                            },
                        )
                    os.replace(swap, target)
                    applied.append(entry)
                    if int(
                        getattr(
                            target.stat(follow_symlinks=False),
                            "st_nlink",
                            1,
                        )
                    ) != 1:
                        raise ContinuityError(
                            "ACTIVATED_FILE_HARDLINKED",
                            "activated target unexpectedly shares an inode",
                            details={"path": relative.as_posix()},
                        )
                    actual = sha256_file(target)
                    if actual != proposed:
                        raise ContinuityError(
                            "ACTIVATED_HASH_MISMATCH",
                            f"activated hash mismatch: {relative.as_posix()}",
                        )
                with self.store.transaction() as connection:
                    self._require_materialization_activation_claim(
                        connection,
                        run_id,
                        activation_owner,
                    )
                    cursor = connection.execute(
                        """
                        UPDATE materialization_files
                        SET actual_hash=?, file_status='activated'
                        WHERE run_id=? AND relative_path=?
                          AND EXISTS (
                              SELECT 1
                              FROM materialization_activation_claims
                              WHERE run_id=? AND owner_host=?
                                AND owner_pid=? AND owner_token=?
                          )
                        """,
                        (
                            actual,
                            run_id,
                            relative.as_posix(),
                            run_id,
                            owner_host,
                            owner_pid,
                            owner_token,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ContinuityError(
                            "MATERIALIZATION_ACTIVATION_OWNERSHIP_LOST",
                            "materialization file checkpoint lost its "
                            "activation claim",
                            details={
                                "run_id": run_id,
                                "path": relative.as_posix(),
                            },
                        )
                    self._journal_materialization(
                        connection,
                        run_id,
                        "file_activation",
                        "completed",
                        details={
                            "path": relative.as_posix(),
                            "content_hash": actual,
                        },
                    )

            awaiting_manifest = False
            with self.store.transaction() as connection:
                self._require_materialization_activation_claim(
                    connection,
                    run_id,
                    activation_owner,
                )
                manifest_rows = connection.execute(
                    """
                    SELECT * FROM accepted_source_manifest
                    WHERE commit_id=? AND manifest_status='pending'
                    """,
                    (commit_id,),
                ).fetchall()
                for row in manifest_rows:
                    metadata = dict(
                        _json_load(str(row["metadata_json"]), {})
                    )
                    source_path = Path(
                        str(
                            metadata.get("real_path")
                            or metadata.get("normalized_real_path")
                            or row["source_path"]
                        )
                    )
                    resolved = (
                        source_path.resolve()
                        if source_path.is_absolute()
                        else (root / source_path).resolve()
                    )
                    if not resolved.is_file():
                        continue
                    actual = sha256_file(resolved)
                    if actual != str(row["content_hash"]):
                        raise ContinuityError(
                            "SOURCE_MANIFEST_HASH_CONFLICT",
                            "accepted source hash does not match the activated file",
                            details={
                                "path": str(row["source_path"]),
                                "expected": row["content_hash"],
                                "actual": actual,
                            },
                        )
                    connection.execute(
                        """
                        UPDATE accepted_source_manifest
                        SET manifest_status='active', activated_at=?
                        WHERE manifest_entry_id=?
                        """,
                        (utc_now(), row["manifest_entry_id"]),
                    )
                pending_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM accepted_source_manifest
                        WHERE commit_id=? AND manifest_status='pending'
                        """,
                        (commit_id,),
                    ).fetchone()[0]
                )
                if pending_count:
                    awaiting_manifest = True
                    cursor = connection.execute(
                        """
                        UPDATE materialization_runs
                        SET run_status='awaiting_manifest', updated_at=?
                        WHERE run_id=?
                          AND EXISTS (
                              SELECT 1
                              FROM materialization_activation_claims
                              WHERE run_id=? AND owner_host=?
                                AND owner_pid=? AND owner_token=?
                          )
                        """,
                        (
                            utc_now(),
                            run_id,
                            run_id,
                            owner_host,
                            owner_pid,
                            owner_token,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ContinuityError(
                            "MATERIALIZATION_ACTIVATION_OWNERSHIP_LOST",
                            "manifest checkpoint lost its activation claim",
                            details={"run_id": run_id},
                        )
                    self._journal_materialization(
                        connection,
                        run_id,
                        "manifest_activation",
                        "pending",
                        {"pending_manifest_count": pending_count},
                    )
                    self._release_materialization_activation_claim(
                        connection,
                        run_id,
                        activation_owner,
                        required=True,
                    )
                else:
                    cursor = connection.execute(
                        """
                        UPDATE materialization_runs
                        SET run_status='ready', updated_at=?
                        WHERE run_id=?
                          AND EXISTS (
                              SELECT 1
                              FROM materialization_activation_claims
                              WHERE run_id=? AND owner_host=?
                                AND owner_pid=? AND owner_token=?
                          )
                        """,
                        (
                            utc_now(),
                            run_id,
                            run_id,
                            owner_host,
                            owner_pid,
                            owner_token,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ContinuityError(
                            "MATERIALIZATION_ACTIVATION_OWNERSHIP_LOST",
                            "completion checkpoint lost its activation claim",
                            details={"run_id": run_id},
                        )
                    self._journal_materialization(
                        connection,
                        run_id,
                        "activation",
                        "completed",
                        {"active_manifest_count": len(manifest_rows)},
                    )
                    replay_result = self.replay_engine.rebuild_in_transaction(
                        connection
                    )
                    receipt = {
                        "commit_id": commit_id,
                        "run_id": run_id,
                        "projection_hash": replay_result["projection_hash"],
                        "files": [
                            row["relative_path"] for row in status["files"]
                        ],
                    }
                    cursor = connection.execute(
                        """
                        UPDATE materialization_runs
                        SET run_status='completed',
                            completion_receipt_json=?, error='',
                            updated_at=?, completed_at=?
                        WHERE run_id=?
                          AND EXISTS (
                              SELECT 1
                              FROM materialization_activation_claims
                              WHERE run_id=? AND owner_host=?
                                AND owner_pid=? AND owner_token=?
                          )
                        """,
                        (
                            canonical_json(receipt),
                            utc_now(),
                            utc_now(),
                            run_id,
                            run_id,
                            owner_host,
                            owner_pid,
                            owner_token,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ContinuityError(
                            "MATERIALIZATION_ACTIVATION_OWNERSHIP_LOST",
                            "completion receipt lost its activation claim",
                            details={"run_id": run_id},
                        )
                    self._journal_materialization(
                        connection,
                        run_id,
                        "completion_receipt",
                        "completed",
                        receipt,
                    )
                    self._release_materialization_activation_claim(
                        connection,
                        run_id,
                        activation_owner,
                        required=True,
                    )
        except Exception as exc:
            if (
                isinstance(exc, ContinuityError)
                and exc.code
                == "MATERIALIZATION_ACTIVATION_OWNERSHIP_LOST"
            ):
                raise
            try:
                self._assert_materialization_activation_owner(
                    run_id,
                    activation_owner,
                )
            except ContinuityError as ownership_exc:
                if (
                    ownership_exc.code
                    == "MATERIALIZATION_ACTIVATION_OWNERSHIP_LOST"
                ):
                    raise ownership_exc from exc
                raise
            rollback_errors: list[dict[str, str]] = []
            for entry in reversed(applied):
                self._assert_materialization_activation_owner(
                    run_id,
                    activation_owner,
                )
                relative = entry["relative"]
                try:
                    target_lexical = root / relative
                    _assert_no_reparse_path(
                        target_lexical,
                        anchor=root,
                        code="UNSAFE_MATERIALIZATION_PATH",
                        label="materialization rollback target",
                    )
                    target = target_lexical.resolve()
                    if (
                        not inside(target, root)
                        or target != entry["target"]
                    ):
                        raise RuntimeError(
                            "target changed identity during rollback"
                        )
                    current = (
                        sha256_file(target) if target.is_file() else None
                    )
                    if current != entry["proposed"]:
                        raise RuntimeError(
                            "target changed after activation; rollback CAS failed"
                        )
                    if entry["old_existed"]:
                        backup_lexical = Path(entry["backup"])
                        backup = _validated_materialization_backup(
                            backup_lexical,
                            anchor=backup_root,
                            expected_hash=str(entry["expected"]),
                            code="MATERIALIZATION_ROLLBACK_EVIDENCE_INVALID",
                            message=(
                                "approved backup is unavailable, corrupt, "
                                "or hard-linked"
                            ),
                        )
                        if backup != Path(entry["backup"]):
                            raise RuntimeError(
                                "approved backup changed identity during rollback"
                            )
                        _assert_no_reparse_path(
                            target_lexical,
                            anchor=root,
                            code="UNSAFE_MATERIALIZATION_PATH",
                            label="materialization rollback target",
                        )
                        self._assert_materialization_activation_owner(
                            run_id,
                            activation_owner,
                        )
                        os.replace(backup, target)
                        if int(
                            getattr(
                                target.stat(follow_symlinks=False),
                                "st_nlink",
                                1,
                            )
                        ) != 1:
                            raise RuntimeError(
                                "restored target unexpectedly shares an inode"
                            )
                        restored_hash = sha256_file(target)
                        if restored_hash != entry["expected"]:
                            raise RuntimeError(
                                "restored target hash does not match old hash"
                            )
                        rollback_states[relative.as_posix()] = (
                            restored_hash,
                            "rolled_back",
                        )
                    else:
                        _assert_no_reparse_path(
                            target_lexical,
                            anchor=root,
                            code="UNSAFE_MATERIALIZATION_PATH",
                            label="materialization rollback target",
                        )
                        self._assert_materialization_activation_owner(
                            run_id,
                            activation_owner,
                        )
                        target.unlink()
                        rollback_states[relative.as_posix()] = (
                            None,
                            "rolled_back",
                        )
                except Exception as rollback_exc:
                    if (
                        isinstance(rollback_exc, ContinuityError)
                        and rollback_exc.code
                        == "MATERIALIZATION_ACTIVATION_OWNERSHIP_LOST"
                    ):
                        raise
                    rollback_states[relative.as_posix()] = (
                        None,
                        "rollback_failed",
                    )
                    rollback_errors.append(
                        {
                            "path": relative.as_posix(),
                            "error": str(rollback_exc),
                        }
                    )

            for entry in prepared:
                swap = entry.get("swap")
                if not swap:
                    continue
                self._assert_materialization_activation_owner(
                    run_id,
                    activation_owner,
                )
                try:
                    swap_path = Path(swap)
                    if swap_path.is_file():
                        self._assert_materialization_activation_owner(
                            run_id,
                            activation_owner,
                        )
                        swap_path.unlink(missing_ok=True)
                except Exception as cleanup_exc:
                    if (
                        isinstance(cleanup_exc, ContinuityError)
                        and cleanup_exc.code
                        == "MATERIALIZATION_ACTIVATION_OWNERSHIP_LOST"
                    ):
                        raise
                    rollback_errors.append(
                        {
                            "path": entry["relative"].as_posix(),
                            "error": f"swap cleanup failed: {cleanup_exc}",
                        }
                    )
            for directory in sorted(
                created_directories,
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                self._assert_materialization_activation_owner(
                    run_id,
                    activation_owner,
                )
                try:
                    if not directory.exists():
                        continue
                    resolved_directory = directory.resolve()
                    if (
                        not inside(resolved_directory, root)
                        or _is_reparse_path(directory)
                    ):
                        raise RuntimeError(
                            "created directory escaped root during rollback"
                        )
                    self._assert_materialization_activation_owner(
                        run_id,
                        activation_owner,
                    )
                    directory.rmdir()
                except OSError as cleanup_exc:
                    if directory.exists() and any(directory.iterdir()):
                        continue
                    rollback_errors.append(
                        {
                            "path": str(directory),
                            "error": f"directory cleanup failed: {cleanup_exc}",
                        }
                    )
                except Exception as cleanup_exc:
                    if (
                        isinstance(cleanup_exc, ContinuityError)
                        and cleanup_exc.code
                        == "MATERIALIZATION_ACTIVATION_OWNERSHIP_LOST"
                    ):
                        raise
                    rollback_errors.append(
                        {
                            "path": str(directory),
                            "error": f"directory cleanup failed: {cleanup_exc}",
                        }
                    )

            failure = (
                exc.as_dict()
                if isinstance(exc, ContinuityError)
                else {
                    "ok": False,
                    "code": "MATERIALIZATION_ACTIVATION_FAILED",
                    "message": str(exc),
                    "details": {},
                }
            )
            failure["rollback_errors"] = rollback_errors
            try:
                with self.store.transaction() as connection:
                    self._require_materialization_activation_claim(
                        connection,
                        run_id,
                        activation_owner,
                    )
                    for relative_path, (
                        actual_hash,
                        file_status,
                    ) in rollback_states.items():
                        cursor = connection.execute(
                            """
                            UPDATE materialization_files
                            SET actual_hash=?, file_status=?
                            WHERE run_id=? AND relative_path=?
                              AND EXISTS (
                                  SELECT 1
                                  FROM materialization_activation_claims
                                  WHERE run_id=? AND owner_host=?
                                    AND owner_pid=? AND owner_token=?
                              )
                            """,
                            (
                                actual_hash,
                                file_status,
                                run_id,
                                relative_path,
                                run_id,
                                owner_host,
                                owner_pid,
                                owner_token,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise ContinuityError(
                                "MATERIALIZATION_ACTIVATION_OWNERSHIP_LOST",
                                "rollback checkpoint lost its activation claim",
                                details={
                                    "run_id": run_id,
                                    "path": relative_path,
                                },
                            )
                    cursor = connection.execute(
                        """
                        UPDATE materialization_runs
                        SET run_status='failed', error=?,
                            updated_at=?, completed_at=NULL
                        WHERE run_id=?
                          AND EXISTS (
                              SELECT 1
                              FROM materialization_activation_claims
                              WHERE run_id=? AND owner_host=?
                                AND owner_pid=? AND owner_token=?
                          )
                        """,
                        (
                            canonical_json(failure),
                            utc_now(),
                            run_id,
                            run_id,
                            owner_host,
                            owner_pid,
                            owner_token,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ContinuityError(
                            "MATERIALIZATION_ACTIVATION_OWNERSHIP_LOST",
                            "failure checkpoint lost its activation claim",
                            details={"run_id": run_id},
                        )
                    self._journal_materialization(
                        connection,
                        run_id,
                        "rollback",
                        "failed" if rollback_errors else "completed",
                        {
                            "restored_files": sorted(rollback_states),
                            "errors": rollback_errors,
                        },
                    )
                    self._journal_materialization(
                        connection,
                        run_id,
                        "activation",
                        "failed",
                        failure,
                    )
                    self._release_materialization_activation_claim(
                        connection,
                        run_id,
                        activation_owner,
                        required=True,
                    )
            except Exception as journal_exc:
                if (
                    isinstance(journal_exc, ContinuityError)
                    and journal_exc.code
                    == "MATERIALIZATION_ACTIVATION_OWNERSHIP_LOST"
                ):
                    raise journal_exc from exc
                failure["journal_error"] = str(journal_exc)

            if rollback_errors:
                raise ContinuityError(
                    "MATERIALIZATION_ROLLBACK_FAILED",
                    "activation failed and one or more files could not be restored",
                    details=failure,
                ) from exc
            if isinstance(exc, ContinuityError):
                raise
            raise ContinuityError(
                "MATERIALIZATION_ACTIVATION_FAILED",
                "materialization activation failed and was rolled back",
                details=failure,
            ) from exc
        finally:
            try:
                with self.store.transaction() as connection:
                    self._release_materialization_activation_claim(
                        connection,
                        run_id,
                        activation_owner,
                        required=False,
                    )
            except Exception:
                pass

        if awaiting_manifest:
            return self.materialization_status(commit_id)
        return self.materialization_status(commit_id)


class HostApprovalAuthority:
    """Host-only approval issuer.

    This class is intentionally separate from ``ContinuityService`` so hook,
    MCP, and ordinary lifecycle clients can be given the service object
    without receiving any grant-issuing method.  The host keeps the returned
    opaque approval token; only its hash is inserted into SQLite.
    """

    def __init__(
        self,
        service: ContinuityService,
        *,
        issuer: str,
        channel: str = "interactive_host",
    ) -> None:
        if not issuer.strip():
            raise ContinuityError(
                "APPROVAL_ISSUER_REQUIRED", "host issuer identity is required"
            )
        self.service = service
        self.issuer = issuer
        self.channel = channel

    def issue(
        self,
        proposal_id: str,
        *,
        expected_canon_revision: int,
        operations: Sequence[str] = ("accept",),
        expires_in_seconds: int = 300,
        target_project_real_path: str | Path | None = None,
        authorized_paths: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        expected_canon_revision = validate_positive_int(
            expected_canon_revision,
            "expected_canon_revision",
            allow_none=False,
            minimum=0,
        )
        if type(expires_in_seconds) is not int or expires_in_seconds <= 0:
            raise ContinuityError(
                "INVALID_GRANT_EXPIRY",
                "approval expiry must be a positive integer",
            )
        allowed_operations = {
            "accept",
            "accept_initialization",
            "accept_power_spec",
            SOURCE_MANIFEST_ACCEPT_OPERATION,
            "materialize",
            "retract",
        }
        requested_operations = tuple(dict.fromkeys(operations))
        if not requested_operations or not set(requested_operations).issubset(
            allowed_operations
        ):
            raise ContinuityError(
                "INVALID_APPROVAL_OPERATION",
                "grant contains an unsupported operation",
                details={"operations": list(requested_operations)},
            )
        active_lifecycle_checks = "retract" not in requested_operations
        event_experience_service = (
            self.service._preflight_event_experience_service(
                proposal_id,
                active_checks=active_lifecycle_checks,
            )
        )
        with self.service.store.transaction() as connection:
            proposal = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if proposal is None:
                raise ContinuityError(
                    "PROPOSAL_NOT_FOUND", f"unknown proposal: {proposal_id}"
                )
            payload, _verified_events = (
                self.service._strict_proposal_content(proposal)
            )
            if "retract" in requested_operations:
                if str(proposal["canon_status"]) != "accepted":
                    raise ContinuityError(
                        "INVALID_PROPOSAL_TRANSITION",
                        "retract grants require an accepted proposal",
                    )
            elif str(proposal["canon_status"]) != "proposed":
                raise ContinuityError(
                    "INVALID_PROPOSAL_TRANSITION",
                    "accept grants require a proposed proposal",
                )
            proposal_kind = str(proposal["proposal_kind"])
            initialization_operations = {
                "accept_initialization",
                "materialize",
            }
            power_spec_operations = {"accept_power_spec"}
            source_manifest_operations = {
                SOURCE_MANIFEST_ACCEPT_OPERATION,
            }
            if (
                set(requested_operations) & initialization_operations
                and proposal_kind != "initialization_bundle"
            ):
                raise ContinuityError(
                    "APPROVAL_OPERATION_SCOPE_MISMATCH",
                    "initialization operations require an initialization proposal",
                    details={"proposal_kind": proposal_kind},
                )
            if (
                proposal_kind == "initialization_bundle"
                and "accept" in requested_operations
                and "accept_initialization" not in requested_operations
            ):
                raise ContinuityError(
                    "APPROVAL_OPERATION_SCOPE_MISMATCH",
                    "initialization acceptance requires accept_initialization",
                    details={"proposal_kind": proposal_kind},
                )
            if (
                set(requested_operations) & power_spec_operations
                and proposal_kind != "power_spec_change"
            ):
                raise ContinuityError(
                    "APPROVAL_OPERATION_SCOPE_MISMATCH",
                    "accept_power_spec requires a power_spec_change proposal",
                    details={"proposal_kind": proposal_kind},
                )
            if (
                proposal_kind == "power_spec_change"
                and "accept" in requested_operations
                and "accept_power_spec" not in requested_operations
            ):
                raise ContinuityError(
                    "APPROVAL_OPERATION_SCOPE_MISMATCH",
                    "power specification acceptance requires accept_power_spec",
                    details={"proposal_kind": proposal_kind},
                )
            if (
                set(requested_operations) & source_manifest_operations
                and proposal_kind != SOURCE_MANIFEST_PROPOSAL_KIND
            ):
                raise ContinuityError(
                    "APPROVAL_OPERATION_SCOPE_MISMATCH",
                    "accept_source_manifest requires a source manifest proposal",
                    details={"proposal_kind": proposal_kind},
                )
            if (
                proposal_kind == SOURCE_MANIFEST_PROPOSAL_KIND
                and "accept" in requested_operations
                and SOURCE_MANIFEST_ACCEPT_OPERATION
                not in requested_operations
            ):
                raise ContinuityError(
                    "APPROVAL_OPERATION_SCOPE_MISMATCH",
                    "source manifest acceptance requires accept_source_manifest",
                    details={"proposal_kind": proposal_kind},
                )
            active = self.service.store.get_meta_int(
                connection, "active_canon_revision"
            )
            if active != expected_canon_revision:
                raise ContinuityError(
                    "CANON_REVISION_CONFLICT",
                    "grant must bind the current active canon revision",
                    details={
                        "expected": expected_canon_revision,
                        "actual": active,
                    },
                )
            if (
                "retract" not in requested_operations
                and int(proposal["prepared_canon_revision"])
                != expected_canon_revision
            ):
                raise ContinuityError(
                    "PREPARED_CANON_REVISION_STALE",
                    "proposal was prepared against an older canon revision",
                    details={
                        "prepared": int(
                            proposal["prepared_canon_revision"]
                        ),
                        "active": active,
                    },
                )
            lifecycle_binding = (
                self.service._proposal_lifecycle_binding(
                    proposal,
                    payload,
                )
            )
            if lifecycle_binding and active_lifecycle_checks:
                self.service._validate_receipt_lifecycle_binding(
                    connection,
                    proposal,
                    payload,
                    lifecycle_binding,
                    allow_assistant_bind=False,
                )
                self.service._validate_active_projection_binding(
                    connection,
                    lifecycle_binding,
                )
                self.service._validate_event_experience_binding(
                    connection,
                    lifecycle_binding,
                    event_experience_service,
                    _verified_events,
                )
            bundle = dict(payload.get("bundle") or {})
            package = dict(payload.get("lifecycle_package") or {})
            target = str(
                target_project_real_path
                or package.get("target_project_real_path")
                or bundle.get("target_project_real_path")
                or payload.get("target_project_real_path")
                or self.service.store.project_root
            )
            materialization_plan = dict(
                package.get("materialization_plan") or {}
            )
            authorized_artifacts = [
                dict(item)
                for item in (
                    materialization_plan.get("artifacts")
                    or materialization_plan.get("files")
                    or bundle.get("artifact_manifest")
                    or []
                )
                if isinstance(item, Mapping) and item.get("path")
            ]
            target_old_new_hashes = [
                {
                    "path": str(item["path"]),
                    "expected_old_hash": item.get("expected_old_hash"),
                    "proposed_new_hash": item.get("proposed_new_hash")
                    or item.get("new_hash"),
                }
                for item in authorized_artifacts
            ]
            if authorized_paths is None:
                derived_authorized_paths = [
                    str(item.get("path"))
                    for item in authorized_artifacts
                ]
            else:
                derived_authorized_paths = list(authorized_paths)
            expires_at = (
                datetime.now(timezone.utc)
                + timedelta(seconds=expires_in_seconds)
            ).isoformat(timespec="microseconds").replace("+00:00", "Z")
            binding = {
                "proposal_id": str(proposal["proposal_id"]),
                "package_hash": package.get("package_hash")
                or bundle.get("package_hash")
                or bundle.get("bundle_hash")
                or payload.get("package_hash")
                or proposal["payload_hash"],
                "artifact_stage": str(proposal["artifact_stage"]),
                "branch_id": str(proposal["branch_id"]),
                "chapter_no": proposal["chapter_no"],
                "artifact_revision": int(proposal["artifact_revision"]),
                "prepared_canon_revision": int(
                    proposal["prepared_canon_revision"]
                ),
                "payload_hash": str(proposal["payload_hash"]),
                "target_project_real_path": str(Path(target).resolve()),
                "source_manifest_hash": stable_hash(
                    package.get("source_manifest")
                    or bundle.get("source_manifest")
                    or [],
                    prefix="source_manifest_",
                ),
                "target_old_new_hashes": target_old_new_hashes,
                "authorized_operations": list(requested_operations),
                "authorized_paths": derived_authorized_paths,
                "expected_canon_revision": expected_canon_revision,
                "issuer": self.issuer,
                "channel": self.channel,
                "expires_at": expires_at,
            }
            if (
                proposal_kind == SOURCE_MANIFEST_PROPOSAL_KIND
                and SOURCE_MANIFEST_ACCEPT_OPERATION in requested_operations
            ):
                validate_source_manifest_proposal_envelope(
                    connection,
                    proposal,
                    payload,
                    _verified_events,
                )
                manifest_change = dict(
                    payload.get("source_manifest_change") or {}
                )
                validate_frozen_manifest_change(
                    connection,
                    self.service.store.project_root,
                    manifest_change,
                )
                binding.update(
                    {
                        "source_manifest_plan_hash": manifest_change.get(
                            "plan_hash"
                        ),
                        "source_manifest_base_hash": manifest_change.get(
                            "base_manifest_hash"
                        ),
                        "source_manifest_target_hash": manifest_change.get(
                            "target_manifest_hash"
                        ),
                    }
                )
            if (
                proposal_kind == SOURCE_MANIFEST_PROPOSAL_KIND
                and "retract" in requested_operations
            ):
                if (
                    latest_active_manifest_proposal_id(connection)
                    != proposal_id
                ):
                    raise ContinuityError(
                        "SOURCE_MANIFEST_RETRACT_NOT_LATEST",
                        "only the current source manifest migration may be retracted",
                        details={"proposal_id": proposal_id},
                    )
                validate_source_manifest_retract_files(
                    self.service.store.project_root,
                    dict(
                        payload.get("source_manifest_change")
                        or {}
                    ),
                )
            if lifecycle_binding:
                binding.update(lifecycle_binding)
            if active_lifecycle_checks:
                self.service._validate_initialization_target_paths(
                    proposal,
                    binding,
                )
            binding_hash = stable_hash(binding, prefix="grant_binding_")
            approval_id = secrets.token_urlsafe(32)
            token_hash = stable_hash(
                approval_id, prefix="grant_token_"
            )
            connection.execute(
                """
                INSERT INTO approval_grants(
                    token_hash, proposal_id, binding_hash, binding_json,
                    authorized_operations_json, expected_canon_revision,
                    issuer, channel, expires_at, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    proposal_id,
                    binding_hash,
                    canonical_json(binding),
                    canonical_json(list(requested_operations)),
                    expected_canon_revision,
                    self.issuer,
                    self.channel,
                    expires_at,
                    utc_now(),
                ),
            )
            return {
                "approval_id": approval_id,
                "proposal_id": proposal_id,
                "binding_hash": binding_hash,
                "expected_canon_revision": expected_canon_revision,
                "operations": list(requested_operations),
                "expires_at": expires_at,
                "issuer": self.issuer,
                "channel": self.channel,
            }
