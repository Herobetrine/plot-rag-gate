"""Accepted source-manifest migration and deterministic active projection.

The initialization completion receipt is immutable.  Later source enrollment,
correction, or deactivation therefore travels through the normal immutable
proposal -> host grant -> canon CAS -> accepted commit lifecycle.  This module
contains the deterministic, local-only plan validator and the replayable
projection over ``accepted_source_manifest``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .store import sha256_file, utc_now
from .validators import (
    ContinuityError,
    canonical_json,
    normalize_source_role,
    stable_hash,
    validate_positive_int,
)


SOURCE_MANIFEST_PLAN_SCHEMA = "plot-rag-source-manifest-migration-plan/v1"
SOURCE_MANIFEST_CHANGE_SCHEMA = "plot-rag-source-manifest/v1"
SOURCE_MANIFEST_PROPOSAL_KIND = "source_manifest_change"
SOURCE_MANIFEST_ARTIFACT_ID = "plot_rag_source_manifest"
SOURCE_MANIFEST_ARTIFACT_KIND = "source_manifest"
SOURCE_MANIFEST_ARTIFACT_STAGE = "bootstrap"
SOURCE_MANIFEST_BRANCH_ID = "main"
SOURCE_MANIFEST_SOURCE_ROLE = "setting"
SOURCE_MANIFEST_ACCEPT_OPERATION = "accept_source_manifest"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _json_load(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    return json.loads(value)


def normalize_source_path(value: Any) -> str:
    """Return a stable project-relative POSIX path."""

    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        raise ContinuityError(
            "SOURCE_MANIFEST_PATH_MISSING",
            "source manifest path is required",
        )
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        raise ContinuityError(
            "SOURCE_MANIFEST_PATH_UNSAFE",
            "source manifest path must stay inside the project",
            details={"path": raw},
        )
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if not parts or ":" in parts[0]:
        raise ContinuityError(
            "SOURCE_MANIFEST_PATH_UNSAFE",
            "source manifest path must be project-relative",
            details={"path": raw},
        )
    return PurePosixPath(*parts).as_posix()


def _hash(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise ContinuityError(
            "SOURCE_MANIFEST_HASH_INVALID",
            f"{field} must be a lowercase SHA-256 digest",
            details={"field": field, "value": value},
        )
    return normalized


def _metadata(source: Mapping[str, Any]) -> dict[str, Any]:
    explicit = source.get("metadata")
    metadata = dict(explicit) if isinstance(explicit, Mapping) else {}
    for key, value in source.items():
        if key not in {
            "source_path",
            "path",
            "content_hash",
            "sha256",
            "source_role",
            "source_id",
            "manifest_entry_id",
            "metadata",
        }:
            metadata[key] = value
    return metadata


def normalize_target_source(source: Mapping[str, Any]) -> dict[str, Any]:
    path = normalize_source_path(source.get("source_path") or source.get("path"))
    content_hash = _hash(
        source.get("content_hash") or source.get("sha256"),
        field=f"{path}.content_hash",
    )
    role = normalize_source_role(source.get("source_role"))
    metadata = _metadata(source)
    identity = {
        "path": path,
        "content_hash": content_hash,
        "source_role": role,
    }
    source_id = str(source.get("source_id") or "").strip() or stable_hash(
        ["source_manifest_version", identity],
        prefix="source_",
    )
    return {
        **identity,
        "source_id": source_id,
        "metadata": metadata,
    }


def _plan_source(source: Mapping[str, Any]) -> dict[str, Any]:
    """Return the user-controlled source fields frozen into a plan."""

    normalized = normalize_target_source(source)
    return {
        "source_path": normalized["path"],
        "content_hash": normalized["content_hash"],
        "source_role": normalized["source_role"],
        "metadata": dict(normalized["metadata"]),
    }


def _exact_string_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ContinuityError(
            "SOURCE_MANIFEST_PLAN_FIELD_INVALID",
            f"{field} must be an array",
            details={"field": field},
        )
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ContinuityError(
                "SOURCE_MANIFEST_PLAN_FIELD_INVALID",
                f"{field} must contain non-empty strings",
                details={"field": field, "index": index},
            )
        result.append(item.strip())
    return result


def _row_descriptor(row: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(row)
    keys = set(data)
    metadata_value = data.get("metadata_json") if "metadata_json" in keys else None
    metadata = (
        _json_load(str(metadata_value), {})
        if metadata_value is not None
        else dict(data.get("metadata") or {})
    )
    return {
        "manifest_entry_id": str(data["manifest_entry_id"]),
        "commit_id": str(data["commit_id"]),
        "source_id": str(data["source_id"]),
        "path": normalize_source_path(
            data.get("source_path") if "source_path" in keys else data.get("path")
        ),
        "content_hash": _hash(
            data.get("content_hash"),
            field="accepted_source_manifest.content_hash",
        ),
        "source_role": normalize_source_role(data.get("source_role")),
        "status": str(
            data.get("manifest_status")
            if "manifest_status" in keys
            else data.get("status")
            or ""
        ),
        "metadata": metadata,
        "activated_at": (
            data.get("activated_at") if "activated_at" in keys else None
        ),
        "created_at": data.get("created_at") if "created_at" in keys else None,
    }


def read_manifest_rows(
    connection: sqlite3.Connection,
    *,
    status: str | None = None,
) -> list[dict[str, Any]]:
    where = "WHERE manifest_status=?" if status else ""
    params: tuple[Any, ...] = (status,) if status else ()
    rows = connection.execute(
        f"""
        SELECT manifest_entry_id, commit_id, source_id, source_path,
               content_hash, source_role, manifest_status, metadata_json,
               activated_at, created_at
        FROM accepted_source_manifest
        {where}
        ORDER BY source_path, created_at, manifest_entry_id
        """,
        params,
    ).fetchall()
    return [_row_descriptor(row) for row in rows]


def effective_active_manifest(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Choose one active version per path without mutating the ledger.

    A legacy initialization bug could leave duplicate rows active.  Before a
    migration commit exists, the oldest active row wins deterministically.  An
    accepted migration replay leaves exactly one row active, so this selector is
    also the long-form reader's stable view.
    """

    selected: dict[str, dict[str, Any]] = {}
    for row in read_manifest_rows(connection, status="active"):
        selected.setdefault(str(row["path"]).casefold(), row)
    return [
        selected[path]
        for path in sorted(selected, key=lambda value: value.casefold())
    ]


def current_manifest_snapshot(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    """Read the active, still-accepted manifest view for authority indexing."""

    history_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM accepted_source_manifest"
        ).fetchone()[0]
    )
    active_migration = _active_manifest_migration(connection)
    if active_migration is not None:
        payload = _json_load(str(active_migration["payload_json"]), {})
        migration = dict(payload.get("source_manifest_change") or {})
        frozen = [
            dict(item)
            for item in migration.get("result_manifest") or []
            if isinstance(item, Mapping)
        ]
        frozen_ids = [
            str(item.get("manifest_entry_id") or "") for item in frozen
        ]
        if (
            "" in frozen_ids
            or len(frozen_ids) != len(set(frozen_ids))
            or not frozen_ids
        ):
            raise ContinuityError(
                "SOURCE_MANIFEST_CURRENT_TARGET_INVALID",
                "active source manifest migration has an invalid target",
            )
        placeholders = ",".join("?" for _ in frozen_ids)
        rows = connection.execute(
            f"""
            SELECT manifest_entry_id, commit_id, source_id, source_path,
                   content_hash, source_role, manifest_status, metadata_json,
                   activated_at, created_at
            FROM accepted_source_manifest
            WHERE manifest_entry_id IN ({placeholders})
            """,
            tuple(frozen_ids),
        ).fetchall()
        actual_by_id = {
            str(row["manifest_entry_id"]): _row_descriptor(row) for row in rows
        }
        entries: list[dict[str, Any]] = []
        for expected in frozen:
            entry_id = str(expected["manifest_entry_id"])
            actual = actual_by_id.get(entry_id)
            if actual is None:
                raise ContinuityError(
                    "SOURCE_MANIFEST_CURRENT_ENTRY_MISSING",
                    "active source manifest target references a missing ledger row",
                    details={"manifest_entry_id": entry_id},
                )
            expected_descriptor = {
                "manifest_entry_id": entry_id,
                "source_id": str(expected.get("source_id") or ""),
                "path": normalize_source_path(expected.get("path")),
                "content_hash": _hash(
                    expected.get("content_hash"),
                    field="current_manifest.content_hash",
                ),
                "source_role": normalize_source_role(
                    expected.get("source_role")
                ),
                "metadata": dict(expected.get("metadata") or {}),
            }
            actual_descriptor = {
                key: actual[key] for key in expected_descriptor
            }
            if (
                actual.get("status") != "active"
                or expected_descriptor != actual_descriptor
            ):
                raise ContinuityError(
                    "SOURCE_MANIFEST_CURRENT_ENTRY_MISMATCH",
                    "active source manifest target differs from its ledger row",
                    details={
                        "manifest_entry_id": entry_id,
                        "expected": expected_descriptor,
                        "actual": {
                            **actual_descriptor,
                            "status": actual.get("status"),
                        },
                    },
                )
            entries.append(actual)
        path_keys = [str(item["path"]).casefold() for item in entries]
        if len(path_keys) != len(set(path_keys)):
            raise ContinuityError(
                "SOURCE_MANIFEST_CURRENT_DUPLICATE_PATH",
                "active source manifest target contains duplicate paths",
            )
        entries.sort(key=lambda item: str(item["path"]).casefold())
        return {
            "managed": True,
            "history_count": history_count,
            "entries": entries,
        }

    rows = connection.execute(
        """
        SELECT m.manifest_entry_id, m.commit_id, m.source_id, m.source_path,
               m.content_hash, m.source_role, m.manifest_status,
               m.metadata_json, m.activated_at, m.created_at,
               c.active_revision_after, c.head_revision_after,
               p.artifact_revision
        FROM accepted_source_manifest AS m
        JOIN canon_commits AS c ON c.commit_id=m.commit_id
        JOIN proposals AS p ON p.proposal_id=c.proposal_id
        JOIN artifacts AS a
          ON a.artifact_version_id=p.artifact_version_id
        WHERE m.manifest_status='active'
          AND c.operation='accept'
          AND c.changes_authority=1
          AND p.canon_status='accepted'
          AND a.active=1
        ORDER BY m.source_path,
                 c.active_revision_after DESC,
                 c.head_revision_after DESC,
                 p.artifact_revision DESC,
                 c.commit_id DESC,
                 m.manifest_entry_id DESC
        """
    ).fetchall()
    selected: dict[str, tuple[dict[str, Any], tuple[int, int, int]]] = {}
    for raw in rows:
        row = _row_descriptor(raw)
        key = str(row["path"]).casefold()
        rank = (
            int(raw["active_revision_after"]),
            int(raw["head_revision_after"]),
            int(raw["artifact_revision"]),
        )
        existing = selected.get(key)
        if existing is None:
            selected[key] = (row, rank)
            continue
        current, current_rank = existing
        comparable = {
            field: row[field]
            for field in (
                "source_id",
                "path",
                "content_hash",
                "source_role",
                "metadata",
            )
        }
        current_comparable = {
            field: current[field]
            for field in comparable
        }
        if (
            rank == current_rank
            and canonical_json(comparable)
            != canonical_json(current_comparable)
        ):
            raise ContinuityError(
                "SOURCE_MANIFEST_CURRENT_VERSION_AMBIGUOUS",
                "accepted manifest contains conflicting active versions at one canon rank",
                details={
                    "path": row["path"],
                    "left": current["manifest_entry_id"],
                    "right": row["manifest_entry_id"],
                },
            )
    entries = [
        selected[key][0]
        for key in sorted(selected, key=lambda value: value.casefold())
    ]
    return {
        "managed": history_count > 0,
        "history_count": history_count,
        "entries": entries,
    }


def manifest_state_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    payload = [
        {
            "manifest_entry_id": str(row.get("manifest_entry_id") or ""),
            "commit_id": str(row.get("commit_id") or ""),
            "source_id": str(row.get("source_id") or ""),
            "path": normalize_source_path(row.get("path") or row.get("source_path")),
            "content_hash": _hash(
                row.get("content_hash"),
                field="manifest_state.content_hash",
            ),
            "source_role": normalize_source_role(row.get("source_role")),
            "status": str(row.get("status") or row.get("manifest_status") or ""),
            "metadata": dict(row.get("metadata") or {}),
        }
        for row in rows
    ]
    payload.sort(
        key=lambda item: (
            item["path"],
            item["manifest_entry_id"],
            item["content_hash"],
        )
    )
    return stable_hash(payload, prefix="source_manifest_state_")


def _target_manifest_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    payload = [
        {
            "path": normalize_source_path(row.get("path") or row.get("source_path")),
            "content_hash": _hash(
                row.get("content_hash"),
                field="target_manifest.content_hash",
            ),
            "source_role": normalize_source_role(row.get("source_role")),
            "source_id": str(row.get("source_id") or ""),
            "manifest_entry_id": str(row.get("manifest_entry_id") or ""),
            "metadata": dict(row.get("metadata") or {}),
        }
        for row in rows
    ]
    payload.sort(key=lambda item: item["path"])
    return stable_hash(payload, prefix="source_manifest_target_")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _verify_target_file(root: Path, source: Mapping[str, Any]) -> dict[str, Any]:
    relative = normalize_source_path(source.get("path") or source.get("source_path"))
    candidate = (root / Path(relative)).resolve(strict=False)
    inside = _inside(candidate, root)
    exists = inside and candidate.is_file()
    actual = sha256_file(candidate) if exists else ""
    expected = _hash(
        source.get("content_hash"),
        field=f"{relative}.content_hash",
    )
    matches = bool(exists) and actual == expected
    return {
        "path": relative,
        "inside_project": inside,
        "exists": exists,
        "expected_sha256": expected,
        "actual_sha256": actual or None,
        "matches": matches,
    }


def verify_target_files(
    project_root: Path | str,
    target_sources: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    root = Path(project_root).expanduser().resolve()
    results = [_verify_target_file(root, source) for source in target_sources]
    failures = [item for item in results if not item["matches"]]
    if failures:
        raise ContinuityError(
            "SOURCE_MANIFEST_TARGET_HASH_MISMATCH",
            "target source bytes do not match the frozen manifest plan",
            details={"files": failures},
        )
    return results


def validate_source_manifest_retract_files(
    project_root: Path | str,
    migration: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Require the frozen pre-migration bytes before retraction."""

    raw_base = migration.get("base_manifest")
    if not isinstance(raw_base, list):
        raise ContinuityError(
            "SOURCE_MANIFEST_RETRACT_BASE_INVALID",
            "source manifest retraction requires a frozen base manifest",
        )
    base = [
        dict(item)
        for item in raw_base
        if isinstance(item, Mapping)
    ]
    if len(base) != len(raw_base):
        raise ContinuityError(
            "SOURCE_MANIFEST_RETRACT_BASE_INVALID",
            "source manifest retraction base must contain source objects",
        )
    root = Path(project_root).expanduser().resolve()
    results = [_verify_target_file(root, source) for source in base]
    failures = [item for item in results if not item["matches"]]
    if failures:
        raise ContinuityError(
            "SOURCE_MANIFEST_RETRACT_BASE_HASH_MISMATCH",
            "restore the frozen pre-migration source bytes before retraction",
            details={"files": failures},
        )
    return results


def _identity(source: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        normalize_source_path(source.get("path") or source.get("source_path")),
        _hash(source.get("content_hash"), field="source_identity.content_hash"),
        normalize_source_role(source.get("source_role")),
    )


def _normalize_plan_project(
    plan: Mapping[str, Any],
    project_root: Path,
) -> None:
    declared = str(plan.get("project_root") or "").strip()
    if not declared:
        return
    if Path(declared).expanduser().resolve() != project_root:
        raise ContinuityError(
            "SOURCE_MANIFEST_PROJECT_MISMATCH",
            "manifest migration plan targets a different project",
            details={
                "declared_project_root": declared,
                "actual_project_root": str(project_root),
            },
        )


def preview_manifest_plan(
    connection: sqlite3.Connection,
    project_root: Path | str,
    plan: Mapping[str, Any],
    *,
    expected_canon_revision: int,
) -> dict[str, Any]:
    """Validate a target plan and freeze its deterministic migration payload."""

    if str(plan.get("schema_version") or "") != SOURCE_MANIFEST_PLAN_SCHEMA:
        raise ContinuityError(
            "SOURCE_MANIFEST_PLAN_SCHEMA_UNSUPPORTED",
            "source manifest migration plan schema is unsupported",
            details={
                "declared": plan.get("schema_version"),
                "supported": SOURCE_MANIFEST_PLAN_SCHEMA,
            },
        )
    root = Path(project_root).expanduser().resolve()
    _normalize_plan_project(plan, root)

    expected = validate_positive_int(
        expected_canon_revision,
        "expected_canon_revision",
        allow_none=False,
        minimum=0,
    )
    plan_revision = validate_positive_int(
        plan.get("expected_canon_revision"),
        "plan.expected_canon_revision",
        allow_none=False,
        minimum=0,
    )
    if plan_revision != expected:
        raise ContinuityError(
            "SOURCE_MANIFEST_PLAN_REVISION_MISMATCH",
            "plan revision differs from the requested canon revision",
            details={
                "plan": plan_revision,
                "requested": expected,
            },
        )

    active_rows = read_manifest_rows(connection, status="active")
    active_ids = {str(row["manifest_entry_id"]) for row in active_rows}
    active_by_id = {
        str(row["manifest_entry_id"]): row for row in active_rows
    }
    # The rollback base is the authoritative read snapshot, not every legacy
    # row that still happens to carry manifest_status='active'.  Older builds
    # could leave rows from retracted initialization commits physically active;
    # replaying those rows would resurrect withdrawn authority.
    effective_before = list(current_manifest_snapshot(connection)["entries"])
    operations = dict(plan.get("operations") or {})
    deactivate_ids = _exact_string_list(
        operations.get("deactivate_entry_ids", []),
        field="operations.deactivate_entry_ids",
    )
    retain_ids = _exact_string_list(
        operations.get("retain_entry_ids", []),
        field="operations.retain_entry_ids",
    )
    if len(deactivate_ids) != len(set(deactivate_ids)) or len(retain_ids) != len(
        set(retain_ids)
    ):
        raise ContinuityError(
            "SOURCE_MANIFEST_PLAN_DUPLICATE_ENTRY",
            "deactivate and retain entry lists must not contain duplicates",
        )
    deactivate_set = set(deactivate_ids)
    retain_set = set(retain_ids)
    if deactivate_set & retain_set:
        raise ContinuityError(
            "SOURCE_MANIFEST_PLAN_ENTRY_CONFLICT",
            "the same manifest entry cannot be retained and deactivated",
            details={"entry_ids": sorted(deactivate_set & retain_set)},
        )
    if deactivate_set | retain_set != active_ids:
        raise ContinuityError(
            "SOURCE_MANIFEST_PLAN_COVERAGE_MISMATCH",
            "migration plan must classify every currently active manifest row",
            details={
                "missing": sorted(active_ids - deactivate_set - retain_set),
                "unknown": sorted((deactivate_set | retain_set) - active_ids),
            },
        )

    target_block = dict(plan.get("target") or {})
    raw_target = target_block.get("sources") or []
    if not isinstance(raw_target, list):
        raise ContinuityError(
            "SOURCE_MANIFEST_TARGET_MISSING",
            "migration plan must contain a target.sources array",
        )
    target_sources = [
        normalize_target_source(dict(source))
        for source in raw_target
        if isinstance(source, Mapping)
    ]
    target_paths = [str(source["path"]) for source in target_sources]
    duplicates = sorted(
        path
        for path, count in Counter(
            value.casefold() for value in target_paths
        ).items()
        if count > 1
    )
    if duplicates or len(target_sources) != len(raw_target) or not target_sources:
        raise ContinuityError(
            "SOURCE_MANIFEST_TARGET_DUPLICATE_PATH",
            "target manifest must contain exactly one source version per path",
            details={"duplicate_paths": duplicates},
        )
    target_sources.sort(key=lambda item: str(item["path"]))

    declared_rows = validate_positive_int(
        target_block.get("active_rows", len(target_sources)),
        "target.active_rows",
        allow_none=False,
        minimum=0,
    )
    declared_unique = validate_positive_int(
        target_block.get("unique_paths", len(set(target_paths))),
        "target.unique_paths",
        allow_none=False,
        minimum=0,
    )
    if declared_rows != len(target_sources) or declared_unique != len(
        set(target_paths)
    ):
        raise ContinuityError(
            "SOURCE_MANIFEST_TARGET_COUNT_MISMATCH",
            "target manifest counts do not match target.sources",
            details={
                "declared_active_rows": declared_rows,
                "declared_unique_paths": declared_unique,
                "actual_active_rows": len(target_sources),
                "actual_unique_paths": len(set(target_paths)),
            },
        )

    raw_upserts = operations.get("upserts", [])
    if not isinstance(raw_upserts, list):
        raise ContinuityError(
            "SOURCE_MANIFEST_PLAN_FIELD_INVALID",
            "operations.upserts must be an array",
        )
    upsert_sources = [
        normalize_target_source(dict(source))
        for source in raw_upserts
        if isinstance(source, Mapping)
    ]
    if len(upsert_sources) != len(raw_upserts):
        raise ContinuityError(
            "SOURCE_MANIFEST_PLAN_FIELD_INVALID",
            "operations.upserts must contain source objects",
        )
    upsert_by_path = {str(source["path"]): source for source in upsert_sources}
    if len(upsert_by_path) != len(upsert_sources):
        raise ContinuityError(
            "SOURCE_MANIFEST_UPSERT_DUPLICATE_PATH",
            "upserts must contain one source per path",
        )

    retain_by_path: dict[str, dict[str, Any]] = {}
    for entry_id in retain_ids:
        row = active_by_id.get(entry_id)
        if row is None:
            raise ContinuityError(
                "SOURCE_MANIFEST_ENTRY_NOT_FOUND",
                "retained manifest entry is not currently active",
                details={"manifest_entry_id": entry_id},
            )
        path = str(row["path"])
        if path in retain_by_path:
            raise ContinuityError(
                "SOURCE_MANIFEST_RETAIN_DUPLICATE_PATH",
                "only one active entry may be retained for a source path",
                details={"path": path},
            )
        retain_by_path[path] = row

    target_by_path = {
        str(source["path"]).casefold(): source for source in target_sources
    }
    retain_by_key = {
        path.casefold(): row for path, row in retain_by_path.items()
    }
    upsert_by_key = {
        path.casefold(): source for path, source in upsert_by_path.items()
    }
    expected_upsert_paths = set(target_by_path) - set(retain_by_key)
    if set(upsert_by_key) != expected_upsert_paths:
        raise ContinuityError(
            "SOURCE_MANIFEST_UPSERT_COVERAGE_MISMATCH",
            "upserts must cover every target path not backed by a retained entry",
            details={
                "missing": sorted(expected_upsert_paths - set(upsert_by_key)),
                "unexpected": sorted(set(upsert_by_key) - expected_upsert_paths),
            },
        )

    result_manifest: list[dict[str, Any]] = []
    retain_descriptors: list[dict[str, Any]] = []
    for path, row in sorted(retain_by_path.items()):
        target = target_by_path.get(path.casefold())
        if (
            target is None
            or _identity(row) != _identity(target)
            or canonical_json(dict(row.get("metadata") or {}))
            != canonical_json(dict(target.get("metadata") or {}))
        ):
            raise ContinuityError(
                "SOURCE_MANIFEST_RETAIN_TARGET_MISMATCH",
                "retained entry differs from the target source version or metadata",
                details={"path": path, "manifest_entry_id": row["manifest_entry_id"]},
            )
        descriptor = {
            "manifest_entry_id": row["manifest_entry_id"],
            "commit_id": row["commit_id"],
            "source_id": row["source_id"],
            "path": path,
            "content_hash": row["content_hash"],
            "source_role": row["source_role"],
            "metadata": dict(target.get("metadata") or row.get("metadata") or {}),
        }
        retain_descriptors.append(descriptor)
        result_manifest.append(descriptor)

    effective_by_path = {
        str(row["path"]).casefold(): row for row in effective_before
    }
    all_identity_by_path: dict[str, set[tuple[str, str, str]]] = {}
    for row in active_rows:
        all_identity_by_path.setdefault(
            str(row["path"]).casefold(),
            set(),
        ).add(_identity(row))

    retire_commits = sorted(
        set(
            _exact_string_list(
                plan.get("retire_commits", []),
                field="retire_commits",
            )
        )
    )
    frozen_plan = {
        "schema_version": SOURCE_MANIFEST_PLAN_SCHEMA,
        "project_root": str(root),
        "expected_canon_revision": expected,
        "retire_commits": retire_commits,
        "operations": {
            "deactivate_entry_ids": sorted(deactivate_ids),
            "retain_entry_ids": sorted(retain_ids),
            "upserts": sorted(
                (_plan_source(source) for source in upsert_sources),
                key=lambda item: str(item["source_path"]).casefold(),
            ),
        },
        "target": {
            "active_rows": len(target_sources),
            "unique_paths": len(target_sources),
            "sources": sorted(
                (_plan_source(source) for source in target_sources),
                key=lambda item: str(item["source_path"]).casefold(),
            ),
        },
    }

    plan_hash = stable_hash(
        frozen_plan,
        prefix="source_manifest_plan_",
    )
    upsert_descriptors: list[dict[str, Any]] = []
    semantic_counts = Counter()
    for path in sorted(upsert_by_path, key=str.casefold):
        source = upsert_by_path[path]
        target = target_by_path[path.casefold()]
        if _identity(source) != _identity(target):
            raise ContinuityError(
                "SOURCE_MANIFEST_UPSERT_TARGET_MISMATCH",
                "upsert source version differs from target.sources",
                details={"path": path},
            )
        target_identity = _identity(target)
        current = effective_by_path.get(path.casefold())
        if current is None:
            action = "enroll"
        elif (
            target_identity == _identity(current)
            and canonical_json(dict(target.get("metadata") or {}))
            == canonical_json(dict(current.get("metadata") or {}))
        ):
            action = "reenroll"
        else:
            action = "correct"
        semantic_counts[action] += 1
        source_id = stable_hash(
            ["source_manifest_version", target_identity],
            prefix="source_",
        )
        entry_id = stable_hash(
            [plan_hash, source_id],
            prefix="manifest_entry_",
        )
        stored_metadata = dict(target.get("metadata") or {})
        stored_metadata.update(
            {
                "origin": "accepted_source_manifest_migration",
                "manifest_plan_hash": plan_hash,
                "manifest_action": action,
            }
        )
        descriptor = {
            "manifest_entry_id": entry_id,
            "commit_id": None,
            "source_id": source_id,
            "path": path,
            "content_hash": target["content_hash"],
            "source_role": target["source_role"],
            "metadata": stored_metadata,
            "action": action,
            "prior_version_present": (
                target_identity
                in all_identity_by_path.get(path.casefold(), set())
            ),
        }
        upsert_descriptors.append(descriptor)
        result_manifest.append(
            {
                key: value
                for key, value in descriptor.items()
                if key not in {"action", "prior_version_present"}
            }
        )

    result_manifest.sort(key=lambda item: str(item["path"]))
    deactivate_descriptors: list[dict[str, Any]] = []
    target_path_set = set(target_by_path)
    for entry_id in deactivate_ids:
        row = active_by_id[entry_id]
        disposition = (
            "superseded"
            if str(row["path"]).casefold() in target_path_set
            else "deactivated"
        )
        deactivate_descriptors.append(
            {
                "manifest_entry_id": entry_id,
                "path": row["path"],
                "content_hash": row["content_hash"],
                "source_role": row["source_role"],
                "disposition": disposition,
            }
        )

    file_verification = verify_target_files(root, target_sources)
    migration = {
        "schema_version": SOURCE_MANIFEST_CHANGE_SCHEMA,
        "plan_schema_version": SOURCE_MANIFEST_PLAN_SCHEMA,
        "frozen_plan": frozen_plan,
        "plan_hash": plan_hash,
        "base_manifest_hash": manifest_state_hash(active_rows),
        "base_manifest": effective_before,
        "base_active_rows": len(active_rows),
        "base_unique_paths": len({str(row["path"]) for row in active_rows}),
        "base_duplicate_rows": len(active_rows)
        - len({str(row["path"]) for row in active_rows}),
        "expected_canon_revision": expected,
        "retire_commits": retire_commits,
        "operations": {
            "deactivate": deactivate_descriptors,
            "retain": retain_descriptors,
            "upsert": upsert_descriptors,
        },
        "result_manifest": result_manifest,
        "target_manifest_hash": _target_manifest_hash(result_manifest),
        "target_active_rows": len(result_manifest),
        "target_unique_paths": len({str(row["path"]) for row in result_manifest}),
        "semantic_counts": {
            "enroll": int(semantic_counts["enroll"]),
            "correct": int(semantic_counts["correct"]),
            "reenroll": int(semantic_counts["reenroll"]),
        },
        "physical_counts": {
            "deactivate": len(deactivate_descriptors),
            "retain": len(retain_descriptors),
            "upsert": len(upsert_descriptors),
        },
        "file_verification": file_verification,
    }
    return {
        "status": "ready",
        "read_only": True,
        "migration": migration,
    }


def validate_frozen_manifest_change(
    connection: sqlite3.Connection,
    project_root: Path | str,
    migration: Mapping[str, Any],
) -> None:
    if str(migration.get("schema_version") or "") != SOURCE_MANIFEST_CHANGE_SCHEMA:
        raise ContinuityError(
            "SOURCE_MANIFEST_CHANGE_SCHEMA_UNSUPPORTED",
            "frozen source manifest change schema is unsupported",
        )
    frozen_plan = migration.get("frozen_plan")
    if not isinstance(frozen_plan, Mapping):
        raise ContinuityError(
            "SOURCE_MANIFEST_FROZEN_PLAN_MISSING",
            "source manifest proposal must preserve its normalized preview plan",
        )
    expected_revision = validate_positive_int(
        migration.get("expected_canon_revision"),
        "source_manifest_change.expected_canon_revision",
        allow_none=False,
        minimum=0,
    )
    expected = preview_manifest_plan(
        connection,
        project_root,
        frozen_plan,
        expected_canon_revision=expected_revision,
    )["migration"]
    if canonical_json(dict(migration)) != canonical_json(expected):
        actual_keys = set(migration)
        expected_keys = set(expected)
        changed = sorted(
            key
            for key in actual_keys & expected_keys
            if canonical_json(migration.get(key))
            != canonical_json(expected.get(key))
        )
        raise ContinuityError(
            "SOURCE_MANIFEST_FROZEN_PLAN_MISMATCH",
            "frozen source manifest payload differs from deterministic preview",
            details={
                "missing_fields": sorted(expected_keys - actual_keys),
                "unexpected_fields": sorted(actual_keys - expected_keys),
                "changed_fields": changed,
            },
        )


def validate_source_manifest_proposal_envelope(
    connection: sqlite3.Connection,
    proposal: Mapping[str, Any],
    payload: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> None:
    """Require the dedicated authority-changing proposal envelope."""

    proposal_data = dict(proposal)
    artifact = connection.execute(
        """
        SELECT artifact_id, artifact_kind, artifact_stage, branch_id,
               chapter_no, scene_index, source_role
        FROM artifacts
        WHERE artifact_version_id=?
        """,
        (str(proposal_data.get("artifact_version_id") or ""),),
    ).fetchone()
    actual = {
        "proposal_kind": str(proposal_data.get("proposal_kind") or ""),
        "artifact_id": str(proposal_data.get("artifact_id") or ""),
        "artifact_kind": (
            str(artifact["artifact_kind"]) if artifact is not None else ""
        ),
        "artifact_stage": str(proposal_data.get("artifact_stage") or ""),
        "branch_id": str(proposal_data.get("branch_id") or ""),
        "chapter_no": proposal_data.get("chapter_no"),
        "scene_index": proposal_data.get("scene_index"),
        "source_role": str(proposal_data.get("source_role") or ""),
        "artifact_row_matches": bool(
            artifact is not None
            and str(artifact["artifact_id"])
            == str(proposal_data.get("artifact_id") or "")
            and str(artifact["artifact_stage"])
            == str(proposal_data.get("artifact_stage") or "")
            and str(artifact["branch_id"])
            == str(proposal_data.get("branch_id") or "")
            and artifact["chapter_no"] == proposal_data.get("chapter_no")
            and artifact["scene_index"] == proposal_data.get("scene_index")
            and str(artifact["source_role"])
            == str(proposal_data.get("source_role") or "")
        ),
        "payload_keys": sorted(str(key) for key in payload),
        "event_count": len(events),
    }
    expected = {
        "proposal_kind": SOURCE_MANIFEST_PROPOSAL_KIND,
        "artifact_id": SOURCE_MANIFEST_ARTIFACT_ID,
        "artifact_kind": SOURCE_MANIFEST_ARTIFACT_KIND,
        "artifact_stage": SOURCE_MANIFEST_ARTIFACT_STAGE,
        "branch_id": SOURCE_MANIFEST_BRANCH_ID,
        "chapter_no": None,
        "scene_index": None,
        "source_role": SOURCE_MANIFEST_SOURCE_ROLE,
        "artifact_row_matches": True,
        "payload_keys": ["source_manifest_change"],
        "event_count": 0,
    }
    if actual != expected:
        raise ContinuityError(
            "SOURCE_MANIFEST_PROPOSAL_ENVELOPE_INVALID",
            "source manifest changes require the dedicated authority proposal envelope",
            details={"expected": expected, "actual": actual},
        )
    migration = payload.get("source_manifest_change")
    if not isinstance(migration, Mapping):
        raise ContinuityError(
            "SOURCE_MANIFEST_CHANGE_MISSING",
            "source manifest proposal payload is missing its frozen change",
        )
    prepared = validate_positive_int(
        proposal_data.get("prepared_canon_revision"),
        "proposal.prepared_canon_revision",
        allow_none=False,
        minimum=0,
    )
    migration_revision = validate_positive_int(
        migration.get("expected_canon_revision"),
        "source_manifest_change.expected_canon_revision",
        allow_none=False,
        minimum=0,
    )
    if prepared != migration_revision:
        raise ContinuityError(
            "SOURCE_MANIFEST_PROPOSAL_REVISION_MISMATCH",
            "proposal and frozen source manifest plan bind different revisions",
            details={
                "prepared_canon_revision": prepared,
                "manifest_canon_revision": migration_revision,
            },
        )


def insert_manifest_upserts(
    connection: sqlite3.Connection,
    proposal: Mapping[str, Any],
    commit_id: str,
) -> None:
    payload = _json_load(str(proposal["payload_json"]), {})
    migration = dict(payload.get("source_manifest_change") or {})
    now = utc_now()
    for raw in dict(migration.get("operations") or {}).get("upsert") or []:
        item = dict(raw)
        entry_id = str(item.get("manifest_entry_id") or "")
        source_id = str(item.get("source_id") or "")
        path = normalize_source_path(item.get("path"))
        content_hash = _hash(
            item.get("content_hash"),
            field=f"{path}.content_hash",
        )
        role = normalize_source_role(item.get("source_role"))
        metadata = dict(item.get("metadata") or {})
        metadata.update(
            {
                "origin": "accepted_source_manifest_migration",
                "manifest_plan_hash": migration.get("plan_hash"),
                "manifest_action": item.get("action"),
            }
        )
        existing = connection.execute(
            """
            SELECT manifest_entry_id, source_id, source_path, content_hash,
                   source_role
            FROM accepted_source_manifest
            WHERE manifest_entry_id=?
            """,
            (entry_id,),
        ).fetchone()
        if existing is not None:
            expected = (source_id, path, content_hash, role)
            actual = (
                str(existing["source_id"]),
                str(existing["source_path"]),
                str(existing["content_hash"]),
                str(existing["source_role"]),
            )
            if actual != expected:
                raise ContinuityError(
                    "SOURCE_MANIFEST_ENTRY_ID_CONFLICT",
                    "manifest migration entry id already has different content",
                    details={"manifest_entry_id": entry_id},
                )
            continue
        connection.execute(
            """
            INSERT INTO accepted_source_manifest(
                manifest_entry_id, commit_id, source_id, source_path,
                content_hash, source_role, manifest_status, metadata_json,
                activated_at, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, 'superseded', ?, NULL, ?)
            """,
            (
                entry_id,
                commit_id,
                source_id,
                path,
                content_hash,
                role,
                canonical_json(metadata),
                now,
            ),
        )


def _active_manifest_migration(
    connection: sqlite3.Connection,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT p.proposal_id, p.payload_json, p.artifact_revision,
               c.commit_id, c.active_revision_after
        FROM proposals AS p
        JOIN artifacts AS a
          ON a.artifact_version_id=p.artifact_version_id
        JOIN canon_commits AS c
          ON c.proposal_id=p.proposal_id AND c.operation='accept'
        WHERE p.proposal_kind=?
          AND p.canon_status='accepted'
          AND a.active=1
          AND p.artifact_id=?
          AND p.artifact_stage=?
          AND p.branch_id=?
          AND p.chapter_no IS NULL
          AND p.scene_index IS NULL
          AND p.source_role=?
          AND a.artifact_kind=?
          AND a.artifact_id=p.artifact_id
          AND a.artifact_stage=p.artifact_stage
          AND a.branch_id=p.branch_id
          AND a.chapter_no IS NULL
          AND a.scene_index IS NULL
          AND a.source_role=p.source_role
          AND c.changes_authority=1
        ORDER BY c.active_revision_after DESC, p.artifact_revision DESC
        LIMIT 1
        """,
        (
            SOURCE_MANIFEST_PROPOSAL_KIND,
            SOURCE_MANIFEST_ARTIFACT_ID,
            SOURCE_MANIFEST_ARTIFACT_STAGE,
            SOURCE_MANIFEST_BRANCH_ID,
            SOURCE_MANIFEST_SOURCE_ROLE,
            SOURCE_MANIFEST_ARTIFACT_KIND,
        ),
    ).fetchone()


def _first_manifest_migration(
    connection: sqlite3.Connection,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT p.proposal_id, p.payload_json, p.artifact_revision,
               c.commit_id, c.active_revision_after
        FROM proposals AS p
        JOIN canon_commits AS c
          ON c.proposal_id=p.proposal_id AND c.operation='accept'
        JOIN artifacts AS a
          ON a.artifact_version_id=p.artifact_version_id
        WHERE p.proposal_kind=?
          AND p.artifact_id=?
          AND p.artifact_stage=?
          AND p.branch_id=?
          AND p.chapter_no IS NULL
          AND p.scene_index IS NULL
          AND p.source_role=?
          AND a.artifact_kind=?
          AND a.artifact_id=p.artifact_id
          AND a.artifact_stage=p.artifact_stage
          AND a.branch_id=p.branch_id
          AND a.chapter_no IS NULL
          AND a.scene_index IS NULL
          AND a.source_role=p.source_role
          AND c.changes_authority=1
        ORDER BY c.active_revision_after, p.artifact_revision
        LIMIT 1
        """,
        (
            SOURCE_MANIFEST_PROPOSAL_KIND,
            SOURCE_MANIFEST_ARTIFACT_ID,
            SOURCE_MANIFEST_ARTIFACT_STAGE,
            SOURCE_MANIFEST_BRANCH_ID,
            SOURCE_MANIFEST_SOURCE_ROLE,
            SOURCE_MANIFEST_ARTIFACT_KIND,
        ),
    ).fetchone()


def latest_active_manifest_proposal_id(
    connection: sqlite3.Connection,
) -> str | None:
    row = _active_manifest_migration(connection)
    return str(row["proposal_id"]) if row is not None else None


def replay_source_manifest(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    """Rebuild active manifest rows from the latest accepted migration."""

    active_migration = _active_manifest_migration(connection)
    fallback = None
    if active_migration is None:
        fallback = _first_manifest_migration(connection)
        if fallback is None:
            active_rows = read_manifest_rows(connection, status="active")
            return {
                "source_manifest_replayed": False,
                "source_manifest_active_rows": len(active_rows),
                "source_manifest_unique_paths": len(
                    {str(row["path"]) for row in active_rows}
                ),
                "source_manifest_duplicate_rows": len(active_rows)
                - len({str(row["path"]) for row in active_rows}),
                "source_manifest_commit_id": None,
            }

    payload = _json_load(
        str((active_migration or fallback)["payload_json"]),
        {},
    )
    migration = dict(payload.get("source_manifest_change") or {})
    target = [
        dict(item)
        for item in (
            migration.get("result_manifest")
            if active_migration is not None
            else migration.get("base_manifest")
        )
        or []
        if isinstance(item, Mapping)
    ]
    target_ids = {str(item.get("manifest_entry_id") or "") for item in target}
    if "" in target_ids or len(target_ids) != len(target):
        raise ContinuityError(
            "SOURCE_MANIFEST_REPLAY_TARGET_INVALID",
            "manifest replay target contains missing or duplicate entry ids",
        )
    existing_ids = {
        str(row[0])
        for row in connection.execute(
            "SELECT manifest_entry_id FROM accepted_source_manifest"
        )
    }
    missing = sorted(target_ids - existing_ids)
    if missing:
        raise ContinuityError(
            "SOURCE_MANIFEST_REPLAY_ENTRY_MISSING",
            "manifest replay target references unknown entry ids",
            details={"manifest_entry_ids": missing},
        )
    frozen_by_id = {
        str(item["manifest_entry_id"]): item for item in target
    }
    placeholders = ",".join("?" for _ in target_ids)
    target_rows = connection.execute(
        f"""
        SELECT manifest_entry_id, commit_id, source_id, source_path,
               content_hash, source_role, manifest_status, metadata_json,
               activated_at, created_at
        FROM accepted_source_manifest
        WHERE manifest_entry_id IN ({placeholders})
        """,
        tuple(sorted(target_ids)),
    ).fetchall()
    for raw in target_rows:
        actual = _row_descriptor(raw)
        expected = frozen_by_id[str(actual["manifest_entry_id"])]
        expected_descriptor = {
            "manifest_entry_id": str(expected.get("manifest_entry_id") or ""),
            "source_id": str(expected.get("source_id") or ""),
            "path": normalize_source_path(expected.get("path")),
            "content_hash": _hash(
                expected.get("content_hash"),
                field="replay_target.content_hash",
            ),
            "source_role": normalize_source_role(expected.get("source_role")),
            "metadata": dict(expected.get("metadata") or {}),
        }
        actual_descriptor = {
            key: actual[key] for key in expected_descriptor
        }
        if expected_descriptor != actual_descriptor:
            raise ContinuityError(
                "SOURCE_MANIFEST_REPLAY_ENTRY_MISMATCH",
                "manifest replay target differs from its ledger row",
                details={
                    "manifest_entry_id": actual["manifest_entry_id"],
                    "expected": expected_descriptor,
                    "actual": actual_descriptor,
                },
            )

    connection.execute(
        """
        UPDATE accepted_source_manifest
        SET manifest_status=CASE
                WHEN manifest_status='pending' THEN 'pending'
                ELSE 'superseded'
            END
        """
    )
    if active_migration is not None:
        dispositions = dict(migration.get("operations") or {}).get(
            "deactivate"
        ) or []
        for raw in dispositions:
            item = dict(raw)
            entry_id = str(item.get("manifest_entry_id") or "")
            disposition = str(item.get("disposition") or "superseded")
            if disposition not in {"superseded", "deactivated"}:
                raise ContinuityError(
                    "SOURCE_MANIFEST_DISPOSITION_INVALID",
                    "manifest replay disposition is unsupported",
                    details={
                        "manifest_entry_id": entry_id,
                        "disposition": disposition,
                    },
                )
            connection.execute(
                """
                UPDATE accepted_source_manifest
                SET manifest_status=?
                WHERE manifest_entry_id=?
                """,
                (disposition, entry_id),
            )

    now = utc_now()
    for entry_id in sorted(target_ids):
        connection.execute(
            """
            UPDATE accepted_source_manifest
            SET manifest_status='active',
                activated_at=COALESCE(activated_at, ?)
            WHERE manifest_entry_id=?
            """,
            (now, entry_id),
        )

    active_rows = read_manifest_rows(connection, status="active")
    active_paths = [str(row["path"]) for row in active_rows]
    active_path_keys = [path.casefold() for path in active_paths]
    if len(active_path_keys) != len(set(active_path_keys)):
        raise ContinuityError(
            "SOURCE_MANIFEST_ACTIVE_DUPLICATE_PATH",
            "manifest replay produced multiple active versions for one path",
            details={
                "duplicate_paths": sorted(
                    path
                    for path, count in Counter(active_path_keys).items()
                    if count > 1
                )
            },
        )
    return {
        "source_manifest_replayed": True,
        "source_manifest_active_rows": len(active_rows),
        "source_manifest_unique_paths": len(set(active_paths)),
        "source_manifest_duplicate_rows": 0,
        "source_manifest_commit_id": (
            str(active_migration["commit_id"])
            if active_migration is not None
            else None
        ),
        "source_manifest_restored_base": active_migration is None,
    }


def manifest_status(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = read_manifest_rows(connection)
    active = [row for row in rows if row["status"] == "active"]
    effective = list(current_manifest_snapshot(connection)["entries"])
    counts = Counter(str(row["status"]) for row in rows)
    active_migration = _active_manifest_migration(connection)
    migration_payload: dict[str, Any] = {}
    if active_migration is not None:
        payload = _json_load(str(active_migration["payload_json"]), {})
        migration_payload = dict(payload.get("source_manifest_change") or {})
    return {
        "status": "ready",
        "read_only": True,
        "active_rows": len(active),
        "unique_active_paths": len({str(row["path"]) for row in active}),
        "duplicate_active_rows": len(active)
        - len({str(row["path"]) for row in active}),
        "effective_rows": len(effective),
        "history_rows": len(rows),
        "status_counts": dict(sorted(counts.items())),
        "active_manifest_proposal_id": (
            str(active_migration["proposal_id"])
            if active_migration is not None
            else None
        ),
        "active_manifest_commit_id": (
            str(active_migration["commit_id"])
            if active_migration is not None
            else None
        ),
        "active_manifest_canon_revision": (
            int(active_migration["active_revision_after"])
            if active_migration is not None
            else None
        ),
        "target_manifest_hash": migration_payload.get("target_manifest_hash"),
        "physical_counts": migration_payload.get("physical_counts"),
        "semantic_counts": migration_payload.get("semantic_counts"),
        "active": effective,
        "history": rows,
    }


def accepted_manifest_overrides(
    connection: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    """Return receipt-hash overrides backed by the active migration commit."""

    active_migration = _active_manifest_migration(connection)
    if active_migration is None:
        return {}
    payload = _json_load(str(active_migration["payload_json"]), {})
    migration = dict(payload.get("source_manifest_change") or {})
    overrides: dict[str, dict[str, Any]] = {}
    for raw in migration.get("result_manifest") or []:
        item = dict(raw)
        path = normalize_source_path(item.get("path"))
        overrides[path] = {
            "sha256": _hash(
                item.get("content_hash"),
                field=f"{path}.content_hash",
            ),
            "manifest_entry_id": str(item.get("manifest_entry_id") or ""),
            "proposal_id": str(active_migration["proposal_id"]),
            "commit_id": str(active_migration["commit_id"]),
            "active_canon_revision": int(
                active_migration["active_revision_after"]
            ),
            "plan_hash": str(migration.get("plan_hash") or ""),
            "target_manifest_hash": str(
                migration.get("target_manifest_hash") or ""
            ),
        }
    return overrides


def sha256_bytes(value: bytes) -> str:
    """Small public helper used by focused tests."""

    return hashlib.sha256(value).hexdigest()
