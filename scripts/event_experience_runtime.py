"""Deterministic orchestration for the event-experience control gate.

The runtime performs no network work.  It translates an already locked Grill
Intent Contract plus prompt/artifact context into explicit control-plane
EventSeed, EventExperienceArc, and EventExperienceContract payloads.  A clear
reader-experience intent is atomically locked; structural ambiguity opens one
bounded question and returns before any plot receipt or remote retrieval.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__:
    from .event_experience import (
        EVENT_EXPERIENCE_SCHEMA_VERSION,
        EventExperienceError,
        EventExperienceService,
        canonical_hash,
    )
else:
    from event_experience import (
        EVENT_EXPERIENCE_SCHEMA_VERSION,
        EventExperienceError,
        EventExperienceService,
        canonical_hash,
    )


INTENT_SCHEMA_VERSION = "plot-rag-intent/v1"
INTENT_FIELDS = (
    "problem_to_solve",
    "expected_deliverable",
    "reader_experience",
    "protagonist_drive_conflict",
    "scope_endpoint",
    "success_criteria",
    "hard_constraints",
    "model_autonomy",
)

_EXPLICIT_SOURCES = {
    "prompt",
    "user_answer",
    "recommended_delegation",
}
_DELEGATION_MARKERS = (
    "你来定",
    "模型决定",
    "模型可决定",
    "模型可以决定",
    "自行决定",
    "自主决定",
    "可自行",
    "交给模型",
)
_EMOTION_TERMS = (
    "期待",
    "好奇",
    "紧张",
    "压迫",
    "恐惧",
    "不安",
    "愤怒",
    "屈辱",
    "痛快",
    "振奋",
    "敬畏",
    "惊奇",
    "悲伤",
    "怜惜",
    "温暖",
    "亲密",
    "满足",
    "释然",
    "余悸",
    "希望",
    "荒诞",
    "厌恶",
)


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise EventExperienceError(
            "EVENT_EXPERIENCE_RUNTIME_TEXT_REQUIRED",
            f"{field} must be a string",
            field=field,
        )
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized and not allow_empty:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_RUNTIME_TEXT_REQUIRED",
            f"{field} must be non-empty",
            field=field,
        )
    return normalized


def _integer(
    value: Any,
    field: str,
    *,
    minimum: int = 0,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_RUNTIME_INTEGER_REQUIRED",
            f"{field} must be an integer >= {minimum}",
            field=field,
        )
    return int(value)


def _unwrap_intent(
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise EventExperienceError(
            "EVENT_EXPERIENCE_RUNTIME_INTENT_REQUIRED",
            "intent_contract must be an object",
        )
    outer = dict(value)
    candidate = outer.get("contract", outer)
    if not isinstance(candidate, Mapping):
        raise EventExperienceError(
            "EVENT_EXPERIENCE_RUNTIME_INTENT_REQUIRED",
            "intent_contract.contract must be an object",
        )
    contract = dict(candidate)
    if contract.get("schema_version") != INTENT_SCHEMA_VERSION:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_RUNTIME_INTENT_SCHEMA",
            "locked Intent Contract must use plot-rag-intent/v1",
            observed=contract.get("schema_version"),
        )
    if contract.get("task_family") != "plot":
        raise EventExperienceError(
            "EVENT_EXPERIENCE_RUNTIME_INTENT_FAMILY",
            "event-experience orchestration requires a plot Intent Contract",
            task_family=contract.get("task_family"),
        )
    fields = contract.get("fields")
    if not isinstance(fields, Mapping) or set(fields) != set(INTENT_FIELDS):
        raise EventExperienceError(
            "EVENT_EXPERIENCE_RUNTIME_INTENT_FIELDS",
            "locked Intent Contract must contain exactly the eight Grill fields",
        )
    normalized_fields: dict[str, dict[str, str]] = {}
    for field in INTENT_FIELDS:
        entry = fields[field]
        if not isinstance(entry, Mapping):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_RUNTIME_INTENT_FIELD",
                f"intent field {field} must be an object",
                field=field,
            )
        normalized_fields[field] = {
            "value": _text(entry.get("value"), f"fields.{field}.value"),
            "source": _text(entry.get("source"), f"fields.{field}.source"),
        }
    normalized = {
        "schema_version": INTENT_SCHEMA_VERSION,
        "task_family": "plot",
        "fields": normalized_fields,
    }
    status = str(outer.get("status", "")).upper()
    if status and status not in {
        "EXECUTING",
        "COMPLETED",
        "LOCKED",
        "READY",
    }:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_RUNTIME_INTENT_NOT_LOCKED",
            "Intent Contract is not in a locked/executing state",
            status=status,
        )
    supplied_hash = outer.get(
        "intent_contract_hash", outer.get("contract_hash")
    )
    computed_hash = canonical_hash(normalized)
    if supplied_hash is not None and str(supplied_hash) != computed_hash:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_RUNTIME_INTENT_HASH_MISMATCH",
            "Intent Contract hash does not match its normalized payload",
            expected=computed_hash,
            supplied=str(supplied_hash),
        )
    identity = {
        "intent_contract_id": _text(
            str(
                outer.get("intent_contract_id")
                or outer.get("contract_id")
                or outer.get("grill_session_id")
                or f"intent-{computed_hash[:24]}"
            ),
            "intent_contract_id",
        ),
        "intent_contract_revision": _integer(
            outer.get(
                "intent_contract_revision",
                outer.get("revision", outer.get("session_revision", 1)),
            ),
            "intent_contract_revision",
            minimum=1,
        ),
        "intent_contract_hash": computed_hash,
    }
    return normalized, identity


def _artifact_context(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EventExperienceError(
            "EVENT_EXPERIENCE_RUNTIME_ARTIFACT_REQUIRED",
            "artifact_context must be an object",
        )
    raw = dict(value)
    branch_id = _text(str(raw.get("branch_id", "main")), "branch_id")
    artifact_id = _text(
        str(
            raw.get("artifact_id")
            or raw.get("path")
            or raw.get("target_artifact")
            or "plot-turn"
        ),
        "artifact_id",
    )
    artifact_revision = _integer(
        raw.get("artifact_revision", 0),
        "artifact_revision",
    )
    chapter_no = raw.get("chapter_no")
    scene_index = raw.get("scene_index")
    if chapter_no is not None:
        chapter_no = _integer(chapter_no, "chapter_no")
    if scene_index is not None:
        scene_index = _integer(scene_index, "scene_index")
    return {
        **raw,
        "branch_id": branch_id,
        "artifact_id": artifact_id,
        "artifact_revision": artifact_revision,
        "chapter_no": chapter_no,
        "scene_index": scene_index,
    }


def _structured_outline_event_seeds(
    content: Mapping[str, Any],
) -> list[dict[str, Any]] | None:
    candidates: list[Any] = [
        content.get("event_seeds"),
    ]
    outline = content.get("outline")
    if isinstance(outline, Mapping):
        candidates.append(outline.get("event_seeds"))
    story_engine = content.get("story_engine")
    if isinstance(story_engine, Mapping):
        first_chain = story_engine.get("first_event_chain")
        if isinstance(first_chain, Mapping):
            candidates.append(first_chain.get("event_seeds"))
    for candidate in candidates:
        if candidate is None:
            continue
        if not isinstance(candidate, Sequence) or isinstance(
            candidate,
            (str, bytes, bytearray),
        ):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_OUTLINE_SEEDS_INVALID",
                "accepted outline event_seeds must be a structured array",
            )
        if not candidate or len(candidate) > 64:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_OUTLINE_SEEDS_INVALID",
                "accepted outline event_seeds must contain between 1 and 64 events",
            )
        if any(not isinstance(item, Mapping) for item in candidate):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_OUTLINE_SEEDS_INVALID",
                "accepted outline event_seeds must contain only objects",
            )
        return [dict(item) for item in candidate]
    return None


def _resolve_accepted_outline(
    project_root: Path | str,
    artifact: Mapping[str, Any],
) -> dict[str, Any] | None:
    database = (
        Path(project_root).expanduser().resolve(strict=False)
        / ".plot-rag"
        / "state.sqlite3"
    )
    if not database.is_file():
        return None
    explicit_outline_id = str(
        artifact.get("accepted_outline_artifact_id")
        or artifact.get("source_outline_artifact_id")
        or artifact.get("outline_artifact_id")
        or ""
    ).strip()
    clauses = [
        "c.operation='accept'",
        "p.canon_status='accepted'",
        "a.canon_status='accepted'",
        "a.active=1",
        "a.artifact_stage='outline'",
        "a.branch_id=?",
    ]
    parameters: list[Any] = [str(artifact.get("branch_id") or "main")]
    if explicit_outline_id:
        clauses.append("a.artifact_id=?")
        parameters.append(explicit_outline_id)
    else:
        clauses.extend(
            [
                "a.chapter_no IS ?",
                "a.scene_index IS ?",
            ]
        )
        parameters.extend(
            [
                artifact.get("chapter_no"),
                artifact.get("scene_index"),
            ]
        )
    try:
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"""
                SELECT
                    c.commit_id,
                    a.artifact_version_id,
                    a.artifact_id,
                    a.artifact_revision,
                    a.branch_id,
                    a.chapter_no,
                    a.scene_index,
                    a.content_hash,
                    a.content_json
                FROM canon_commits AS c
                JOIN proposals AS p
                  ON p.proposal_id=c.proposal_id
                JOIN artifacts AS a
                  ON a.artifact_version_id=p.artifact_version_id
                WHERE {' AND '.join(clauses)}
                ORDER BY a.artifact_revision DESC, c.commit_id
                """,
                tuple(parameters),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).casefold():
            return None
        raise EventExperienceError(
            "EVENT_EXPERIENCE_OUTLINE_LOOKUP_FAILED",
            "accepted outline lookup failed",
            reason=str(exc),
        ) from exc
    if not rows:
        return None
    identities = {
        (
            str(row["artifact_id"]),
            int(row["artifact_revision"]),
            str(row["commit_id"]),
        )
        for row in rows
    }
    if len(identities) != 1:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_OUTLINE_AMBIGUOUS",
            "branch/chapter/scene resolves to multiple active accepted outlines",
            explicit_outline_artifact_id=explicit_outline_id or None,
            candidates=[
                {
                    "artifact_id": identity[0],
                    "artifact_revision": identity[1],
                    "commit_id": identity[2],
                }
                for identity in sorted(identities)
            ],
        )
    row = rows[0]
    raw_content = str(row["content_json"])
    try:
        content = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_OUTLINE_CONTENT_INVALID",
            "accepted outline content_json is not readable JSON",
            commit_id=str(row["commit_id"]),
        ) from exc
    if not isinstance(content, Mapping):
        raise EventExperienceError(
            "EVENT_EXPERIENCE_OUTLINE_CONTENT_INVALID",
            "accepted outline content must be an object",
            commit_id=str(row["commit_id"]),
        )
    return {
        "binding": {
            "source_outline_commit_id": str(row["commit_id"]),
            "source_outline_artifact_version_id": str(
                row["artifact_version_id"]
            ),
            "source_outline_artifact_id": str(row["artifact_id"]),
            "source_outline_artifact_revision": int(
                row["artifact_revision"]
            ),
            "source_outline_content_hash": hashlib.sha256(
                raw_content.encode("utf-8")
            ).hexdigest(),
        },
        "event_seeds": _structured_outline_event_seeds(content),
        "content": dict(content),
    }


def _apply_accepted_outline(
    project_root: Path | str,
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    resolved = dict(artifact)
    if resolved.get("event_seeds") is not None:
        return resolved
    outline = _resolve_accepted_outline(project_root, resolved)
    if outline is None:
        return resolved
    event_seeds = outline.get("event_seeds")
    binding = dict(outline.get("binding") or {})
    if event_seeds is None:
        same_artifact = (
            str(resolved.get("artifact_id") or "")
            == str(binding.get("source_outline_artifact_id") or "")
        )
        requested_revision = int(resolved.get("artifact_revision") or 0)
        accepted_revision = int(
            binding.get("source_outline_artifact_revision") or 0
        )
        if same_artifact and requested_revision <= accepted_revision:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_GRANDFATHERED_REVISION_REQUIRED",
                "legacy accepted artifact requires a new revision before intended contracts are created",
                artifact_id=resolved.get("artifact_id"),
                accepted_revision=accepted_revision,
                requested_revision=requested_revision,
            )
        return resolved
    resolved["event_seeds"] = [dict(item) for item in event_seeds]
    resolved.update(binding)
    return resolved


def _derive_identity(
    *,
    prompt: str,
    artifact: Mapping[str, Any],
    intent_identity: Mapping[str, Any],
    session_identity: str,
    turn_identity: str,
) -> dict[str, Any]:
    material = {
        "prompt": prompt,
        "artifact": {
            "branch_id": artifact["branch_id"],
            "artifact_id": artifact["artifact_id"],
            "artifact_revision": artifact["artifact_revision"],
            "chapter_no": artifact.get("chapter_no"),
            "scene_index": artifact.get("scene_index"),
            "accepted_outline_binding": {
                key: artifact.get(key)
                for key in (
                    "source_outline_commit_id",
                    "source_outline_artifact_version_id",
                    "source_outline_artifact_id",
                    "source_outline_artifact_revision",
                    "source_outline_content_hash",
                )
                if artifact.get(key)
            },
        },
        "intent_contract_hash": intent_identity["intent_contract_hash"],
        "session_identity": session_identity,
        "turn_identity": turn_identity,
    }
    digest = canonical_hash(material)
    if artifact.get("source_outline_artifact_id"):
        lineage_digest = canonical_hash(
            {
                "lineage_kind": "accepted-outline-event-chain",
                "source_outline_artifact_id": artifact[
                    "source_outline_artifact_id"
                ],
                "branch_id": artifact["branch_id"],
                "artifact_id": artifact["artifact_id"],
                "chapter_no": artifact.get("chapter_no"),
                "scene_index": artifact.get("scene_index"),
            }
        )
    else:
        lineage_digest = digest
    return {
        "request_hash": digest,
        "lineage_hash": lineage_digest,
        "parent_chain_id": _text(
            str(
                artifact.get("parent_chain_id")
                or f"event-chain-{lineage_digest[:24]}"
            ),
            "parent_chain_id",
        ),
        "narrative_event_id": _text(
            str(
                artifact.get("narrative_event_id")
                or f"narrative-event-{lineage_digest[:24]}"
            ),
            "narrative_event_id",
        ),
    }


def _derive_seed_payloads(
    *,
    prompt: str,
    artifact: Mapping[str, Any],
    contract: Mapping[str, Any],
    identity: Mapping[str, Any],
) -> list[dict[str, Any]]:
    fields = contract["fields"]
    explicit = artifact.get("event_seeds")
    if explicit is None:
        explicit = [{}]
    if not isinstance(explicit, Sequence) or isinstance(
        explicit, (str, bytes, bytearray)
    ):
        raise EventExperienceError(
            "EVENT_EXPERIENCE_RUNTIME_SEEDS",
            "artifact_context.event_seeds must be an array",
        )
    if not explicit or len(explicit) > 64:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_RUNTIME_SEEDS",
            "event_seeds must contain between 1 and 64 events",
        )
    payloads: list[dict[str, Any]] = []
    for index, item in enumerate(explicit, start=1):
        if not isinstance(item, Mapping):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_RUNTIME_SEED",
                "each event seed candidate must be an object",
                index=index - 1,
            )
        item = dict(item)
        dependency_order = _integer(
            item.get("dependency_order", index),
            f"event_seeds[{index - 1}].dependency_order",
        )
        seed_scene_index = item.get(
            "scene_index",
            artifact.get("scene_index")
            if len(explicit) == 1
            else index - 1,
        )
        if artifact.get("source_outline_artifact_id"):
            event_material = {
                "lineage_hash": identity["lineage_hash"],
                "dependency_order": dependency_order,
                "scene_index": seed_scene_index,
            }
        else:
            event_material = {
                "request_hash": identity["request_hash"],
                "dependency_order": dependency_order,
                "candidate": item,
            }
        event_digest = canonical_hash(event_material)
        payload = {
            "event_seed_id": _text(
                str(
                    item.get("event_seed_id")
                    or f"event-seed-{event_digest[:24]}"
                ),
                "event_seed_id",
            ),
            "event_seed_revision": 1,
            "parent_chain_id": identity["parent_chain_id"],
            "dependency_order": dependency_order,
            "dramatic_function": _text(
                str(
                    item.get("dramatic_function")
                    or fields["problem_to_solve"]["value"]
                ),
                "dramatic_function",
            ),
            "causal_role": _text(
                str(
                    item.get("causal_role")
                    or fields["protagonist_drive_conflict"]["value"]
                ),
                "causal_role",
            ),
            "intended_state_change": _text(
                str(
                    item.get("intended_state_change")
                    or fields["success_criteria"]["value"]
                ),
                "intended_state_change",
            ),
            "event_boundary": _text(
                str(
                    item.get("event_boundary")
                    or fields["scope_endpoint"]["value"]
                ),
                "event_boundary",
            ),
            "narrative_event_id": _text(
                str(
                    item.get("narrative_event_id")
                    or (
                        identity["narrative_event_id"]
                        if len(explicit) == 1
                        else f"{identity['narrative_event_id']}-{index}"
                    )
                ),
                "narrative_event_id",
            ),
            "artifact_id": artifact["artifact_id"],
            "artifact_revision": artifact["artifact_revision"],
            "branch_id": artifact["branch_id"],
            "chapter_no": artifact.get("chapter_no"),
            "scene_index": seed_scene_index,
        }
        if artifact.get("source_outline_commit_id"):
            payload.update(
                {
                    "source_outline_commit_id": artifact[
                        "source_outline_commit_id"
                    ],
                    "source_outline_artifact_version_id": artifact[
                        "source_outline_artifact_version_id"
                    ],
                    "source_outline_artifact_id": artifact[
                        "source_outline_artifact_id"
                    ],
                    "source_outline_artifact_revision": artifact[
                        "source_outline_artifact_revision"
                    ],
                    "source_outline_content_hash": artifact[
                        "source_outline_content_hash"
                    ],
                }
            )
        payloads.append(payload)
    orders = [payload["dependency_order"] for payload in payloads]
    if len(set(orders)) != len(orders):
        raise EventExperienceError(
            "EVENT_EXPERIENCE_RUNTIME_SEED_ORDER",
            "derived EventSeeds require unique dependency_order values",
        )
    seed_ids = [payload["event_seed_id"] for payload in payloads]
    if len(set(seed_ids)) != len(seed_ids):
        raise EventExperienceError(
            "EVENT_EXPERIENCE_RUNTIME_SEED_ID",
            "derived EventSeeds require unique event_seed_id values",
        )
    return payloads


def _seed_scope(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_outline_artifact_id": str(
            payload.get("source_outline_artifact_id") or ""
        ),
        "branch_id": str(payload.get("branch_id") or "main"),
        "artifact_id": str(payload.get("artifact_id") or ""),
        "chapter_no": payload.get("chapter_no"),
        "scene_index": payload.get("scene_index"),
    }


def _seed_semantic_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "event_seed_revision",
            "supersedes_seed_revision",
            "seed_hash",
            "status",
            "experience_contract_id",
            "experience_contract_hash",
            "retired_at",
            "retired_reason",
            "created_at",
            "updated_at",
        }
    }


def _seed_version(payload: Mapping[str, Any]) -> tuple[int, int]:
    return (
        int(payload.get("artifact_revision") or 0),
        int(payload.get("source_outline_artifact_revision") or 0),
    )


def _preflight_seed_mutations(
    service: EventExperienceService,
    payloads: Sequence[Mapping[str, Any]],
    *,
    expected_control_revision: int,
) -> list[dict[str, Any]]:
    """Plan every Seed mutation before the first semantic write."""

    plans: list[dict[str, Any]] = []
    planned_ids = {str(payload["event_seed_id"]) for payload in payloads}
    desired_orders = {
        int(payload["dependency_order"]): str(payload["event_seed_id"])
        for payload in payloads
    }
    parent_chain_ids = {
        str(payload["parent_chain_id"]) for payload in payloads
    }
    if len(parent_chain_ids) != 1:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_RUNTIME_SEED_CHAIN",
            "one runtime request must derive exactly one parent chain",
        )
    parent_chain_id = next(iter(parent_chain_ids))

    with service._transaction(write=False) as connection:
        actual_control_revision = service._control_revision(connection)
        if actual_control_revision != expected_control_revision:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_STALE_CONTROL",
                "event-experience control revision changed",
                expected_control_revision=expected_control_revision,
                actual_control_revision=actual_control_revision,
            )

        for raw_payload in payloads:
            normalized = service._seed_payload(raw_payload)
            seed_id = str(normalized["event_seed_id"])
            previous_row = connection.execute(
                """
                SELECT * FROM event_seeds
                WHERE event_seed_id=?
                ORDER BY event_seed_revision DESC LIMIT 1
                """,
                (seed_id,),
            ).fetchone()
            if previous_row is None:
                plans.append(
                    {
                        "action": "create",
                        "payload": normalized,
                        "seed": None,
                        "event_seed_revision": 1,
                        "seed_hash": canonical_hash(normalized),
                    }
                )
                continue

            previous = service._seed_from_row(previous_row)
            if previous["status"] == "retired":
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_SEED_RETIRED",
                    "retired EventSeed cannot be reused by the runtime",
                    event_seed_id=seed_id,
                    event_seed_revision=previous["event_seed_revision"],
                )
            previous_scope = _seed_scope(previous)
            current_scope = _seed_scope(normalized)
            if (
                previous_scope != current_scope
                or str(previous["parent_chain_id"])
                != str(normalized["parent_chain_id"])
            ):
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_SEED_SCOPE_COLLISION",
                    "EventSeed id belongs to a different outline/runtime scope",
                    event_seed_id=seed_id,
                    previous_scope=previous_scope,
                    current_scope=current_scope,
                )

            previous_version = _seed_version(previous)
            current_version = _seed_version(normalized)
            if any(
                current < old
                for current, old in zip(current_version, previous_version)
            ):
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_SEED_REVISION_REGRESSION",
                    "EventSeed source revisions cannot move backwards",
                    event_seed_id=seed_id,
                    previous_artifact_revision=previous_version[0],
                    current_artifact_revision=current_version[0],
                    previous_outline_revision=previous_version[1],
                    current_outline_revision=current_version[1],
                )
            advanced = any(
                current > old
                for current, old in zip(current_version, previous_version)
            )
            if not advanced:
                if canonical_hash(_seed_semantic_payload(previous)) != canonical_hash(
                    _seed_semantic_payload(normalized)
                ):
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_SEED_REVISION_CONFLICT",
                        "same source revision produced a different EventSeed payload",
                        event_seed_id=seed_id,
                        artifact_revision=current_version[0],
                        source_outline_artifact_revision=current_version[1],
                    )
                plans.append(
                    {
                        "action": "reuse",
                        "payload": normalized,
                        "seed": previous,
                        "event_seed_revision": int(
                            previous["event_seed_revision"]
                        ),
                        "seed_hash": str(previous["seed_hash"]),
                    }
                )
                continue

            replacement = dict(normalized)
            replacement["event_seed_revision"] = (
                int(previous["event_seed_revision"]) + 1
            )
            replacement["supersedes_seed_revision"] = int(
                previous["event_seed_revision"]
            )
            normalized_replacement = service._seed_payload(replacement)
            plans.append(
                {
                    "action": "supersede",
                    "payload": normalized,
                    "replacement": normalized_replacement,
                    "seed": previous,
                    "event_seed_revision": int(
                        normalized_replacement["event_seed_revision"]
                    ),
                    "seed_hash": canonical_hash(normalized_replacement),
                }
            )

        active_rows = connection.execute(
            """
            SELECT event_seed_id, event_seed_revision, dependency_order
            FROM event_seeds
            WHERE parent_chain_id=? AND status!='retired'
            """,
            (parent_chain_id,),
        ).fetchall()
        for active in active_rows:
            active_id = str(active["event_seed_id"])
            if active_id in planned_ids:
                continue
            dependency_order = int(active["dependency_order"])
            if dependency_order in desired_orders:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_DEPENDENCY_ORDER_CONFLICT",
                    "another active EventSeed occupies a planned dependency order",
                    event_seed_id=active_id,
                    event_seed_revision=int(active["event_seed_revision"]),
                    dependency_order=dependency_order,
                    planned_event_seed_id=desired_orders[dependency_order],
                )
    return plans


def _arc_semantic_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "arc_revision",
            "supersedes_arc_revision",
            "event_seed_bindings",
            "arc_hash",
            "status",
            "locked_at",
            "retired_at",
            "retired_reason",
            "created_at",
            "updated_at",
        }
    }


def _load_arc_snapshot(
    service: EventExperienceService,
    arc_id: str,
    *,
    expected_control_revision: int,
) -> dict[str, Any] | None:
    with service._transaction(write=False) as connection:
        actual_control_revision = service._control_revision(connection)
        if actual_control_revision != expected_control_revision:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_STALE_CONTROL",
                "event-experience control revision changed",
                expected_control_revision=expected_control_revision,
                actual_control_revision=actual_control_revision,
            )
        row = connection.execute(
            """
            SELECT * FROM event_experience_arcs
            WHERE arc_id=?
            ORDER BY arc_revision DESC LIMIT 1
            """,
            (arc_id,),
        ).fetchone()
        if row is None:
            return None
        arc = service._arc_from_row(row)
        source_outline_revision = 0
        for binding in arc.get("event_seed_bindings", []):
            seed_row = connection.execute(
                """
                SELECT payload_json FROM event_seeds
                WHERE event_seed_id=? AND event_seed_revision=?
                """,
                (
                    binding["event_seed_id"],
                    binding["event_seed_revision"],
                ),
            ).fetchone()
            if seed_row is None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_ARC_BINDING_STALE",
                    "arc references a missing EventSeed revision",
                    event_seed_id=binding["event_seed_id"],
                    event_seed_revision=binding["event_seed_revision"],
                )
            seed_payload = json.loads(str(seed_row["payload_json"]))
            source_outline_revision = max(
                source_outline_revision,
                int(
                    seed_payload.get("source_outline_artifact_revision")
                    or 0
                ),
            )
        return {
            "arc": arc,
            "artifact_revision": int(arc.get("artifact_revision") or 0),
            "source_outline_artifact_revision": source_outline_revision,
        }


def _plan_arc_mutation(
    snapshot: Mapping[str, Any] | None,
    desired_arc: Mapping[str, Any],
    seed_plans: Sequence[Mapping[str, Any]],
    *,
    auto_lock: bool,
) -> str:
    if snapshot is None:
        return "create"
    arc = snapshot["arc"]
    if arc["status"] == "retired":
        raise EventExperienceError(
            "EVENT_EXPERIENCE_ARC_RETIRED",
            "retired EventExperienceArc cannot be reused by the runtime",
            arc_id=arc["arc_id"],
            arc_revision=arc["arc_revision"],
        )
    if (
        str(arc["parent_chain_id"]) != str(desired_arc["parent_chain_id"])
        or str(arc.get("branch_id") or "main")
        != str(desired_arc.get("branch_id") or "main")
        or str(arc.get("artifact_id") or "")
        != str(desired_arc.get("artifact_id") or "")
    ):
        raise EventExperienceError(
            "EVENT_EXPERIENCE_ARC_SCOPE_COLLISION",
            "EventExperienceArc id belongs to a different runtime scope",
            arc_id=arc["arc_id"],
        )

    previous_version = (
        int(snapshot["artifact_revision"]),
        int(snapshot["source_outline_artifact_revision"]),
    )
    current_outline_revisions = {
        int(
            plan["payload"].get("source_outline_artifact_revision")
            or 0
        )
        for plan in seed_plans
    }
    if len(current_outline_revisions) != 1:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_RUNTIME_SEED_SOURCE",
            "one runtime arc must bind one accepted outline revision",
        )
    current_version = (
        int(desired_arc.get("artifact_revision") or 0),
        next(iter(current_outline_revisions)),
    )
    if any(
        current < old
        for current, old in zip(current_version, previous_version)
    ):
        raise EventExperienceError(
            "EVENT_EXPERIENCE_ARC_REVISION_REGRESSION",
            "EventExperienceArc source revisions cannot move backwards",
            arc_id=arc["arc_id"],
            previous_artifact_revision=previous_version[0],
            current_artifact_revision=current_version[0],
            previous_outline_revision=previous_version[1],
            current_outline_revision=current_version[1],
        )
    advanced = any(
        current > old
        for current, old in zip(current_version, previous_version)
    )
    if advanced:
        return "supersede"

    expected_bindings = [
        {
            "event_seed_id": str(plan["payload"]["event_seed_id"]),
            "event_seed_revision": int(plan["event_seed_revision"]),
            "seed_hash": str(plan["seed_hash"]),
        }
        for plan in seed_plans
    ]
    if arc.get("event_seed_bindings") != expected_bindings:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_ARC_REVISION_CONFLICT",
            "same source revision produced different EventSeed bindings",
            arc_id=arc["arc_id"],
            arc_revision=arc["arc_revision"],
        )
    if canonical_hash(_arc_semantic_payload(arc)) == canonical_hash(
        _arc_semantic_payload(desired_arc)
    ):
        return "reuse"
    if not auto_lock:
        # A previously answered bounded question intentionally stores a
        # selected trajectory that differs from the initial default plan.
        return "reuse"
    raise EventExperienceError(
        "EVENT_EXPERIENCE_ARC_REVISION_CONFLICT",
        "same source revision produced a different EventExperienceArc payload",
        arc_id=arc["arc_id"],
        arc_revision=arc["arc_revision"],
    )


def _arc_matches(
    arc: Mapping[str, Any],
    payload: Mapping[str, Any],
    seeds: Sequence[Mapping[str, Any]],
) -> bool:
    expected_bindings = [
        {
            "event_seed_id": seed["event_seed_id"],
            "event_seed_revision": seed["event_seed_revision"],
            "seed_hash": seed["seed_hash"],
        }
        for seed in seeds
    ]
    return (
        arc.get("event_seed_bindings") == expected_bindings
        and canonical_hash(_arc_semantic_payload(arc))
        == canonical_hash(_arc_semantic_payload(payload))
    )


def _emotion_plan(
    *,
    contract: Mapping[str, Any],
    artifact: Mapping[str, Any],
    selected_value: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    fields = contract["fields"]
    experience = fields["reader_experience"]["value"]
    selected = dict(selected_value or {})
    hits = [term for term in _EMOTION_TERMS if term in experience]
    primary = str(
        selected.get("primary_emotion")
        or artifact.get("primary_emotion")
        or (hits[0] if hits else "期待")
    )
    secondaries = selected.get("ordered_secondary_emotions")
    if secondaries is None:
        secondaries = [term for term in hits[1:] if term != primary]
    if not secondaries:
        secondaries = ["紧张", "余悸"] if primary != "紧张" else ["希望", "余悸"]
    entry = str(
        selected.get("entry_reader_state")
        or artifact.get("entry_reader_state")
        or artifact.get("previous_aftertaste")
        or "承接上一事件余味并感到局势仍有压力"
    )
    target = str(
        selected.get("target_reader_state")
        or artifact.get("target_reader_state")
        or experience
    )
    aftertaste = str(
        selected.get("aftertaste")
        or artifact.get("aftertaste")
        or "阶段兑现后仍保留推动下一事件的未解压力"
    )
    return {
        "entry_reader_state": _text(entry, "entry_reader_state"),
        "target_reader_state": _text(target, "target_reader_state"),
        "primary_emotion": _text(primary, "primary_emotion"),
        "ordered_secondary_emotions": [
            _text(str(item), "ordered_secondary_emotions")
            for item in secondaries
        ],
        "emotional_turn": _text(
            str(
                selected.get("emotional_turn")
                or f"从{entry}转向{target}"
            ),
            "emotional_turn",
        ),
        "intensity": selected.get(
            "intensity", {"entry": 35, "peak": 80, "exit": 55}
        ),
        "emotion_curve": selected.get(
            "emotion_curve",
            ["期待", "压力升级", "信息转折", "阶段兑现", aftertaste],
        ),
        "mechanisms": selected.get(
            "mechanisms",
            ["信息差", "选择代价", "主动对手反应", "局部兑现"],
        ),
        "reader_knowledge_position": _text(
            str(
                selected.get("reader_knowledge_position")
                or artifact.get(
                    "reader_knowledge_position", "与视角人物同步"
                )
            ),
            "reader_knowledge_position",
        ),
        "viewpoint_character_state": _text(
            str(
                selected.get("viewpoint_character_state")
                or artifact.get("viewpoint_character_state")
                or fields["protagonist_drive_conflict"]["value"]
            ),
            "viewpoint_character_state",
        ),
        "payoff_or_reveal": _text(
            str(
                selected.get("payoff_or_reveal")
                or fields["success_criteria"]["value"]
            ),
            "payoff_or_reveal",
        ),
        "aftertaste": _text(aftertaste, "aftertaste"),
        "anti_experiences": selected.get(
            "anti_experiences",
            [fields["hard_constraints"]["value"]],
        ),
        "success_signals": selected.get(
            "success_signals",
            [
                fields["success_criteria"]["value"],
                f"读者离场状态达到：{target}",
            ],
        ),
        "open_loop_links": selected.get(
            "open_loop_links", artifact.get("open_loop_links", [])
        ),
    }


def _derivation_mode(contract: Mapping[str, Any]) -> dict[str, Any]:
    fields = contract["fields"]
    experience = fields["reader_experience"]
    autonomy = fields["model_autonomy"]
    delegated = experience["source"] == "recommended_delegation" or any(
        marker in autonomy["value"] for marker in _DELEGATION_MARKERS
    )
    explicit = experience["source"] in _EXPLICIT_SOURCES
    return {
        "auto_lock": bool(explicit or delegated),
        "delegated": bool(delegated),
        "user_confirmed": experience["source"] in {"prompt", "user_answer"},
        "confidence": 0.94 if explicit else (0.88 if delegated else 0.55),
    }


def _derive_arc(
    *,
    seeds: Sequence[Mapping[str, Any]],
    artifact: Mapping[str, Any],
    identity: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": EVENT_EXPERIENCE_SCHEMA_VERSION,
        "arc_id": f"experience-arc-{identity['lineage_hash'][:24]}",
        "arc_revision": 1,
        "parent_chain_id": identity["parent_chain_id"],
        "entry_reader_state": plan["entry_reader_state"],
        "target_reader_state": plan["target_reader_state"],
        "overall_peak": plan["payoff_or_reveal"],
        "release_rhythm": "逐步加压，在事件峰值完成一次有限释放",
        "aftertaste": plan["aftertaste"],
        "event_seed_ids": [seed["event_seed_id"] for seed in seeds],
        "branch_id": artifact["branch_id"],
        "artifact_id": artifact["artifact_id"],
        "artifact_revision": artifact["artifact_revision"],
    }


def _derive_contracts(
    *,
    seeds: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any],
    intent_identity: Mapping[str, Any],
    mode: Mapping[str, Any],
    identity: Mapping[str, Any],
) -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = []
    total = len(seeds)
    for index, seed in enumerate(seeds):
        primary = plan["primary_emotion"]
        if total > 1 and index < total - 1:
            primary = (
                "压迫"
                if index == 0
                else str(plan["ordered_secondary_emotions"][0])
            )
        contract_digest = canonical_hash(
            {
                "request_hash": identity["request_hash"],
                "event_seed_id": seed["event_seed_id"],
                "event_seed_revision": seed["event_seed_revision"],
                "primary_emotion": primary,
                "plan": plan,
            }
        )
        provenance = {
            field: {
                "source": "locked_intent_contract_or_artifact_context",
                "source_intent_contract_hash": intent_identity[
                    "intent_contract_hash"
                ],
                "request_hash": identity["request_hash"],
            }
            for field in (
                "entry_reader_state",
                "target_reader_state",
                "primary_emotion",
                "emotional_turn",
                "intensity",
                "emotion_curve",
                "mechanisms",
                "reader_knowledge_position",
                "viewpoint_character_state",
                "payoff_or_reveal",
                "aftertaste",
                "anti_experiences",
                "success_signals",
            )
        }
        provenance.update(
            {
                "source_intent_contract_hash": {
                    "source": "locked_intent_contract",
                    "value": intent_identity["intent_contract_hash"],
                },
                "runtime_schema": {
                    "source": "event_experience_runtime",
                    "value": EVENT_EXPERIENCE_SCHEMA_VERSION,
                },
                "request_hash": {
                    "source": "event_experience_runtime",
                    "value": identity["request_hash"],
                },
            }
        )
        contracts.append(
            {
                "contract_id": f"experience-contract-{contract_digest[:24]}",
                "contract_revision": 1,
                "event_seed_id": seed["event_seed_id"],
                "event_seed_revision": seed["event_seed_revision"],
                "source_intent_contract_id": intent_identity[
                    "intent_contract_id"
                ],
                "source_intent_contract_revision": intent_identity[
                    "intent_contract_revision"
                ],
                "source_intent_contract_hash": intent_identity[
                    "intent_contract_hash"
                ],
                "entry_reader_state": plan["entry_reader_state"],
                "target_reader_state": plan["target_reader_state"],
                "primary_emotion": primary,
                "ordered_secondary_emotions": plan[
                    "ordered_secondary_emotions"
                ],
                "emotional_turn": plan["emotional_turn"],
                "intensity": plan["intensity"],
                "emotion_curve": plan["emotion_curve"],
                "mechanisms": plan["mechanisms"],
                "reader_knowledge_position": plan[
                    "reader_knowledge_position"
                ],
                "viewpoint_character_state": plan[
                    "viewpoint_character_state"
                ],
                "payoff_or_reveal": plan["payoff_or_reveal"],
                "aftertaste": plan["aftertaste"],
                "anti_experiences": plan["anti_experiences"],
                "success_signals": plan["success_signals"],
                "open_loop_links": plan["open_loop_links"],
                "derivation": {
                    "source": "locked_intent_contract",
                    "confidence": mode["confidence"],
                    "user_confirmed": mode["user_confirmed"],
                    "delegated_choice": mode["delegated"],
                },
                "field_provenance": provenance,
            }
        )
    return contracts


def _question_options(
    contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    success = contract["fields"]["success_criteria"]["value"]
    constraint = contract["fields"]["hard_constraints"]["value"]
    return [
        {
            "option_id": "A",
            "label": "压迫中看到一线可行突破口",
            "value": {
                "primary_emotion": "希望",
                "ordered_secondary_emotions": ["压迫", "紧张", "期待"],
                "target_reader_state": "压力仍在，但确认主角找到可行突破口",
                "emotional_turn": "从窒息压迫转向有限希望",
                "aftertaste": "突破口真实存在，但代价尚未完全显现",
                "payoff_or_reveal": success,
                "anti_experiences": [constraint, "过早彻底释压"],
            },
        },
        {
            "option_id": "B",
            "label": "短暂痛快后意识到代价更大",
            "value": {
                "primary_emotion": "痛快",
                "ordered_secondary_emotions": ["紧张", "释放", "余悸"],
                "target_reader_state": "获得局部痛快，同时确认后果正在扩大",
                "emotional_turn": "从受压转为反击释放，再落入代价余悸",
                "aftertaste": "胜利不是终点，更大的代价已经启动",
                "payoff_or_reveal": success,
                "anti_experiences": [constraint, "无代价碾压"],
            },
        },
        {
            "option_id": "C",
            "label": "先松一口气，再留下身份或风险暴露的余悸",
            "value": {
                "primary_emotion": "余悸",
                "ordered_secondary_emotions": ["紧张", "释然", "不安"],
                "target_reader_state": "局部脱困后意识到新的暴露风险",
                "emotional_turn": "从持续紧张转为短暂释然，再回落到余悸",
                "aftertaste": "局部问题解决，新的追索理由成立",
                "payoff_or_reveal": success,
                "anti_experiences": [constraint, "把风险滑稽化"],
            },
        },
    ]


def _lifecycle_binding(
    manifest: Mapping[str, Any],
    arc: Mapping[str, Any],
) -> dict[str, Any]:
    binding = {
        "schema_version": EVENT_EXPERIENCE_SCHEMA_VERSION,
        "event_seed_manifest_hash": manifest[
            "event_seed_manifest_hash"
        ],
        "control_revision": manifest["control_revision"],
        "parent_chain_id": manifest["parent_chain_id"],
        "branch_id": manifest["branch_id"],
        "artifact_id": manifest["artifact_id"],
        "artifact_revision": manifest["artifact_revision"],
        "source_intent_contract_id": manifest[
            "source_intent_contract_id"
        ],
        "source_intent_contract_revision": manifest[
            "source_intent_contract_revision"
        ],
        "source_intent_contract_hash": manifest[
            "source_intent_contract_hash"
        ],
        "arc_id": arc["arc_id"],
        "arc_revision": arc["arc_revision"],
        "arc_hash": arc["arc_hash"],
        "contracts": [
            {
                "event_seed_id": item["event_seed_id"],
                "event_seed_revision": item["event_seed_revision"],
                "seed_hash": item["seed_hash"],
                "contract_id": item["contract_id"],
                "contract_revision": item["contract_revision"],
                "contract_hash": item["contract_hash"],
            }
            for item in manifest["contracts"]
        ],
    }
    if isinstance(manifest.get("accepted_outline_binding"), Mapping):
        binding["accepted_outline_binding"] = dict(
            manifest["accepted_outline_binding"]
        )
    binding["binding_hash"] = canonical_hash(
        {
            key: value
            for key, value in binding.items()
            if key != "control_revision"
        }
    )
    return binding


def ensure_locked_manifest(
    project_root: Path | str,
    *,
    prompt: str,
    artifact_context: Mapping[str, Any],
    intent_contract: Mapping[str, Any],
    session_identity: str,
    turn_identity: str,
    expected_control_revision: int | None = None,
    idempotency_key: str | None = None,
    question_ttl_seconds: int = 21_600,
) -> dict[str, Any]:
    """Ensure an event-experience gate result without performing remote work."""

    prompt_text = _text(prompt, "prompt")
    session_key = _text(session_identity, "session_identity")
    turn_key = _text(turn_identity, "turn_identity")
    artifact = _apply_accepted_outline(
        project_root,
        _artifact_context(artifact_context),
    )
    intent, intent_identity = _unwrap_intent(intent_contract)
    identity = _derive_identity(
        prompt=prompt_text,
        artifact=artifact,
        intent_identity=intent_identity,
        session_identity=session_key,
        turn_identity=turn_key,
    )
    base_key = _text(
        idempotency_key
        or f"experience-runtime-{identity['request_hash'][:32]}",
        "idempotency_key",
    )
    service = EventExperienceService.for_project(project_root)
    control_revision = (
        service.get_control_revision()
        if expected_control_revision is None
        else _integer(
            expected_control_revision,
            "expected_control_revision",
        )
    )
    seed_payloads = _derive_seed_payloads(
        prompt=prompt_text,
        artifact=artifact,
        contract=intent,
        identity=identity,
    )
    seed_plans = _preflight_seed_mutations(
        service,
        seed_payloads,
        expected_control_revision=control_revision,
    )
    mode = _derivation_mode(intent)
    initial_plan = _emotion_plan(
        contract=intent,
        artifact=artifact,
    )
    planned_seeds = [
        {
            "event_seed_id": plan["payload"]["event_seed_id"],
            "event_seed_revision": plan["event_seed_revision"],
            "seed_hash": plan["seed_hash"],
        }
        for plan in seed_plans
    ]
    arc_payload = _derive_arc(
        seeds=planned_seeds,
        artifact=artifact,
        identity=identity,
        plan=initial_plan,
    )
    arc_snapshot = _load_arc_snapshot(
        service,
        arc_payload["arc_id"],
        expected_control_revision=control_revision,
    )
    arc_action = _plan_arc_mutation(
        arc_snapshot,
        arc_payload,
        seed_plans,
        auto_lock=bool(mode["auto_lock"]),
    )
    service.claim_runtime_request(
        {
            "schema_version": EVENT_EXPERIENCE_SCHEMA_VERSION,
            "request_hash": identity["request_hash"],
            "lineage_hash": identity["lineage_hash"],
            "seed_payloads": seed_payloads,
            "arc_payload": arc_payload,
        },
        expected_control_revision=control_revision,
        idempotency_key=base_key,
    )

    seeds: list[dict[str, Any]] = []
    for index, plan in enumerate(seed_plans):
        if plan["action"] == "reuse":
            seeds.append(dict(plan["seed"]))
            continue
        if plan["action"] == "create":
            result = service.create_seed(
                plan["payload"],
                expected_control_revision=control_revision,
                idempotency_key=f"{base_key}:seed:{index}",
            )
        else:
            result = service.supersede_seed(
                plan["payload"]["event_seed_id"],
                plan["replacement"],
                expected_control_revision=control_revision,
                idempotency_key=f"{base_key}:seed:{index}",
                reason="accepted outline/runtime source revision advanced",
            )
        control_revision = int(result["control_revision"])
        seeds.append(result["seed"])
    seed_references = [
        {
            "event_seed_id": seed["event_seed_id"],
            "event_seed_revision": seed["event_seed_revision"],
        }
        for seed in seeds
    ]
    arc_payload = _derive_arc(
        seeds=seeds,
        artifact=artifact,
        identity=identity,
        plan=initial_plan,
    )
    if arc_action == "reuse":
        arc = dict(arc_snapshot["arc"])
    elif arc_action == "create":
        arc_result = service.create_arc(
            arc_payload,
            expected_control_revision=control_revision,
            idempotency_key=f"{base_key}:arc",
        )
        control_revision = int(arc_result["control_revision"])
        arc = arc_result["arc"]
    else:
        arc_result = service.supersede_arc(
            arc_payload["arc_id"],
            arc_payload,
            expected_control_revision=control_revision,
            idempotency_key=f"{base_key}:arc",
            reason="accepted outline/runtime source revision advanced",
        )
        control_revision = int(arc_result["control_revision"])
        arc = arc_result["arc"]

    selected_value: Mapping[str, Any] | None = None
    if not mode["auto_lock"]:
        seed_manifest = service.seed_manifest(seed_references)
        try:
            question = service.get_question(
                seed_manifest["event_seed_manifest_hash"]
            )
        except EventExperienceError as exc:
            if exc.code != "EVENT_EXPERIENCE_QUESTION_NOT_FOUND":
                raise
            opened = service.open_question(
                event_seed_manifest_hash=seed_manifest[
                    "event_seed_manifest_hash"
                ],
                seed_references=seed_references,
                question=(
                    "这条事件链结束时，你更希望读者经历哪一条主要体验轨迹？"
                ),
                options=_question_options(intent),
                recommended_option_id="C",
                rationale=(
                    "推荐 C：既完成局部兑现，又保留下一事件需要的持续压力。"
                ),
                expected_control_revision=control_revision,
                idempotency_key=f"{base_key}:question",
                ttl_seconds=question_ttl_seconds,
            )
            return {
                "action": "ask",
                "reason": "structural_ambiguity",
                "ready": False,
                "zero_remote": True,
                "seed_references": seed_references,
                "seed_manifest": seed_manifest,
                "arc": arc,
                "question": opened["question"],
                "control_revision": opened["control_revision"],
                "suppress_plot_receipt": True,
                "suppress_remote_retrieval": True,
                "suppress_stop_proposal": True,
            }
        if question["effective_status"] == "EXPIRED":
            return {
                "action": "expired",
                "reason": "session_ttl_expired",
                "ready": False,
                "zero_remote": True,
                "seed_references": seed_references,
                "seed_manifest": seed_manifest,
                "arc": arc,
                "question": question,
                "control_revision": service.get_control_revision(),
                "suppress_plot_receipt": True,
                "suppress_remote_retrieval": True,
                "suppress_stop_proposal": True,
            }
        if question["status"] in {
            "AWAITING_ANSWER",
            "AWAITING_EVENT_EXPERIENCE",
        }:
            return {
                "action": "ask",
                "reason": (
                    "awaiting_explicit_choice"
                    if question["status"] == "AWAITING_EVENT_EXPERIENCE"
                    else "question_already_open"
                ),
                "ready": False,
                "zero_remote": True,
                "seed_references": seed_references,
                "seed_manifest": seed_manifest,
                "arc": arc,
                "question": question,
                "control_revision": service.get_control_revision(),
                "suppress_plot_receipt": True,
                "suppress_remote_retrieval": True,
                "suppress_stop_proposal": True,
            }
        if question["status"] == "RETIRED":
            return {
                "action": "cancelled",
                "reason": "question_retired",
                "ready": False,
                "zero_remote": True,
                "seed_references": seed_references,
                "seed_manifest": seed_manifest,
                "arc": arc,
                "question": question,
                "control_revision": service.get_control_revision(),
                "suppress_plot_receipt": True,
                "suppress_remote_retrieval": True,
                "suppress_stop_proposal": True,
            }
        if question["status"] != "ANSWERED":
            raise EventExperienceError(
                "EVENT_EXPERIENCE_RUNTIME_QUESTION_STATE",
                "event-experience question has an unsupported state",
                status=question["status"],
            )
        selected = next(
            option
            for option in question["options"]
            if option["option_id"] == question["selected_option_id"]
        )
        selected_value = selected["value"]
        mode = {
            "auto_lock": True,
            "delegated": False,
            "user_confirmed": True,
            "confidence": 0.98,
        }

    plan = _emotion_plan(
        contract=intent,
        artifact=artifact,
        selected_value=selected_value,
    )
    if selected_value is not None:
        selected_arc_payload = _derive_arc(
            seeds=seeds,
            artifact=artifact,
            identity=identity,
            plan=plan,
        )
        selected_arc_payload["arc_id"] = arc["arc_id"]
        if not _arc_matches(arc, selected_arc_payload, seeds):
            selected_arc = service.supersede_arc(
                arc["arc_id"],
                selected_arc_payload,
                expected_control_revision=service.get_control_revision(),
                idempotency_key=f"{base_key}:arc-selected",
                reason="event experience question selected a locked trajectory",
            )
            arc = selected_arc["arc"]
    contracts = _derive_contracts(
        seeds=seeds,
        plan=plan,
        intent_identity=intent_identity,
        mode=mode,
        identity=identity,
    )
    control_revision = service.get_control_revision()
    for index, payload in enumerate(contracts):
        result = service.propose_and_lock_contract(
            payload,
            expected_control_revision=control_revision,
            idempotency_key=f"{base_key}:contract:{index}",
        )
        control_revision = int(result["control_revision"])
    if arc["status"] != "locked":
        locked_arc = service.lock_arc(
            arc["arc_id"],
            arc["arc_revision"],
            expected_control_revision=control_revision,
            idempotency_key=f"{base_key}:arc-lock",
            expected_arc_hash=arc["arc_hash"],
        )
        arc = locked_arc["arc"]
    manifest = service.locked_manifest(seed_references)
    binding = _lifecycle_binding(manifest, arc)
    return {
        "action": "locked",
        "reason": (
            "question_answer_locked"
            if selected_value is not None
            else (
                "delegated_auto_lock"
                if mode["delegated"]
                else "high_confidence_auto_lock"
            )
        ),
        "ready": True,
        "zero_remote": True,
        "seed_references": seed_references,
        "arc": arc,
        "manifest": manifest,
        "binding": binding,
        "control_revision": manifest["control_revision"],
        "suppress_plot_receipt": False,
        "suppress_remote_retrieval": False,
        "suppress_stop_proposal": False,
    }


def verify_locked_manifest(
    project_root: Path | str,
    *,
    seed_references: Sequence[Mapping[str, Any] | Sequence[Any]],
    expected_event_seed_manifest_hash: str | None = None,
    expected_control_revision: int | None = None,
    binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Revalidate receipt/proposal/grant/accept identity before remote or canon work."""

    expected_binding = dict(binding or {})
    manifest_hash = (
        expected_event_seed_manifest_hash
        or expected_binding.get("event_seed_manifest_hash")
    )
    revision = (
        expected_control_revision
        if expected_control_revision is not None
        else expected_binding.get("control_revision")
    )
    if manifest_hash is None or revision is None:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_RUNTIME_BINDING_REQUIRED",
            "manifest hash and control revision are required",
        )
    service = EventExperienceService.for_project(project_root)
    manifest = service.validate_locked_manifest(
        seed_references,
        expected_event_seed_manifest_hash=str(manifest_hash),
        expected_control_revision=_integer(
            revision, "expected_control_revision"
        ),
    )
    if expected_binding:
        contract_identities = [
            {
                "event_seed_id": item["event_seed_id"],
                "event_seed_revision": item["event_seed_revision"],
                "seed_hash": item["seed_hash"],
                "contract_id": item["contract_id"],
                "contract_revision": item["contract_revision"],
                "contract_hash": item["contract_hash"],
            }
            for item in manifest["contracts"]
        ]
        if contract_identities != expected_binding.get("contracts"):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_RUNTIME_BINDING_MISMATCH",
                "contract identity tuple changed",
            )
        arc_id = expected_binding.get("arc_id")
        arc_revision = expected_binding.get("arc_revision")
        arc_hash = expected_binding.get("arc_hash")
        if arc_id is None or arc_revision is None or arc_hash is None:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_RUNTIME_BINDING_REQUIRED",
                "binding must contain arc id, revision, and hash",
            )
        arc = service.get_arc(
            str(arc_id),
            _integer(arc_revision, "arc_revision", minimum=1),
        )
        if (
            arc["status"] != "locked"
            or arc["arc_hash"] != str(arc_hash)
            or arc["parent_chain_id"] != manifest["parent_chain_id"]
            or arc["branch_id"] != manifest["branch_id"]
            or arc["artifact_id"] != manifest["artifact_id"]
            or arc["artifact_revision"] != manifest["artifact_revision"]
        ):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_RUNTIME_BINDING_MISMATCH",
                "EventExperienceArc identity changed",
            )
        supplied_binding_hash = expected_binding.get("binding_hash")
        if supplied_binding_hash is not None:
            current_hash = canonical_hash(
                {
                    key: value
                    for key, value in expected_binding.items()
                    if key not in {"control_revision", "binding_hash"}
                }
            )
            if str(supplied_binding_hash) != current_hash:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_RUNTIME_BINDING_HASH_MISMATCH",
                    "lifecycle binding hash is invalid",
                )
    return {
        "action": "verified",
        "ready": True,
        "zero_remote": True,
        "manifest": manifest,
        "control_revision": manifest["control_revision"],
    }


__all__ = [
    "ensure_locked_manifest",
    "verify_locked_manifest",
]
