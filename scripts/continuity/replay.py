"""Deterministic replay from immutable accepted commits."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import defaultdict
from typing import Any, Iterable, Mapping

from .advantages import ADVANTAGE_EVENT_TYPES, rebuild_advantage_projection
from .items import ITEM_EVENT_TYPES, rebuild_item_projection
from .schema import PROJECTION_TABLES
from .source_manifest import replay_source_manifest
from .store import ContinuityStore, utc_now
from .validators import (
    ContinuityError,
    canonical_json,
    changes_authority,
    stable_hash,
)


MAX_CORRECTION_DEPTH = 32
_CORRECTION_EVENT_TYPES = frozenset(
    {"correction", "item_correction", "advantage_correction"}
)
_CORRECTION_INHERITED_FIELDS = (
    "scope",
    "branch_id",
    "chapter_no",
    "scene_index",
    "story_time",
    "story_coordinate",
    "narrative_mode",
)
_EVENT_LINK_FIELDS = ("supersedes", "retracts", "caused_by")
_BRANCH_LOCAL_ADVANTAGE_EVENT_TYPES = frozenset(
    {
        "advantage_bind",
        "advantage_activate",
        "advantage_trigger",
        "advantage_use",
        "advantage_reward",
        "advantage_cost",
        "advantage_upgrade",
    }
)


def _as_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    return json.loads(value)


def _fact_key(*parts: Any) -> str:
    return stable_hash([str(part or "") for part in parts], prefix="fact_")


def _strict_json_equal(left: Any, right: Any) -> bool:
    """Compare decoded JSON without Python's bool/numeric coercion.

    ``dict`` equality treats ``False == 0`` and ``1 == 1.0`` as true.  A
    frozen accepted payload is a typed JSON contract, so compare its canonical
    JSON representation instead.  Returning ``False`` for values that cannot
    be canonically encoded keeps the replay boundary fail-closed.
    """

    try:
        return canonical_json(left) == canonical_json(right)
    except (TypeError, ValueError, RecursionError):
        return False


def validate_event_branch_consistency(
    event: Mapping[str, Any],
    expected_branch_id: str,
    *,
    event_id: str = "",
    max_depth: int = MAX_CORRECTION_DEPTH,
) -> None:
    """Require every correction wrapper and replacement to stay in one branch.

    The public proposal path calls this before ``deepcopy``/normalization so a
    self-referential mapping fails with a controlled continuity error instead
    of a Python recursion failure. Replay calls it again against frozen JSON so
    direct database corruption is also fail-closed.
    """

    expected = str(expected_branch_id or "main")
    current: Mapping[str, Any] = event
    seen: set[int] = set()
    depth = 0
    while True:
        identity = id(current)
        if identity in seen:
            raise ContinuityError(
                "CORRECTION_REPLACEMENT_CYCLE",
                "correction replacement contains a cycle",
                details={"event_id": event_id, "depth": depth},
            )
        seen.add(identity)
        branch = str(current.get("branch_id") or expected)
        if branch != expected:
            raise ContinuityError(
                "PROPOSAL_EVENT_BRANCH_MISMATCH",
                "proposal events and correction replacements must stay in the proposal branch",
                details={
                    "event_id": event_id,
                    "expected_branch_id": expected,
                    "actual_branch_id": branch,
                    "depth": depth,
                },
            )
        event_type = str(
            current.get("event_type") or current.get("type") or "fact"
        )
        if event_type not in _CORRECTION_EVENT_TYPES:
            return
        if event_type.endswith("_correction") and str(
            current.get("action") or ""
        ) == "retract":
            return
        replacement = current.get("replacement")
        if not isinstance(replacement, Mapping):
            raise ContinuityError(
                "CORRECTION_REPLACEMENT_INVALID",
                "correction replacement must be an object",
                details={"event_id": event_id, "depth": depth},
            )
        if depth >= max_depth:
            raise ContinuityError(
                "CORRECTION_REPLACEMENT_DEPTH_EXCEEDED",
                "correction replacement nesting exceeds the replay limit",
                details={
                    "event_id": event_id,
                    "max_depth": max_depth,
                },
            )
        current = replacement
        depth += 1


def validate_correction_link_consistency(
    event: Mapping[str, Any],
    *,
    event_id: str = "",
    max_depth: int = MAX_CORRECTION_DEPTH,
) -> None:
    """Reject nested link claims that the stored outer event would lose.

    One accepted continuity row represents the complete correction chain, and
    ``event_links`` is sourced from that row's outer envelope.  Nested wrappers
    (or their leaf) may repeat an outer link, but they may not introduce a new
    supersede/retract/causality target that would otherwise disappear.
    """

    current: Mapping[str, Any] = event
    seen: set[int] = set()
    outer_links: dict[str, frozenset[str]] | None = None
    depth = 0
    while True:
        identity = id(current)
        if identity in seen:
            raise ContinuityError(
                "CORRECTION_REPLACEMENT_CYCLE",
                "correction replacement contains a cycle",
                details={"event_id": event_id, "depth": depth},
            )
        seen.add(identity)

        link_sets: dict[str, frozenset[str]] = {}
        for field in _EVENT_LINK_FIELDS:
            raw = current.get(field)
            if raw is None:
                values: list[Any] = []
            elif isinstance(raw, str):
                values = [raw]
            elif isinstance(raw, (list, tuple)):
                values = list(raw)
            else:
                raise ContinuityError(
                    "INVALID_EVENT_LINK",
                    f"{field} must be an event id or list of event ids",
                    details={
                        "event_id": event_id,
                        "depth": depth,
                        "field": field,
                    },
                )
            targets: list[str] = []
            for value in values:
                if not isinstance(value, str) or not value.strip():
                    raise ContinuityError(
                        "INVALID_EVENT_LINK",
                        f"{field} contains an invalid event id",
                        details={
                            "event_id": event_id,
                            "depth": depth,
                            "field": field,
                        },
                    )
                targets.append(value.strip())
            if len(targets) != len(set(targets)):
                raise ContinuityError(
                    "INVALID_EVENT_LINK",
                    f"{field} contains duplicate event ids",
                    details={
                        "event_id": event_id,
                        "depth": depth,
                        "field": field,
                    },
                )
            link_sets[field] = frozenset(targets)

        if outer_links is None:
            outer_links = link_sets
        else:
            for field, targets in link_sets.items():
                if targets and not targets.issubset(outer_links[field]):
                    raise ContinuityError(
                        "CORRECTION_NESTED_LINK_MISMATCH",
                        "nested correction links must be represented by the outer event",
                        details={
                            "event_id": event_id,
                            "depth": depth,
                            "field": field,
                            "outer_targets": sorted(outer_links[field]),
                            "nested_targets": sorted(targets),
                        },
                    )

        event_type = str(
            current.get("event_type") or current.get("type") or "fact"
        )
        if event_type not in _CORRECTION_EVENT_TYPES:
            return
        if event_type.endswith("_correction") and str(
            current.get("action") or ""
        ) == "retract":
            return
        replacement = current.get("replacement")
        if not isinstance(replacement, Mapping):
            raise ContinuityError(
                "CORRECTION_REPLACEMENT_INVALID",
                "correction replacement must be an object",
                details={"event_id": event_id, "depth": depth},
            )
        if depth >= max_depth:
            raise ContinuityError(
                "CORRECTION_REPLACEMENT_DEPTH_EXCEEDED",
                "correction replacement nesting exceeds the replay limit",
                details={"event_id": event_id, "max_depth": max_depth},
            )
        current = replacement
        depth += 1


def expand_correction_event(
    event_type: str,
    payload: Mapping[str, Any],
    *,
    event_id: str = "",
    max_depth: int = MAX_CORRECTION_DEPTH,
) -> tuple[str, dict[str, Any]] | None:
    """Recursively resolve correction wrappers to one deterministic leaf.

    ``None`` represents a retraction-style wrapper that contributes no fact of
    its own. Wrapper story coordinates are inherited only when the replacement
    omits them, matching the original single-level replay behavior.
    """

    current_type = str(event_type or payload.get("event_type") or "fact")
    current: Mapping[str, Any] = payload
    seen: set[int] = set()
    depth = 0
    while True:
        if current_type == "retraction":
            return None
        if current_type not in _CORRECTION_EVENT_TYPES:
            return current_type, dict(current)
        if current_type.endswith("_correction") and str(
            current.get("action") or ""
        ) == "retract":
            return None

        identity = id(current)
        if identity in seen:
            raise ContinuityError(
                "CORRECTION_REPLACEMENT_CYCLE",
                "correction replacement contains a cycle",
                details={"event_id": event_id, "depth": depth},
            )
        seen.add(identity)
        replacement = current.get("replacement")
        if not isinstance(replacement, Mapping):
            raise ContinuityError(
                "CORRECTION_REPLACEMENT_INVALID",
                "correction replacement must be an object",
                details={"event_id": event_id, "depth": depth},
            )
        if id(replacement) in seen:
            raise ContinuityError(
                "CORRECTION_REPLACEMENT_CYCLE",
                "correction replacement contains a cycle",
                details={"event_id": event_id, "depth": depth + 1},
            )
        if depth >= max_depth:
            raise ContinuityError(
                "CORRECTION_REPLACEMENT_DEPTH_EXCEEDED",
                "correction replacement nesting exceeds the replay limit",
                details={
                    "event_id": event_id,
                    "max_depth": max_depth,
                },
            )
        next_payload = dict(replacement)
        for field in _CORRECTION_INHERITED_FIELDS:
            if field not in next_payload and current.get(field) is not None:
                next_payload[field] = current.get(field)
        current = next_payload
        current_type = str(current.get("event_type") or "fact")
        depth += 1


def _expanded_event_row(
    event_row: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return one replay row whose type/payload describe the correction leaf."""

    row = dict(event_row)
    event_id = str(row.get("event_id") or "")
    try:
        payload = _as_json(str(row.get("payload_json") or ""), {})
    except (
        TypeError,
        ValueError,
        json.JSONDecodeError,
        RecursionError,
    ) as exc:
        raise ContinuityError(
            "ACCEPTED_EVENT_PAYLOAD_CORRUPT",
            "accepted event payload is not valid JSON",
            details={"event_id": event_id},
        ) from exc
    if not isinstance(payload, Mapping):
        raise ContinuityError(
            "ACCEPTED_EVENT_PAYLOAD_CORRUPT",
            "accepted event payload must be an object",
            details={"event_id": event_id},
        )
    expanded = expand_correction_event(
        str(row.get("event_type") or payload.get("event_type") or "fact"),
        payload,
        event_id=event_id,
    )
    if expanded is None:
        return None
    leaf_type, leaf_payload = expanded
    row["event_type"] = leaf_type
    row["payload_json"] = canonical_json(leaf_payload)
    row["branch_id"] = str(
        leaf_payload.get("branch_id") or row.get("branch_id") or "main"
    )
    row["scope"] = str(
        leaf_payload.get("scope") or row.get("scope") or "current"
    )
    return row


def _advantage_replay_rows(
    event_rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Route Advantage events to current or branch-local replay planes.

    Planned/history facts stay in the generic projections.  A non-main
    accepted proposal may replay only state that is physically branch-keyed in
    Advantage v1; global definitions, anchors, knowledge and contracts remain
    represented by ``branch_facts`` until their schemas gain branch identity.

    Correction leaves are replayed at the original target's order so dependent
    events observe the corrected definition/module rather than the later
    correction commit position.
    """

    rows = [dict(row) for row in event_rows]
    order_by_event_id = {
        str(row.get("event_id") or ""): int(row.get("updated_order") or 0)
        for row in rows
        if str(row.get("event_id") or "")
    }
    correction_targets: dict[str, str] = {}
    for row in rows:
        event_id = str(row.get("event_id") or "")
        if not event_id:
            continue
        try:
            payload = _as_json(str(row.get("payload_json") or ""), {})
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(payload, Mapping):
            continue
        event_type = str(
            row.get("event_type") or payload.get("event_type") or ""
        )
        if event_type not in _CORRECTION_EVENT_TYPES:
            continue
        target = str(payload.get("target_event_id") or "").strip()
        if not target:
            supersedes = payload.get("supersedes")
            if isinstance(supersedes, str):
                target = supersedes.strip()
            elif isinstance(supersedes, list) and len(supersedes) == 1:
                target = str(supersedes[0] or "").strip()
        if target:
            correction_targets[event_id] = target

    def effective_order(event_id: str, fallback: int) -> int:
        current = event_id
        seen: set[str] = set()
        while current in correction_targets and current not in seen:
            seen.add(current)
            current = correction_targets[current]
        return int(order_by_event_id.get(current, fallback))

    replay_rows: list[dict[str, Any]] = []
    for row in rows:
        expanded = _expanded_event_row(row)
        if expanded is None:
            continue
        event_type = str(expanded.get("event_type") or "")
        if event_type not in ADVANTAGE_EVENT_TYPES:
            continue
        payload = _as_json(str(expanded.get("payload_json") or ""), {})
        if not isinstance(payload, Mapping):
            continue
        scope = str(payload.get("scope") or expanded.get("scope") or "current")
        is_flashback = str(
            payload.get("narrative_mode")
            or expanded.get("narrative_mode")
            or "linear"
        ) == "flashback"
        authoritative = bool(expanded.get("changes_authority"))
        branch_id = str(expanded.get("branch_id") or "main")
        if scope in {"planned", "historical"} or is_flashback:
            continue
        if authoritative:
            if branch_id != "main":
                continue
        else:
            if branch_id == "main":
                continue
            if scope != "current":
                continue
            if event_type == "advantage_module":
                if str(payload.get("action") or "") not in {
                    "unlock",
                    "enable",
                    "lock",
                    "suppress",
                    "deprecate",
                }:
                    continue
            elif event_type not in _BRANCH_LOCAL_ADVANTAGE_EVENT_TYPES:
                continue
        original_order = int(expanded.get("updated_order") or 0)
        expanded["updated_order"] = effective_order(
            str(expanded.get("event_id") or ""),
            original_order,
        )
        expanded["_advantage_original_order"] = original_order
        replay_rows.append(expanded)
    replay_rows.sort(
        key=lambda row: (
            int(row.get("updated_order") or 0),
            int(row.get("_advantage_original_order") or 0),
            str(row.get("event_id") or ""),
        )
    )
    for row in replay_rows:
        row.pop("_advantage_original_order", None)
    return replay_rows


def _event_descriptor(
    event_row: Mapping[str, Any],
) -> dict[str, Any] | None:
    expanded_row = _expanded_event_row(event_row)
    if expanded_row is None:
        return None
    payload = _as_json(str(expanded_row["payload_json"]), {})
    event_type = str(expanded_row["event_type"])

    descriptor: dict[str, Any] = {
        "event_id": str(expanded_row["event_id"]),
        "event_type": event_type,
        "scope": str(payload.get("scope") or expanded_row["scope"]),
        "branch_id": str(expanded_row["branch_id"]),
        "entity_id": payload.get("entity_id"),
        "subject_entity_id": None,
        "target_entity_id": None,
        "chapter_no": payload.get("chapter_no", expanded_row["chapter_no"]),
        "scene_index": payload.get("scene_index", expanded_row["scene_index"]),
        "story_time": payload.get("story_time", expanded_row["story_time"]),
        "narrative_mode": str(
            payload.get("narrative_mode") or expanded_row["narrative_mode"]
        ),
        "updated_order": int(expanded_row["updated_order"]),
        "raw": payload,
    }

    if event_type in {"fact", "state", "world_rule", "time"}:
        entity_id = payload.get("entity_id")
        field_name = str(payload.get("field") or payload.get("field_name"))
        descriptor.update(
            {
                "fact_key": _fact_key(event_type, entity_id, field_name),
                "fact_type": event_type,
                "entity_id": entity_id,
                "field_name": field_name,
                "value": payload.get("value"),
            }
        )
        return descriptor

    if event_type == "entity":
        entity_id = str(payload.get("entity_id") or "")
        descriptor.update(
            {
                "fact_key": _fact_key("entity", entity_id),
                "fact_type": "entity",
                "entity_id": entity_id,
                "field_name": "definition",
                "value": {
                    "entity_type": payload.get("entity_type"),
                    "canonical_name": payload.get("canonical_name"),
                    "attributes": payload.get("attributes") or {},
                },
            }
        )
        return descriptor

    if event_type == "relation":
        source = str(payload.get("source_entity_id") or "")
        target = str(payload.get("target_entity_id") or "")
        dimension = str(payload.get("dimension") or "")
        descriptor.update(
            {
                "fact_key": _fact_key("relation", source, target, dimension),
                "fact_type": "relation",
                "entity_id": source,
                "subject_entity_id": source,
                "target_entity_id": target,
                "field_name": dimension,
                "value": payload.get("value"),
            }
        )
        return descriptor

    if event_type == "movement":
        actor = str(payload.get("actor_entity_id") or "")
        destination = payload.get("to_location_entity_id")
        action = str(payload.get("action") or "move")
        descriptor.update(
            {
                "fact_key": _fact_key("location", actor),
                "fact_type": "location",
                "entity_id": actor,
                "subject_entity_id": actor,
                "target_entity_id": destination,
                "field_name": "location",
                "value": {
                    "location_entity_id": destination,
                    "from_location_entity_id": payload.get(
                        "from_location_entity_id"
                    ),
                    "action": action,
                    "route": payload.get("route"),
                    "method": payload.get("method"),
                    "departed_at": payload.get("departed_at"),
                    "arrived_at": payload.get("arrived_at"),
                },
            }
        )
        return descriptor

    if event_type == "inventory":
        item = str(payload.get("item_entity_id") or "")
        unique = bool(payload.get("unique", False))
        action = str(payload.get("action") or "acquire")
        owner = payload.get("to_owner_entity_id")
        if action in {"consume", "lose"}:
            owner = None
        key_owner = "" if unique else (
            owner or payload.get("from_owner_entity_id") or ""
        )
        descriptor.update(
            {
                "fact_key": _fact_key("inventory", item, key_owner),
                "fact_type": "inventory",
                "entity_id": item,
                "subject_entity_id": owner,
                "field_name": "ownership",
                "value": {
                    "item_entity_id": item,
                    "owner_entity_id": owner,
                    "from_owner_entity_id": payload.get("from_owner_entity_id"),
                    "quantity": payload.get("quantity", 1),
                    "unique": unique,
                    "status": (
                        "consumed"
                        if action == "consume"
                        else "lost"
                        if action == "lose"
                        else "held"
                    ),
                    "action": action,
                },
            }
        )
        return descriptor

    if event_type == "ability":
        owner = str(payload.get("owner_entity_id") or "")
        ability = str(payload.get("ability_entity_id") or "")
        descriptor.update(
            {
                "fact_key": _fact_key("ability_event", owner, ability),
                "fact_type": "ability_event",
                "entity_id": owner,
                "subject_entity_id": owner,
                "target_entity_id": ability,
                "field_name": "event",
                "value": payload,
            }
        )
        return descriptor

    if event_type == "power_spec":
        spec_type = str(payload.get("spec_type") or "")
        spec_id = str(payload.get("spec_entity_id") or "")
        descriptor.update(
            {
                "fact_key": _fact_key("power_spec", spec_type, spec_id),
                "fact_type": "power_spec",
                "entity_id": spec_id,
                "subject_entity_id": spec_id,
                "field_name": spec_type,
                "value": {
                    "spec_type": spec_type,
                    "spec_entity_id": spec_id,
                    "action": payload.get("action"),
                    "definition": dict(payload.get("definition") or {}),
                    "status": (
                        "deprecated"
                        if payload.get("action") == "deprecate"
                        else "active"
                    ),
                },
            }
        )
        return descriptor

    if event_type == "progression":
        actor = str(payload.get("actor_entity_id") or "")
        track = str(payload.get("track_entity_id") or "")
        descriptor.update(
            {
                "fact_key": _fact_key("progression", actor, track),
                "fact_type": "progression_event",
                "entity_id": actor,
                "subject_entity_id": actor,
                "target_entity_id": track,
                "field_name": "event",
                "value": payload,
            }
        )
        return descriptor

    if event_type == "resource":
        actor = str(payload.get("actor_entity_id") or "")
        resource = str(payload.get("resource_entity_id") or "")
        descriptor.update(
            {
                "fact_key": _fact_key("resource_event", actor, resource),
                "fact_type": "resource_event",
                "entity_id": actor,
                "subject_entity_id": actor,
                "target_entity_id": resource,
                "field_name": "event",
                "value": payload,
            }
        )
        return descriptor

    if event_type == "status_effect":
        actor = str(payload.get("actor_entity_id") or "")
        status = str(payload.get("status_entity_id") or "")
        descriptor.update(
            {
                "fact_key": _fact_key("status_effect", actor, status),
                "fact_type": "status_effect_event",
                "entity_id": actor,
                "subject_entity_id": actor,
                "target_entity_id": status,
                "field_name": "event",
                "value": payload,
            }
        )
        return descriptor

    if event_type == "power_binding":
        actor = str(payload.get("actor_entity_id") or "")
        binding_id = str(payload.get("binding_id") or "")
        descriptor.update(
            {
                "fact_key": _fact_key("power_binding", actor, binding_id),
                "fact_type": "power_binding_event",
                "entity_id": actor,
                "subject_entity_id": actor,
                "target_entity_id": payload.get("source_entity_id"),
                "field_name": binding_id,
                "value": payload,
            }
        )
        return descriptor

    if event_type == "qualification":
        actor = str(payload.get("actor_entity_id") or "")
        qualification = str(payload.get("qualification_entity_id") or "")
        descriptor.update(
            {
                "fact_key": _fact_key(
                    "qualification", actor, qualification
                ),
                "fact_type": "qualification_event",
                "entity_id": actor,
                "subject_entity_id": actor,
                "target_entity_id": qualification,
                "field_name": "event",
                "value": payload,
            }
        )
        return descriptor

    if event_type == "power_observation":
        observer = str(payload.get("observer_entity_id") or "")
        subject = str(payload.get("subject_entity_id") or "")
        ability = str(payload.get("ability_entity_id") or "")
        knowledge_plane = str(
            payload.get("knowledge_plane") or "actor_belief"
        )
        descriptor.update(
            {
                "fact_key": _fact_key(
                    "power_observation",
                    observer,
                    subject,
                    ability,
                    knowledge_plane,
                ),
                "fact_type": "power_observation",
                "entity_id": observer,
                "subject_entity_id": subject or observer,
                "target_entity_id": ability or None,
                "field_name": "observation",
                "value": {
                    **payload,
                    "observer_entity_id": observer,
                    "subject_entity_id": subject or None,
                    "ability_entity_id": ability or None,
                    "knowledge_plane": knowledge_plane,
                },
            }
        )
        return descriptor

    if event_type == "belief":
        believer = str(payload.get("believer_entity_id") or "")
        proposition = str(payload.get("proposition_key") or "")
        descriptor.update(
            {
                "fact_key": _fact_key("belief", believer, proposition),
                "fact_type": "belief",
                "entity_id": believer,
                "subject_entity_id": believer,
                "field_name": proposition,
                "value": {
                    "value": payload.get("value"),
                    "confidence": payload.get("confidence"),
                    "knowledge_plane": payload.get(
                        "knowledge_plane", "character_belief"
                    ),
                },
            }
        )
        return descriptor

    if event_type == "open_loop":
        loop_id = str(payload.get("loop_id") or "")
        descriptor.update(
            {
                "fact_key": _fact_key("open_loop", loop_id),
                "fact_type": "open_loop",
                "entity_id": payload.get("owner_entity_id"),
                "subject_entity_id": payload.get("owner_entity_id"),
                "field_name": "open_loop",
                "value": {
                    **payload,
                    "loop_id": loop_id,
                },
            }
        )
        return descriptor

    if event_type in ADVANTAGE_EVENT_TYPES:
        # Current main Advantage events have their own typed projection.
        # Planned, historical and provisional events need a generic fact row
        # so their scope remains queryable without contaminating current
        # Advantage runtime.
        if (
            descriptor["scope"] in {"planned", "historical"}
            or descriptor["branch_id"] != "main"
            or not bool(expanded_row.get("changes_authority"))
        ):
            advantage_id = str(payload.get("advantage_id") or "")
            descriptor.update(
                {
                    "fact_key": _fact_key(
                        "advantage_event",
                        descriptor["scope"],
                        descriptor["branch_id"],
                        descriptor["event_id"],
                    ),
                    "fact_type": event_type,
                    "entity_id": advantage_id or None,
                    "subject_entity_id": advantage_id or None,
                    "field_name": "event",
                    "value": payload,
                }
            )
            return descriptor

    return None


def _nonunique_inventory_descriptor(
    template: Mapping[str, Any],
    *,
    owner_entity_id: str,
    quantity: float,
    status: str,
    action: str,
) -> dict[str, Any]:
    """Materialize one owner's balance after a non-unique inventory event.

    Non-unique inventory facts are owner-scoped balances.  An event can update
    two balances (transfer) even though it has one immutable source event.
    """

    descriptor = dict(template)
    raw = dict(template.get("raw") or {})
    item = str(raw.get("item_entity_id") or template.get("entity_id") or "")
    descriptor.update(
        {
            "fact_key": _fact_key("inventory", item, owner_entity_id),
            "fact_type": "inventory",
            "entity_id": item,
            "subject_entity_id": owner_entity_id,
            "target_entity_id": None,
            "field_name": "ownership",
            "value": {
                "item_entity_id": item,
                "owner_entity_id": owner_entity_id,
                "from_owner_entity_id": raw.get("from_owner_entity_id"),
                "to_owner_entity_id": raw.get("to_owner_entity_id"),
                "quantity": quantity,
                "unique": False,
                "status": status,
                "action": action,
            },
        }
    )
    return descriptor


def _apply_nonunique_inventory_event(
    descriptor: Mapping[str, Any],
    balances: dict[tuple[str, str], float],
) -> list[dict[str, Any]]:
    """Apply an authoritative current non-unique inventory event atomically.

    ``acquire`` adds to an owner's balance, ``set`` replaces that owner's
    balance, ``transfer`` subtracts from one owner and adds to another, while
    ``consume`` and ``lose`` subtract from the named owner.  Replay raises
    before publishing any projection when a debit exceeds the accepted
    balance, so proposal acceptance remains fail-closed inside its transaction.
    """

    raw = dict(descriptor.get("raw") or {})
    item = str(raw.get("item_entity_id") or descriptor.get("entity_id") or "")
    action = str(raw.get("action") or "acquire")
    try:
        quantity = float(raw.get("quantity", 1))
    except (TypeError, ValueError) as exc:
        raise ContinuityError(
            "INVALID_QUANTITY",
            "accepted inventory event has a non-numeric quantity",
            details={"event_id": descriptor.get("event_id")},
        ) from exc
    if quantity < 0 or quantity != quantity or quantity in {
        float("inf"),
        float("-inf"),
    }:
        raise ContinuityError(
            "INVALID_QUANTITY",
            "accepted inventory event has an invalid quantity",
            details={"event_id": descriptor.get("event_id")},
        )
    if action != "set" and quantity <= 0:
        raise ContinuityError(
            "INVALID_QUANTITY",
            "accepted inventory delta must be greater than zero",
            details={"event_id": descriptor.get("event_id")},
        )

    def owner_id(field: str) -> str:
        value = str(raw.get(field) or "").strip()
        if not value:
            raise ContinuityError(
                "EVENT_ENTITY_REQUIRED",
                f"inventory {action} requires {field}",
                details={
                    "event_id": descriptor.get("event_id"),
                    "field": field,
                },
            )
        return value

    def current_balance(owner: str) -> float:
        return float(balances.get((item, owner), 0.0))

    def debit(owner: str) -> float:
        available = current_balance(owner)
        if available + 1e-12 < quantity:
            raise ContinuityError(
                "INVENTORY_INSUFFICIENT_BALANCE",
                "inventory debit exceeds the accepted owner balance",
                details={
                    "event_id": descriptor.get("event_id"),
                    "item_entity_id": item,
                    "owner_entity_id": owner,
                    "available": available,
                    "requested": quantity,
                    "action": action,
                },
            )
        remaining = available - quantity
        if abs(remaining) < 1e-12:
            remaining = 0.0
        return remaining

    if action == "acquire":
        to_owner = owner_id("to_owner_entity_id")
        new_balance = current_balance(to_owner) + quantity
        balances[(item, to_owner)] = new_balance
        return [
            _nonunique_inventory_descriptor(
                descriptor,
                owner_entity_id=to_owner,
                quantity=new_balance,
                status="held",
                action=action,
            )
        ]

    if action == "set":
        to_owner = owner_id("to_owner_entity_id")
        balances[(item, to_owner)] = quantity
        return [
            _nonunique_inventory_descriptor(
                descriptor,
                owner_entity_id=to_owner,
                quantity=quantity,
                status="held" if quantity > 0 else "empty",
                action=action,
            )
        ]

    from_owner = owner_id("from_owner_entity_id")
    remaining = debit(from_owner)
    balances[(item, from_owner)] = remaining

    if action == "transfer":
        to_owner = owner_id("to_owner_entity_id")
        if from_owner == to_owner:
            raise ContinuityError(
                "INVENTORY_TRANSFER_SAME_OWNER",
                "inventory transfer requires distinct owners",
                details={"event_id": descriptor.get("event_id")},
            )
        destination_balance = current_balance(to_owner) + quantity
        balances[(item, to_owner)] = destination_balance
        return [
            _nonunique_inventory_descriptor(
                descriptor,
                owner_entity_id=from_owner,
                quantity=remaining,
                status="held" if remaining > 0 else "transferred",
                action=action,
            ),
            _nonunique_inventory_descriptor(
                descriptor,
                owner_entity_id=to_owner,
                quantity=destination_balance,
                status="held",
                action=action,
            ),
        ]

    if action in {"consume", "lose"}:
        return [
            _nonunique_inventory_descriptor(
                descriptor,
                owner_entity_id=from_owner,
                quantity=remaining,
                status=(
                    "held"
                    if remaining > 0
                    else "consumed"
                    if action == "consume"
                    else "lost"
                ),
                action=action,
            )
        ]

    raise ContinuityError(
        "INVALID_INVENTORY_ACTION",
        f"unsupported accepted inventory action: {action}",
        details={"event_id": descriptor.get("event_id")},
    )


_POWER_RUNTIME_EVENT_TYPES = {
    "ability",
    "progression",
    "resource",
    "status_effect",
    "power_binding",
    "qualification",
}


def _descriptor_variant(
    template: Mapping[str, Any],
    *,
    fact_key: str,
    fact_type: str,
    field_name: str,
    value: Any,
    entity_id: str | None = None,
    subject_entity_id: str | None = None,
    target_entity_id: str | None = None,
) -> dict[str, Any]:
    descriptor = dict(template)
    descriptor.update(
        {
            "fact_key": fact_key,
            "fact_type": fact_type,
            "field_name": field_name,
            "value": value,
            "entity_id": (
                entity_id
                if entity_id is not None
                else template.get("entity_id")
            ),
            "subject_entity_id": (
                subject_entity_id
                if subject_entity_id is not None
                else template.get("subject_entity_id")
            ),
            "target_entity_id": (
                target_entity_id
                if target_entity_id is not None
                else template.get("target_entity_id")
            ),
        }
    )
    return descriptor


def _ability_state_parts(
    raw: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = dict(raw.get("state") or {})
    state.pop("action", None)
    state.pop("use_count", None)
    runtime_keys = {
        "active",
        "available",
        "charges",
        "cooldown_until",
        "last_used_at",
    }
    runtime = {
        key: state.pop(key) for key in list(state) if key in runtime_keys
    }
    for key in ("cost", "cooldown", "limits", "level", "status"):
        if key in raw:
            state[key] = raw[key]
    if raw.get("cooldown_until") is not None:
        runtime["cooldown_until"] = raw["cooldown_until"]
    return state, runtime


def _apply_ability_event(
    template: Mapping[str, Any],
    ownership_states: dict[tuple[str, str], dict[str, Any]],
    runtime_states: dict[tuple[str, str], dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw = dict(template.get("raw") or {})
    owner = str(raw.get("owner_entity_id") or "")
    ability = str(raw.get("ability_entity_id") or "")
    key = (owner, ability)
    action = str(raw.get("action") or "set")
    ownership = dict(ownership_states.get(key, {}))
    runtime = dict(runtime_states.get(key, {}))
    ownership_patch, runtime_patch = _ability_state_parts(raw)
    acquired = bool(ownership.get("acquired", False))
    legacy_v4_semantics = bool(
        template.get("legacy_v4_ability_semantics")
    )

    if action in {"gain", "set", "unlock"}:
        ownership.update(ownership_patch)
        acquired = bool(ownership_patch.get("acquired", True))
        ownership["acquired"] = acquired
        runtime.update(runtime_patch)
        runtime.setdefault("available", acquired)
    elif (
        action == "breakthrough"
        and not acquired
        and legacy_v4_semantics
    ):
        ownership.update(ownership_patch)
        ownership["acquired"] = True
        acquired = True
        runtime.update(runtime_patch)
        runtime.setdefault("available", True)
    elif (
        action == "lose"
        and not acquired
        and legacy_v4_semantics
    ):
        ownership.update(ownership_patch)
        ownership["acquired"] = False
        runtime.update(runtime_patch)
        runtime["available"] = False
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
            "accepted ability event acts on an unowned ability",
            details={
                "event_id": template.get("event_id"),
                "owner_entity_id": owner,
                "ability_entity_id": ability,
                "action": action,
            },
        )

    if action in {"breakthrough", "upgrade"}:
        ownership.update(ownership_patch)
        ownership["acquired"] = True
    elif action == "use":
        previous_cooldown = runtime.get("cooldown_until")
        current_coordinate = raw.get("story_coordinate")
        if previous_cooldown and current_coordinate:
            if (
                str(previous_cooldown.get("calendar_id") or "")
                == str(current_coordinate.get("calendar_id") or "")
                and previous_cooldown.get("ordinal") is not None
                and current_coordinate.get("ordinal") is not None
                and int(current_coordinate["ordinal"])
                >= int(previous_cooldown["ordinal"])
            ):
                runtime.pop("cooldown_until", None)
                runtime["available"] = True
        runtime.update(runtime_patch)
        runtime["last_used_at"] = raw.get("story_coordinate")
        previous_use_count = runtime.get("use_count", 0)
        if type(previous_use_count) is not int or previous_use_count < 0:
            raise ContinuityError(
                "POWER_RUNTIME_STATE_INVALID",
                "ability runtime use_count must be a non-negative integer",
                details={
                    "owner_entity_id": owner,
                    "ability_entity_id": ability,
                },
            )
        runtime["use_count"] = previous_use_count + 1
        if raw.get("cooldown_until") is not None:
            runtime["cooldown_until"] = raw["cooldown_until"]
            runtime["available"] = False
        else:
            runtime["available"] = True
    elif action == "cooldown":
        runtime.update(runtime_patch)
        runtime["cooldown_until"] = raw.get("cooldown_until") or runtime.get(
            "cooldown_until"
        )
        runtime["available"] = False
    elif action == "charge":
        runtime.update(runtime_patch)
        runtime["charges"] = float(runtime.get("charges", 0)) + float(
            (raw.get("state") or {}).get("amount", 1)
        )
    elif action == "activate":
        runtime.update(runtime_patch)
        runtime["active"] = True
    elif action == "deactivate":
        runtime.update(runtime_patch)
        runtime["active"] = False
    elif action == "refresh":
        runtime.update(runtime_patch)
    elif action == "lose":
        ownership["acquired"] = False
        ownership["lost_at"] = raw.get("story_coordinate")
        runtime["available"] = False
        runtime["active"] = False

    ownership["last_action"] = action
    ownership_states[key] = ownership
    runtime["last_action"] = action
    runtime["last_story_coordinate"] = raw.get("story_coordinate")
    runtime_states[key] = runtime
    ownership_descriptor = _descriptor_variant(
        template,
        fact_key=_fact_key("ability_ownership", owner, ability),
        fact_type="ability_ownership",
        field_name="ownership",
        value=ownership,
        entity_id=owner,
        subject_entity_id=owner,
        target_entity_id=ability,
    )
    runtime_descriptor = _descriptor_variant(
        template,
        fact_key=_fact_key("ability_runtime", owner, ability),
        fact_type="ability_runtime",
        field_name="runtime",
        value=runtime,
        entity_id=owner,
        subject_entity_id=owner,
        target_entity_id=ability,
    )
    merged = {
        **ownership,
        **runtime,
        "acquired": bool(ownership.get("acquired")),
        "action": action,
    }
    compatibility_descriptor = _descriptor_variant(
        template,
        fact_key=_fact_key("ability", owner, ability),
        fact_type="ability",
        field_name="ability",
        value=merged,
        entity_id=owner,
        subject_entity_id=owner,
        target_entity_id=ability,
    )
    history = {
        "source_event_id": template["event_id"],
        "owner_entity_id": owner,
        "ability_entity_id": ability,
        "action": action,
        "runtime": {
            **runtime_patch,
            "state": dict(raw.get("state") or {}),
        },
        "story_coordinate": raw.get("story_coordinate") or {},
        "chapter_no": template.get("chapter_no"),
        "scene_index": template.get("scene_index"),
        "updated_order": template["updated_order"],
    }
    return [
        ownership_descriptor,
        runtime_descriptor,
        compatibility_descriptor,
    ], history


def _apply_progression_event(
    template: Mapping[str, Any],
    states: dict[tuple[str, str], dict[str, Any]],
    rank_edges: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    raw = dict(template.get("raw") or {})
    actor = str(raw.get("actor_entity_id") or "")
    track = str(raw.get("track_entity_id") or "")
    key = (actor, track)
    previous = dict(states.get(key, {}))
    current_rank = previous.get("rank_entity_id")
    action = str(raw.get("action") or "initialize")
    if action != "initialize" and current_rank is None:
        raise ContinuityError(
            "POWER_PREREQUISITE_UNMET",
            "accepted progression event has no initialized source rank",
            details={"event_id": template.get("event_id")},
        )
    if action != "initialize":
        edge_id = str(raw.get("rank_edge_entity_id") or "")
        candidates = [
            (candidate_id, dict(edge))
            for candidate_id, edge in rank_edges.items()
            if str(edge.get("track_entity_id") or "") == track
            and str(edge.get("to_rank_entity_id") or "")
            == str(raw.get("to_rank_entity_id") or "")
            and str(current_rank)
            in {
                str(item)
                for item in edge.get("from_rank_entity_ids") or []
            }
        ]
        if edge_id:
            edge = rank_edges.get(edge_id)
            candidates = (
                [(edge_id, dict(edge))]
                if edge is not None
                else []
            )
        if len(candidates) != 1:
            raise ContinuityError(
                "POWER_TRANSITION_EDGE_MISSING",
                "accepted progression event has no active rank edge",
                details={
                    "event_id": template.get("event_id"),
                    "rank_edge_entity_id": edge_id or None,
                },
            )
        selected_id, selected = candidates[0]
        if (
            str(selected.get("track_entity_id") or "") != track
            or str(selected.get("to_rank_entity_id") or "")
            != str(raw.get("to_rank_entity_id") or "")
            or str(current_rank)
            not in {
                str(item)
                for item in selected.get("from_rank_entity_ids") or []
            }
        ):
            raise ContinuityError(
                "POWER_TRANSITION_EDGE_MISSING",
                "accepted rank edge endpoints do not match progression",
                details={"event_id": template.get("event_id")},
            )
        raw["rank_edge_entity_id"] = selected_id
    state = {
        **previous,
        **dict(raw.get("state") or {}),
        "actor_entity_id": actor,
        "track_entity_id": track,
        "from_rank_entity_id": current_rank,
        "rank_entity_id": raw.get("to_rank_entity_id"),
        "rank_edge_entity_id": raw.get("rank_edge_entity_id"),
        "action": action,
        "story_coordinate": raw.get("story_coordinate"),
    }
    states[key] = state
    return [
        _descriptor_variant(
            template,
            fact_key=_fact_key("progression", actor, track),
            fact_type="progression",
            field_name="rank",
            value=state,
            entity_id=actor,
            subject_entity_id=actor,
            target_entity_id=track,
        )
    ]


def _apply_resource_event(
    template: Mapping[str, Any],
    states: dict[tuple[str, str], dict[str, Any]],
    conversion_rules: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    raw = dict(template.get("raw") or {})
    actor = str(raw.get("actor_entity_id") or "")
    resource = str(raw.get("resource_entity_id") or "")
    key = (actor, resource)
    state = dict(states.get(key, {"balance": 0.0, "reserved": 0.0}))
    balance = float(state.get("balance", 0))
    reserved = float(state.get("reserved", 0))
    action = str(raw.get("action") or "set")
    amount = float(raw.get("amount", 0))
    descriptors: list[dict[str, Any]] = []
    if action in {"initialize", "set"}:
        balance = amount
        reserved = min(
            float((raw.get("state") or {}).get("reserved", 0)),
            balance,
        )
    elif action in {"gain", "recover"}:
        balance += amount
    elif action == "spend":
        available = (
            balance
            if raw.get("from_reserved")
            else balance - reserved
        )
        if available + 1e-12 < amount:
            raise ContinuityError(
                "POWER_RESOURCE_INSUFFICIENT",
                "accepted resource spend exceeds balance",
                details={"event_id": template.get("event_id")},
            )
        balance -= amount
        if raw.get("from_reserved"):
            reserved = max(0.0, reserved - amount)
    elif action == "reserve":
        if balance - reserved + 1e-12 < amount:
            raise ContinuityError(
                "POWER_RESOURCE_INSUFFICIENT",
                "accepted resource reservation exceeds balance",
                details={"event_id": template.get("event_id")},
            )
        reserved += amount
    elif action == "release":
        if reserved + 1e-12 < amount:
            raise ContinuityError(
                "POWER_RESOURCE_INSUFFICIENT",
                "accepted resource release exceeds reserve",
                details={"event_id": template.get("event_id")},
            )
        reserved -= amount
    elif action == "convert":
        target_resource = str(raw.get("target_resource_entity_id") or "")
        rule_id = str(raw.get("conversion_rule_entity_id") or "")
        rule = dict(conversion_rules.get(rule_id) or {})
        if (
            str(rule.get("source_resource_entity_id") or "") != resource
            or str(rule.get("target_resource_entity_id") or "")
            != target_resource
        ):
            raise ContinuityError(
                "POWER_INTERACTION_UNKNOWN",
                "accepted resource conversion has no matching rule",
                details={"event_id": template.get("event_id")},
            )
        if balance - reserved + 1e-12 < amount:
            raise ContinuityError(
                "POWER_RESOURCE_INSUFFICIENT",
                "accepted resource conversion exceeds balance",
                details={"event_id": template.get("event_id")},
            )
        ratio = float(rule.get("ratio", 0))
        if ratio <= 0:
            raise ContinuityError(
                "POWER_INTERACTION_UNKNOWN",
                "accepted conversion rule has no positive ratio",
                details={"event_id": template.get("event_id")},
            )
        target_amount = float(raw.get("target_amount", amount * ratio))
        if abs(target_amount - amount * ratio) > 1e-9:
            raise ContinuityError(
                "POWER_INTERACTION_UNKNOWN",
                "accepted conversion amount differs from rule",
                details={"event_id": template.get("event_id")},
            )
        balance -= amount
        target_key = (actor, target_resource)
        target_state = dict(
            states.get(target_key, {"balance": 0.0, "reserved": 0.0})
        )
        target_state.update(
            {
                "balance": float(target_state.get("balance", 0))
                + target_amount,
                "reserved": float(target_state.get("reserved", 0)),
                "action": "convert_gain",
                "conversion_rule_entity_id": rule_id,
                "story_coordinate": raw.get("story_coordinate"),
            }
        )
        states[target_key] = target_state
        descriptors.append(
            _descriptor_variant(
                template,
                fact_key=_fact_key("resource", actor, target_resource),
                fact_type="resource",
                field_name="balance",
                value=target_state,
                entity_id=actor,
                subject_entity_id=actor,
                target_entity_id=target_resource,
            )
        )
    state.update(dict(raw.get("state") or {}))
    state.update(
        {
            "balance": balance,
            "reserved": reserved,
            "available": balance - reserved,
            "action": action,
            "story_coordinate": raw.get("story_coordinate"),
        }
    )
    states[key] = state
    descriptors.append(
        _descriptor_variant(
            template,
            fact_key=_fact_key("resource", actor, resource),
            fact_type="resource",
            field_name="balance",
            value=state,
            entity_id=actor,
            subject_entity_id=actor,
            target_entity_id=resource,
        )
    )
    return descriptors


def _apply_status_event(
    template: Mapping[str, Any],
    states: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    raw = dict(template.get("raw") or {})
    actor = str(raw.get("actor_entity_id") or "")
    status = str(raw.get("status_entity_id") or "")
    key = (actor, status)
    state = dict(states.get(key, {}))
    action = str(raw.get("action") or "apply")
    active = bool(state.get("active"))
    if action in {"stack", "refresh", "remove", "expire"} and not active:
        raise ContinuityError(
            "POWER_PREREQUISITE_UNMET",
            "accepted status event requires an active status",
            details={"event_id": template.get("event_id")},
        )
    if action == "apply":
        active = True
        stacks = int(raw.get("stacks", 1))
    elif action == "stack":
        stacks = int(state.get("stacks", 0)) + int(
            raw.get("stacks", 1)
        )
    elif action == "refresh":
        stacks = int(raw.get("stacks", state.get("stacks", 1)))
    else:
        active = False
        stacks = 0
    state.update(dict(raw.get("state") or {}))
    state.update(
        {
            "active": active,
            "stacks": stacks,
            "source_entity_id": raw.get("source_entity_id"),
            "expires_coordinate": raw.get("expires_coordinate")
            or state.get("expires_coordinate"),
            "action": action,
            "story_coordinate": raw.get("story_coordinate"),
        }
    )
    states[key] = state
    return [
        _descriptor_variant(
            template,
            fact_key=_fact_key("status_effect", actor, status),
            fact_type="status_effect",
            field_name="status",
            value=state,
            entity_id=actor,
            subject_entity_id=actor,
            target_entity_id=status,
        )
    ]


def _apply_binding_event(
    template: Mapping[str, Any],
    states: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    raw = dict(template.get("raw") or {})
    actor = str(raw.get("actor_entity_id") or "")
    binding_id = str(raw.get("binding_id") or "")
    key = (actor, binding_id)
    state = dict(states.get(key, {}))
    action = str(raw.get("action") or "bind")
    active = action in {"bind", "equip", "contract", "summon"}
    state.update(dict(raw.get("state") or {}))
    state.update(
        {
            "binding_id": binding_id,
            "active": active,
            "source_entity_id": raw.get("source_entity_id"),
            "ability_entity_ids": list(
                raw.get("ability_entity_ids") or []
            ),
            "binding_kind": action,
            "slot_key": raw.get("slot_key"),
            "unique": bool(raw.get("unique")),
            "action": action,
            "story_coordinate": raw.get("story_coordinate"),
        }
    )
    states[key] = state
    return [
        _descriptor_variant(
            template,
            fact_key=_fact_key("power_binding", actor, binding_id),
            fact_type="power_binding",
            field_name=binding_id,
            value=state,
            entity_id=actor,
            subject_entity_id=actor,
            target_entity_id=str(raw.get("source_entity_id") or ""),
        )
    ]


def _apply_qualification_event(
    template: Mapping[str, Any],
    states: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    raw = dict(template.get("raw") or {})
    actor = str(raw.get("actor_entity_id") or "")
    qualification = str(raw.get("qualification_entity_id") or "")
    key = (actor, qualification)
    state = dict(states.get(key, {}))
    quantity = float(state.get("quantity", 0))
    amount = float(raw.get("quantity", 1))
    action = str(raw.get("action") or "grant")
    if action == "grant":
        quantity += amount
    elif action == "consume":
        if quantity + 1e-12 < amount:
            raise ContinuityError(
                "POWER_PREREQUISITE_UNMET",
                "accepted qualification consumption exceeds quantity",
                details={"event_id": template.get("event_id")},
            )
        quantity -= amount
    else:
        quantity = max(0.0, quantity - amount)
    state.update(dict(raw.get("state") or {}))
    state.update(
        {
            "active": quantity > 0,
            "quantity": quantity,
            "source_entity_id": raw.get("source_entity_id"),
            "expires_coordinate": raw.get("expires_coordinate"),
            "action": action,
            "story_coordinate": raw.get("story_coordinate"),
        }
    )
    states[key] = state
    return [
        _descriptor_variant(
            template,
            fact_key=_fact_key("qualification", actor, qualification),
            fact_type="qualification",
            field_name="qualification",
            value=state,
            entity_id=actor,
            subject_entity_id=actor,
            target_entity_id=qualification,
        )
    ]


def _apply_power_spec_event(
    template: Mapping[str, Any],
    states: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    raw = dict(template.get("raw") or {})
    spec_type = str(raw.get("spec_type") or "")
    spec_id = str(raw.get("spec_entity_id") or "")
    key = (spec_type, spec_id)
    previous = dict(states.get(key, {}))
    action = str(raw.get("action") or "define")
    if action == "define":
        definition = dict(raw.get("definition") or {})
    elif action == "amend":
        definition = {
            **dict(previous.get("definition") or {}),
            **dict(raw.get("definition") or {}),
        }
    else:
        definition = dict(previous.get("definition") or {})
    value = {
        "spec_type": spec_type,
        "spec_entity_id": spec_id,
        "action": action,
        "status": "deprecated" if action == "deprecate" else "active",
        "definition": definition,
    }
    states[key] = value
    return _descriptor_variant(
        template,
        fact_key=_fact_key("power_spec", spec_type, spec_id),
        fact_type="power_spec",
        field_name=spec_type,
        value=value,
        entity_id=spec_id,
        subject_entity_id=spec_id,
        target_entity_id=None,
    )


def _apply_power_runtime_event(
    template: Mapping[str, Any],
    context: dict[str, Any],
    conversion_rules: Mapping[str, Mapping[str, Any]],
    rank_edges: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    event_type = str(template.get("event_type") or "")
    if event_type == "ability":
        return _apply_ability_event(
            template,
            context.setdefault("ability_ownership", {}),
            context.setdefault("ability_runtime", {}),
        )
    if event_type == "progression":
        return (
            _apply_progression_event(
                template,
                context.setdefault("progression", {}),
                rank_edges,
            ),
            None,
        )
    if event_type == "resource":
        return (
            _apply_resource_event(
                template,
                context.setdefault("resource", {}),
                conversion_rules,
            ),
            None,
        )
    if event_type == "status_effect":
        return (
            _apply_status_event(
                template, context.setdefault("status", {})
            ),
            None,
        )
    if event_type == "power_binding":
        return (
            _apply_binding_event(
                template, context.setdefault("binding", {})
            ),
            None,
        )
    if event_type == "qualification":
        return (
            _apply_qualification_event(
                template, context.setdefault("qualification", {})
            ),
            None,
        )
    return [dict(template)], None


def _projection_row(descriptor: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        descriptor["fact_key"],
        descriptor["fact_type"],
        descriptor["scope"],
        descriptor.get("entity_id"),
        descriptor.get("subject_entity_id"),
        descriptor.get("target_entity_id"),
        descriptor["field_name"],
        canonical_json(descriptor.get("value")),
        descriptor["event_id"],
        descriptor.get("chapter_no"),
        descriptor.get("scene_index"),
        descriptor.get("story_time"),
        descriptor["updated_order"],
    )


class ReplayEngine:
    """Rebuilds every authority projection from the accepted ledger."""

    def __init__(self, store: ContinuityStore) -> None:
        self.store = store

    @staticmethod
    def _strict_proposal_content(
        proposal: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        values = dict(proposal)
        proposal_id = str(values.get("proposal_id") or "")
        try:
            payload = json.loads(str(values["payload_json"]))
            events = json.loads(str(values["events_json"]))
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            RecursionError,
        ) as exc:
            raise ContinuityError(
                "ACCEPTED_PAYLOAD_CORRUPT",
                "accepted proposal payload or events are not valid JSON",
                details={"proposal_id": proposal_id},
            ) from exc
        if not isinstance(payload, dict) or not isinstance(events, list):
            raise ContinuityError(
                "ACCEPTED_PAYLOAD_CORRUPT",
                "accepted proposal payload must be an object and events must be an array",
                details={"proposal_id": proposal_id},
            )
        if any(not isinstance(event, Mapping) for event in events):
            raise ContinuityError(
                "ACCEPTED_PAYLOAD_CORRUPT",
                "accepted proposal events must contain only objects",
                details={"proposal_id": proposal_id},
            )
        normalized_events = [dict(event) for event in events]
        branch_id = str(values.get("branch_id") or "main")
        for index, event in enumerate(normalized_events):
            validate_event_branch_consistency(
                event,
                branch_id,
                event_id=f"{proposal_id}:{index}",
            )
            validate_correction_link_consistency(
                event,
                event_id=f"{proposal_id}:{index}",
            )
            expand_correction_event(
                str(
                    event.get("event_type")
                    or event.get("type")
                    or "fact"
                ),
                event,
                event_id=f"{proposal_id}:{index}",
            )
        try:
            actual_hash = stable_hash(
                {"payload": payload, "events": normalized_events},
                prefix="payload_",
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise ContinuityError(
                "ACCEPTED_PAYLOAD_CORRUPT",
                "accepted proposal payload cannot be canonically hashed",
                details={"proposal_id": proposal_id},
            ) from exc
        expected_hash = str(values.get("payload_hash") or "")
        if actual_hash != expected_hash:
            raise ContinuityError(
                "ACCEPTED_PAYLOAD_HASH_MISMATCH",
                "accepted proposal JSON no longer matches its payload hash",
                details={
                    "proposal_id": proposal_id,
                    "expected": expected_hash,
                    "actual": actual_hash,
                },
            )
        return payload, normalized_events

    @classmethod
    def _validate_accepted_ledger_integrity(
        cls,
        connection: sqlite3.Connection,
    ) -> None:
        """Validate frozen proposals, accepted rows and links before replay.

        Replay clears and republishes every projection, so it is the final
        trust boundary for direct SQLite damage. Every check runs before the
        first projection DELETE and therefore leaves the last good snapshot
        intact when the ledger is incomplete or inconsistent.
        """

        legacy_marker_row = connection.execute(
            """
            SELECT value
            FROM state_meta
            WHERE key='legacy_v4_power_compat_head_revision'
            """
        ).fetchone()
        legacy_v4_power_compat_enabled = legacy_marker_row is not None
        legacy_v4_power_cutoff = ContinuityStore.get_meta_int(
            connection,
            "legacy_v4_power_compat_head_revision",
            default=0,
        )
        commit_rows = list(
            connection.execute(
                """
                SELECT
                    c.*,
                    p.proposal_id AS stored_proposal_id,
                    p.artifact_version_id AS proposal_artifact_version_id,
                    p.artifact_id AS proposal_artifact_id,
                    p.artifact_stage AS proposal_artifact_stage,
                    p.branch_id AS proposal_branch_id,
                    p.chapter_no AS proposal_chapter_no,
                    p.scene_index AS proposal_scene_index,
                    p.artifact_revision AS proposal_artifact_revision,
                    p.proposal_kind AS proposal_kind,
                    p.payload_hash AS proposal_payload_hash,
                    p.payload_json AS proposal_payload_json,
                    p.events_json AS proposal_events_json,
                    p.accepted_commit_id AS proposal_accepted_commit_id,
                    p.canon_status AS proposal_canon_status
                FROM canon_commits AS c
                LEFT JOIN proposals AS p ON p.proposal_id=c.proposal_id
                ORDER BY c.head_revision_after, c.commit_id
                """
            )
        )
        proposal_rows = list(
            connection.execute(
                "SELECT * FROM proposals ORDER BY proposal_id"
            )
        )
        proposal_rows_by_id = {
            str(row["proposal_id"]): row for row in proposal_rows
        }
        proposals: dict[str, dict[str, Any]] = {}
        commits_by_id: dict[str, sqlite3.Row] = {}
        accepts_by_proposal: dict[str, sqlite3.Row] = {}
        retracts_by_proposal: dict[str, list[sqlite3.Row]] = defaultdict(list)
        previous_head = 0
        previous_active = 0
        for commit in commit_rows:
            commit_id = str(commit["commit_id"])
            proposal_id = str(commit["proposal_id"])
            commits_by_id[commit_id] = commit
            if commit["stored_proposal_id"] is None:
                raise ContinuityError(
                    "ACCEPTED_PROPOSAL_MISSING",
                    "canon commit references a missing proposal",
                    details={
                        "commit_id": commit_id,
                        "proposal_id": proposal_id,
                    },
                )
            for field in (
                "artifact_revision",
                "head_revision_before",
                "head_revision_after",
                "active_revision_before",
                "active_revision_after",
                "changes_authority",
            ):
                if type(commit[field]) is not int:
                    raise ContinuityError(
                        "ACCEPTED_COMMIT_NUMERIC_CORRUPT",
                        "canon commit integer fields must retain their SQLite integer type",
                        details={
                            "commit_id": commit_id,
                            "field": field,
                            "value": commit[field],
                            "sqlite_type": type(commit[field]).__name__,
                        },
                    )
            if int(commit["changes_authority"]) not in {0, 1}:
                raise ContinuityError(
                    "ACCEPTED_COMMIT_AUTHORITY_MISMATCH",
                    "canon commit authority flag must be exactly 0 or 1",
                    details={
                        "commit_id": commit_id,
                        "changes_authority": commit["changes_authority"],
                    },
                )
            if int(commit["artifact_revision"]) < 1:
                raise ContinuityError(
                    "ACCEPTED_COMMIT_NUMERIC_CORRUPT",
                    "artifact revision must be at least 1",
                    details={
                        "commit_id": commit_id,
                        "field": "artifact_revision",
                        "value": commit["artifact_revision"],
                    },
                )
            for field in (
                "head_revision_before",
                "head_revision_after",
                "active_revision_before",
                "active_revision_after",
            ):
                if int(commit[field]) < 0:
                    raise ContinuityError(
                        "ACCEPTED_COMMIT_NUMERIC_CORRUPT",
                        "canon revisions must be non-negative",
                        details={
                            "commit_id": commit_id,
                            "field": field,
                            "value": commit[field],
                        },
                    )
            for commit_field, proposal_field in (
                ("chapter_no", "proposal_chapter_no"),
                ("scene_index", "proposal_scene_index"),
            ):
                for field, value in (
                    (commit_field, commit[commit_field]),
                    (proposal_field, commit[proposal_field]),
                ):
                    if value is not None and type(value) is not int:
                        raise ContinuityError(
                            "ACCEPTED_COMMIT_NUMERIC_CORRUPT",
                            "canon commit story coordinates must be SQLite integers",
                            details={
                                "commit_id": commit_id,
                                "field": field,
                                "value": value,
                                "sqlite_type": type(value).__name__,
                            },
                        )
            if type(commit["proposal_artifact_revision"]) is not int:
                raise ContinuityError(
                    "ACCEPTED_COMMIT_NUMERIC_CORRUPT",
                    "proposal artifact revision must retain its SQLite integer type",
                    details={
                        "commit_id": commit_id,
                        "field": "proposal_artifact_revision",
                        "value": commit["proposal_artifact_revision"],
                        "sqlite_type": type(
                            commit["proposal_artifact_revision"]
                        ).__name__,
                    },
                )
            if int(commit["proposal_artifact_revision"]) < 1:
                raise ContinuityError(
                    "ACCEPTED_COMMIT_NUMERIC_CORRUPT",
                    "proposal artifact revision must be at least 1",
                    details={
                        "commit_id": commit_id,
                        "field": "proposal_artifact_revision",
                        "value": commit["proposal_artifact_revision"],
                    },
                )
            try:
                head_before = int(commit["head_revision_before"])
                head_after = int(commit["head_revision_after"])
                active_before = int(commit["active_revision_before"])
                active_after = int(commit["active_revision_after"])
            except (TypeError, ValueError) as exc:
                raise ContinuityError(
                    "ACCEPTED_COMMIT_REVISION_CORRUPT",
                    "canon commit revision fields are not integers",
                    details={"commit_id": commit_id},
                ) from exc
            if head_before != previous_head or head_after != head_before + 1:
                raise ContinuityError(
                    "ACCEPTED_COMMIT_REVISION_CORRUPT",
                    "canon commit revisions are not a contiguous ledger",
                    details={
                        "commit_id": commit_id,
                        "expected_head_before": previous_head,
                        "actual_head_before": head_before,
                        "head_revision_after": head_after,
                    },
                )
            previous_head = head_after
            expected_active_after = (
                head_after
                if bool(commit["changes_authority"])
                else previous_active
            )
            if (
                active_before != previous_active
                or active_after != expected_active_after
            ):
                raise ContinuityError(
                    "ACCEPTED_ACTIVE_REVISION_CORRUPT",
                    "canon commit active revisions are not a contiguous authority ledger",
                    details={
                        "commit_id": commit_id,
                        "expected_active_before": previous_active,
                        "actual_active_before": active_before,
                        "expected_active_after": expected_active_after,
                        "actual_active_after": active_after,
                    },
                )
            previous_active = active_after
            for commit_field, proposal_field in (
                ("artifact_id", "proposal_artifact_id"),
                ("artifact_stage", "proposal_artifact_stage"),
                ("branch_id", "proposal_branch_id"),
                ("chapter_no", "proposal_chapter_no"),
                ("scene_index", "proposal_scene_index"),
                ("artifact_revision", "proposal_artifact_revision"),
            ):
                if commit[commit_field] != commit[proposal_field]:
                    raise ContinuityError(
                        "ACCEPTED_COMMIT_PROPOSAL_MISMATCH",
                        "canon commit metadata differs from its frozen proposal",
                        details={
                            "commit_id": commit_id,
                            "proposal_id": proposal_id,
                            "field": commit_field,
                            "commit": commit[commit_field],
                            "proposal": commit[proposal_field],
                        },
                    )
            if str(commit["payload_hash"]) != str(
                commit["proposal_payload_hash"]
            ):
                raise ContinuityError(
                    "ACCEPTED_COMMIT_PAYLOAD_MISMATCH",
                    "canon commit payload hash differs from its frozen proposal",
                    details={
                        "commit_id": commit_id,
                        "proposal_id": proposal_id,
                    },
                )
            expected_authority = changes_authority(
                str(commit["artifact_stage"]),
                str(commit["branch_id"]),
            )
            if bool(commit["changes_authority"]) != expected_authority:
                raise ContinuityError(
                    "ACCEPTED_COMMIT_AUTHORITY_MISMATCH",
                    "canon commit authority flag is inconsistent with its branch and stage",
                    details={
                        "commit_id": commit_id,
                        "branch_id": str(commit["branch_id"]),
                        "artifact_stage": str(commit["artifact_stage"]),
                        "changes_authority": bool(
                            commit["changes_authority"]
                        ),
                    },
                )
            if proposal_id not in proposals:
                proposal_values = {
                    "proposal_id": proposal_id,
                    "payload_hash": str(commit["proposal_payload_hash"]),
                    "payload_json": str(commit["proposal_payload_json"]),
                    "events_json": str(commit["proposal_events_json"]),
                    "branch_id": str(commit["proposal_branch_id"]),
                    "proposal_kind": str(commit["proposal_kind"]),
                }
                _payload, proposal_events = cls._strict_proposal_content(
                    proposal_values
                )
                proposals[proposal_id] = {
                    **proposal_values,
                    "events": proposal_events,
                }
            operation = str(commit["operation"])
            if operation == "accept":
                if proposal_id in accepts_by_proposal:
                    raise ContinuityError(
                        "ACCEPTED_COMMIT_DUPLICATE",
                        "proposal has more than one accept commit",
                        details={"proposal_id": proposal_id},
                    )
                accepts_by_proposal[proposal_id] = commit
                if str(
                    commit["proposal_accepted_commit_id"] or ""
                ) != commit_id:
                    raise ContinuityError(
                        "ACCEPTED_COMMIT_LINK_MISSING",
                        "accepted proposal does not point to its accept commit",
                        details={
                            "proposal_id": proposal_id,
                            "commit_id": commit_id,
                        },
                    )
            elif operation != "retract":
                raise ContinuityError(
                    "ACCEPTED_COMMIT_OPERATION_INVALID",
                    "canon ledger contains an unsupported operation",
                    details={"commit_id": commit_id, "operation": operation},
                )
            else:
                retracts_by_proposal[proposal_id].append(commit)

        for proposal_id, proposal_row in proposal_rows_by_id.items():
            status = str(proposal_row["canon_status"])
            accepted_commit = accepts_by_proposal.get(proposal_id)
            retract_commits = retracts_by_proposal.get(proposal_id, [])
            accepted_commit_id = str(
                proposal_row["accepted_commit_id"] or ""
            )
            if len(retract_commits) > 1:
                raise ContinuityError(
                    "ACCEPTED_COMMIT_DUPLICATE",
                    "proposal has more than one retract commit",
                    details={"proposal_id": proposal_id},
                )
            if accepted_commit is None:
                if retract_commits:
                    raise ContinuityError(
                        "ACCEPTED_COMMIT_NOT_FOUND",
                        "retract commit has no corresponding accept commit",
                        details={"proposal_id": proposal_id},
                    )
                if status in {"accepted", "retracted"}:
                    raise ContinuityError(
                        "ACCEPTED_PROPOSAL_COMMIT_MISSING",
                        "accepted proposal has no immutable accept commit",
                        details={
                            "proposal_id": proposal_id,
                            "canon_status": status,
                        },
                    )
                if accepted_commit_id:
                    raise ContinuityError(
                        "ACCEPTED_PROPOSAL_LINK_MISMATCH",
                        "unaccepted proposal points to an accept commit",
                        details={"proposal_id": proposal_id},
                    )
                if status not in {"proposed", "rejected"}:
                    raise ContinuityError(
                        "ACCEPTED_PROPOSAL_STATUS_INVALID",
                        "proposal has an unsupported unaccepted canon status",
                        details={
                            "proposal_id": proposal_id,
                            "canon_status": status,
                        },
                    )
                continue

            accept_id = str(accepted_commit["commit_id"])
            if accepted_commit_id != accept_id:
                raise ContinuityError(
                    "ACCEPTED_COMMIT_LINK_MISSING",
                    "proposal accepted_commit_id does not point to its accept commit",
                    details={
                        "proposal_id": proposal_id,
                        "expected": accept_id,
                        "actual": accepted_commit_id,
                    },
                )
            if retract_commits:
                retract_commit = retract_commits[0]
                if (
                    int(retract_commit["head_revision_after"])
                    <= int(accepted_commit["head_revision_after"])
                ):
                    raise ContinuityError(
                        "ACCEPTED_COMMIT_REVISION_CORRUPT",
                        "retract commit must follow its accept commit",
                        details={"proposal_id": proposal_id},
                    )
                expected_status = "retracted"
            else:
                expected_status = "accepted"
            if status != expected_status:
                raise ContinuityError(
                    "ACCEPTED_PROPOSAL_STATUS_MISMATCH",
                    "proposal canon status does not match immutable commit history",
                    details={
                        "proposal_id": proposal_id,
                        "expected": expected_status,
                        "actual": status,
                    },
                )

        def _meta_revision(key: str) -> int:
            row = connection.execute(
                "SELECT value FROM state_meta WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                raise ContinuityError(
                    "ACCEPTED_REVISION_META_MISSING",
                    "accepted ledger revision metadata is missing",
                    details={"key": key},
                )
            try:
                return int(row[0])
            except (TypeError, ValueError) as exc:
                raise ContinuityError(
                    "ACCEPTED_REVISION_META_CORRUPT",
                    "accepted ledger revision metadata is not an integer",
                    details={"key": key, "value": row[0]},
                ) from exc

        stored_head = _meta_revision("head_canon_revision")
        stored_active = _meta_revision("active_canon_revision")
        if stored_head != previous_head or stored_active != previous_active:
            raise ContinuityError(
                "ACCEPTED_REVISION_META_MISMATCH",
                "state_meta revisions do not match the immutable commit ledger",
                details={
                    "expected_head": previous_head,
                    "actual_head": stored_head,
                    "expected_active": previous_active,
                    "actual_active": stored_active,
                },
            )

        event_rows = list(
            connection.execute(
                """
                SELECT e.*, c.proposal_id, c.operation, c.branch_id AS commit_branch_id,
                       c.changes_authority, c.head_revision_after
                FROM continuity_events AS e
                LEFT JOIN canon_commits AS c ON c.commit_id=e.commit_id
                ORDER BY c.head_revision_after, e.event_ordinal, e.event_id
                """
            )
        )
        events_by_commit: dict[str, list[sqlite3.Row]] = defaultdict(list)
        event_info: dict[str, dict[str, Any]] = {}
        for row in event_rows:
            event_id = str(row["event_id"])
            commit_id = str(row["commit_id"])
            if row["proposal_id"] is None or commit_id not in commits_by_id:
                raise ContinuityError(
                    "ACCEPTED_EVENT_COMMIT_MISSING",
                    "accepted event references a missing canon commit",
                    details={"event_id": event_id, "commit_id": commit_id},
                )
            for field in (
                "event_ordinal",
                "artifact_revision",
                "head_revision_after",
                "changes_authority",
            ):
                if type(row[field]) is not int:
                    raise ContinuityError(
                        "ACCEPTED_EVENT_NUMERIC_CORRUPT",
                        "accepted event integer fields must retain their SQLite integer type",
                        details={
                            "event_id": event_id,
                            "field": field,
                            "value": row[field],
                            "sqlite_type": type(row[field]).__name__,
                        },
                    )
            if int(row["changes_authority"]) not in {0, 1}:
                raise ContinuityError(
                    "ACCEPTED_COMMIT_AUTHORITY_MISMATCH",
                    "accepted event commit authority flag must be exactly 0 or 1",
                    details={
                        "event_id": event_id,
                        "changes_authority": row["changes_authority"],
                    },
                )
            if int(row["event_ordinal"]) < 0:
                raise ContinuityError(
                    "ACCEPTED_EVENT_NUMERIC_CORRUPT",
                    "event ordinal must be non-negative",
                    details={
                        "event_id": event_id,
                        "field": "event_ordinal",
                        "value": row["event_ordinal"],
                    },
                )
            if int(row["artifact_revision"]) < 1:
                raise ContinuityError(
                    "ACCEPTED_EVENT_NUMERIC_CORRUPT",
                    "event artifact revision must be at least 1",
                    details={
                        "event_id": event_id,
                        "field": "artifact_revision",
                        "value": row["artifact_revision"],
                    },
                )
            if event_id in event_info:
                raise ContinuityError(
                    "ACCEPTED_EVENT_DUPLICATE",
                    "accepted event id is not unique",
                    details={"event_id": event_id},
                )
            events_by_commit[commit_id].append(row)
            event_info[event_id] = {
                "event_id": event_id,
                "commit_id": commit_id,
                "proposal_id": str(row["proposal_id"]),
                "branch_id": str(row["branch_id"]),
                "changes_authority": bool(row["changes_authority"]),
                "head_revision_after": int(row["head_revision_after"]),
                "artifact_id": str(row["artifact_id"]),
                "artifact_revision": int(row["artifact_revision"]),
                "event_ordinal": int(row["event_ordinal"]),
            }

        for commit in commit_rows:
            commit_id = str(commit["commit_id"])
            proposal_id = str(commit["proposal_id"])
            actual_rows = events_by_commit.get(commit_id, [])
            if str(commit["operation"]) != "accept":
                if actual_rows:
                    raise ContinuityError(
                        "ACCEPTED_EVENT_SET_INCOMPLETE",
                        "non-accept commit unexpectedly owns continuity events",
                        details={"commit_id": commit_id},
                    )
                continue
            expected_events = list(proposals[proposal_id]["events"])
            if len(actual_rows) != len(expected_events):
                raise ContinuityError(
                    "ACCEPTED_EVENT_SET_INCOMPLETE",
                    "accepted event rows do not match the frozen proposal count",
                    details={
                        "commit_id": commit_id,
                        "proposal_id": proposal_id,
                        "expected_count": len(expected_events),
                        "actual_count": len(actual_rows),
                    },
                )
            for index, (expected, actual) in enumerate(
                zip(expected_events, actual_rows)
            ):
                event_id = str(actual["event_id"])
                if int(actual["event_ordinal"]) != index:
                    raise ContinuityError(
                        "ACCEPTED_EVENT_SET_INCOMPLETE",
                        "accepted event ordinals are incomplete or reordered",
                        details={
                            "commit_id": commit_id,
                            "event_id": event_id,
                            "expected_ordinal": index,
                            "actual_ordinal": int(actual["event_ordinal"]),
                        },
                    )
                expected_event_id = stable_hash(
                    [commit_id, index, expected],
                    prefix="story_event_",
                )
                legacy_event_id = str(expected.get("event_id") or "")
                legacy_event_id_allowed = (
                    str(commit["proposal_kind"]) == "legacy_power_import"
                    and legacy_v4_power_compat_enabled
                    and legacy_event_id.startswith("story_event_legacy_power_")
                    and event_id == legacy_event_id
                )
                if event_id != expected_event_id and not legacy_event_id_allowed:
                    raise ContinuityError(
                        "ACCEPTED_EVENT_ID_MISMATCH",
                        "accepted event id does not match the frozen event identity",
                        details={
                            "commit_id": commit_id,
                            "proposal_id": proposal_id,
                            "event_ordinal": index,
                            "expected": expected_event_id,
                            "actual": event_id,
                        },
                    )
                try:
                    stored_payload = json.loads(str(actual["payload_json"]))
                    stored_evidence = json.loads(str(actual["evidence_json"]))
                except (
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                    RecursionError,
                ) as exc:
                    raise ContinuityError(
                        "ACCEPTED_EVENT_PAYLOAD_CORRUPT",
                        "accepted event payload or evidence is not valid JSON",
                        details={"event_id": event_id},
                    ) from exc
                payload_matches = (
                    isinstance(stored_payload, Mapping)
                    and _strict_json_equal(stored_payload, expected)
                )
                if (
                    not payload_matches
                    and legacy_v4_power_cutoff > 0
                    and int(actual["head_revision_after"])
                    <= legacy_v4_power_cutoff
                    and isinstance(stored_payload, Mapping)
                ):
                    legacy_payload = dict(stored_payload)
                    payload_matches = (
                        str(expected.get("event_type") or "") == "ability"
                        and str(expected.get("action") or "") == "gain"
                        and str(legacy_payload.get("action") or "")
                        == "breakthrough"
                    )
                    if payload_matches:
                        legacy_payload["action"] = "gain"
                        payload_matches = _strict_json_equal(
                            legacy_payload,
                            expected,
                        )
                if not payload_matches:
                    raise ContinuityError(
                        "ACCEPTED_EVENT_PAYLOAD_MISMATCH",
                        "accepted event payload differs from the frozen proposal",
                        details={
                            "event_id": event_id,
                            "proposal_id": proposal_id,
                            "event_ordinal": index,
                        },
                    )
                expected_evidence = dict(expected.get("evidence") or {})
                if (
                    not isinstance(stored_evidence, Mapping)
                    or not _strict_json_equal(
                        stored_evidence,
                        expected_evidence,
                    )
                ):
                    raise ContinuityError(
                        "ACCEPTED_EVENT_EVIDENCE_MISMATCH",
                        "accepted event evidence differs from the frozen proposal",
                        details={"event_id": event_id},
                    )
                expected_type = str(
                    expected.get("event_type")
                    or expected.get("type")
                    or "fact"
                )
                if str(actual["event_type"]) != expected_type:
                    raise ContinuityError(
                        "ACCEPTED_EVENT_TYPE_MISMATCH",
                        "accepted event type differs from the frozen proposal",
                        details={
                            "event_id": event_id,
                            "expected": expected_type,
                            "actual": str(actual["event_type"]),
                        },
                    )
                expected_branch = str(
                    expected.get("branch_id")
                    or commit["proposal_branch_id"]
                    or "main"
                )
                if (
                    expected_branch != str(commit["proposal_branch_id"])
                    or str(actual["branch_id"]) != expected_branch
                    or str(actual["commit_branch_id"]) != expected_branch
                ):
                    raise ContinuityError(
                        "ACCEPTED_EVENT_BRANCH_MISMATCH",
                        "accepted event, proposal and commit branches differ",
                        details={
                            "event_id": event_id,
                            "proposal_branch_id": str(
                                commit["proposal_branch_id"]
                            ),
                            "event_payload_branch_id": expected_branch,
                            "event_row_branch_id": str(actual["branch_id"]),
                            "commit_branch_id": str(
                                actual["commit_branch_id"]
                            ),
                        },
                    )

        link_rows = list(
            connection.execute(
                """
                SELECT
                    l.*,
                    sc.operation AS source_operation,
                    sc.branch_id AS source_commit_branch,
                    sc.changes_authority AS source_changes_authority,
                    sc.head_revision_after AS source_head_revision,
                    se.commit_id AS source_event_commit_id,
                    se.branch_id AS source_event_branch,
                    tc.branch_id AS target_commit_branch,
                    tc.changes_authority AS target_changes_authority,
                    tc.head_revision_after AS target_head_revision,
                    te.branch_id AS target_event_branch
                FROM event_links AS l
                LEFT JOIN canon_commits AS sc
                  ON sc.commit_id=l.source_commit_id
                LEFT JOIN continuity_events AS se
                  ON se.event_id=l.source_event_id
                LEFT JOIN continuity_events AS te
                  ON te.event_id=l.target_event_id
                LEFT JOIN canon_commits AS tc
                  ON tc.commit_id=te.commit_id
                ORDER BY sc.head_revision_after, l.link_id
                """
            )
        )
        links_by_commit: dict[str, list[sqlite3.Row]] = defaultdict(list)
        logical_links: set[tuple[str, str | None, str, str]] = set()
        for link in link_rows:
            link_id = str(link["link_id"])
            source_commit_id = str(link["source_commit_id"])
            source_event_id = (
                str(link["source_event_id"])
                if link["source_event_id"] is not None
                else None
            )
            target_event_id = str(link["target_event_id"])
            link_type = str(link["link_type"])
            if (
                source_commit_id not in commits_by_id
                or target_event_id not in event_info
            ):
                raise ContinuityError(
                    "ACCEPTED_LINK_TARGET_MISSING",
                    "accepted event link has a missing source commit or target event",
                    details={"link_id": link_id},
                )
            if link_type not in {"supersedes", "retracts", "caused_by"}:
                raise ContinuityError(
                    "ACCEPTED_LINK_TYPE_INVALID",
                    "accepted event link has an unsupported type",
                    details={"link_id": link_id, "link_type": link_type},
                )
            if source_event_id is not None:
                if source_event_id not in event_info:
                    raise ContinuityError(
                        "ACCEPTED_LINK_SOURCE_MISSING",
                        "accepted event link references a missing source event",
                        details={"link_id": link_id},
                    )
                if str(link["source_event_commit_id"]) != source_commit_id:
                    raise ContinuityError(
                        "ACCEPTED_LINK_SOURCE_MISMATCH",
                        "accepted event link source event belongs to another commit",
                        details={"link_id": link_id},
                    )
            source_head = int(link["source_head_revision"])
            target_head = int(link["target_head_revision"])
            if target_head >= source_head:
                raise ContinuityError(
                    "ACCEPTED_LINK_CYCLE",
                    "accepted event links must point strictly backward in the ledger",
                    details={
                        "link_id": link_id,
                        "source_head_revision": source_head,
                        "target_head_revision": target_head,
                    },
                )
            if link_type in {"supersedes", "retracts"}:
                source_branch = str(link["source_commit_branch"])
                target_branch = str(link["target_event_branch"])
                if source_branch != target_branch:
                    raise ContinuityError(
                        "ACCEPTED_LINK_BRANCH_MISMATCH",
                        "supersede and retract links must stay in one branch",
                        details={
                            "link_id": link_id,
                            "source_branch_id": source_branch,
                            "target_branch_id": target_branch,
                        },
                    )
                if bool(link["source_changes_authority"]) != bool(
                    link["target_changes_authority"]
                ):
                    raise ContinuityError(
                        "ACCEPTED_LINK_AUTHORITY_MISMATCH",
                        "supersede and retract links must stay in one authority plane",
                        details={"link_id": link_id},
                    )
            logical = (
                source_commit_id,
                source_event_id,
                target_event_id,
                link_type,
            )
            if logical in logical_links:
                raise ContinuityError(
                    "ACCEPTED_LINK_DUPLICATE",
                    "accepted event link is duplicated",
                    details={"link_id": link_id},
                )
            logical_links.add(logical)
            links_by_commit[source_commit_id].append(link)

        accepted_so_far: list[dict[str, Any]] = []
        # Reconstruct the effective link ledger in commit order.  An older
        # supersession link may stop being effective when its source proposal
        # is retracted, or when its source event is itself superseded.  A
        # permanent ``linked_targets`` set would therefore reject the
        # automatic link needed by a later artifact revision.
        simulated_links: list[dict[str, Any]] = []
        simulated_retracted: set[str] = set()

        def simulated_inactive(
            target_branch: str,
            target_authority: bool,
        ) -> set[str]:
            rows = [
                link
                for link in simulated_links
                if link["branch_id"] == target_branch
                and link["changes_authority"] == target_authority
            ]
            rows.sort(
                key=lambda link: (
                    int(link["head_revision_after"]),
                    int(link["source_ordinal"]),
                    int(link["sequence"]),
                ),
                reverse=True,
            )
            inactive: set[str] = set()
            for link in rows:
                if (
                    link["source_operation"] == "accept"
                    and link["proposal_id"] in simulated_retracted
                ):
                    continue
                source_event_id = link["source_event_id"]
                if source_event_id and source_event_id in inactive:
                    continue
                inactive.add(str(link["target_event_id"]))
            return inactive

        simulation_sequence = 0

        def append_simulated_link(
            *,
            source_event_id: str | None,
            target_event_id: str,
            link_type: str,
            commit: sqlite3.Row,
            source_ordinal: int,
        ) -> None:
            nonlocal simulation_sequence
            if link_type not in {"supersedes", "retracts"}:
                return
            simulated_links.append(
                {
                    "source_event_id": source_event_id,
                    "target_event_id": target_event_id,
                    "link_type": link_type,
                    "source_operation": str(commit["operation"]),
                    "proposal_id": str(commit["proposal_id"]),
                    "branch_id": str(commit["branch_id"]),
                    "changes_authority": bool(commit["changes_authority"]),
                    "head_revision_after": int(commit["head_revision_after"]),
                    "source_ordinal": int(source_ordinal),
                    "sequence": simulation_sequence,
                }
            )
            simulation_sequence += 1

        for commit in commit_rows:
            commit_id = str(commit["commit_id"])
            proposal_id = str(commit["proposal_id"])
            branch_id = str(commit["branch_id"])
            authority = bool(commit["changes_authority"])
            actual_links = links_by_commit.get(commit_id, [])
            actual_explicit = {
                (
                    str(link["source_event_id"]),
                    str(link["target_event_id"]),
                    str(link["link_type"]),
                )
                for link in actual_links
                if link["source_event_id"] is not None
            }
            actual_automatic = {
                (str(link["target_event_id"]), str(link["link_type"]))
                for link in actual_links
                if link["source_event_id"] is None
            }
            if str(commit["operation"]) == "accept":
                expected_explicit: set[tuple[str, str, str]] = set()
                current_events = events_by_commit.get(commit_id, [])
                proposal_events = list(proposals[proposal_id]["events"])
                for index, event in enumerate(proposal_events):
                    source_event_id = str(current_events[index]["event_id"])
                    for field, link_type in (
                        ("supersedes", "supersedes"),
                        ("retracts", "retracts"),
                        ("caused_by", "caused_by"),
                    ):
                        targets = list(event.get(field) or [])
                        if len(targets) != len(set(targets)):
                            raise ContinuityError(
                                "ACCEPTED_LINK_DUPLICATE",
                                "frozen proposal contains duplicate event links",
                                details={
                                    "proposal_id": proposal_id,
                                    "event_ordinal": index,
                                    "field": field,
                                },
                            )
                        for target in targets:
                            expected_explicit.add(
                                (
                                    source_event_id,
                                    str(target),
                                    link_type,
                                )
                            )
                if actual_explicit != expected_explicit:
                    raise ContinuityError(
                        "ACCEPTED_LINK_SET_INCOMPLETE",
                        "accepted explicit links do not match the frozen event payloads",
                        details={
                            "commit_id": commit_id,
                            "missing": sorted(
                                expected_explicit - actual_explicit
                            ),
                            "unexpected": sorted(
                                actual_explicit - expected_explicit
                            ),
                        },
                    )
                event_ordinals = {
                    str(row["event_id"]): int(row["event_ordinal"])
                    for row in current_events
                }
                for source, target, link_type in expected_explicit:
                    append_simulated_link(
                        source_event_id=source,
                        target_event_id=target,
                        link_type=link_type,
                        commit=commit,
                        source_ordinal=event_ordinals.get(source, 0),
                    )
                inactive = simulated_inactive(branch_id, authority)
                expected_automatic = {
                    (str(item["event_id"]), "supersedes")
                    for item in accepted_so_far
                    if item["artifact_id"] == str(commit["artifact_id"])
                    and item["branch_id"] == branch_id
                    and item["changes_authority"] == authority
                    and int(item["artifact_revision"])
                    < int(commit["artifact_revision"])
                    and str(item["event_id"]) not in inactive
                }
                if actual_automatic != expected_automatic:
                    raise ContinuityError(
                        "ACCEPTED_LINK_SET_INCOMPLETE",
                        "automatic artifact supersession links are incomplete",
                        details={
                            "commit_id": commit_id,
                            "missing": sorted(
                                expected_automatic - actual_automatic
                            ),
                            "unexpected": sorted(
                                actual_automatic - expected_automatic
                            ),
                        },
                    )
                for target, link_type in expected_automatic:
                    append_simulated_link(
                        source_event_id=None,
                        target_event_id=target,
                        link_type=link_type,
                        commit=commit,
                        source_ordinal=1000000,
                    )
                accepted_so_far.extend(
                    event_info[str(row["event_id"])]
                    for row in current_events
                )
            else:
                if actual_explicit:
                    raise ContinuityError(
                        "ACCEPTED_LINK_SET_INCOMPLETE",
                        "retract commits cannot own explicit source events",
                        details={"commit_id": commit_id},
                    )
                accepted_commit = accepts_by_proposal.get(proposal_id)
                if accepted_commit is None:
                    raise ContinuityError(
                        "ACCEPTED_COMMIT_NOT_FOUND",
                        "retract commit has no earlier accept commit",
                        details={"proposal_id": proposal_id},
                    )
                accepted_commit_id = str(accepted_commit["commit_id"])
                expected_automatic = {
                    (str(row["event_id"]), "retracts")
                    for row in events_by_commit.get(accepted_commit_id, [])
                }
                if actual_automatic != expected_automatic:
                    raise ContinuityError(
                        "ACCEPTED_LINK_SET_INCOMPLETE",
                        "proposal retraction links are incomplete",
                        details={
                            "commit_id": commit_id,
                            "missing": sorted(
                                expected_automatic - actual_automatic
                            ),
                            "unexpected": sorted(
                                actual_automatic - expected_automatic
                            ),
                        },
                    )
                for target, link_type in expected_automatic:
                    append_simulated_link(
                        source_event_id=None,
                        target_event_id=target,
                        link_type=link_type,
                        commit=commit,
                        source_ordinal=1000000,
                    )
                simulated_retracted.add(proposal_id)

    @staticmethod
    def _inactive_event_sets(
        connection: sqlite3.Connection,
    ) -> tuple[set[str], dict[str, set[str]]]:
        # Links always point to earlier accepted events. Processing sources
        # newest-first lets a later retraction deactivate a correction before
        # that correction can supersede its own target. Authority and each
        # provisional branch use independent inactive sets.
        rows = connection.execute(
            """
            SELECT
                l.source_event_id,
                l.target_event_id,
                c.operation AS source_operation,
                c.changes_authority AS source_changes_authority,
                c.branch_id AS source_branch_id,
                p.canon_status AS source_proposal_status,
                c.head_revision_after,
                COALESCE(e.event_ordinal, 1000000) AS source_ordinal
            FROM event_links AS l
            JOIN canon_commits AS c
              ON c.commit_id=l.source_commit_id
            JOIN proposals AS p
              ON p.proposal_id=c.proposal_id
            LEFT JOIN continuity_events AS e
              ON e.event_id=l.source_event_id
            WHERE l.link_type IN ('supersedes', 'retracts')
            ORDER BY c.head_revision_after DESC,
                     source_ordinal DESC,
                     l.link_id DESC
            """
        ).fetchall()
        authority_inactive: set[str] = set()
        branch_inactive: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            inactive = (
                authority_inactive
                if bool(row["source_changes_authority"])
                else branch_inactive[str(row["source_branch_id"])]
            )
            # A retracted accepted proposal no longer supplies active
            # correction/supersession links.  The later retract commit itself
            # still applies because its operation is "retract".
            if (
                str(row["source_operation"]) == "accept"
                and str(row["source_proposal_status"]) == "retracted"
            ):
                continue
            source_event_id = row["source_event_id"]
            if (
                source_event_id is not None
                and str(source_event_id) in inactive
            ):
                continue
            inactive.add(str(row["target_event_id"]))
        return authority_inactive, {
            branch_id: set(event_ids)
            for branch_id, event_ids in branch_inactive.items()
        }

    @staticmethod
    def _inactive_event_ids(connection: sqlite3.Connection) -> set[str]:
        """Compatibility view: item/current replay consumes authority only."""

        authority, _branches = ReplayEngine._inactive_event_sets(connection)
        return authority

    @staticmethod
    def _accepted_events(
        connection: sqlite3.Connection,
    ) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                """
                SELECT
                    e.*,
                    c.operation,
                    c.changes_authority,
                    c.head_revision_after,
                    (c.head_revision_after * 1000000 + e.event_ordinal)
                        AS updated_order
                FROM continuity_events AS e
                JOIN canon_commits AS c ON c.commit_id = e.commit_id
                WHERE c.operation = 'accept'
                ORDER BY c.head_revision_after, e.event_ordinal, e.event_id
                """
            )
        )

    @staticmethod
    def _clear_projections(connection: sqlite3.Connection) -> None:
        for table in PROJECTION_TABLES:
            connection.execute(f"DELETE FROM {table}")

    @staticmethod
    def _insert_generic(
        connection: sqlite3.Connection,
        table: str,
        descriptor: Mapping[str, Any],
    ) -> None:
        row = _projection_row(descriptor)
        if table == "canon_facts":
            connection.execute(
                """
                INSERT INTO canon_facts(
                    fact_key, fact_type, scope, entity_id,
                    subject_entity_id, target_entity_id, field_name,
                    value_json, source_event_id, chapter_no, scene_index,
                    story_time, updated_order
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
        elif table == "timeless_facts":
            connection.execute(
                """
                INSERT INTO timeless_facts(
                    fact_key, fact_type, entity_id, subject_entity_id,
                    target_entity_id, field_name, value_json, source_event_id,
                    updated_order
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row[0],
                    row[1],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                    row[12],
                ),
            )
        elif table == "planned_facts":
            connection.execute(
                """
                INSERT INTO planned_facts(
                    fact_key, fact_type, entity_id, subject_entity_id,
                    target_entity_id, field_name, value_json, source_event_id,
                    chapter_no, scene_index, story_time, updated_order
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row[0],
                    row[1],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                    row[9],
                    row[10],
                    row[11],
                    row[12],
                ),
            )

    @staticmethod
    def _insert_branch(
        connection: sqlite3.Connection,
        descriptor: Mapping[str, Any],
    ) -> None:
        branch_fact_key = stable_hash(
            [descriptor["branch_id"], descriptor["fact_key"]],
            prefix="branch_fact_",
        )
        connection.execute(
            """
            INSERT INTO branch_facts(
                branch_fact_key, branch_id, fact_key, fact_type, scope,
                entity_id, subject_entity_id, target_entity_id, field_name,
                value_json, source_event_id, chapter_no, scene_index,
                story_time, provisional, updated_order
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                branch_fact_key,
                descriptor["branch_id"],
                descriptor["fact_key"],
                descriptor["fact_type"],
                descriptor["scope"],
                descriptor.get("entity_id"),
                descriptor.get("subject_entity_id"),
                descriptor.get("target_entity_id"),
                descriptor["field_name"],
                canonical_json(descriptor.get("value")),
                descriptor["event_id"],
                descriptor.get("chapter_no"),
                descriptor.get("scene_index"),
                descriptor.get("story_time"),
                descriptor["updated_order"],
            ),
        )

    @staticmethod
    def _insert_fact_versions(
        connection: sqlite3.Connection,
        descriptors: Iterable[Mapping[str, Any]],
    ) -> None:
        by_key: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for descriptor in descriptors:
            by_key[str(descriptor["fact_key"])].append(descriptor)

        for fact_key, versions in sorted(by_key.items()):
            current_versions = [
                item for item in versions if item["scope"] != "historical"
            ]
            historical_versions = [
                item for item in versions if item["scope"] == "historical"
            ]
            ordered = sorted(
                current_versions,
                key=lambda item: (
                    item.get("chapter_no")
                    if item.get("chapter_no") is not None
                    else 2**31,
                    item.get("scene_index")
                    if item.get("scene_index") is not None
                    else 2**31,
                    item["updated_order"],
                    item["event_id"],
                ),
            )
            for index, descriptor in enumerate(ordered):
                next_descriptor = (
                    ordered[index + 1] if index + 1 < len(ordered) else None
                )
                version_id = stable_hash(
                    [
                        fact_key,
                        descriptor["event_id"],
                        descriptor["updated_order"],
                    ],
                    prefix="version_",
                )
                connection.execute(
                    """
                    INSERT INTO fact_versions(
                        version_id, fact_key, fact_type, scope, entity_id,
                        subject_entity_id, target_entity_id, field_name,
                        value_json, source_event_id, valid_from_chapter,
                        valid_from_scene, valid_to_chapter, valid_to_scene,
                        story_time, updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id,
                        fact_key,
                        descriptor["fact_type"],
                        descriptor["scope"],
                        descriptor.get("entity_id"),
                        descriptor.get("subject_entity_id"),
                        descriptor.get("target_entity_id"),
                        descriptor["field_name"],
                        canonical_json(descriptor.get("value")),
                        descriptor["event_id"],
                        descriptor.get("chapter_no"),
                        descriptor.get("scene_index"),
                        (
                            next_descriptor.get("chapter_no")
                            if next_descriptor is not None
                            else None
                        ),
                        (
                            next_descriptor.get("scene_index")
                            if next_descriptor is not None
                            else None
                        ),
                        descriptor.get("story_time"),
                        descriptor["updated_order"],
                    ),
                )
            for descriptor in sorted(
                historical_versions,
                key=lambda item: (
                    item.get("chapter_no")
                    if item.get("chapter_no") is not None
                    else 2**31,
                    item.get("scene_index")
                    if item.get("scene_index") is not None
                    else 2**31,
                    item["updated_order"],
                    item["event_id"],
                ),
            ):
                version_id = stable_hash(
                    [
                        fact_key,
                        descriptor["event_id"],
                        descriptor["updated_order"],
                        "historical",
                    ],
                    prefix="version_",
                )
                connection.execute(
                    """
                    INSERT INTO fact_versions(
                        version_id, fact_key, fact_type, scope, entity_id,
                        subject_entity_id, target_entity_id, field_name,
                        value_json, source_event_id, valid_from_chapter,
                        valid_from_scene, valid_to_chapter, valid_to_scene,
                        story_time, updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        version_id,
                        fact_key,
                        descriptor["fact_type"],
                        "historical",
                        descriptor.get("entity_id"),
                        descriptor.get("subject_entity_id"),
                        descriptor.get("target_entity_id"),
                        descriptor["field_name"],
                        canonical_json(descriptor.get("value")),
                        descriptor["event_id"],
                        descriptor.get("chapter_no"),
                        descriptor.get("scene_index"),
                        descriptor.get("story_time"),
                        descriptor["updated_order"],
                    ),
                )

    @staticmethod
    def _insert_typed_states(
        connection: sqlite3.Connection,
        current: Mapping[str, Mapping[str, Any]],
        timeless: Mapping[str, Mapping[str, Any]],
        ability_history: Iterable[Mapping[str, Any]],
    ) -> None:
        for descriptor in sorted(
            current.values(), key=lambda item: str(item["fact_key"])
        ):
            fact_type = descriptor["fact_type"]
            value = descriptor.get("value")
            if fact_type == "location":
                location = dict(value or {})
                connection.execute(
                    """
                    INSERT INTO location_state(
                        actor_entity_id, location_entity_id, transit_json,
                        source_event_id, chapter_no, scene_index, updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        descriptor["entity_id"],
                        location.get("location_entity_id"),
                        canonical_json(
                            {
                                key: location.get(key)
                                for key in (
                                    "from_location_entity_id",
                                    "action",
                                    "route",
                                    "method",
                                    "departed_at",
                                    "arrived_at",
                                )
                            }
                        ),
                        descriptor["event_id"],
                        descriptor.get("chapter_no"),
                        descriptor.get("scene_index"),
                        descriptor["updated_order"],
                    ),
                )
            elif fact_type == "inventory":
                inventory = dict(value or {})
                connection.execute(
                    """
                    INSERT INTO inventory_state(
                        inventory_key, item_entity_id, owner_entity_id,
                        quantity, is_unique, item_status, source_event_id,
                        updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        descriptor["fact_key"],
                        inventory.get("item_entity_id"),
                        inventory.get("owner_entity_id"),
                        inventory.get("quantity"),
                        int(bool(inventory.get("unique"))),
                        inventory.get("status") or "held",
                        descriptor["event_id"],
                        descriptor["updated_order"],
                    ),
                )
            elif fact_type == "relation":
                connection.execute(
                    """
                    INSERT INTO relation_state(
                        relation_key, source_entity_id, target_entity_id,
                        dimension, value_json, source_event_id, updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        descriptor["fact_key"],
                        descriptor["subject_entity_id"],
                        descriptor["target_entity_id"],
                        descriptor["field_name"],
                        canonical_json(value),
                        descriptor["event_id"],
                        descriptor["updated_order"],
                    ),
                )
            elif fact_type == "ability":
                ability = dict(value or {})
                if ability.get("acquired"):
                    connection.execute(
                        """
                        INSERT INTO ability_state(
                            ability_key, owner_entity_id, ability_entity_id,
                            state_json, source_event_id, updated_order
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (
                            descriptor["fact_key"],
                            descriptor["subject_entity_id"],
                            descriptor["target_entity_id"],
                            canonical_json(ability),
                            descriptor["event_id"],
                            descriptor["updated_order"],
                        ),
                    )
            elif fact_type == "ability_ownership":
                ownership = dict(value or {})
                connection.execute(
                    """
                    INSERT INTO actor_ability_state(
                        ability_key, owner_entity_id, ability_entity_id,
                        acquired, ownership_json, source_event_id,
                        story_coordinate_json, updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _fact_key(
                            "ability",
                            descriptor["subject_entity_id"],
                            descriptor["target_entity_id"],
                        ),
                        descriptor["subject_entity_id"],
                        descriptor["target_entity_id"],
                        int(bool(ownership.get("acquired"))),
                        canonical_json(ownership),
                        descriptor["event_id"],
                        canonical_json(
                            ownership.get("story_coordinate")
                            or (descriptor.get("raw") or {}).get(
                                "story_coordinate"
                            )
                            or {}
                        ),
                        descriptor["updated_order"],
                    ),
                )
            elif fact_type == "ability_runtime":
                runtime = dict(value or {})
                connection.execute(
                    """
                    INSERT INTO ability_runtime_state(
                        ability_key, owner_entity_id, ability_entity_id,
                        available, runtime_json, source_event_id,
                        story_coordinate_json, updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _fact_key(
                            "ability",
                            descriptor["subject_entity_id"],
                            descriptor["target_entity_id"],
                        ),
                        descriptor["subject_entity_id"],
                        descriptor["target_entity_id"],
                        int(bool(runtime.get("available", True))),
                        canonical_json(runtime),
                        descriptor["event_id"],
                        canonical_json(
                            runtime.get("last_story_coordinate") or {}
                        ),
                        descriptor["updated_order"],
                    ),
                )
            elif fact_type == "progression":
                progression = dict(value or {})
                connection.execute(
                    """
                    INSERT INTO actor_progression_state(
                        progression_key, actor_entity_id, track_entity_id,
                        rank_entity_id, state_json, source_event_id,
                        story_coordinate_json, updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        descriptor["fact_key"],
                        descriptor["subject_entity_id"],
                        descriptor["target_entity_id"],
                        progression.get("rank_entity_id"),
                        canonical_json(progression),
                        descriptor["event_id"],
                        canonical_json(
                            progression.get("story_coordinate") or {}
                        ),
                        descriptor["updated_order"],
                    ),
                )
            elif fact_type == "resource":
                resource = dict(value or {})
                connection.execute(
                    """
                    INSERT INTO actor_resource_state(
                        resource_key, actor_entity_id, resource_entity_id,
                        balance, reserved, state_json, source_event_id,
                        story_coordinate_json, updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        descriptor["fact_key"],
                        descriptor["subject_entity_id"],
                        descriptor["target_entity_id"],
                        float(resource.get("balance", 0)),
                        float(resource.get("reserved", 0)),
                        canonical_json(resource),
                        descriptor["event_id"],
                        canonical_json(
                            resource.get("story_coordinate") or {}
                        ),
                        descriptor["updated_order"],
                    ),
                )
            elif fact_type == "status_effect":
                status = dict(value or {})
                connection.execute(
                    """
                    INSERT INTO actor_status_state(
                        status_key, actor_entity_id, status_entity_id, active,
                        stacks, state_json, source_event_id,
                        story_coordinate_json, updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        descriptor["fact_key"],
                        descriptor["subject_entity_id"],
                        descriptor["target_entity_id"],
                        int(bool(status.get("active"))),
                        int(status.get("stacks", 0)),
                        canonical_json(status),
                        descriptor["event_id"],
                        canonical_json(
                            status.get("story_coordinate") or {}
                        ),
                        descriptor["updated_order"],
                    ),
                )
            elif fact_type == "power_binding":
                binding = dict(value or {})
                connection.execute(
                    """
                    INSERT INTO power_bindings(
                        binding_key, binding_id, actor_entity_id,
                        source_entity_id, binding_kind, active,
                        ability_entity_ids_json, state_json, source_event_id,
                        story_coordinate_json, updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        descriptor["fact_key"],
                        binding.get("binding_id"),
                        descriptor["subject_entity_id"],
                        binding.get("source_entity_id"),
                        binding.get("binding_kind")
                        or binding.get("action")
                        or "bind",
                        int(bool(binding.get("active"))),
                        canonical_json(
                            binding.get("ability_entity_ids") or []
                        ),
                        canonical_json(binding),
                        descriptor["event_id"],
                        canonical_json(
                            binding.get("story_coordinate") or {}
                        ),
                        descriptor["updated_order"],
                    ),
                )
            elif fact_type == "qualification":
                qualification = dict(value or {})
                connection.execute(
                    """
                    INSERT INTO qualification_state(
                        qualification_key, actor_entity_id,
                        qualification_entity_id, active, quantity, state_json,
                        source_event_id, story_coordinate_json, updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        descriptor["fact_key"],
                        descriptor["subject_entity_id"],
                        descriptor["target_entity_id"],
                        int(bool(qualification.get("active"))),
                        float(qualification.get("quantity", 0)),
                        canonical_json(qualification),
                        descriptor["event_id"],
                        canonical_json(
                            qualification.get("story_coordinate") or {}
                        ),
                        descriptor["updated_order"],
                    ),
                )
            elif fact_type == "power_observation":
                observation = dict(value or {})
                connection.execute(
                    """
                    INSERT INTO power_observations(
                        observation_key, observer_entity_id,
                        subject_entity_id, ability_entity_id,
                        observation_action, knowledge_plane,
                        observation_json, source_event_id,
                        story_coordinate_json, updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        descriptor["fact_key"],
                        observation.get("observer_entity_id")
                        or descriptor["entity_id"],
                        observation.get("subject_entity_id"),
                        observation.get("ability_entity_id"),
                        observation.get("action") or "observe",
                        observation.get("knowledge_plane")
                        or "actor_belief",
                        canonical_json(observation),
                        descriptor["event_id"],
                        canonical_json(
                            observation.get("story_coordinate") or {}
                        ),
                        descriptor["updated_order"],
                    ),
                )
            elif fact_type == "belief":
                connection.execute(
                    """
                    INSERT INTO belief_state(
                        belief_key, believer_entity_id, proposition_key,
                        belief_json, source_event_id, updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        descriptor["fact_key"],
                        descriptor["subject_entity_id"],
                        descriptor["field_name"],
                        canonical_json(value),
                        descriptor["event_id"],
                        descriptor["updated_order"],
                    ),
                )
            elif fact_type == "open_loop":
                loop = dict(value or {})
                connection.execute(
                    """
                    INSERT INTO open_loops(
                        loop_id, owner_entity_id, loop_type, loop_status,
                        due_chapter, due_scene, payload_json, source_event_id,
                        updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        loop.get("loop_id"),
                        descriptor.get("subject_entity_id"),
                        loop.get("loop_type") or "promise",
                        loop.get("status") or "open",
                        loop.get("due_chapter"),
                        loop.get("due_scene"),
                        canonical_json(loop),
                        descriptor["event_id"],
                        descriptor["updated_order"],
                    ),
                )

        for history in sorted(
            ability_history,
            key=lambda item: (
                int(item["updated_order"]),
                str(item["source_event_id"]),
            ),
        ):
            connection.execute(
                """
                INSERT INTO ability_use_history(
                    source_event_id, owner_entity_id, ability_entity_id,
                    action, runtime_json, story_coordinate_json, chapter_no,
                    scene_index, updated_order
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    history["source_event_id"],
                    history["owner_entity_id"],
                    history["ability_entity_id"],
                    history["action"],
                    canonical_json(history.get("runtime") or {}),
                    canonical_json(
                        history.get("story_coordinate") or {}
                    ),
                    history.get("chapter_no"),
                    history.get("scene_index"),
                    history["updated_order"],
                ),
            )

        for descriptor in sorted(
            timeless.values(), key=lambda item: str(item["fact_key"])
        ):
            if descriptor.get("fact_type") != "power_spec":
                continue
            value = dict(descriptor.get("value") or {})
            definition = dict(value.get("definition") or {})
            spec_type = str(value.get("spec_type") or "")
            spec_id = str(value.get("spec_entity_id") or "")
            status = str(value.get("status") or "active")
            event_id = descriptor["event_id"]
            updated_order = descriptor["updated_order"]
            if spec_type == "power_system":
                connection.execute(
                    """
                    INSERT INTO power_system_specs(
                        spec_entity_id, spec_status, definition_json,
                        source_event_id, updated_order
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        spec_id,
                        status,
                        canonical_json(definition),
                        event_id,
                        updated_order,
                    ),
                )
            elif spec_type == "progression_track":
                connection.execute(
                    """
                    INSERT INTO progression_tracks(
                        track_entity_id, system_entity_id, track_kind,
                        track_status, definition_json, source_event_id,
                        updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spec_id,
                        definition.get("system_entity_id"),
                        definition.get("track_kind") or "ordered_rank",
                        status,
                        canonical_json(definition),
                        event_id,
                        updated_order,
                    ),
                )
            elif spec_type == "rank_node":
                connection.execute(
                    """
                    INSERT INTO rank_nodes(
                        rank_entity_id, track_entity_id, rank_status,
                        definition_json, source_event_id, updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spec_id,
                        definition.get("track_entity_id"),
                        status,
                        canonical_json(definition),
                        event_id,
                        updated_order,
                    ),
                )
            elif spec_type == "rank_edge":
                from_ranks = (
                    definition.get("from_rank_entity_ids")
                    or definition.get("from_node_ids")
                    or []
                )
                connection.execute(
                    """
                    INSERT INTO rank_edges(
                        edge_entity_id, track_entity_id,
                        from_rank_ids_json, to_rank_entity_id, edge_status,
                        definition_json, source_event_id, updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spec_id,
                        definition.get("track_entity_id"),
                        canonical_json(list(from_ranks)),
                        definition.get("to_rank_entity_id")
                        or definition.get("to_node_id"),
                        status,
                        canonical_json(definition),
                        event_id,
                        updated_order,
                    ),
                )
            elif spec_type == "ability_definition":
                connection.execute(
                    """
                    INSERT INTO ability_definitions(
                        ability_entity_id, system_entity_id,
                        definition_status, definition_json, source_event_id,
                        updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spec_id,
                        definition.get("system_entity_id"),
                        status,
                        canonical_json(definition),
                        event_id,
                        updated_order,
                    ),
                )
            elif spec_type == "resource_definition":
                connection.execute(
                    """
                    INSERT INTO resource_definitions(
                        resource_entity_id, system_entity_id,
                        definition_status, definition_json, source_event_id,
                        updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spec_id,
                        definition.get("system_entity_id"),
                        status,
                        canonical_json(definition),
                        event_id,
                        updated_order,
                    ),
                )
            elif spec_type == "status_definition":
                connection.execute(
                    """
                    INSERT INTO status_definitions(
                        status_entity_id, system_entity_id,
                        definition_status, definition_json, source_event_id,
                        updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spec_id,
                        definition.get("system_entity_id"),
                        status,
                        canonical_json(definition),
                        event_id,
                        updated_order,
                    ),
                )
            elif spec_type == "qualification_definition":
                connection.execute(
                    """
                    INSERT INTO qualification_definitions(
                        qualification_entity_id, system_entity_id,
                        definition_status, definition_json, source_event_id,
                        updated_order
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spec_id,
                        definition.get("system_entity_id"),
                        status,
                        canonical_json(definition),
                        event_id,
                        updated_order,
                    ),
                )
            elif spec_type in {
                "counter_rule",
                "bridge_rule",
                "conversion_rule",
            }:
                table = {
                    "counter_rule": "counter_rules",
                    "bridge_rule": "bridge_rules",
                    "conversion_rule": "conversion_rules",
                }[spec_type]
                connection.execute(
                    f"""
                    INSERT INTO {table}(
                        rule_entity_id, rule_status, definition_json,
                        source_event_id, updated_order
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        spec_id,
                        status,
                        canonical_json(definition),
                        event_id,
                        updated_order,
                    ),
                )

    @staticmethod
    def _projection_payload(connection: sqlite3.Connection) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for table in PROJECTION_TABLES:
            columns = [
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            ]
            stable_columns = [
                column
                for column in columns
                if column not in {"created_at", "updated_at", "completed_at"}
            ]
            order = ", ".join(stable_columns)
            rows = connection.execute(
                f"SELECT {order} FROM {table} ORDER BY {order}"
            ).fetchall()
            payload[table] = [
                {column: row[column] for column in stable_columns} for row in rows
            ]
        payload["active_source_manifest"] = [
            dict(row)
            for row in connection.execute(
                """
                SELECT source_id, source_path, content_hash, source_role,
                       metadata_json
                FROM accepted_source_manifest
                WHERE manifest_status='active'
                ORDER BY source_id, source_path, content_hash
                """
            )
        ]
        return payload

    def rebuild_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        record_run: bool = True,
    ) -> dict[str, Any]:
        self._validate_accepted_ledger_integrity(connection)
        authority_inactive, branch_inactive = self._inactive_event_sets(
            connection
        )
        inactive_count = len(authority_inactive) + sum(
            len(event_ids) for event_ids in branch_inactive.values()
        )
        event_rows = self._accepted_events(connection)
        self._clear_projections(connection)
        legacy_v4_power_cutoff = self.store.get_meta_int(
            connection,
            "legacy_v4_power_compat_head_revision",
            default=0,
        )

        current: dict[str, dict[str, Any]] = {}
        timeless: dict[str, dict[str, Any]] = {}
        planned: dict[str, dict[str, Any]] = {}
        branches: dict[tuple[str, str], dict[str, Any]] = {}
        versions: list[dict[str, Any]] = []
        nonunique_inventory_balances: dict[tuple[str, str], float] = {}
        authority_power_context: dict[str, Any] = {}
        planned_power_context: dict[str, Any] = {}
        branch_power_contexts: dict[str, dict[str, Any]] = {}
        spec_states: dict[tuple[str, str], dict[str, Any]] = {}
        conversion_rules: dict[str, dict[str, Any]] = {}
        rank_edges: dict[str, dict[str, Any]] = {}
        ability_history: list[dict[str, Any]] = []

        for row in event_rows:
            is_authoritative = bool(row["changes_authority"])
            inactive = (
                authority_inactive
                if is_authoritative
                else branch_inactive.get(str(row["branch_id"]), set())
            )
            if str(row["event_id"]) in inactive:
                continue
            descriptor = _event_descriptor(row)
            if descriptor is None:
                continue
            if (
                descriptor.get("event_type") == "ability"
                and legacy_v4_power_cutoff > 0
                and int(row["head_revision_after"])
                <= legacy_v4_power_cutoff
            ):
                descriptor["legacy_v4_ability_semantics"] = True
            is_flashback = descriptor["narrative_mode"] == "flashback"
            scope = descriptor["scope"]

            if descriptor["event_type"] == "power_spec":
                if is_authoritative and scope == "timeless":
                    spec_descriptor = _apply_power_spec_event(
                        descriptor, spec_states
                    )
                    timeless[spec_descriptor["fact_key"]] = spec_descriptor
                    value = dict(spec_descriptor.get("value") or {})
                    if value.get("spec_type") == "conversion_rule":
                        rule_id = str(value.get("spec_entity_id") or "")
                        if value.get("status") == "deprecated":
                            conversion_rules.pop(rule_id, None)
                        else:
                            conversion_rules[rule_id] = dict(
                                value.get("definition") or {}
                            )
                    elif value.get("spec_type") == "rank_edge":
                        edge_id = str(value.get("spec_entity_id") or "")
                        if value.get("status") == "deprecated":
                            rank_edges.pop(edge_id, None)
                        else:
                            edge_definition = dict(
                                value.get("definition") or {}
                            )
                            rank_edges[edge_id] = {
                                **edge_definition,
                                "from_rank_entity_ids": list(
                                    edge_definition.get(
                                        "from_rank_entity_ids"
                                    )
                                    or edge_definition.get(
                                        "from_node_ids"
                                    )
                                    or []
                                ),
                                "to_rank_entity_id": (
                                    edge_definition.get(
                                        "to_rank_entity_id"
                                    )
                                    or edge_definition.get("to_node_id")
                                ),
                            }
                elif is_authoritative and scope == "planned":
                    planned[descriptor["fact_key"]] = descriptor
                else:
                    branches[
                        (descriptor["branch_id"], descriptor["fact_key"])
                    ] = descriptor
                continue

            if not is_authoritative:
                if (
                    descriptor["event_type"] in _POWER_RUNTIME_EVENT_TYPES
                    and not is_flashback
                ):
                    branch_context = branch_power_contexts.setdefault(
                        descriptor["branch_id"],
                        {
                            key: {
                                state_key: dict(state_value)
                                for state_key, state_value in states.items()
                            }
                            for key, states in authority_power_context.items()
                        },
                    )
                    projected, _ = _apply_power_runtime_event(
                        descriptor,
                        branch_context,
                        conversion_rules,
                        rank_edges,
                    )
                    for item in projected:
                        branches[
                            (item["branch_id"], item["fact_key"])
                        ] = item
                else:
                    branches[
                        (descriptor["branch_id"], descriptor["fact_key"])
                    ] = descriptor
                continue

            if (
                descriptor["event_type"] in _POWER_RUNTIME_EVENT_TYPES
                and scope == "current"
                and not is_flashback
            ):
                projected, history = _apply_power_runtime_event(
                    descriptor,
                    authority_power_context,
                    conversion_rules,
                    rank_edges,
                )
                for item in projected:
                    current[item["fact_key"]] = item
                    versions.append(item)
                if history is not None:
                    ability_history.append(history)
                continue
            if (
                descriptor["event_type"] in _POWER_RUNTIME_EVENT_TYPES
                and scope == "planned"
                and not is_flashback
            ):
                if not planned_power_context and authority_power_context:
                    planned_power_context.update(
                        {
                            key: {
                                state_key: dict(state_value)
                                for state_key, state_value in states.items()
                            }
                            for key, states in authority_power_context.items()
                        }
                    )
                projected, _ = _apply_power_runtime_event(
                    descriptor,
                    planned_power_context,
                    conversion_rules,
                    rank_edges,
                )
                for item in projected:
                    planned[item["fact_key"]] = item
                continue
            if (
                descriptor["event_type"] in _POWER_RUNTIME_EVENT_TYPES
                and (scope == "historical" or is_flashback)
            ):
                historical = _descriptor_variant(
                    descriptor,
                    fact_key=_fact_key(
                        descriptor["event_type"],
                        "historical",
                        descriptor["event_id"],
                    ),
                    fact_type=f"{descriptor['event_type']}_event",
                    field_name="event",
                    value=dict(descriptor.get("raw") or {}),
                )
                historical["scope"] = "historical"
                versions.append(historical)
                continue

            if (
                scope == "current"
                and not is_flashback
                and descriptor["fact_type"] == "inventory"
                and not bool((descriptor.get("raw") or {}).get("unique"))
            ):
                inventory_descriptors = _apply_nonunique_inventory_event(
                    descriptor,
                    nonunique_inventory_balances,
                )
                for inventory_descriptor in inventory_descriptors:
                    current[
                        inventory_descriptor["fact_key"]
                    ] = inventory_descriptor
                    versions.append(inventory_descriptor)
                continue
            if scope == "timeless":
                timeless[descriptor["fact_key"]] = descriptor
            elif scope == "planned":
                planned[descriptor["fact_key"]] = descriptor
            elif scope == "historical" or is_flashback:
                historical = dict(descriptor)
                historical["scope"] = "historical"
                versions.append(historical)
            else:
                current[descriptor["fact_key"]] = descriptor
                versions.append(descriptor)

        for descriptor in sorted(
            current.values(), key=lambda item: str(item["fact_key"])
        ):
            self._insert_generic(connection, "canon_facts", descriptor)
        for descriptor in sorted(
            timeless.values(), key=lambda item: str(item["fact_key"])
        ):
            self._insert_generic(connection, "timeless_facts", descriptor)
        for descriptor in sorted(
            planned.values(), key=lambda item: str(item["fact_key"])
        ):
            self._insert_generic(connection, "planned_facts", descriptor)
        for descriptor in sorted(
            branches.values(),
            key=lambda item: (str(item["branch_id"]), str(item["fact_key"])),
        ):
            self._insert_branch(connection, descriptor)
        self._insert_fact_versions(connection, versions)
        self._insert_typed_states(
            connection,
            current,
            timeless,
            ability_history,
        )
        item_event_rows = [
            expanded
            for row in event_rows
            if bool(row["changes_authority"])
            and str(row["branch_id"]) == "main"
            for expanded in [_expanded_event_row(row)]
            if expanded is not None
            and str(expanded.get("event_type") or "") in ITEM_EVENT_TYPES
        ]
        item_projection = rebuild_item_projection(
            connection,
            item_event_rows,
            authority_inactive,
            record_run=record_run,
        )
        advantage_event_rows = _advantage_replay_rows(event_rows)
        advantage_inactive = set(authority_inactive)
        for event_ids in branch_inactive.values():
            advantage_inactive.update(event_ids)
        advantage_projection = rebuild_advantage_projection(
            connection,
            advantage_event_rows,
            advantage_inactive,
            record_run=record_run,
        )
        source_manifest_projection = replay_source_manifest(connection)

        projection_payload = self._projection_payload(connection)
        projection_hash = stable_hash(
            projection_payload, prefix="projection_"
        )
        head = self.store.get_meta_int(connection, "head_canon_revision")
        active = self.store.get_meta_int(connection, "active_canon_revision")
        run_id = f"projection_run_{uuid.uuid4().hex}"
        now = utc_now()
        if record_run:
            connection.execute(
                """
                INSERT INTO projection_runs(
                    run_id, projection_name, source_head_revision,
                    source_active_revision, run_status, projection_hash,
                    details_json, created_at, completed_at
                ) VALUES(?, 'continuity', ?, ?, 'completed', ?, ?, ?, ?)
                """,
                (
                    run_id,
                    head,
                    active,
                    projection_hash,
                    canonical_json(
                        {
                            "event_count": len(event_rows),
                            "inactive_event_count": inactive_count,
                            "authority_inactive_event_count": len(
                                authority_inactive
                            ),
                            "branch_inactive_event_count": (
                                inactive_count - len(authority_inactive)
                            ),
                        }
                    ),
                    now,
                    now,
                ),
            )
        return {
            "projection_hash": projection_hash,
            "head_canon_revision": head,
            "active_canon_revision": active,
            "event_count": len(event_rows),
            "inactive_event_count": inactive_count,
            "authority_inactive_event_count": len(authority_inactive),
            "branch_inactive_event_count": (
                inactive_count - len(authority_inactive)
            ),
            "run_id": run_id if record_run else None,
            **item_projection,
            **advantage_projection,
            **source_manifest_projection,
        }

    def rebuild(self) -> dict[str, Any]:
        with self.store.transaction() as connection:
            return self.rebuild_in_transaction(connection)
