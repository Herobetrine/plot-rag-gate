"""Deterministic adapter from a frozen initialization proposal to v0.5 lifecycle input."""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from .canonical import (
    canonical_hash,
    canonical_json,
    path_is_within,
    stable_id,
)
from .constants import SCOPES
from .errors import PlotInitError
from .advantages import (
    ADVANTAGE_SCHEMA_VERSION,
    advantage_package_from_frozen_proposal,
)
from .items import item_package_from_frozen_proposal


EVENT_TYPES = frozenset(
    {
        "entity",
        "world_rule",
        "state",
        "relation",
        "movement",
        "inventory",
        "ability",
        "belief",
        "open_loop",
        "time",
        "power_spec",
        "progression",
        "resource",
        "status_effect",
        "power_binding",
        "qualification",
        "power_observation",
        "item_spec",
        "item_instance",
        "item_custody",
        "item_runtime",
        "item_function_runtime",
        "item_observation",
        "advantage_spec",
        "advantage_anchor",
        "advantage_module",
        "advantage_bind",
        "advantage_activate",
        "advantage_trigger",
        "advantage_use",
        "advantage_reward",
        "advantage_cost",
        "advantage_upgrade",
        "advantage_reveal",
        "advantage_contract",
        "advantage_correction",
    }
)

ITEM_EVENT_TYPES = frozenset(
    {
        "item_spec",
        "item_instance",
        "item_custody",
        "item_runtime",
        "item_function_runtime",
        "item_observation",
    }
)

ADVANTAGE_EVENT_TYPES = frozenset(
    {
        "advantage_spec",
        "advantage_anchor",
        "advantage_module",
        "advantage_bind",
        "advantage_activate",
        "advantage_trigger",
        "advantage_use",
        "advantage_reward",
        "advantage_cost",
        "advantage_upgrade",
        "advantage_reveal",
        "advantage_contract",
        "advantage_correction",
    }
)


def _scope(value: Any, default: str) -> str:
    candidate = str(value or "")
    return candidate if candidate in SCOPES else default


def _entity_id(entity_type: str, name: str) -> str:
    return stable_id("ent", entity_type, name.strip().casefold())


def _normalize_entity(value: dict[str, Any]) -> dict[str, Any] | None:
    name = str(value.get("canonical_name") or value.get("name") or "").strip()
    if not name:
        return None
    entity_type = str(value.get("entity_type") or "concept").strip() or "concept"
    aliases = value.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    return {
        "entity_id": str(value.get("entity_id") or _entity_id(entity_type, name)),
        "entity_type": entity_type,
        "canonical_name": name,
        "aliases": sorted({str(alias).strip() for alias in aliases if str(alias).strip()}),
    }


def _add_entity(
    entities: dict[str, dict[str, Any]],
    *,
    name: Any,
    entity_type: str,
    aliases: Iterable[str] = (),
) -> str | None:
    cleaned = str(name or "").strip()
    if not cleaned:
        return None
    entity_id = _entity_id(entity_type, cleaned)
    record = entities.setdefault(
        entity_id,
        {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "canonical_name": cleaned,
            "aliases": [],
        },
    )
    record["aliases"] = sorted(
        {
            *[str(alias).strip() for alias in record.get("aliases") or [] if str(alias).strip()],
            *[str(alias).strip() for alias in aliases if str(alias).strip()],
        }
    )
    return entity_id


def _put_entity(
    entities: dict[str, dict[str, Any]],
    *,
    entity_id: Any,
    entity_type: str,
    name: Any,
    aliases: Iterable[str] = (),
) -> str | None:
    cleaned_id = str(entity_id or "").strip()
    cleaned_name = str(name or "").strip()
    if not cleaned_id or not cleaned_name:
        return None
    existing = entities.get(cleaned_id)
    if existing is not None and existing.get("entity_type") != entity_type:
        raise PlotInitError(
            "POWER_ENTITY_TYPE_MISMATCH",
            "power entity id is already registered with another type",
            entity_id=cleaned_id,
            expected=entity_type,
            actual=existing.get("entity_type"),
        )
    record = entities.setdefault(
        cleaned_id,
        {
            "entity_id": cleaned_id,
            "entity_type": entity_type,
            "canonical_name": cleaned_name,
            "aliases": [],
        },
    )
    record["aliases"] = sorted(
        {
            *[str(alias).strip() for alias in record.get("aliases") or [] if str(alias).strip()],
            *[str(alias).strip() for alias in aliases if str(alias).strip()],
        }
    )
    return cleaned_id


def _event(
    proposal_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise PlotInitError(
            "INVALID_LIFECYCLE_EVENT",
            f"unsupported initialization lifecycle event: {event_type}",
        )
    normalized = copy.deepcopy(payload)
    normalized["event_type"] = event_type
    normalized["scope"] = _scope(normalized.get("scope"), "current")
    # Typed item event identity is created by the accepted continuity commit
    # from commit_id + ordinal + normalized content.  Initialization input must
    # not be able to preselect that canonical identity.
    if event_type not in ITEM_EVENT_TYPES:
        normalized["event_id"] = stable_id(
            "initevt",
            proposal_id,
            event_type,
            {
                key: value
                for key, value in normalized.items()
                if key not in {"event_id", "created_at"}
            },
        )
    return normalized


def _claim_evidence(claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "claim_id": claim.get("claim_id"),
        "source_id": claim.get("source_id"),
        "source_version_id": claim.get("source_version_id"),
        "path": claim.get("path"),
        "line_start": claim.get("line_start"),
        "line_end": claim.get("line_end"),
        "source_hash": claim.get("source_hash"),
        "exact_evidence": claim.get("exact_evidence"),
        "knowledge_plane": claim.get("knowledge_plane"),
    }


def _actor_records(actor_system: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    protagonist = actor_system.get("protagonist")
    if isinstance(protagonist, dict):
        record = copy.deepcopy(protagonist)
        record["_bundle_path"] = "/actor_system/protagonist"
        records.append(record)
    for key in ("opponents", "third_parties"):
        for index, value in enumerate(actor_system.get(key) or []):
            if isinstance(value, dict):
                record = copy.deepcopy(value)
                record["_bundle_path"] = f"/actor_system/{key}/{index}"
                records.append(record)
    return records


def _source_indexes(
    source_manifest: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_source: dict[str, dict[str, Any]] = {}
    by_version: dict[str, dict[str, Any]] = {}
    for item in source_manifest:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_id") or "")
        version_id = str(item.get("source_version_id") or "")
        if source_id:
            by_source[source_id] = item
        if version_id:
            by_version[version_id] = item
    return by_source, by_version


def _source_can_enter_canon(source: dict[str, Any] | None) -> bool:
    if source is None:
        return False
    if str(source.get("ingest_policy") or "review") != "include":
        return False
    role = str(source.get("source_role") or "note")
    tier = str(source.get("authority_tier") or "T4")
    stage = str(source.get("artifact_stage") or "")
    if role == "outline":
        return tier in {"T0", "T1", "T2"} and stage == "outline"
    if role == "setting":
        return (
            stage in {"final", "published", "normalized"}
            and tier in {"T0", "T1", "T2", "T3"}
        )
    if role == "canon":
        return stage in {"final", "published"} and tier in {"T0", "T1"}
    return False


def _source_for_claim(
    claim: dict[str, Any],
    by_source: dict[str, dict[str, Any]],
    by_version: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    source_id = str(claim.get("source_id") or "")
    version_id = str(claim.get("source_version_id") or "")
    return by_source.get(source_id) or by_version.get(version_id)


def _open_conflict_claim_ids(
    bundle: dict[str, Any],
    *,
    claims_by_id: dict[str, dict[str, Any]],
    by_source: dict[str, dict[str, Any]],
    by_version: dict[str, dict[str, Any]],
) -> set[str]:
    blocked: set[str] = set()
    for conflict in bundle.get("conflicts") or []:
        if not isinstance(conflict, dict):
            continue
        if str(conflict.get("resolution_status") or "open") in {
            "merge_evidence",
            "resolved",
            "accepted",
        }:
            continue
        eligible_by_scope: dict[str, list[str]] = {}
        for value in conflict.get("claim_ids") or []:
            claim_id = str(value or "")
            claim = claims_by_id.get(claim_id)
            if claim is None or not _claim_can_enter_canon(
                claim,
                by_source=by_source,
                by_version=by_version,
                blocked_claim_ids=set(),
            ):
                continue
            source = _source_for_claim(claim, by_source, by_version)
            eligible_by_scope.setdefault(_claim_scope(claim, source), []).append(
                claim_id
            )
        for claim_ids in eligible_by_scope.values():
            if len(claim_ids) > 1:
                blocked.update(claim_ids)
    return blocked


def _claim_can_enter_canon(
    claim: dict[str, Any],
    *,
    by_source: dict[str, dict[str, Any]],
    by_version: dict[str, dict[str, Any]],
    blocked_claim_ids: set[str],
) -> bool:
    claim_id = str(claim.get("claim_id") or "")
    if claim_id and claim_id in blocked_claim_ids:
        return False
    if str(claim.get("field_status") or "") not in {
        "source_supported",
        "user_confirmed",
    }:
        return False
    if str(claim.get("modality") or "asserted") != "asserted":
        return False
    source = _source_for_claim(claim, by_source, by_version)
    return _source_can_enter_canon(source)


def _claim_scope(
    claim: dict[str, Any],
    source: dict[str, Any] | None,
) -> str:
    if str((source or {}).get("source_role") or "") == "outline":
        return "planned"
    if str(claim.get("knowledge_plane") or "") == "author_plan":
        return "planned"
    return _scope(claim.get("scope"), "current")


def _field_state_can_enter_canon(
    bundle: dict[str, Any],
    path: str,
    *,
    claims_by_id: dict[str, dict[str, Any]],
    by_source: dict[str, dict[str, Any]],
    by_version: dict[str, dict[str, Any]],
    blocked_claim_ids: set[str],
) -> bool:
    all_states = bundle.get("field_states")
    if not isinstance(all_states, dict) or not all_states:
        # v0.5 accepted hand-authored InitializationBundle fixtures did not
        # carry field envelopes. Keep that explicit host-approved format
        # readable; all bundles emitted by plot-rag-init/v1 include states and
        # therefore use the strict gate below.
        return True
    state = all_states.get(path)
    if not isinstance(state, dict):
        return False
    status = str(state.get("field_status") or "")
    origin = str(state.get("origin") or "")
    decision = str(state.get("decision_status") or "open")
    if origin == "user_input":
        return status == "user_confirmed"
    if origin == "model_suggestion":
        # Model-produced values remain proposal-only here.  They can be
        # included in the lifecycle proposal, but only the later host grant
        # can promote the proposal events into accepted canon.
        return status in {"model_proposed", "user_confirmed"} and decision in {
            "open",
            "session_locked",
            "delegated",
        }
    if origin != "source_extract":
        return False
    if status not in {"source_supported", "user_confirmed"}:
        return False
    refs = [str(value) for value in state.get("source_refs") or [] if str(value)]
    if not refs:
        return False
    resolved_sources: list[dict[str, Any]] = []
    for ref in refs:
        claim = claims_by_id.get(ref)
        if claim is not None:
            if not _claim_can_enter_canon(
                claim,
                by_source=by_source,
                by_version=by_version,
                blocked_claim_ids=blocked_claim_ids,
            ):
                return False
            source = _source_for_claim(claim, by_source, by_version)
            if source is not None:
                resolved_sources.append(source)
            continue
        source = by_source.get(ref) or by_version.get(ref)
        if source is not None:
            resolved_sources.append(source)
    return bool(resolved_sources) and all(
        _source_can_enter_canon(source) for source in resolved_sources
    )


def _normalize_accepted_source_manifest(
    source_manifest: list[dict[str, Any]],
    *,
    target_project_real_path: Any,
) -> list[dict[str, Any]]:
    """Bind inventory display paths to unambiguous accepted-source paths."""

    target_root = (
        Path(str(target_project_real_path)).expanduser().resolve(strict=False)
        if str(target_project_real_path or "").strip()
        else None
    )
    normalized: list[dict[str, Any]] = []
    for raw in source_manifest:
        if not isinstance(raw, dict):
            normalized.append(copy.deepcopy(raw))
            continue
        item = copy.deepcopy(raw)
        display_path = str(item.get("path") or item.get("source_path") or "")
        declared_real_path = item.get("real_path") or item.get(
            "normalized_real_path"
        )
        if declared_real_path:
            source_path = Path(str(declared_real_path)).expanduser()
            if not source_path.is_absolute():
                raise PlotInitError(
                    "INVALID_SOURCE_REAL_PATH",
                    "inventory real_path must be absolute before lifecycle acceptance",
                    path=display_path,
                    real_path=str(declared_real_path),
                )
            resolved = source_path.resolve(strict=False)
            item["inventory_path"] = display_path
            if target_root is not None and path_is_within(resolved, target_root):
                try:
                    relative = resolved.relative_to(target_root)
                except ValueError as exc:
                    raise PlotInitError(
                        "SOURCE_PATH_NORMALIZATION_FAILED",
                        "project source could not be made project-relative",
                        path=display_path,
                        real_path=str(resolved),
                    ) from exc
                item["path"] = relative.as_posix()
                item["external_source"] = False
                item["accepted_path_kind"] = "project_relative"
            else:
                item["path"] = resolved.as_posix()
                item["external_source"] = True
                item["accepted_path_kind"] = "external_absolute"
        elif re.match(r"^source-\d+(?:/|$)", display_path.replace("\\", "/"), re.I):
            raise PlotInitError(
                "SOURCE_REAL_PATH_REQUIRED",
                "inventory display paths require a bound absolute real_path",
                path=display_path,
            )
        normalized.append(item)
    return normalized


def _item_bootstrap_coordinate(
    package: Mapping[str, Any],
    record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    record = record or {}
    candidate = (
        record.get("story_coordinate")
        or record.get("effective_from_coordinate")
    )
    if (
        isinstance(candidate, Mapping)
        and str(candidate.get("calendar_id") or "").strip()
        and type(candidate.get("ordinal")) is int
    ):
        return copy.deepcopy(dict(candidate))
    return {
        "calendar_id": stable_id(
            "calendar",
            package.get("work_id"),
            "initialization-bootstrap",
        ),
        "ordinal": 0,
        "label": "initialization bootstrap",
        "precision": "bootstrap",
    }


def _item_evidence(
    package: Mapping[str, Any],
    record: Mapping[str, Any],
    claims_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    claim_ids = sorted(
        {
            str(value).strip()
            for value in record.get("source_claim_ids") or []
            if str(value).strip()
        }
    )
    quote = ""
    source_claim_id: str | None = None
    for claim_id in claim_ids:
        claim = claims_by_id.get(claim_id)
        if not isinstance(claim, Mapping):
            continue
        candidate = str(claim.get("exact_evidence") or "").strip()
        if candidate:
            quote = candidate
            source_claim_id = claim_id
            break
    if not quote:
        quote = canonical_json(record)
    if len(quote) > 8192:
        quote = quote[:8192]
    return {
        "kind": "initialization_item_sidecar",
        "quote": quote,
        "source_claim_id": source_claim_id,
        "source_claim_ids": claim_ids,
        "item_package_hash": str(package.get("package_hash") or ""),
    }


def _item_event_base(
    package: Mapping[str, Any],
    record: Mapping[str, Any],
    claims_by_id: Mapping[str, Mapping[str, Any]],
    *,
    scope: str,
) -> dict[str, Any]:
    plane = str(record.get("knowledge_plane") or "objective").strip()
    if plane not in {
        "objective",
        "actor_belief",
        "public_narrative",
        "reader_disclosed",
        "author_plan",
    }:
        plane = "objective"
    return {
        "schema_version": "plot-rag-delta/v4",
        "scope": scope,
        "story_coordinate": _item_bootstrap_coordinate(package, record),
        "knowledge_plane": plane,
        "confidence": record.get("confidence", 1.0),
        "evidence": _item_evidence(package, record, claims_by_id),
    }


def _advantage_bootstrap_coordinate(
    package: Mapping[str, Any],
    record: Mapping[str, Any] | None = None,
    *,
    ordinal: int = 0,
) -> dict[str, Any]:
    record = record or {}
    candidate = (
        record.get("story_coordinate")
        or record.get("effective_from_coordinate")
    )
    if (
        isinstance(candidate, Mapping)
        and str(candidate.get("calendar_id") or "").strip()
        and type(candidate.get("ordinal")) is int
    ):
        return copy.deepcopy(dict(candidate))
    return {
        "calendar_id": stable_id(
            "calendar",
            package.get("work_id"),
            "advantage-initialization",
        ),
        "ordinal": int(ordinal),
        "label": "advantage initialization",
        "precision": "bootstrap",
    }


def _advantage_evidence(
    package: Mapping[str, Any],
    record: Mapping[str, Any],
    claims_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    claim_ids = sorted(
        {
            str(value).strip()
            for value in record.get("source_claim_ids") or []
            if str(value).strip()
        }
    )
    quote = ""
    source_claim_id: str | None = None
    for claim_id in claim_ids:
        claim = claims_by_id.get(claim_id)
        if not isinstance(claim, Mapping):
            continue
        candidate = str(claim.get("exact_evidence") or "").strip()
        if candidate:
            quote = candidate
            source_claim_id = claim_id
            break
    if not quote:
        quote = canonical_json(record)
    if len(quote) > 8192:
        quote = quote[:8192]
    return {
        "kind": "initialization_advantage_sidecar",
        "quote": quote,
        "source_claim_id": source_claim_id,
        "source_claim_ids": claim_ids,
        "advantage_package_hash": str(package.get("package_hash") or ""),
    }


def _advantage_event_base(
    package: Mapping[str, Any],
    record: Mapping[str, Any],
    claims_by_id: Mapping[str, Mapping[str, Any]],
    *,
    scope: str,
    ordinal: int,
    branch_id: str = "main",
) -> dict[str, Any]:
    plane = str(record.get("knowledge_plane") or "objective").strip()
    if plane not in {
        "objective",
        "actor_belief",
        "public_narrative",
        "reader_disclosed",
        "author_plan",
    }:
        plane = "objective"
    return {
        "schema_version": ADVANTAGE_SCHEMA_VERSION,
        "scope": scope,
        "branch_id": str(record.get("branch_id") or branch_id),
        "story_coordinate": _advantage_bootstrap_coordinate(
            package,
            record,
            ordinal=ordinal,
        ),
        "knowledge_plane": plane,
        "confidence": record.get("confidence", 1.0),
        "source_claim_ids": [
            str(value)
            for value in record.get("source_claim_ids") or []
            if str(value)
        ],
        "evidence": _advantage_evidence(package, record, claims_by_id),
    }


def _advantage_entity_type(anchor_type: str) -> str:
    return {
        "item_instance": "item",
        "item_stack": "item",
        "body_or_vessel": "body_or_vessel",
        "actor": "character",
        "virtual_system": "virtual_system",
        "knowledge_set": "knowledge_set",
        "temporal_rule": "temporal_rule",
        "contract": "contract",
        "location": "location",
        "power_source": "power_source",
        "social_graph": "social_graph",
    }.get(str(anchor_type), "concept")


def _advantage_numeric_resource_events(
    record: Mapping[str, Any],
) -> list[dict[str, Any]]:
    resources = record.get("resources")
    if not isinstance(resources, Mapping):
        return []
    return [
        {"resource": str(key), "amount": value}
        for key, value in sorted(resources.items(), key=lambda item: str(item[0]))
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]


def _append_advantage_bootstrap_events(
    events: list[dict[str, Any]],
    entities: dict[str, dict[str, Any]],
    *,
    proposal_id: str,
    package: Mapping[str, Any],
    claims_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Materialize the frozen Advantage package as replayable typed events.

    The JSON sidecar is retained as an artifact/reference only.  Every
    authoritative field is represented by one of the Advantage event families
    below, so replay never reads the sidecar a second time.
    """

    definitions = [
        item for item in package.get("definitions") or []
        if isinstance(item, Mapping)
    ]
    if not definitions:
        return None
    advantage_id = str(definitions[0].get("advantage_id") or "")
    if not advantage_id:
        raise PlotInitError(
            "ADVANTAGE_ID_REQUIRED",
            "Advantage initialization requires a stable advantage_id",
        )
    runtime_by_advantage: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for raw in package.get("runtime_bootstrap") or []:
        if isinstance(raw, Mapping) and raw.get("advantage_id"):
            runtime_by_advantage[str(raw["advantage_id"])].append(raw)

    ordinal = 0
    for raw in sorted(
        definitions,
        key=lambda item: str(item.get("advantage_id") or ""),
    ):
        aid = str(raw.get("advantage_id") or "")
        runtime = next(
            iter(
                sorted(
                    runtime_by_advantage.get(aid, []),
                    key=lambda item: (
                        str(item.get("branch_id") or "main"),
                        str(item.get("runtime_id") or ""),
                    ),
                )
            ),
            None,
        )
        definition = copy.deepcopy(dict(raw))
        if runtime is not None:
            # _ensure_runtime derives the initial branch-independent values
            # from the immutable definition event; branch-specific activation
            # below then applies owner/enabled/stage and the exact snapshot.
            for source_key, target_key in (
                ("stage", "initial_stage"),
                ("charges", "initial_charges"),
                ("max_charges", "initial_max_charges"),
                ("resources", "initial_resources"),
                ("pollution", "initial_pollution"),
                ("exposure", "initial_exposure"),
                ("debt", "initial_debt"),
                ("runtime_metadata", "initial_runtime_metadata"),
                ("cooldown_until", "initial_cooldown_until"),
            ):
                if runtime.get(source_key) is not None:
                    definition[target_key] = copy.deepcopy(runtime[source_key])
        events.append(
            _event(
                proposal_id,
                "advantage_spec",
                {
                    **_advantage_event_base(
                        package,
                        raw,
                        claims_by_id,
                        scope="timeless",
                        ordinal=ordinal,
                    ),
                    "advantage_id": aid,
                    "action": "define",
                    "spec_type": "advantage_definition",
                    "spec_id": aid,
                    "title": raw.get("title") or raw.get("name") or aid,
                    "profiles": copy.deepcopy(raw.get("profiles") or []),
                    "anchor_type": raw.get("anchor_type") or "item_instance",
                    "acquisition_mode": raw.get("acquisition_mode") or "unknown",
                    "uniqueness": raw.get("uniqueness") or "unknown",
                    "status": raw.get("status") or "canon",
                    "promise": copy.deepcopy(raw.get("promise") or {}),
                    "counterplay": copy.deepcopy(raw.get("counterplay") or []),
                    "definition": definition,
                },
            )
        )
        ordinal += 1

    anchor_records = [
        item for item in package.get("anchors") or []
        if isinstance(item, Mapping)
    ]
    for raw in sorted(anchor_records, key=lambda item: str(item.get("anchor_id") or "")):
        aid = str(raw.get("advantage_id") or advantage_id)
        anchor_id = str(raw.get("anchor_id") or "")
        anchor_type = str(raw.get("anchor_type") or "item_instance")
        anchor_ref_id = str(raw.get("anchor_ref_id") or "")
        if not anchor_id or not anchor_ref_id:
            raise PlotInitError(
                "ADVANTAGE_ANCHOR_REFERENCE_REQUIRED",
                "Advantage initialization anchors require stable ids",
                advantage_id=aid,
                anchor_id=anchor_id,
            )
        _put_entity(
            entities,
            entity_id=anchor_ref_id,
            entity_type=_advantage_entity_type(anchor_type),
            name=str(raw.get("anchor_name") or anchor_ref_id),
        )
        events.append(
            _event(
                proposal_id,
                "advantage_anchor",
                {
                    **_advantage_event_base(
                        package,
                        raw,
                        claims_by_id,
                        scope="timeless",
                        ordinal=ordinal,
                    ),
                    "advantage_id": aid,
                    "action": "define",
                    "anchor_id": anchor_id,
                    "anchor_type": anchor_type,
                    "anchor_ref_id": anchor_ref_id,
                    "anchor_name": raw.get("anchor_name"),
                    "owner_entity_id": raw.get("owner_entity_id"),
                    "binding_state": "unbound",
                    "transfer_rule": copy.deepcopy(raw.get("transfer_rule") or {}),
                    "anchor_status": raw.get("anchor_status") or "active",
                    "status": raw.get("status") or "canon",
                    "attributes": {
                        "origin": raw.get("origin"),
                        "source_anchor": copy.deepcopy(dict(raw)),
                    },
                },
            )
        )
        ordinal += 1

    module_records = [
        item for item in package.get("modules") or []
        if isinstance(item, Mapping)
    ]
    module_by_id = {
        str(item.get("module_id")): item for item in module_records
        if item.get("module_id")
    }
    for raw in sorted(module_records, key=lambda item: str(item.get("module_id") or "")):
        aid = str(raw.get("advantage_id") or advantage_id)
        module_id = str(raw.get("module_id") or "")
        status = str(raw.get("status") or "canon")
        scope = "planned" if status != "canon" else "timeless"
        events.append(
            _event(
                proposal_id,
                "advantage_module",
                {
                    **_advantage_event_base(
                        package,
                        raw,
                        claims_by_id,
                        scope=scope,
                        ordinal=ordinal,
                    ),
                    "advantage_id": aid,
                    "action": "define",
                    "module_id": module_id,
                    "title": raw.get("name") or raw.get("title") or module_id,
                    "name": raw.get("name"),
                    "kind": raw.get("kind") or raw.get("module_kind") or "ability",
                    "module_kind": raw.get("module_kind") or raw.get("kind") or "ability",
                    "status": status,
                    "module_status": raw.get("module_status")
                    or ("available" if status == "canon" else "locked"),
                    "stage": raw.get("stage") or "initial",
                    "trigger": copy.deepcopy(raw.get("trigger") or {}),
                    "preconditions": copy.deepcopy(raw.get("preconditions") or []),
                    "targets": copy.deepcopy(raw.get("targets") or []),
                    "costs": copy.deepcopy(raw.get("costs") or []),
                    "effects": copy.deepcopy(raw.get("effects") or []),
                    "side_effects": copy.deepcopy(raw.get("side_effects") or []),
                    "failure_modes": copy.deepcopy(raw.get("failure_modes") or []),
                    "counters": copy.deepcopy(raw.get("counters") or []),
                    "definition": copy.deepcopy(dict(raw)),
                },
            )
        )
        ordinal += 1

    for raw in sorted(
        [item for item in package.get("runtime_slots") or [] if isinstance(item, Mapping)],
        key=lambda item: str(item.get("slot_id") or ""),
    ):
        aid = str(raw.get("advantage_id") or advantage_id)
        slot_id = str(raw.get("slot_id") or "")
        status = str(raw.get("status") or "canon")
        events.append(
            _event(
                proposal_id,
                "advantage_spec",
                {
                    **_advantage_event_base(
                        package,
                        raw,
                        claims_by_id,
                        scope="timeless" if status == "canon" else "planned",
                        ordinal=ordinal,
                    ),
                    "advantage_id": aid,
                    "action": "define",
                    "spec_type": "runtime_slot",
                    "spec_id": slot_id,
                    "slot_id": slot_id,
                    "name": raw.get("name") or slot_id,
                    "slot_kind": raw.get("slot_kind"),
                    "stage": raw.get("stage") or "initial",
                    "capacity": raw.get("capacity"),
                    "unlock_graph": copy.deepcopy(raw.get("unlock_graph") or []),
                    "set_membership": copy.deepcopy(raw.get("set_membership") or []),
                    "slot_status": raw.get("slot_status")
                    or ("available" if status == "canon" else "locked"),
                    "status": status,
                    "definition": copy.deepcopy(dict(raw)),
                },
            )
        )
        ordinal += 1

    for raw in sorted(
        [item for item in package.get("narrative_contracts") or [] if isinstance(item, Mapping)],
        key=lambda item: str(item.get("narrative_contract_id") or ""),
    ):
        aid = str(raw.get("advantage_id") or advantage_id)
        contract_id = str(raw.get("narrative_contract_id") or "")
        status = str(raw.get("status") or "canon")
        events.append(
            _event(
                proposal_id,
                "advantage_spec",
                {
                    **_advantage_event_base(
                        package,
                        raw,
                        claims_by_id,
                        scope="timeless" if status == "canon" else "planned",
                        ordinal=ordinal,
                    ),
                    "advantage_id": aid,
                    "action": "define",
                    "spec_type": "narrative_contract",
                    "spec_id": contract_id,
                    "narrative_contract_id": contract_id,
                    "contract_status": raw.get("contract_status")
                    or ("active" if status == "canon" else "planned"),
                    "status": status,
                    "reading_promise": copy.deepcopy(raw.get("reading_promise") or {}),
                    "reward_loop": copy.deepcopy(raw.get("reward_loop") or []),
                    "risk_loop": copy.deepcopy(raw.get("risk_loop") or []),
                    "reveal_ladder": copy.deepcopy(raw.get("reveal_ladder") or []),
                    "experience_binding": copy.deepcopy(raw.get("experience_binding") or {}),
                    "definition": copy.deepcopy(dict(raw)),
                },
            )
        )
        ordinal += 1

    # Binding and activation are separate causal events.  This prevents a
    # bound anchor from silently granting runtime authority before the owner
    # event has been replayed.
    for raw in sorted(anchor_records, key=lambda item: str(item.get("anchor_id") or "")):
        if str(raw.get("status") or "canon") != "canon":
            continue
        if str(raw.get("binding_state") or "") != "bound":
            continue
        events.append(
            _event(
                proposal_id,
                "advantage_bind",
                {
                    **_advantage_event_base(
                        package,
                        raw,
                        claims_by_id,
                        scope="current",
                        ordinal=ordinal,
                    ),
                    "advantage_id": str(raw.get("advantage_id") or advantage_id),
                    "action": "bind",
                    "anchor_id": str(raw.get("anchor_id") or ""),
                    "owner_entity_id": raw.get("owner_entity_id"),
                },
            )
        )
        ordinal += 1

    for raw in sorted(
        [item for item in package.get("runtime_bootstrap") or [] if isinstance(item, Mapping)],
        key=lambda item: (
            str(item.get("branch_id") or "main"),
            str(item.get("runtime_id") or ""),
        ),
    ):
        aid = str(raw.get("advantage_id") or advantage_id)
        enabled = bool(raw.get("enabled"))
        events.append(
            _event(
                proposal_id,
                "advantage_activate",
                {
                    **_advantage_event_base(
                        package,
                        raw,
                        claims_by_id,
                        scope="current",
                        ordinal=ordinal,
                        branch_id=str(raw.get("branch_id") or "main"),
                    ),
                    "advantage_id": aid,
                    "action": "activate" if enabled else "deactivate",
                    "owner_entity_id": raw.get("owner_entity_id")
                    or next(
                        (
                            anchor.get("owner_entity_id")
                            for anchor in anchor_records
                            if str(anchor.get("advantage_id") or "") == aid
                            and anchor.get("owner_entity_id")
                        ),
                        None,
                    ),
                    "stage": raw.get("stage") or "initial",
                    "charges": raw.get("charges"),
                    "max_charges": raw.get("max_charges"),
                    "resources": copy.deepcopy(raw.get("resources") or {}),
                    "pollution": raw.get("pollution", 0),
                    "exposure": raw.get("exposure", 0),
                    "debt": raw.get("debt", 0),
                    "cooldown_until": copy.deepcopy(raw.get("cooldown_until")),
                    "runtime_metadata": copy.deepcopy(
                        raw.get("runtime_metadata") or {}
                    ),
                },
            )
        )
        ordinal += 1
        unlocked = [
            str(value)
            for value in raw.get("unlocked_modules") or []
            if str(value)
        ]
        for module_id in unlocked:
            module = module_by_id.get(module_id)
            if module is None or str(module.get("status") or "canon") != "canon":
                continue
            target_action = (
                "enable"
                if str(module.get("module_status") or "") == "enabled"
                else "unlock"
            )
            events.append(
                _event(
                    proposal_id,
                    "advantage_module",
                    {
                        **_advantage_event_base(
                            package,
                            module,
                            claims_by_id,
                            scope="current",
                            ordinal=ordinal,
                            branch_id=str(raw.get("branch_id") or "main"),
                        ),
                        "advantage_id": aid,
                        "action": target_action,
                        "module_id": module_id,
                    },
                )
            )
            ordinal += 1

    # Historical sidecar ledger rows are represented once as record-only
    # reward/cost events.  They preserve input/output/loss/provenance without
    # applying their amounts a second time to the runtime snapshot.
    for raw in sorted(
        [item for item in package.get("ledger_bootstrap") or [] if isinstance(item, Mapping)],
        key=lambda item: str(item.get("entry_id") or ""),
    ):
        aid = str(raw.get("advantage_id") or advantage_id)
        kind = str(raw.get("entry_kind") or "bootstrap")
        event_type = (
            "advantage_cost"
            if kind in {"cost", "consume", "debit"}
            else "advantage_reward"
        )
        events.append(
            _event(
                proposal_id,
                event_type,
                {
                    **_advantage_event_base(
                        package,
                        raw,
                        claims_by_id,
                        scope="current",
                        ordinal=ordinal,
                        branch_id=str(raw.get("branch_id") or "main"),
                    ),
                    "advantage_id": aid,
                    "record_only": True,
                    "ledger_entry_kind": kind,
                    "entry_id": raw.get("entry_id"),
                    "input": copy.deepcopy(raw.get("input") or {}),
                    "output": copy.deepcopy(raw.get("output") or {}),
                    "loss": copy.deepcopy(raw.get("loss") or {}),
                    "causal_provenance": copy.deepcopy(
                        raw.get("provenance") or {}
                    ),
                },
            )
        )
        ordinal += 1

    for raw in sorted(
        [item for item in package.get("knowledge") or [] if isinstance(item, Mapping)],
        key=lambda item: str(item.get("knowledge_id") or ""),
    ):
        aid = str(raw.get("advantage_id") or advantage_id)
        status = str(raw.get("status") or "canon")
        events.append(
            _event(
                proposal_id,
                "advantage_reveal",
                {
                    **_advantage_event_base(
                        package,
                        raw,
                        claims_by_id,
                        scope="current" if status == "canon" else "planned",
                        ordinal=ordinal,
                    ),
                    "advantage_id": aid,
                    "knowledge_id": raw.get("knowledge_id"),
                    "module_id": raw.get("module_id"),
                    "observer_entity_id": raw.get("observer_entity_id"),
                    "knowledge_plane": raw.get("knowledge_plane") or "objective",
                    "status": status,
                    "confidence": raw.get("confidence", 1.0),
                    "claim": copy.deepcopy(raw.get("claim") or {}),
                    "reveal_stage": raw.get("reveal_stage") or "current",
                    "misread_of": raw.get("misread_of"),
                    "record_ledger": False,
                },
            )
        )
        ordinal += 1

    for raw in sorted(
        [item for item in package.get("contracts") or [] if isinstance(item, Mapping)],
        key=lambda item: str(item.get("contract_id") or ""),
    ):
        aid = str(raw.get("advantage_id") or advantage_id)
        status = str(raw.get("status") or "canon")
        parties = [
            str(value) for value in raw.get("parties") or [] if str(value)
        ]
        agency = copy.deepcopy(raw.get("agency") or {})
        if raw.get("trust") not in (None, {}, []):
            agency["trust_detail"] = copy.deepcopy(raw.get("trust"))
        if raw.get("debt") not in (None, {}, []):
            agency["debt_detail"] = copy.deepcopy(raw.get("debt"))
        events.append(
            _event(
                proposal_id,
                "advantage_contract",
                {
                    **_advantage_event_base(
                        package,
                        raw,
                        claims_by_id,
                        scope="current" if status == "canon" else "planned",
                        ordinal=ordinal,
                    ),
                    "advantage_id": aid,
                    "action": "define",
                    "contract_id": raw.get("contract_id"),
                    "contract_kind": raw.get("contract_kind"),
                    "actor_entity_id": parties[0] if parties else None,
                    "counterparty_entity_id": parties[1] if len(parties) > 1 else None,
                    "contract_status": (
                        "active" if status == "canon" else "proposed"
                    ),
                    "status": status,
                    "terms": copy.deepcopy(raw.get("terms") or []),
                    "agency": agency,
                    "trust_delta": 0,
                    "debt_delta": 0,
                    "breach_effect": copy.deepcopy(raw.get("breach_effect") or {}),
                    "parties": parties,
                    "origin": raw.get("origin"),
                },
            )
        )
        ordinal += 1

    return {
        "advantage_id": advantage_id,
        "package_hash": str(package.get("package_hash") or ""),
        "event_count": len(events),
    }


def _register_item_reference_entities(
    entities: dict[str, dict[str, Any]],
    record: Mapping[str, Any],
) -> None:
    references = (
        ("legal_owner_entity_id", "legal_owner", "character"),
        ("custodian_entity_id", "custodian", "character"),
        ("carrier_entity_id", "carrier", "character"),
        ("access_controller_entity_id", "access_controller", "character"),
        ("equipped_by_entity_id", "equipped_by", "character"),
        ("bound_actor_entity_id", "bound_actor", "character"),
        ("observer_entity_id", "observer", "character"),
        ("location_entity_id", "location", "location"),
    )
    for id_key, name_key, entity_type in references:
        entity_id = str(record.get(id_key) or "").strip()
        if not entity_id:
            continue
        name = str(record.get(name_key) or entity_id).strip()
        _put_entity(
            entities,
            entity_id=entity_id,
            entity_type=entity_type,
            name=name,
        )


def _append_item_bootstrap_events(
    events: list[dict[str, Any]],
    entities: dict[str, dict[str, Any]],
    *,
    proposal_id: str,
    package: Mapping[str, Any],
    claims_by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    item_event_start = len(events)

    for record in package.get("item_definitions") or []:
        if not isinstance(record, Mapping):
            continue
        entity_id = str(record.get("item_entity_id") or "").strip()
        if entity_id:
            _put_entity(
                entities,
                entity_id=entity_id,
                entity_type="item",
                name=str(record.get("name") or entity_id),
            )
        events.append(
            _event(
                proposal_id,
                "item_spec",
                {
                    **_item_event_base(
                        package,
                        record,
                        claims_by_id,
                        scope="timeless",
                    ),
                    "action": "define",
                    "spec_type": "item_definition",
                    "spec_id": str(record["item_definition_id"]),
                    "item_definition_id": str(record["item_definition_id"]),
                    "definition": copy.deepcopy(dict(record)),
                },
            )
        )

    for record in package.get("item_functions") or []:
        if not isinstance(record, Mapping):
            continue
        events.append(
            _event(
                proposal_id,
                "item_spec",
                {
                    **_item_event_base(
                        package,
                        record,
                        claims_by_id,
                        scope="timeless",
                    ),
                    "action": "define",
                    "spec_type": "function_definition",
                    "spec_id": str(record["function_id"]),
                    "function_id": str(record["function_id"]),
                    "item_definition_id": str(
                        record["item_definition_id"]
                    ),
                    "definition": copy.deepcopy(dict(record)),
                },
            )
        )

    for record in package.get("item_instances") or []:
        if not isinstance(record, Mapping):
            continue
        entity_id = str(record.get("item_entity_id") or "").strip()
        if entity_id:
            _put_entity(
                entities,
                entity_id=entity_id,
                entity_type="item",
                name=str(record.get("instance_name") or entity_id),
            )
        instance_id = str(record["item_instance_id"])
        attributes = copy.deepcopy(
            record.get("attributes")
            if isinstance(record.get("attributes"), Mapping)
            else record.get("legacy_attributes")
            if isinstance(record.get("legacy_attributes"), Mapping)
            else {}
        )
        events.append(
            _event(
                proposal_id,
                "item_instance",
                {
                    **_item_event_base(
                        package,
                        record,
                        claims_by_id,
                        scope="current",
                    ),
                    "action": "instantiate",
                    "subject_type": "item_instance",
                    "subject_id": instance_id,
                    "item_instance_id": instance_id,
                    "item_definition_id": str(
                        record["item_definition_id"]
                    ),
                    "quantity": 1,
                    "item_entity_id": record.get("item_entity_id"),
                    "instance_name": record.get("instance_name"),
                    "serial_or_mark": record.get("serial_or_mark"),
                    "unique": record.get("unique"),
                    "provenance": copy.deepcopy(
                        record.get("provenance") or {}
                    ),
                    "attributes": attributes,
                },
            )
        )

    for record in package.get("item_stacks") or []:
        if not isinstance(record, Mapping):
            continue
        stack_id = str(record["stack_id"])
        quantity = record.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
            continue
        if quantity <= 0:
            continue
        batch = copy.deepcopy(record.get("batch_properties") or {})
        batch.setdefault(
            "initialization_record",
            {
                key: copy.deepcopy(value)
                for key, value in record.items()
                if key not in {"batch_properties", "quantity"}
            },
        )
        events.append(
            _event(
                proposal_id,
                "item_instance",
                {
                    **_item_event_base(
                        package,
                        record,
                        claims_by_id,
                        scope="current",
                    ),
                    "action": "instantiate",
                    "subject_type": "item_stack",
                    "subject_id": stack_id,
                    "stack_id": stack_id,
                    "item_definition_id": str(
                        record["item_definition_id"]
                    ),
                    "quantity": quantity,
                    "batch": batch,
                },
            )
        )

    for record in package.get("item_function_bindings") or []:
        if not isinstance(record, Mapping):
            continue
        target_type = str(record.get("target_type") or "")
        target_id = str(record.get("target_id") or "")
        target_fields = {
            "item_definition": {"item_definition_id": target_id},
            "item_instance": {"item_instance_id": target_id},
            "item_stack": {"stack_id": target_id},
        }.get(target_type)
        if target_fields is None:
            continue
        definition = {
            **copy.deepcopy(dict(record)),
            **target_fields,
        }
        events.append(
            _event(
                proposal_id,
                "item_spec",
                {
                    **_item_event_base(
                        package,
                        record,
                        claims_by_id,
                        scope="timeless",
                    ),
                    "action": "define",
                    "spec_type": "function_binding",
                    "spec_id": str(record["binding_id"]),
                    "binding_id": str(record["binding_id"]),
                    "function_id": str(record["function_id"]),
                    **target_fields,
                    "definition": definition,
                },
            )
        )

    for record in package.get("item_custody_bootstrap") or []:
        if not isinstance(record, Mapping):
            continue
        _register_item_reference_entities(entities, record)
        subject_type = str(record.get("subject_type") or "")
        subject_id = str(record.get("subject_id") or "")
        if subject_type not in {"item_instance", "item_stack"} or not subject_id:
            continue
        destination = {
            "to_legal_owner_entity_id": record.get(
                "legal_owner_entity_id"
            ),
            "to_custodian_entity_id": record.get("custodian_entity_id"),
            "to_carrier_entity_id": record.get("carrier_entity_id"),
            "to_location_entity_id": record.get("location_entity_id"),
            "to_container_instance_id": record.get(
                "container_instance_id"
            ),
            "to_access_controller_entity_id": record.get(
                "access_controller_entity_id"
            ),
        }
        destination = {
            key: value
            for key, value in destination.items()
            if value not in (None, "")
        }
        custody_status = str(
            record.get("custody_status") or "unknown"
        ).strip()
        action_by_status = {
            "possessed": "acquire",
            "stored": "store",
            "loaned": "loan",
            "seized": "seize",
            "lost": "lose",
            "abandoned": "abandon",
            "in_transit": "handover",
            "destroyed": "lose",
            "unknown": "acquire",
        }
        action = action_by_status.get(custody_status)
        if action is None:
            raise PlotInitError(
                "ITEM_CUSTODY_STATUS_INVALID",
                "item custody bootstrap has an unsupported status",
                custody_key=record.get("custody_key"),
                custody_status=custody_status,
            )
        typed_id = (
            {"item_instance_id": subject_id}
            if subject_type == "item_instance"
            else {"stack_id": subject_id}
        )
        events.append(
            _event(
                proposal_id,
                "item_custody",
                {
                    **_item_event_base(
                        package,
                        record,
                        claims_by_id,
                        scope="current",
                    ),
                    "action": action,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    **typed_id,
                    "quantity": (
                        1
                        if subject_type == "item_instance"
                        else record.get("quantity")
                    ),
                    "custody_status": custody_status,
                    **destination,
                },
            )
        )

    for record in package.get("item_runtime_bootstrap") or []:
        if not isinstance(record, Mapping):
            continue
        _register_item_reference_entities(entities, record)
        instance_id = str(record.get("item_instance_id") or "")
        if not instance_id:
            continue
        runtime_state = (
            copy.deepcopy(record.get("state"))
            if isinstance(record.get("state"), Mapping)
            else copy.deepcopy(record.get("state_json"))
            if isinstance(record.get("state_json"), Mapping)
            else {}
        )
        runtime_payload = {
            field: copy.deepcopy(record.get(field))
            for field in (
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
            )
            if record.get(field) is not None
        }
        events.append(
            _event(
                proposal_id,
                "item_runtime",
                {
                    **_item_event_base(
                        package,
                        record,
                        claims_by_id,
                        scope="current",
                    ),
                    "action": "bootstrap",
                    "subject_type": "item_instance",
                    "subject_id": instance_id,
                    "item_instance_id": instance_id,
                    **runtime_payload,
                    "state": runtime_state,
                },
            )
        )

    for record in package.get("item_function_runtime_bootstrap") or []:
        if not isinstance(record, Mapping):
            continue
        subject_type = str(record.get("subject_type") or "").strip()
        instance_id = str(record.get("item_instance_id") or "").strip()
        stack_id = str(record.get("stack_id") or "").strip()
        subject_id = str(record.get("subject_id") or "").strip()
        function_id = str(record.get("function_id") or "")
        if not subject_type:
            subject_type = "item_stack" if stack_id else "item_instance"
        if not subject_id:
            subject_id = stack_id if subject_type == "item_stack" else instance_id
        if subject_type == "item_stack":
            typed_id = {"stack_id": subject_id}
        else:
            typed_id = {"item_instance_id": subject_id}
            subject_type = "item_instance"
        if not subject_id or not function_id:
            continue
        runtime_state = (
            copy.deepcopy(record.get("state"))
            if isinstance(record.get("state"), Mapping)
            else copy.deepcopy(record.get("state_json"))
            if isinstance(record.get("state_json"), Mapping)
            else {}
        )
        function_runtime_payload = {
            field: copy.deepcopy(record.get(field))
            for field in (
                "enabled",
                "unlock_state",
                "remaining_charges",
                "cooldown_until",
            )
            if record.get(field) is not None
        }
        events.append(
            _event(
                proposal_id,
                "item_function_runtime",
                {
                    **_item_event_base(
                        package,
                        record,
                        claims_by_id,
                        scope="current",
                    ),
                    "action": "bootstrap",
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    **typed_id,
                    "function_id": function_id,
                    **function_runtime_payload,
                    "state": runtime_state,
                },
            )
        )

    for record in package.get("item_observations") or []:
        if not isinstance(record, Mapping):
            continue
        observer_id = str(record.get("observer_entity_id") or "").strip()
        _register_item_reference_entities(entities, record)
        subject_type = str(record.get("subject_type") or "")
        subject_id = str(record.get("subject_id") or "")
        if subject_type not in {
            "item_definition",
            "item_instance",
            "item_stack",
        }:
            continue
        typed_id = {
            "item_definition": {"item_definition_id": subject_id},
            "item_instance": {"item_instance_id": subject_id},
            "item_stack": {"stack_id": subject_id},
        }[subject_type]
        events.append(
            _event(
                proposal_id,
                "item_observation",
                {
                    **_item_event_base(
                        package,
                        record,
                        claims_by_id,
                        scope="current",
                    ),
                    "action": str(
                        record.get("observation_action") or "observe"
                    ),
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    **typed_id,
                    "observer_entity_id": observer_id or None,
                    "function_id": record.get("function_id"),
                    "observation": copy.deepcopy(
                        record.get("observation") or {}
                    ),
                },
            )
        )

    for source_ordinal, event in enumerate(events[item_event_start:]):
        if str(event.get("event_type") or "") not in ITEM_EVENT_TYPES:
            continue
        evidence = event.get("evidence")
        evidence = (
            copy.deepcopy(dict(evidence))
            if isinstance(evidence, Mapping)
            else {}
        )
        evidence["source_ordinal"] = source_ordinal
        event["evidence"] = evidence


def proposal_to_lifecycle_package(
    frozen_proposal: dict[str, Any],
) -> dict[str, Any]:
    """Convert a verified proposal envelope into deterministic entities and typed events."""

    if not isinstance(frozen_proposal, dict):
        raise PlotInitError(
            "INVALID_INITIALIZATION_PROPOSAL",
            "frozen initialization proposal must be an object",
        )
    if frozen_proposal.get("status") != "PROPOSAL_FROZEN":
        raise PlotInitError(
            "PROPOSAL_NOT_FROZEN",
            "lifecycle conversion only accepts PROPOSAL_FROZEN envelopes",
        )
    proposal_id = str(frozen_proposal.get("proposal_id") or "")
    package_hash = str(frozen_proposal.get("package_hash") or "")
    bundle = frozen_proposal.get("bundle")
    if not proposal_id or not isinstance(bundle, dict):
        raise PlotInitError(
            "INVALID_INITIALIZATION_PROPOSAL",
            "proposal_id and bundle are required",
        )
    if package_hash != str(bundle.get("bundle_hash") or ""):
        raise PlotInitError(
            "PACKAGE_HASH_MISMATCH",
            "proposal package_hash does not match bundle_hash",
            proposal_id=proposal_id,
        )
    recomputed = canonical_hash(
        bundle,
        extra_volatile_keys=(
            "real_path",
            "normalized_real_path",
            "unified_diff",
        ),
        strip_default_volatile=True,
    )
    if recomputed != package_hash:
        raise PlotInitError(
            "PACKAGE_HASH_MISMATCH",
            "bundle content no longer matches its canonical package hash",
            proposal_id=proposal_id,
            expected=package_hash,
            actual=recomputed,
        )
    provenance = bundle.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    item_sidecar_declared = bool(provenance.get("item_sidecars")) or any(
        isinstance(item, Mapping)
        and (
            str(item.get("logical_owner") or "") == "item_sidecar"
            or str(item.get("path") or "") == ".plot-rag/items.v1.json"
        )
        for item in bundle.get("artifact_manifest") or []
    )
    item_package: dict[str, Any] | None = None
    item_sidecar_reference: dict[str, Any] | None = None
    if item_sidecar_declared:
        item_package, item_sidecar_reference = (
            item_package_from_frozen_proposal(frozen_proposal)
        )
    advantage_sidecar_declared = bool(provenance.get("advantage_sidecars")) or any(
        isinstance(item, Mapping)
        and (
            str(item.get("logical_owner") or "") == "advantage_sidecar"
            or str(item.get("path") or "") == ".plot-rag/advantages.v1.json"
        )
        for item in bundle.get("artifact_manifest") or []
    )
    advantage_package: dict[str, Any] | None = None
    advantage_sidecar_reference: dict[str, Any] | None = None
    if advantage_sidecar_declared:
        advantage_package, advantage_sidecar_reference = (
            advantage_package_from_frozen_proposal(frozen_proposal)
        )
    source_manifest = copy.deepcopy(bundle.get("source_manifest") or [])
    expected_manifest_hash = str(frozen_proposal.get("source_manifest_hash") or "")
    frozen_manifest_hash = canonical_hash(source_manifest)
    if expected_manifest_hash and expected_manifest_hash != frozen_manifest_hash:
        raise PlotInitError(
            "SOURCE_MANIFEST_HASH_MISMATCH",
            "proposal source manifest changed after freeze",
            proposal_id=proposal_id,
        )
    target_project_real_path = (
        frozen_proposal.get("target_project_real_path")
        or bundle.get("target_project_real_path")
    )
    source_manifest = _normalize_accepted_source_manifest(
        source_manifest,
        target_project_real_path=target_project_real_path,
    )
    actual_manifest_hash = canonical_hash(source_manifest)
    by_source, by_version = _source_indexes(source_manifest)
    raw_claims = (
        (bundle.get("provenance") or {}).get("claims") or []
        if isinstance(bundle.get("provenance"), dict)
        else []
    )
    claims_by_id = {
        str(claim.get("claim_id")): claim
        for claim in raw_claims
        if isinstance(claim, dict) and claim.get("claim_id")
    }
    blocked_claim_ids = _open_conflict_claim_ids(
        bundle,
        claims_by_id=claims_by_id,
        by_source=by_source,
        by_version=by_version,
    )
    claims = [
        claim
        for claim in raw_claims
        if isinstance(claim, dict)
        and _claim_can_enter_canon(
            claim,
            by_source=by_source,
            by_version=by_version,
            blocked_claim_ids=blocked_claim_ids,
        )
    ]

    strict_field_gate = bool(bundle.get("field_states"))
    eligible_claim_ids = {
        str(claim.get("claim_id") or "")
        for claim in claims
        if str(claim.get("claim_id") or "")
    }

    def field_is_eligible(*paths: str) -> bool:
        return any(
            _field_state_can_enter_canon(
                bundle,
                path,
                claims_by_id=claims_by_id,
                by_source=by_source,
                by_version=by_version,
                blocked_claim_ids=blocked_claim_ids,
            )
            for path in paths
        )

    entities: dict[str, dict[str, Any]] = {}
    raw_entities_by_id: dict[str, dict[str, Any]] = {}
    for entity_index, raw in enumerate(bundle.get("entities") or []):
        if not isinstance(raw, dict):
            continue
        entity = _normalize_entity(raw)
        if entity is None:
            continue
        raw_entities_by_id[entity["entity_id"]] = entity
        source_refs = {
            str(value)
            for value in raw.get("source_refs") or []
            if str(value)
        }
        source_authorized = any(
            ref in eligible_claim_ids
            or _source_can_enter_canon(by_source.get(ref) or by_version.get(ref))
            for ref in source_refs
        )
        name_authorized = field_is_eligible(
            f"/entities/{entity_index}/canonical_name",
            f"/entities/{entity_index}/name",
        )
        if not strict_field_gate or source_authorized or name_authorized:
            entities[entity["entity_id"]] = entity

    actor_system = bundle.get("actor_system")
    actor_system = actor_system if isinstance(actor_system, dict) else {}
    actor_records = _actor_records(actor_system)
    actor_ids: dict[str, str] = {}
    for actor in actor_records:
        name = str(actor.get("name") or actor.get("canonical_name") or "").strip()
        actor_path = str(actor.get("_bundle_path") or "")
        name_key = "name" if actor.get("name") is not None else "canonical_name"
        if not _field_state_can_enter_canon(
            bundle,
            f"{actor_path}/{name_key}",
            claims_by_id=claims_by_id,
            by_source=by_source,
            by_version=by_version,
            blocked_claim_ids=blocked_claim_ids,
        ):
            continue
        entity_id = _add_entity(
            entities,
            name=name,
            entity_type="character",
            aliases=actor.get("aliases") or [],
        )
        if entity_id:
            actor_ids[name.casefold()] = entity_id

    world_model = bundle.get("world_model")
    world_model = world_model if isinstance(world_model, dict) else {}
    mvw = world_model.get("mvw")
    mvw = mvw if isinstance(mvw, dict) else {}
    location_ids: dict[str, str] = {}
    for location_index, raw_location in enumerate(
        mvw.get("locations_and_routes") or []
    ):
        if isinstance(raw_location, dict):
            name = str(
                raw_location.get("name")
                or raw_location.get("location")
                or raw_location.get("route")
                or ""
            )
            candidate_paths = [
                f"/world_model/mvw/locations_and_routes/{location_index}/{key}"
                for key in ("name", "location", "route")
                if key in raw_location
            ]
        else:
            name = str(raw_location)
            candidate_paths = [
                f"/world_model/mvw/locations_and_routes/{location_index}"
            ]
        if strict_field_gate and not field_is_eligible(*candidate_paths):
            continue
        entity_id = _add_entity(entities, name=name, entity_type="location")
        if entity_id:
            location_ids[name.casefold()] = entity_id

    for claim in claims:
        subject = str(claim.get("subject") or "").strip()
        predicate = str(claim.get("predicate") or "")
        if subject and subject not in {"作品", "故事时间"}:
            actor_ids.setdefault(
                subject.casefold(),
                _add_entity(entities, name=subject, entity_type="character") or "",
            )
        value = claim.get("object_or_value")
        if predicate == "actor.location":
            name = str(value or "")
            location_ids.setdefault(
                name.casefold(),
                _add_entity(entities, name=name, entity_type="location") or "",
            )
        elif predicate == "inventory.holds":
            _add_entity(entities, name=value, entity_type="item")
        elif predicate == "relation" and isinstance(value, dict):
            target = str(value.get("target") or "").strip()
            if target:
                actor_ids.setdefault(
                    target.casefold(),
                    _add_entity(
                        entities,
                        name=target,
                        entity_type="character",
                    )
                    or "",
                )

    power_spec_proposal_id = stable_id(
        "power-spec-init",
        proposal_id,
        package_hash,
    )
    power_spec_events: list[dict[str, Any]] = []
    power_model = (
        bundle.get("power_model")
        if bundle.get("schema_version") == "plot-rag-init/v2"
        else None
    )
    if isinstance(power_model, dict):
        namespace_to_system_id = {
            str(item.get("namespace") or ""): str(
                item.get("power_system_id") or ""
            )
            for item in power_model.get("power_systems") or []
            if isinstance(item, dict)
        }

        def record_claim_ids(record: dict[str, Any]) -> set[str]:
            values: set[str] = set()
            for key, value in record.items():
                if key in {"source_claim_ids", "evidence_claim_ids"}:
                    values.update(str(item) for item in value or [] if str(item))
                elif isinstance(value, dict):
                    values.update(record_claim_ids(value))
                elif isinstance(value, list):
                    for child in value:
                        if isinstance(child, dict):
                            values.update(record_claim_ids(child))
            return values

        def power_record_eligible(
            record: dict[str, Any],
            paths: list[str],
        ) -> bool:
            if not strict_field_gate:
                return True
            if record_claim_ids(record) & eligible_claim_ids:
                return True
            return field_is_eligible(*paths)

        spec_collections = (
            (
                "power_systems",
                "power_system_id",
                "power_system",
                "power_system",
            ),
            (
                "progression_tracks",
                "track_id",
                "progression_track",
                "progression_track",
            ),
            ("rank_nodes", "rank_node_id", "rank_node", "rank_node"),
            ("rank_edges", "rank_edge_id", "rank_edge", "rank_edge"),
            (
                "ability_definitions",
                "ability_id",
                "ability_definition",
                "ability",
            ),
            (
                "resource_definitions",
                "resource_id",
                "resource_definition",
                "resource_pool",
            ),
            (
                "status_definitions",
                "status_id",
                "status_definition",
                "status_effect",
            ),
            (
                "qualification_definitions",
                "qualification_id",
                "qualification_definition",
                "qualification",
            ),
            (
                "counter_rules",
                "counter_rule_id",
                "counter_rule",
                "counter_rule",
            ),
            (
                "bridge_rules",
                "bridge_rule_id",
                "bridge_rule",
                "bridge_rule",
            ),
            (
                "conversion_rules",
                "conversion_rule_id",
                "conversion_rule",
                "conversion_rule",
            ),
        )
        for collection, id_key, spec_type, entity_type in spec_collections:
            for index, raw_definition in enumerate(
                power_model.get(collection) or []
            ):
                if not isinstance(raw_definition, dict):
                    continue
                spec_entity_id = str(raw_definition.get(id_key) or "")
                name = str(
                    raw_definition.get("name")
                    or raw_definition.get("native_term")
                    or f"{spec_type}:{spec_entity_id}"
                )
                candidate_paths = [
                    f"/{collection}/{index}/{key}"
                    for key in raw_definition
                ]
                if not power_record_eligible(
                    raw_definition,
                    candidate_paths,
                ):
                    continue
                _put_entity(
                    entities,
                    entity_id=spec_entity_id,
                    entity_type=entity_type,
                    name=name,
                    aliases=[
                        str(binding.get("native_term"))
                        for binding in raw_definition.get(
                            "native_term_bindings"
                        )
                        or []
                        if isinstance(binding, dict)
                        and binding.get("native_term")
                    ],
                )
                definition = copy.deepcopy(raw_definition)
                if spec_type in {
                    "progression_track",
                    "ability_definition",
                    "resource_definition",
                    "status_definition",
                    "qualification_definition",
                    "counter_rule",
                }:
                    definition["system_entity_id"] = str(
                        raw_definition.get("power_system_id") or ""
                    )
                elif spec_type == "rank_node":
                    definition["track_entity_id"] = str(
                        raw_definition.get("track_id") or ""
                    )
                elif spec_type == "rank_edge":
                    definition.update(
                        {
                            "track_entity_id": str(
                                raw_definition.get("track_id") or ""
                            ),
                            "from_rank_entity_ids": list(
                                raw_definition.get("from_node_ids") or []
                            ),
                            "to_rank_entity_id": str(
                                raw_definition.get("to_node_id") or ""
                            ),
                        }
                    )
                elif spec_type == "bridge_rule":
                    definition.update(
                        {
                            "source_system_entity_id": namespace_to_system_id.get(
                                str(
                                    raw_definition.get("source_namespace")
                                    or ""
                                ),
                                "",
                            ),
                            "target_system_entity_id": namespace_to_system_id.get(
                                str(
                                    raw_definition.get("target_namespace")
                                    or ""
                                ),
                                "",
                            ),
                        }
                    )
                elif spec_type == "conversion_rule":
                    definition.update(
                        {
                            "source_resource_entity_id": str(
                                raw_definition.get("source_resource_id") or ""
                            ),
                            "target_resource_entity_id": str(
                                raw_definition.get("target_resource_id") or ""
                            ),
                            "source_system_entity_id": str(
                                raw_definition.get("source_system_id") or ""
                            ),
                            "target_system_entity_id": str(
                                raw_definition.get("target_system_id") or ""
                            ),
                        }
                    )
                power_spec_events.append(
                    _event(
                        power_spec_proposal_id,
                        "power_spec",
                        {
                            "scope": "timeless",
                            "artifact_stage": "bootstrap",
                            "action": "define",
                            "spec_type": spec_type,
                            "spec_entity_id": spec_entity_id,
                            "definition": definition,
                            "evidence": {
                                "kind": "initialization_bundle_v2",
                                "paths": candidate_paths,
                                "source_claim_ids": sorted(
                                    record_claim_ids(raw_definition)
                                ),
                            },
                        },
                    )
                )

    events: list[dict[str, Any]] = []
    advantage_info: dict[str, Any] | None = None
    if advantage_package is not None:
        # The sidecar is an artifact/reference only.  Convert every
        # authoritative record into typed lifecycle events before any other
        # bootstrap family is appended so the final causal sorter can place
        # definition/anchor/module events ahead of runtime activation.
        advantage_info = _append_advantage_bootstrap_events(
            events,
            entities,
            proposal_id=proposal_id,
            package=advantage_package,
            claims_by_id=claims_by_id,
        )
    if item_package is not None:
        _append_item_bootstrap_events(
            events,
            entities,
            proposal_id=proposal_id,
            package=item_package,
            claims_by_id=claims_by_id,
        )
    rules = list(world_model.get("rules") or [])
    for rule_index, rule in enumerate(rules):
        if not _field_state_can_enter_canon(
            bundle,
            f"/world_model/rules/{rule_index}",
            claims_by_id=claims_by_id,
            by_source=by_source,
            by_version=by_version,
            blocked_claim_ids=blocked_claim_ids,
        ):
            continue
        if isinstance(rule, dict):
            statement = str(rule.get("statement") or canonical_json(rule))
            rule_payload = copy.deepcopy(rule)
        else:
            statement = str(rule)
            rule_payload = {"statement": statement}
        events.append(
            _event(
                proposal_id,
                "world_rule",
                {
                    "scope": "timeless",
                    "rule_id": stable_id("rule", statement),
                    **rule_payload,
                },
            )
        )

    for actor in actor_records:
        name = str(actor.get("name") or actor.get("canonical_name") or "").strip()
        actor_path = str(actor.get("_bundle_path") or "")
        name_key = "name" if actor.get("name") is not None else "canonical_name"
        if not _field_state_can_enter_canon(
            bundle,
            f"{actor_path}/{name_key}",
            claims_by_id=claims_by_id,
            by_source=by_source,
            by_version=by_version,
            blocked_claim_ids=blocked_claim_ids,
        ):
            continue
        entity_id = actor_ids.get(name.casefold()) or _add_entity(
            entities, name=name, entity_type="character"
        )
        if not entity_id:
            continue
        for field in (
            "identity",
            "social_position",
            "immediate_need",
            "external_goal",
            "long_term_desire",
            "internal_lack",
            "values_and_limits",
            "debts",
            "knows",
            "suspects",
            "misunderstands",
            "secrets",
            "default_strategy",
            "world_blocker",
        ):
            if field in actor and actor[field] not in (None, "", [], {}):
                if not _field_state_can_enter_canon(
                    bundle,
                    f"{actor_path}/{field}",
                    claims_by_id=claims_by_id,
                    by_source=by_source,
                    by_version=by_version,
                    blocked_claim_ids=blocked_claim_ids,
                ):
                    continue
                events.append(
                    _event(
                        proposal_id,
                        "state",
                        {
                            "scope": "current",
                            "entity_id": entity_id,
                            "field": field,
                            "value": copy.deepcopy(actor[field]),
                            "evidence": {"kind": "initialization_bundle", "path": f"/actor_system/{field}"},
                        },
                    )
                )
        if actor.get("offscreen_plan") and _field_state_can_enter_canon(
            bundle,
            f"{actor_path}/offscreen_plan",
            claims_by_id=claims_by_id,
            by_source=by_source,
            by_version=by_version,
            blocked_claim_ids=blocked_claim_ids,
        ):
            events.append(
                _event(
                    proposal_id,
                    "state",
                    {
                        "scope": "planned",
                        "entity_id": entity_id,
                        "field": "offscreen_plan",
                        "value": copy.deepcopy(actor["offscreen_plan"]),
                        "evidence": {"kind": "initialization_bundle"},
                    },
                )
            )
        if actor.get("location") and _field_state_can_enter_canon(
            bundle,
            f"{actor_path}/location",
            claims_by_id=claims_by_id,
            by_source=by_source,
            by_version=by_version,
            blocked_claim_ids=blocked_claim_ids,
        ):
            location_name = str(actor["location"])
            location_id = location_ids.get(location_name.casefold()) or _add_entity(
                entities, name=location_name, entity_type="location"
            )
            if location_id:
                events.append(
                    _event(
                        proposal_id,
                        "movement",
                        {
                            "scope": "current",
                            "actor_entity_id": entity_id,
                            "action": "initialize_at",
                            "to_location_entity_id": location_id,
                            "evidence": {"kind": "initialization_bundle"},
                        },
                    )
                )
        for item_index, raw_item in enumerate(actor.get("resources") or []):
            item_path = f"{actor_path}/resources/{item_index}"
            if isinstance(raw_item, dict):
                candidate_paths = [
                    f"{item_path}/{key}"
                    for key in ("name", "item", "quantity", "unique")
                    if key in raw_item
                ]
            else:
                candidate_paths = [item_path]
            if not any(
                _field_state_can_enter_canon(
                    bundle,
                    path,
                    claims_by_id=claims_by_id,
                    by_source=by_source,
                    by_version=by_version,
                    blocked_claim_ids=blocked_claim_ids,
                )
                for path in candidate_paths
            ):
                continue
            if isinstance(raw_item, dict):
                item_name = str(raw_item.get("name") or raw_item.get("item") or "")
                quantity = raw_item.get("quantity", 1)
                unique = bool(raw_item.get("unique", False))
            else:
                item_name = str(raw_item)
                quantity = 1
                unique = False
            item_id = _add_entity(entities, name=item_name, entity_type="item")
            if item_id:
                events.append(
                    _event(
                        proposal_id,
                        "inventory",
                        {
                            "scope": "current",
                            "item_entity_id": item_id,
                            "action": "initialize_owner",
                            "to_owner_entity_id": entity_id,
                            "unique": unique,
                            "quantity": quantity,
                            "evidence": {"kind": "initialization_bundle"},
                        },
                    )
                )
        for ability_index, raw_ability in enumerate(actor.get("capabilities") or []):
            ability_path = f"{actor_path}/capabilities/{ability_index}"
            if isinstance(raw_ability, dict):
                candidate_paths = [
                    f"{ability_path}/{key}"
                    for key in raw_ability
                ]
            else:
                candidate_paths = [ability_path]
            if not any(
                _field_state_can_enter_canon(
                    bundle,
                    path,
                    claims_by_id=claims_by_id,
                    by_source=by_source,
                    by_version=by_version,
                    blocked_claim_ids=blocked_claim_ids,
                )
                for path in candidate_paths
            ):
                continue
            if isinstance(raw_ability, dict):
                ability_name = str(
                    raw_ability.get("name") or raw_ability.get("ability") or ""
                )
                ability_state = copy.deepcopy(raw_ability)
            else:
                ability_name = str(raw_ability)
                ability_state = {"name": ability_name}
            ability_id = _add_entity(entities, name=ability_name, entity_type="ability")
            if ability_id:
                events.append(
                    _event(
                        proposal_id,
                        "ability",
                        {
                            "scope": "current",
                            "owner_entity_id": entity_id,
                            "ability_entity_id": ability_id,
                            "action": "initialize",
                            "state": ability_state,
                            "evidence": {"kind": "initialization_bundle"},
                        },
                    )
                )

    if isinstance(power_model, dict):
        power_ability_definitions = {
            str(item.get("ability_id") or ""): copy.deepcopy(item)
            for item in power_model.get("ability_definitions") or []
            if isinstance(item, dict) and item.get("ability_id")
        }
        for bootstrap_index, raw_bootstrap in enumerate(
            power_model.get("actor_power_bootstrap") or []
        ):
            if not isinstance(raw_bootstrap, dict):
                continue
            actor_name = str(raw_bootstrap.get("actor_name") or "").strip()
            actor_entity_id = (
                actor_ids.get(actor_name.casefold())
                or _add_entity(
                    entities,
                    name=actor_name,
                    entity_type="character",
                )
            )
            if not actor_entity_id:
                continue
            actor_ids[actor_name.casefold()] = actor_entity_id
            base_path = f"/actor_power_bootstrap/{bootstrap_index}"
            for index, progression in enumerate(
                raw_bootstrap.get("progression_states") or []
            ):
                if not isinstance(progression, dict):
                    continue
                if not power_record_eligible(
                    progression,
                    [
                        f"{base_path}/progression_states/{index}/{key}"
                        for key in progression
                    ],
                ):
                    continue
                track_id = str(
                    progression.get("track_id")
                    or progression.get("track_entity_id")
                    or ""
                )
                rank_id = str(
                    progression.get("rank_node_id")
                    or progression.get("to_rank_entity_id")
                    or progression.get("current_rank_id")
                    or ""
                )
                if not track_id or not rank_id:
                    continue
                events.append(
                    _event(
                        proposal_id,
                        "progression",
                        {
                            "scope": "current",
                            "actor_entity_id": actor_entity_id,
                            "track_entity_id": track_id,
                            "action": "initialize",
                            "to_rank_entity_id": rank_id,
                            "story_coordinate": copy.deepcopy(
                                progression.get("story_coordinate")
                            ),
                            "state": copy.deepcopy(progression),
                            "evidence": {
                                "kind": "initialization_bundle_v2",
                                "source_claim_ids": sorted(
                                    record_claim_ids(progression)
                                ),
                            },
                        },
                    )
                )
            for index, resource in enumerate(
                raw_bootstrap.get("resources") or []
            ):
                if not isinstance(resource, dict):
                    continue
                if not power_record_eligible(
                    resource,
                    [
                        f"{base_path}/resources/{index}/{key}"
                        for key in resource
                    ],
                ):
                    continue
                resource_id = str(
                    resource.get("resource_id")
                    or resource.get("resource_entity_id")
                    or ""
                )
                if not resource_id:
                    continue
                events.append(
                    _event(
                        proposal_id,
                        "resource",
                        {
                            "scope": "current",
                            "actor_entity_id": actor_entity_id,
                            "resource_entity_id": resource_id,
                            "action": "initialize",
                            "amount": resource.get(
                                "amount",
                                resource.get("current", 0),
                            ),
                            "source": copy.deepcopy(resource.get("source")),
                            "story_coordinate": copy.deepcopy(
                                resource.get("story_coordinate")
                            ),
                            "state": copy.deepcopy(resource),
                            "evidence": {
                                "kind": "initialization_bundle_v2",
                                "source_claim_ids": sorted(
                                    record_claim_ids(resource)
                                ),
                            },
                        },
                    )
                )
            for index, ownership in enumerate(
                raw_bootstrap.get("ability_ownerships") or []
            ):
                if not isinstance(ownership, dict):
                    continue
                if not power_record_eligible(
                    ownership,
                    [
                        f"{base_path}/ability_ownerships/{index}/{key}"
                        for key in ownership
                    ],
                ):
                    continue
                ability_id = str(
                    ownership.get("ability_id")
                    or ownership.get("ability_entity_id")
                    or ""
                )
                if not ability_id:
                    continue
                ability_state = {
                    **copy.deepcopy(
                        power_ability_definitions.get(ability_id) or {}
                    ),
                    **copy.deepcopy(ownership),
                }
                events.append(
                    _event(
                        proposal_id,
                        "ability",
                        {
                            "scope": "current",
                            "owner_entity_id": actor_entity_id,
                            "ability_entity_id": ability_id,
                            "action": "initialize",
                            "state": ability_state,
                            "story_coordinate": copy.deepcopy(
                                ownership.get("story_coordinate")
                            ),
                            "cooldown_until": copy.deepcopy(
                                ownership.get("cooldown_until")
                            ),
                            "evidence": {
                                "kind": "initialization_bundle_v2",
                                "source_claim_ids": sorted(
                                    record_claim_ids(ownership)
                                ),
                            },
                        },
                    )
                )
            for index, status in enumerate(
                raw_bootstrap.get("statuses") or []
            ):
                if not isinstance(status, dict):
                    continue
                if not power_record_eligible(
                    status,
                    [
                        f"{base_path}/statuses/{index}/{key}"
                        for key in status
                    ],
                ):
                    continue
                status_name = str(
                    status.get("name")
                    or status.get("status")
                    or "初始化状态"
                )
                status_id = str(
                    status.get("status_id")
                    or status.get("status_entity_id")
                    or _entity_id("status_effect", status_name)
                )
                _put_entity(
                    entities,
                    entity_id=status_id,
                    entity_type="status_effect",
                    name=status_name,
                )
                events.append(
                    _event(
                        proposal_id,
                        "status_effect",
                        {
                            "scope": "current",
                            "actor_entity_id": actor_entity_id,
                            "status_entity_id": status_id,
                            "action": "apply",
                            "stacks": status.get("stacks", 1),
                            "source_entity_id": status.get("source_entity_id"),
                            "story_coordinate": copy.deepcopy(
                                status.get("story_coordinate")
                            ),
                            "expires_coordinate": copy.deepcopy(
                                status.get("expires_coordinate")
                            ),
                            "state": copy.deepcopy(status),
                            "evidence": {
                                "kind": "initialization_bundle_v2",
                                "source_claim_ids": sorted(
                                    record_claim_ids(status)
                                ),
                            },
                        },
                    )
                )
            for index, binding in enumerate(
                raw_bootstrap.get("bindings") or []
            ):
                if not isinstance(binding, dict):
                    continue
                if not power_record_eligible(
                    binding,
                    [
                        f"{base_path}/bindings/{index}/{key}"
                        for key in binding
                    ],
                ):
                    continue
                source_name = str(
                    binding.get("source_name")
                    or binding.get("name")
                    or "力量来源"
                )
                source_type = str(
                    binding.get("source_entity_type")
                    or binding.get("source_type")
                    or "item"
                )
                source_id = str(
                    binding.get("source_entity_id")
                    or _entity_id(source_type, source_name)
                )
                _put_entity(
                    entities,
                    entity_id=source_id,
                    entity_type=source_type,
                    name=source_name,
                )
                events.append(
                    _event(
                        proposal_id,
                        "power_binding",
                        {
                            "scope": "current",
                            "actor_entity_id": actor_entity_id,
                            "binding_id": str(
                                binding.get("binding_id")
                                or stable_id(
                                    "binding",
                                    actor_entity_id,
                                    source_id,
                                )
                            ),
                            "source_entity_id": source_id,
                            "action": str(binding.get("action") or "bind"),
                            "ability_entity_ids": list(
                                binding.get("ability_entity_ids")
                                or binding.get("ability_ids")
                                or []
                            ),
                            "slot_key": binding.get("slot_key"),
                            "unique": bool(binding.get("unique", False)),
                            "story_coordinate": copy.deepcopy(
                                binding.get("story_coordinate")
                            ),
                            "state": copy.deepcopy(binding),
                            "evidence": {
                                "kind": "initialization_bundle_v2",
                                "source_claim_ids": sorted(
                                    record_claim_ids(binding)
                                ),
                            },
                        },
                    )
                )
            for index, qualification in enumerate(
                raw_bootstrap.get("qualifications") or []
            ):
                if not isinstance(qualification, dict):
                    continue
                if not power_record_eligible(
                    qualification,
                    [
                        f"{base_path}/qualifications/{index}/{key}"
                        for key in qualification
                    ],
                ):
                    continue
                qualification_name = str(
                    qualification.get("name")
                    or qualification.get("qualification")
                    or "初始化资格"
                )
                qualification_id = str(
                    qualification.get("qualification_id")
                    or qualification.get("qualification_entity_id")
                    or _entity_id("qualification", qualification_name)
                )
                _put_entity(
                    entities,
                    entity_id=qualification_id,
                    entity_type="qualification",
                    name=qualification_name,
                )
                events.append(
                    _event(
                        proposal_id,
                        "qualification",
                        {
                            "scope": "current",
                            "actor_entity_id": actor_entity_id,
                            "qualification_entity_id": qualification_id,
                            "action": str(
                                qualification.get("action") or "grant"
                            ),
                            "quantity": qualification.get("quantity", 1),
                            "source_entity_id": qualification.get(
                                "source_entity_id"
                            ),
                            "story_coordinate": copy.deepcopy(
                                qualification.get("story_coordinate")
                            ),
                            "expires_coordinate": copy.deepcopy(
                                qualification.get("expires_coordinate")
                            ),
                            "state": copy.deepcopy(qualification),
                            "evidence": {
                                "kind": "initialization_bundle_v2",
                                "source_claim_ids": sorted(
                                    record_claim_ids(qualification)
                                ),
                            },
                        },
                    )
                )
            for index, observation in enumerate(
                raw_bootstrap.get("observed_capabilities") or []
            ):
                if not isinstance(observation, dict):
                    continue
                if not power_record_eligible(
                    observation,
                    [
                        f"{base_path}/observed_capabilities/{index}/{key}"
                        for key in observation
                    ],
                ):
                    continue
                events.append(
                    _event(
                        proposal_id,
                        "power_observation",
                        {
                            "scope": "current",
                            "observer_entity_id": actor_entity_id,
                            "subject_entity_id": observation.get(
                                "subject_entity_id",
                                actor_entity_id,
                            ),
                            "ability_entity_id": observation.get(
                                "ability_id",
                                observation.get("ability_entity_id"),
                            ),
                            "action": str(
                                observation.get("action") or "observe"
                            ),
                            "knowledge_plane": str(
                                observation.get("knowledge_plane")
                                or "actor_belief"
                            ),
                            "confidence": observation.get("confidence", 1.0),
                            "observed_fields": copy.deepcopy(
                                observation.get("observed_fields") or {}
                            ),
                            "story_coordinate": copy.deepcopy(
                                observation.get("story_coordinate")
                            ),
                            "evidence": {
                                "kind": "initialization_bundle_v2",
                                "source_claim_ids": sorted(
                                    record_claim_ids(observation)
                                ),
                            },
                        },
                    )
                )

    for relation_index, relation in enumerate(bundle.get("relations") or []):
        if not isinstance(relation, dict):
            continue
        source_claim_id = str(relation.get("source_claim_id") or "")
        source_claim = claims_by_id.get(source_claim_id)
        if source_claim is not None:
            if source_claim not in claims:
                continue
        elif not any(
            _field_state_can_enter_canon(
                bundle,
                f"/relations/{relation_index}/{field}",
                claims_by_id=claims_by_id,
                by_source=by_source,
                by_version=by_version,
                blocked_claim_ids=blocked_claim_ids,
            )
            for field in (
                "source_entity_id",
                "from_entity_id",
                "target_entity_id",
                "to_entity_id",
                "relation_type",
            )
        ):
            continue
        source_id = str(
            relation.get("source_entity_id")
            or relation.get("from_entity_id")
            or ""
        )
        target_id = str(
            relation.get("target_entity_id")
            or relation.get("to_entity_id")
            or ""
        )
        if source_id and target_id:
            for entity_id in (source_id, target_id):
                if entity_id not in entities and entity_id in raw_entities_by_id:
                    entities[entity_id] = raw_entities_by_id[entity_id]
            relation_scope = _scope(relation.get("scope"), "current")
            if source_claim is not None:
                relation_scope = _claim_scope(
                    source_claim,
                    _source_for_claim(source_claim, by_source, by_version),
                )
            events.append(
                _event(
                    proposal_id,
                    "relation",
                    {
                        "scope": relation_scope,
                        "source_entity_id": source_id,
                        "target_entity_id": target_id,
                        "relation_type": relation.get("relation_type") or "related",
                        "dimensions": copy.deepcopy(relation.get("dimensions") or {}),
                        "evidence": {
                            "source_claim_id": relation.get("source_claim_id")
                        },
                    },
                )
            )

    for claim in claims:
        predicate = str(claim.get("predicate") or "")
        subject = str(claim.get("subject") or "")
        needs_actor = predicate in {"actor.location", "inventory.holds"} or (
            claim.get("knowledge_plane") == "actor_belief"
        )
        subject_id = (
            actor_ids.get(subject.casefold())
            or _add_entity(entities, name=subject, entity_type="character")
            if needs_actor
            else None
        )
        evidence = _claim_evidence(claim)
        source = _source_for_claim(claim, by_source, by_version)
        common = {
            "scope": _claim_scope(claim, source),
            "story_time": copy.deepcopy(claim.get("story_time")),
            "evidence": evidence,
        }
        if predicate == "actor.location" and subject_id:
            location_name = str(claim.get("object_or_value") or "")
            location_id = location_ids.get(location_name.casefold()) or _add_entity(
                entities, name=location_name, entity_type="location"
            )
            if location_id:
                events.append(
                    _event(
                        proposal_id,
                        "movement",
                        {
                            **common,
                            "actor_entity_id": subject_id,
                            "action": "initialize_at",
                            "to_location_entity_id": location_id,
                        },
                    )
                )
        elif predicate == "inventory.holds" and subject_id:
            item_name = str(claim.get("object_or_value") or "")
            item_id = _add_entity(entities, name=item_name, entity_type="item")
            if item_id:
                events.append(
                    _event(
                        proposal_id,
                        "inventory",
                        {
                            **common,
                            "item_entity_id": item_id,
                            "action": "initialize_owner",
                            "to_owner_entity_id": subject_id,
                            "unique": False,
                            "quantity": 1,
                        },
                    )
                )
        elif claim.get("knowledge_plane") == "actor_belief" and subject_id:
            events.append(
                _event(
                    proposal_id,
                    "belief",
                    {
                        **common,
                        "believer_entity_id": subject_id,
                        "proposition_key": stable_id(
                            "proposition",
                            subject_id,
                            predicate,
                            claim.get("object_or_value"),
                        ),
                        "value": copy.deepcopy(claim.get("object_or_value")),
                    },
                )
            )

    for loop_index, loop in enumerate(bundle.get("open_loops") or []):
        if not isinstance(loop, dict):
            loop = {"description": loop}
        source_claim_id = str(loop.get("source_claim_id") or "")
        source_claim = claims_by_id.get(source_claim_id)
        if source_claim is not None:
            if source_claim not in claims:
                continue
        elif not any(
            _field_state_can_enter_canon(
                bundle,
                f"/open_loops/{loop_index}/{field}",
                claims_by_id=claims_by_id,
                by_source=by_source,
                by_version=by_version,
                blocked_claim_ids=blocked_claim_ids,
            )
            for field in ("description", "status", "loop_type", "due")
        ):
            continue
        loop_id = str(
            loop.get("loop_id")
            or stable_id("loop", loop.get("description") or loop)
        )
        events.append(
            _event(
                proposal_id,
                "open_loop",
                {
                    "scope": _scope(loop.get("scope"), "current"),
                    "loop_id": loop_id,
                    "status": loop.get("status") or "open",
                    "loop_type": loop.get("loop_type") or "promise",
                    "description": copy.deepcopy(loop.get("description")),
                    "due": copy.deepcopy(loop.get("due")),
                    "evidence": {
                        "source_claim_id": loop.get("source_claim_id"),
                        "kind": "initialization_bundle",
                    },
                },
            )
        )

    for timeline_index, timeline_item in enumerate(bundle.get("timeline") or []):
        if not isinstance(timeline_item, dict):
            timeline_item = {"label": timeline_item}
        source_claim_id = str(timeline_item.get("source_claim_id") or "")
        source_claim = claims_by_id.get(source_claim_id)
        if source_claim is not None:
            if source_claim not in claims:
                continue
        elif not any(
            _field_state_can_enter_canon(
                bundle,
                f"/timeline/{timeline_index}/{field}",
                claims_by_id=claims_by_id,
                by_source=by_source,
                by_version=by_version,
                blocked_claim_ids=blocked_claim_ids,
            )
            for field in ("label", "event", "story_time", "scope")
        ):
            continue
        events.append(
            _event(
                proposal_id,
                "time",
                {
                    "scope": _scope(timeline_item.get("scope"), "historical"),
                    "time_id": str(
                        timeline_item.get("timeline_id")
                        or stable_id("time", timeline_item)
                    ),
                    "label": timeline_item.get("label")
                    or timeline_item.get("event"),
                    "story_time": copy.deepcopy(timeline_item.get("story_time")),
                    "chapter_no": timeline_item.get("chapter_no"),
                    "scene_index": timeline_item.get("scene_index"),
                    "evidence": {
                        "source_claim_id": timeline_item.get("source_claim_id"),
                        "kind": "initialization_bundle",
                    },
                },
            )
        )

    for entity in sorted(entities.values(), key=lambda item: item["entity_id"]):
        events.append(
            _event(
                proposal_id,
                "entity",
                {
                    "scope": "timeless",
                    "entity_id": entity["entity_id"],
                    "entity_type": entity["entity_type"],
                    "canonical_name": entity["canonical_name"],
                    "aliases": entity["aliases"],
                },
            )
        )

    deduplicated: dict[str, dict[str, Any]] = {}
    for event in events:
        event_key = str(event.get("event_id") or canonical_hash(event))
        deduplicated.setdefault(event_key, event)
    final_entities = sorted(entities.values(), key=lambda item: item["entity_id"])

    # Keep initialization replay causal across all typed families.  In
    # particular, Advantage definitions/anchors/modules must exist before a
    # bind/activate event is replayed, while the historical ledger must be
    # recorded only after the runtime snapshot has been established.  Item
    # source ordinals remain the local ordering authority for item events.
    item_dependency_order = {
        ("item_spec", "item_definition"): 60,
        ("item_spec", "function_definition"): 70,
        ("item_instance", ""): 80,
        ("item_spec", "function_binding"): 90,
        ("item_custody", ""): 100,
        ("item_runtime", ""): 110,
        ("item_function_runtime", ""): 115,
        ("item_observation", ""): 120,
    }
    advantage_spec_order = {
        "advantage_definition": 10,
        "runtime_slot": 40,
        "narrative_contract": 50,
    }
    advantage_action_order = {
        "advantage_bind": 130,
        "advantage_activate": 140,
        "advantage_upgrade": 160,
        "advantage_trigger": 170,
        "advantage_use": 170,
        "advantage_reward": 175,
        "advantage_cost": 175,
        "advantage_reveal": 180,
        "advantage_contract": 190,
        "advantage_correction": 200,
    }

    def _event_ordinal(item: Mapping[str, Any]) -> int:
        evidence = item.get("evidence")
        if isinstance(evidence, Mapping) and type(
            evidence.get("source_ordinal")
        ) is int:
            return int(evidence["source_ordinal"])
        coordinate = item.get("story_coordinate")
        if isinstance(coordinate, Mapping) and type(
            coordinate.get("ordinal")
        ) is int:
            return int(coordinate["ordinal"])
        return 1_000_000_000

    def lifecycle_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
        event_type = str(item.get("event_type") or "")
        if event_type == "entity":
            return (0, 0, str(item.get("entity_id") or ""), canonical_hash(item))
        if event_type == "advantage_spec":
            spec_type = str(item.get("spec_type") or "")
            dependency = advantage_spec_order.get(spec_type, 30)
            return (
                1,
                dependency,
                _event_ordinal(item),
                str(item.get("spec_id") or item.get("advantage_id") or ""),
                canonical_hash(item),
            )
        if event_type == "advantage_anchor":
            return (
                1,
                20,
                _event_ordinal(item),
                str(item.get("anchor_id") or ""),
                canonical_hash(item),
            )
        if event_type == "advantage_module":
            action = str(item.get("action") or "define")
            dependency = 30 if action in {"define", "update"} else 150
            return (
                1,
                dependency,
                _event_ordinal(item),
                str(item.get("module_id") or ""),
                canonical_hash(item),
            )
        item_dependency = item_dependency_order.get(
            (event_type, str(item.get("spec_type") or "")),
        )
        if item_dependency is not None:
            return (
                1,
                item_dependency,
                _event_ordinal(item),
                str(item.get("spec_id") or item.get("subject_id") or ""),
                canonical_hash(item),
            )
        advantage_dependency = advantage_action_order.get(event_type)
        if advantage_dependency is not None:
            return (
                1,
                advantage_dependency,
                _event_ordinal(item),
                str(
                    item.get("entry_id")
                    or item.get("module_id")
                    or item.get("advantage_id")
                    or item.get("event_id")
                    or ""
                ),
                canonical_hash(item),
            )
        return (
            2,
            event_type,
            _event_ordinal(item),
            str(item.get("event_id") or canonical_hash(item)),
        )

    final_events = sorted(deduplicated.values(), key=lifecycle_sort_key)
    deduplicated_power_specs: dict[str, dict[str, Any]] = {}
    for event in power_spec_events:
        deduplicated_power_specs.setdefault(str(event["event_id"]), event)
    final_power_spec_events = sorted(
        deduplicated_power_specs.values(),
        key=lambda item: (
            str(item.get("spec_type") or ""),
            str(item.get("spec_entity_id") or ""),
            str(item.get("event_id") or ""),
        ),
    )
    apply_plan = frozen_proposal.get("apply_plan")
    apply_plan = apply_plan if isinstance(apply_plan, dict) else {}
    artifacts = copy.deepcopy(apply_plan.get("artifacts") or [])
    if not artifacts:
        artifacts = [
            {
                "artifact_id": item.get("artifact_id"),
                "path": item.get("path"),
                "operation": item.get("operation"),
                "expected_old_hash": item.get("expected_old_hash"),
                "proposed_new_hash": item.get("proposed_new_hash"),
            }
            for item in bundle.get("artifact_manifest") or []
            if isinstance(item, dict)
        ]
    power_spec_package = (
        {
            "schema_version": "plot-rag-lifecycle/power-spec-package-v1",
            "proposal_id": power_spec_proposal_id,
            "parent_initialization_proposal_id": proposal_id,
            "proposal_kind": "power_spec_change",
            "required_operation": "accept_power_spec",
            "scope": "timeless",
            "entities": [
                entity
                for entity in final_entities
                if entity.get("entity_type")
                in {
                    "power_system",
                    "progression_track",
                    "rank_node",
                    "rank_edge",
                    "ability",
                    "resource_pool",
                    "status_effect",
                    "counter_rule",
                    "bridge_rule",
                    "conversion_rule",
                    "qualification",
                }
            ],
            "events": final_power_spec_events,
            "power_package_hash": str(
                (power_model or {}).get("power_package_hash") or ""
            ),
        }
        if final_power_spec_events
        else None
    )
    if power_spec_package is not None:
        power_spec_package["package_hash"] = canonical_hash(
            power_spec_package
        )
    result = {
        "schema_version": "plot-rag-lifecycle/init-package-v1",
        "initialization_bundle_schema_version": str(
            bundle.get("schema_version") or "plot-rag-init/v1"
        ),
        "proposal_id": proposal_id,
        "package_hash": package_hash,
        "target_project_real_path": target_project_real_path,
        "source_manifest": source_manifest,
        "source_manifest_hash": actual_manifest_hash,
        "materialization_plan": {
            "requires_approval_grant": True,
            "authorized_operations_required": copy.deepcopy(
                apply_plan.get("authorized_operations_required")
                or ["accept_initialization", "materialize"]
            ),
            "artifacts": artifacts,
            "executed": False,
        },
        "entities": final_entities,
        "events": final_events,
        "adapter_hash": canonical_hash(
            {
                "proposal_id": proposal_id,
                "package_hash": package_hash,
                "entities": final_entities,
                "events": final_events,
                "artifacts": artifacts,
                "power_spec_package": power_spec_package,
                "item_sidecar": item_sidecar_reference,
                "item_package_hash": (
                    item_package.get("package_hash")
                    if item_package is not None
                    else None
                ),
                "advantage_sidecar": advantage_sidecar_reference,
                "advantage_package_hash": (
                    advantage_package.get("package_hash")
                    if advantage_package is not None
                    else None
                ),
                "advantage_event_count": (
                    int(advantage_info.get("event_count") or 0)
                    if advantage_info is not None
                    else 0
                ),
                "advantage_info": advantage_info,
            }
        ),
    }
    if item_sidecar_reference is not None and item_package is not None:
        result["item_sidecar"] = copy.deepcopy(item_sidecar_reference)
        result["item_package_hash"] = str(item_package["package_hash"])
    if advantage_sidecar_reference is not None and advantage_package is not None:
        result["advantage_sidecar"] = copy.deepcopy(
            advantage_sidecar_reference
        )
        result["advantage_package_hash"] = str(
            advantage_package["package_hash"]
        )
        result["advantage_info"] = copy.deepcopy(advantage_info or {})
        result["advantage_event_count"] = int(
            (advantage_info or {}).get("event_count") or 0
        )
        result["requires_advantage_acceptance"] = True
    else:
        result["advantage_event_count"] = 0
        result["requires_advantage_acceptance"] = False
    if power_spec_package is not None:
        result["power_spec_package"] = power_spec_package
        result["requires_power_spec_acceptance"] = True
    else:
        result["requires_power_spec_acceptance"] = False
    return result
