"""Deterministic ``plot-rag-advantage/v1`` initialization sidecars.

Initialization bundle v1/v2 remain frozen protocols.  Golden-finger data is
therefore normalized into a separately hash-bound sidecar.  The sidecar keeps
physical objects, power definitions, actors, locations, and contracts as
external stable references; it does not duplicate the Item, Power, Relation,
Location, or Timeline projections.
"""

from __future__ import annotations

import copy
import difflib
import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

# ``scripts.plot_init`` is imported both as a package (the normal v1 runtime
# path) and as a direct ``plot_init`` module by legacy CLI entry points. Keep
# one module identity in package mode so ``scripts.v1_runtime`` does not fall
# through to its duplicate-module compatibility imports.
if __package__ and __package__.startswith("scripts."):
    from ..advantage_profiles import (
        ADVANTAGE_ANCHOR_TYPES,
        ADVANTAGE_PROFILES,
        PROFILE_REGISTRY_SCHEMA_VERSION,
        advantage_profile_registry_hash,
        detect_advantage_profiles,
        get_advantage_profile,
    )
else:
    from advantage_profiles import (
        ADVANTAGE_ANCHOR_TYPES,
        ADVANTAGE_PROFILES,
        PROFILE_REGISTRY_SCHEMA_VERSION,
        advantage_profile_registry_hash,
        detect_advantage_profiles,
        get_advantage_profile,
    )

from .canonical import (
    canonical_hash,
    canonical_json,
    path_is_within,
    sha256_bytes,
    stable_id,
)
from .errors import PlotInitError


ADVANTAGE_SCHEMA_VERSION = "plot-rag-advantage/v1"
ADVANTAGE_SIDECAR_PATH = ".plot-rag/advantages.v1.json"
ADVANTAGE_SIDECAR_OWNER = "advantage_sidecar"
ADVANTAGE_JSON_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "plot-rag-advantage.v1.json"
)

ADVANTAGE_STATUSES = frozenset({"canon", "planned", "rumor", "misread"})
KNOWLEDGE_PLANES = frozenset(
    {
        "objective",
        "actor_belief",
        "public_narrative",
        "reader_disclosed",
        "author_plan",
    }
)
ANCHOR_BINDING_STATES = frozenset(
    {"unbound", "bound", "dormant", "sealed", "contested", "released"}
)
ADVANTAGE_PACKAGE_ARRAY_FIELDS = (
    "definitions",
    "anchors",
    "modules",
    "runtime_slots",
    "runtime_bootstrap",
    "ledger_bootstrap",
    "knowledge",
    "contracts",
    "narrative_contracts",
)
ADVANTAGE_DOSSIER_KEYS = (
    "advantages",
    "advantage_definitions",
    "definitions",
    "advantage_anchors",
    "anchors",
    "advantage_modules",
    "modules",
    "runtime_slots",
    "advantage_runtime_slots",
    "advantage_runtime",
    "runtime_bootstrap",
    "advantage_ledger",
    "ledger_bootstrap",
    "advantage_knowledge",
    "knowledge",
    "advantage_contracts",
    "contracts",
    "advantage_narrative_contracts",
    "narrative_contracts",
)
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "work_id",
        "source_initialization_schema_version",
        "source_snapshot_hash",
        *ADVANTAGE_PACKAGE_ARRAY_FIELDS,
        "provenance",
        "package_hash",
    }
)
_COLLECTION_ID_FIELDS = {
    "definitions": "advantage_id",
    "anchors": "anchor_id",
    "modules": "module_id",
    "runtime_slots": "slot_id",
    "runtime_bootstrap": "runtime_id",
    "ledger_bootstrap": "entry_id",
    "knowledge": "knowledge_id",
    "contracts": "contract_id",
    "narrative_contracts": "narrative_contract_id",
}
_CLAIM_PREDICATE_TO_COLLECTION = {
    "advantage.definition": "definitions",
    "advantage.anchor": "anchors",
    "advantage.module": "modules",
    "advantage.runtime_slot": "runtime_slots",
    "advantage.runtime": "runtime_bootstrap",
    "advantage.ledger": "ledger_bootstrap",
    "advantage.knowledge": "knowledge",
    "advantage.contract": "contracts",
    "advantage.narrative_contract": "narrative_contracts",
}


class _AdvantageSchemaContractError(ValueError):
    """One required/type failure from the checked-in Advantage JSON Schema."""

    def __init__(
        self,
        *,
        path: str,
        keyword: str,
        expected: Any,
        actual: Any,
    ) -> None:
        super().__init__(
            f"{path} violates JSON Schema {keyword}: "
            f"expected {expected!r}, got {actual!r}"
        )
        self.path = path
        self.keyword = keyword
        self.expected = expected
        self.actual = actual


@lru_cache(maxsize=1)
def _advantage_json_schema() -> dict[str, Any]:
    """Load the repository schema used by initialization and release gates."""

    try:
        value = json.loads(
            ADVANTAGE_JSON_SCHEMA_PATH.read_text(encoding="utf-8-sig")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlotInitError(
            "ADVANTAGE_PACKAGE_SCHEMA_UNAVAILABLE",
            "advantage sidecar JSON Schema cannot be loaded",
            path=str(ADVANTAGE_JSON_SCHEMA_PATH),
        ) from exc
    if not isinstance(value, dict):
        raise PlotInitError(
            "ADVANTAGE_PACKAGE_SCHEMA_UNAVAILABLE",
            "advantage sidecar JSON Schema root must be an object",
            path=str(ADVANTAGE_JSON_SCHEMA_PATH),
        )
    return value


def _schema_pointer(
    document: Mapping[str, Any],
    reference: str,
) -> Mapping[str, Any]:
    if not reference.startswith("#/"):
        raise PlotInitError(
            "ADVANTAGE_PACKAGE_SCHEMA_UNAVAILABLE",
            "advantage sidecar JSON Schema uses an unsupported reference",
            reference=reference,
        )
    current: Any = document
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or part not in current:
            raise PlotInitError(
                "ADVANTAGE_PACKAGE_SCHEMA_UNAVAILABLE",
                "advantage sidecar JSON Schema reference is unresolved",
                reference=reference,
            )
        current = current[part]
    if not isinstance(current, Mapping):
        raise PlotInitError(
            "ADVANTAGE_PACKAGE_SCHEMA_UNAVAILABLE",
            "advantage sidecar JSON Schema reference is not an object",
            reference=reference,
        )
    return current


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return type(value) is str
    if expected == "boolean":
        return type(value) is bool
    if expected == "null":
        return value is None
    if expected == "integer":
        return type(value) is int
    if expected == "number":
        return (
            type(value) in {int, float}
            and math.isfinite(float(value))
        )
    raise PlotInitError(
        "ADVANTAGE_PACKAGE_SCHEMA_UNAVAILABLE",
        "advantage sidecar JSON Schema uses an unsupported type",
        schema_type=expected,
    )


def _validate_schema_required_types(
    value: Any,
    schema: Mapping[str, Any],
    *,
    document: Mapping[str, Any],
    path: str,
) -> None:
    """Execute the repository schema's required/type contract.

    The plug-in intentionally has no third-party JSON Schema dependency.  This
    small deterministic executor follows local refs and composition only far
    enough to enforce every nested ``required`` and ``type`` declaration in
    ``schemas/plot-rag-advantage.v1.json``.  Domain references, lifecycle
    rules, hashes, enums, and numeric invariants remain the semantic validator's
    responsibility and therefore run afterwards.
    """

    reference = schema.get("$ref")
    if isinstance(reference, str):
        _validate_schema_required_types(
            value,
            _schema_pointer(document, reference),
            document=document,
            path=path,
        )

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for child in all_of:
            if isinstance(child, Mapping):
                _validate_schema_required_types(
                    value,
                    child,
                    document=document,
                    path=path,
                )

    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        failures: list[_AdvantageSchemaContractError] = []
        for child in one_of:
            if not isinstance(child, Mapping):
                continue
            try:
                _validate_schema_required_types(
                    value,
                    child,
                    document=document,
                    path=path,
                )
            except _AdvantageSchemaContractError as exc:
                failures.append(exc)
            else:
                break
        else:
            expected = [
                child.get("type") or child.get("$ref")
                for child in one_of
                if isinstance(child, Mapping)
            ]
            raise _AdvantageSchemaContractError(
                path=path,
                keyword="oneOf(required/type)",
                expected=expected,
                actual=type(value).__name__,
            ) from (failures[0] if failures else None)

    expected_type = schema.get("type")
    if isinstance(expected_type, str):
        expected_types = [expected_type]
    elif isinstance(expected_type, list):
        expected_types = [
            str(item) for item in expected_type if isinstance(item, str)
        ]
    else:
        expected_types = []
    if expected_types and not any(
        _schema_type_matches(value, item) for item in expected_types
    ):
        raise _AdvantageSchemaContractError(
            path=path,
            keyword="type",
            expected=(
                expected_types[0]
                if len(expected_types) == 1
                else expected_types
            ),
            actual=type(value).__name__,
        )

    if isinstance(value, Mapping):
        required = schema.get("required")
        if isinstance(required, list):
            missing = [
                str(name)
                for name in required
                if isinstance(name, str) and name not in value
            ]
            if missing:
                raise _AdvantageSchemaContractError(
                    path=path,
                    keyword="required",
                    expected=missing,
                    actual=sorted(str(name) for name in value),
                )
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            for name, child in properties.items():
                if (
                    name in value
                    and isinstance(child, Mapping)
                ):
                    _validate_schema_required_types(
                        value[name],
                        child,
                        document=document,
                        path=f"{path}.{name}",
                    )
        additional = schema.get("additionalProperties")
        if isinstance(additional, Mapping):
            known = (
                set(str(name) for name in properties)
                if isinstance(properties, Mapping)
                else set()
            )
            for name, child_value in value.items():
                if str(name) not in known:
                    _validate_schema_required_types(
                        child_value,
                        additional,
                        document=document,
                        path=f"{path}.{name}",
                    )

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                _validate_schema_required_types(
                    item,
                    items,
                    document=document,
                    path=f"{path}[{index}]",
                )


def _validate_advantage_json_schema_contract(value: Mapping[str, Any]) -> None:
    document = _advantage_json_schema()
    try:
        _validate_schema_required_types(
            value,
            document,
            document=document,
            path="$",
        )
    except _AdvantageSchemaContractError as exc:
        raise PlotInitError(
            "ADVANTAGE_PACKAGE_SCHEMA_INVALID",
            "advantage sidecar violates the repository JSON Schema contract",
            schema_path=str(ADVANTAGE_JSON_SCHEMA_PATH),
            instance_path=exc.path,
            keyword=exc.keyword,
            expected=exc.expected,
            actual=exc.actual,
        ) from exc


def recompute_advantage_package_hash(package: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(package))
    payload.pop("package_hash", None)
    return canonical_hash(payload)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _as_records(value: Any) -> list[Any]:
    if isinstance(value, list):
        return copy.deepcopy(value)
    if value in (None, "", {}):
        return []
    return [copy.deepcopy(value)]


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        values = list(value)
    else:
        values = []
    return sorted({_clean_text(item) for item in values if _clean_text(item)})


def _source_claim_ids(
    raw: Mapping[str, Any],
    fallback: Iterable[str] = (),
) -> list[str]:
    values = raw.get("source_claim_ids")
    if values is None:
        values = raw.get("evidence_claim_ids")
    return sorted(
        {
            *[
                _clean_text(item)
                for item in _as_records(values)
                if _clean_text(item)
            ],
            *[_clean_text(item) for item in fallback if _clean_text(item)],
        }
    )


def _status(raw: Mapping[str, Any], default: str = "canon") -> str:
    value = _clean_text(raw.get("status") or default).casefold()
    aliases = {
        "accepted": "canon",
        "confirmed": "canon",
        "current": "canon",
        "future": "planned",
        "plan": "planned",
        "传闻": "rumor",
        "误解": "misread",
        "已确认": "canon",
        "规划": "planned",
    }
    return aliases.get(value, value)


def _knowledge_plane(
    raw: Mapping[str, Any],
    default: str = "objective",
) -> str:
    value = _clean_text(raw.get("knowledge_plane") or default).casefold()
    aliases = {
        "character": "actor_belief",
        "character_belief": "actor_belief",
        "public": "public_narrative",
        "reader": "reader_disclosed",
        "author": "author_plan",
    }
    return aliases.get(value, value)


def _merge_unique(
    records: dict[str, dict[str, Any]],
    record: dict[str, Any],
    *,
    id_field: str,
) -> None:
    record_id = _clean_text(record.get(id_field))
    if not record_id:
        raise PlotInitError(
            "ADVANTAGE_RECORD_ID_REQUIRED",
            f"advantage sidecar record requires {id_field}",
            field=id_field,
        )
    previous = records.get(record_id)
    if previous is None:
        records[record_id] = record
        return
    if canonical_json(previous) == canonical_json(record):
        return
    raise PlotInitError(
        "ADVANTAGE_RECORD_ID_CONFLICT",
        "advantage sidecar id is bound to different immutable content",
        field=id_field,
        record_id=record_id,
    )


def _raw_records(dossier: Mapping[str, Any], *keys: str) -> list[Any]:
    values: list[Any] = []
    for key in keys:
        values.extend(_as_records(dossier.get(key)))
    return values


def _profile_list(raw: Mapping[str, Any]) -> list[str]:
    explicit = _string_list(
        raw.get("profiles")
        or raw.get("profile_ids")
        or raw.get("profile")
    )
    try:
        if explicit:
            profiles = [
                get_advantage_profile(item).profile for item in explicit
            ]
        else:
            profiles = list(detect_advantage_profiles(raw))
    except ValueError as exc:
        raise PlotInitError(
            "ADVANTAGE_PROFILE_INVALID",
            "advantage definition references an unsupported profile",
            profiles=explicit,
        ) from exc
    return sorted(set(profiles))


def _default_anchor_type(profiles: Iterable[str]) -> str:
    values = list(profiles)
    if not values:
        return "virtual_system"
    return get_advantage_profile(values[0]).anchor_types[0]


def _normalize_definition(
    raw_value: Any,
    *,
    work_id: str,
    fallback_claim_ids: Iterable[str] = (),
    origin: str,
) -> dict[str, Any] | None:
    raw = (
        copy.deepcopy(dict(raw_value))
        if isinstance(raw_value, Mapping)
        else {"title": _clean_text(raw_value)}
    )
    title = _clean_text(
        raw.get("title")
        or raw.get("name")
        or raw.get("advantage_name")
        or raw.get("canonical_name")
    )
    if not title:
        return None
    profiles = _profile_list(raw)
    if not profiles:
        raise PlotInitError(
            "ADVANTAGE_PROFILE_REQUIRED",
            "advantage definition requires at least one registered profile",
            title=title,
        )
    anchor_type = _clean_text(raw.get("anchor_type")) or _default_anchor_type(
        profiles
    )
    if anchor_type not in ADVANTAGE_ANCHOR_TYPES:
        raise PlotInitError(
            "ADVANTAGE_ANCHOR_TYPE_INVALID",
            "advantage definition uses an unsupported anchor type",
            title=title,
            anchor_type=anchor_type,
        )
    advantage_id = _clean_text(raw.get("advantage_id")) or stable_id(
        "adv",
        work_id,
        title.casefold(),
        profiles,
    )
    promise = raw.get("promise")
    if promise in (None, "", [], {}):
        promise = raw.get("reading_promise") or ""
    counterplay = raw.get("counterplay")
    if counterplay in (None, "", [], {}):
        counterplay = raw.get("counters") or []
    return {
        **raw,
        "advantage_id": advantage_id,
        "profiles": profiles,
        "title": title,
        "anchor_type": anchor_type,
        "acquisition_mode": _clean_text(raw.get("acquisition_mode"))
        or "unknown",
        "uniqueness": _clean_text(raw.get("uniqueness")) or "unknown",
        "promise": copy.deepcopy(promise),
        "counterplay": copy.deepcopy(counterplay),
        "status": _status(raw),
        "source_claim_ids": _source_claim_ids(raw, fallback_claim_ids),
        "origin": _clean_text(raw.get("origin")) or origin,
    }


def _definition_lookup(
    definitions: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for advantage_id, record in definitions.items():
        for name in (
            record.get("title"),
            record.get("name"),
            record.get("advantage_name"),
        ):
            cleaned = _clean_text(name).casefold()
            if cleaned and cleaned not in result:
                result[cleaned] = advantage_id
    return result


def _ensure_definition(
    definitions: dict[str, dict[str, Any]],
    *,
    work_id: str,
    advantage_id: Any = None,
    advantage_name: Any = None,
    profiles: Any = None,
    origin: str,
    source_claim_ids: Iterable[str] = (),
) -> str | None:
    explicit_id = _clean_text(advantage_id)
    if explicit_id and explicit_id in definitions:
        return explicit_id
    name = _clean_text(advantage_name)
    if name:
        found = _definition_lookup(definitions).get(name.casefold())
        if found:
            return found
        raw: dict[str, Any] = {
            "advantage_id": explicit_id or None,
            "title": name,
            "profiles": profiles,
            "source_claim_ids": list(source_claim_ids),
        }
        record = _normalize_definition(
            raw,
            work_id=work_id,
            origin=origin,
        )
        if record is not None:
            _merge_unique(
                definitions,
                record,
                id_field="advantage_id",
            )
            return str(record["advantage_id"])
    return explicit_id or None


def _advantage_reference(
    raw: Mapping[str, Any],
    definitions: dict[str, dict[str, Any]],
    *,
    work_id: str,
    origin: str,
    fallback_claim_ids: Iterable[str],
) -> str | None:
    return _ensure_definition(
        definitions,
        work_id=work_id,
        advantage_id=raw.get("advantage_id"),
        advantage_name=(
            raw.get("advantage_name")
            or raw.get("advantage_title")
            or raw.get("title")
        ),
        profiles=raw.get("profiles") or raw.get("profile"),
        origin=origin,
        source_claim_ids=fallback_claim_ids,
    )


def _normalize_anchor(
    raw_value: Any,
    *,
    definitions: dict[str, dict[str, Any]],
    work_id: str,
    fallback_claim_ids: Iterable[str] = (),
    origin: str,
) -> dict[str, Any] | None:
    if not isinstance(raw_value, Mapping):
        return None
    raw = copy.deepcopy(dict(raw_value))
    advantage_id = _advantage_reference(
        raw,
        definitions,
        work_id=work_id,
        origin=origin,
        fallback_claim_ids=fallback_claim_ids,
    )
    if not advantage_id:
        return None
    definition = definitions.get(advantage_id, {})
    anchor_type = _clean_text(
        raw.get("anchor_type") or raw.get("type")
    ) or _clean_text(definition.get("anchor_type"))
    if anchor_type not in ADVANTAGE_ANCHOR_TYPES:
        raise PlotInitError(
            "ADVANTAGE_ANCHOR_TYPE_INVALID",
            "advantage anchor uses an unsupported anchor type",
            advantage_id=advantage_id,
            anchor_type=anchor_type,
        )
    anchor_ref_id = _clean_text(
        raw.get("anchor_ref_id")
        or raw.get("external_id")
        or raw.get(f"{anchor_type}_id")
        or raw.get("subject_id")
    )
    anchor_name = _clean_text(
        raw.get("anchor_name")
        or raw.get("name")
        or raw.get("subject_name")
    )
    if not anchor_ref_id and anchor_name:
        anchor_ref_id = stable_id(
            "ent",
            anchor_type,
            anchor_name.casefold(),
        )
    if not anchor_ref_id:
        raise PlotInitError(
            "ADVANTAGE_ANCHOR_REFERENCE_REQUIRED",
            "advantage anchor requires an external stable reference",
            advantage_id=advantage_id,
            anchor_type=anchor_type,
        )
    owner_entity_id = _clean_text(
        raw.get("owner_entity_id") or raw.get("owner_id")
    ) or None
    anchor_id = _clean_text(raw.get("anchor_id")) or stable_id(
        "advanchor",
        advantage_id,
        anchor_type,
        anchor_ref_id,
        owner_entity_id,
    )
    return {
        **raw,
        "anchor_id": anchor_id,
        "advantage_id": advantage_id,
        "anchor_type": anchor_type,
        "anchor_ref_id": anchor_ref_id,
        "anchor_name": anchor_name or None,
        "owner_entity_id": owner_entity_id,
        "binding_state": {
            "transferred": "released",
            "broken": "released",
            "unknown": "unbound",
        }.get(
            _clean_text(raw.get("binding_state")),
            _clean_text(raw.get("binding_state")) or "bound",
        ),
        "transfer_rule": copy.deepcopy(raw.get("transfer_rule", "unknown")),
        "status": _status(raw),
        "source_claim_ids": _source_claim_ids(raw, fallback_claim_ids),
        "origin": _clean_text(raw.get("origin")) or origin,
    }


def _normalize_module(
    raw_value: Any,
    *,
    definitions: dict[str, dict[str, Any]],
    work_id: str,
    fallback_claim_ids: Iterable[str] = (),
    origin: str,
) -> dict[str, Any] | None:
    if not isinstance(raw_value, Mapping):
        return None
    raw = copy.deepcopy(dict(raw_value))
    advantage_id = _advantage_reference(
        raw,
        definitions,
        work_id=work_id,
        origin=origin,
        fallback_claim_ids=fallback_claim_ids,
    )
    name = _clean_text(
        raw.get("name")
        or raw.get("module_name")
        or raw.get("title")
    )
    kind = _clean_text(raw.get("kind") or raw.get("module_kind"))
    if not advantage_id or not name or not kind:
        return None
    profile = _clean_text(raw.get("profile")) or None
    if profile:
        profile = get_advantage_profile(profile).profile
        if profile not in definitions[advantage_id]["profiles"]:
            raise PlotInitError(
                "ADVANTAGE_MODULE_PROFILE_INVALID",
                "advantage module profile is not declared by its definition",
                advantage_id=advantage_id,
                profile=profile,
            )
    module_id = _clean_text(raw.get("module_id")) or stable_id(
        "advmod",
        advantage_id,
        kind.casefold(),
        name.casefold(),
    )
    record_status = _status(raw)
    module_status = _clean_text(raw.get("module_status")) or (
        "available" if record_status == "canon" else "locked"
    )
    return {
        **raw,
        "module_id": module_id,
        "advantage_id": advantage_id,
        "profile": profile,
        "name": name,
        "kind": kind,
        "module_kind": kind,
        "module_status": module_status,
        "trigger": copy.deepcopy(raw.get("trigger") or {}),
        "preconditions": copy.deepcopy(raw.get("preconditions") or []),
        "targets": copy.deepcopy(raw.get("targets") or []),
        "range": copy.deepcopy(raw.get("range")),
        "costs": copy.deepcopy(raw.get("costs") or []),
        "effects": copy.deepcopy(raw.get("effects") or []),
        "side_effects": copy.deepcopy(raw.get("side_effects") or []),
        "failure_modes": copy.deepcopy(raw.get("failure_modes") or []),
        "counters": copy.deepcopy(raw.get("counters") or []),
        "anchor_ids": _string_list(raw.get("anchor_ids")),
        "granted_ability_ids": _string_list(
            raw.get("granted_ability_ids")
        ),
        "status": record_status,
        "knowledge_plane": _knowledge_plane(raw),
        "reveal_stage": _clean_text(raw.get("reveal_stage")) or "initial",
        "source_claim_ids": _source_claim_ids(raw, fallback_claim_ids),
        "origin": _clean_text(raw.get("origin")) or origin,
    }


def _normalize_runtime_slot(
    raw_value: Any,
    *,
    definitions: dict[str, dict[str, Any]],
    work_id: str,
    fallback_claim_ids: Iterable[str] = (),
    origin: str,
) -> dict[str, Any] | None:
    if not isinstance(raw_value, Mapping):
        return None
    raw = copy.deepcopy(dict(raw_value))
    advantage_id = _advantage_reference(
        raw,
        definitions,
        work_id=work_id,
        origin=origin,
        fallback_claim_ids=fallback_claim_ids,
    )
    name = _clean_text(raw.get("name") or raw.get("slot_name"))
    stage = _clean_text(raw.get("stage")) or "initial"
    if not advantage_id or not name:
        return None
    slot_id = _clean_text(raw.get("slot_id")) or stable_id(
        "advslot",
        advantage_id,
        stage.casefold(),
        name.casefold(),
    )
    return {
        **raw,
        "slot_id": slot_id,
        "advantage_id": advantage_id,
        "name": name,
        "slot_kind": _clean_text(raw.get("slot_kind") or raw.get("kind"))
        or "capacity",
        "stage": stage,
        "capacity": copy.deepcopy(raw.get("capacity")),
        "unlock_graph": copy.deepcopy(raw.get("unlock_graph") or []),
        "set_membership": copy.deepcopy(raw.get("set_membership") or []),
        "status": _status(raw),
        "source_claim_ids": _source_claim_ids(raw, fallback_claim_ids),
        "origin": _clean_text(raw.get("origin")) or origin,
    }


def _normalize_runtime(
    raw_value: Any,
    *,
    definitions: dict[str, dict[str, Any]],
    work_id: str,
    fallback_claim_ids: Iterable[str] = (),
    origin: str,
) -> dict[str, Any] | None:
    if not isinstance(raw_value, Mapping):
        return None
    raw = copy.deepcopy(dict(raw_value))
    advantage_id = _advantage_reference(
        raw,
        definitions,
        work_id=work_id,
        origin=origin,
        fallback_claim_ids=fallback_claim_ids,
    )
    if not advantage_id:
        return None
    branch_id = _clean_text(raw.get("branch_id")) or "main"
    runtime_id = _clean_text(raw.get("runtime_id")) or stable_id(
        "advrt",
        advantage_id,
        branch_id,
    )
    charges = raw.get("charges")
    max_charges = raw.get("max_charges")
    return {
        **raw,
        "runtime_id": runtime_id,
        "advantage_id": advantage_id,
        "branch_id": branch_id,
        "stage": _clean_text(raw.get("stage")) or "initial",
        "enabled": bool(raw.get("enabled", True)),
        "charges": copy.deepcopy(charges),
        "max_charges": copy.deepcopy(max_charges),
        "cooldown_until": copy.deepcopy(raw.get("cooldown_until")),
        "resources": copy.deepcopy(raw.get("resources") or {}),
        "pollution": copy.deepcopy(raw.get("pollution", 0)),
        "exposure": copy.deepcopy(raw.get("exposure", 0)),
        "debt": copy.deepcopy(raw.get("debt", 0)),
        "unlocked_modules": _string_list(raw.get("unlocked_modules")),
        "source_event_id": _clean_text(raw.get("source_event_id")) or None,
        "status": _status(raw),
        "source_claim_ids": _source_claim_ids(raw, fallback_claim_ids),
        "origin": _clean_text(raw.get("origin")) or origin,
    }


def _normalize_ledger(
    raw_value: Any,
    *,
    definitions: dict[str, dict[str, Any]],
    work_id: str,
    fallback_claim_ids: Iterable[str] = (),
    origin: str,
) -> dict[str, Any] | None:
    if not isinstance(raw_value, Mapping):
        return None
    raw = copy.deepcopy(dict(raw_value))
    advantage_id = _advantage_reference(
        raw,
        definitions,
        work_id=work_id,
        origin=origin,
        fallback_claim_ids=fallback_claim_ids,
    )
    entry_kind = _clean_text(
        raw.get("entry_kind") or raw.get("kind") or raw.get("action")
    )
    if not advantage_id or not entry_kind:
        return None
    source_event_id = _clean_text(raw.get("source_event_id")) or None
    discriminator = (
        _clean_text(raw.get("entry_key"))
        or source_event_id
        or canonical_hash(
            {
                "input": raw.get("input"),
                "output": raw.get("output"),
                "loss": raw.get("loss"),
                "provenance": raw.get("provenance"),
            }
        )
    )
    entry_id = _clean_text(raw.get("entry_id")) or stable_id(
        "advledger",
        advantage_id,
        entry_kind,
        discriminator,
    )
    return {
        **raw,
        "entry_id": entry_id,
        "advantage_id": advantage_id,
        "entry_kind": entry_kind,
        "source_event_id": source_event_id,
        "input": copy.deepcopy(raw.get("input") or {}),
        "output": copy.deepcopy(raw.get("output") or {}),
        "loss": copy.deepcopy(raw.get("loss") or {}),
        "provenance": copy.deepcopy(raw.get("provenance") or {}),
        "status": _status(raw),
        "source_claim_ids": _source_claim_ids(raw, fallback_claim_ids),
        "origin": _clean_text(raw.get("origin")) or origin,
    }


def _normalize_knowledge(
    raw_value: Any,
    *,
    definitions: dict[str, dict[str, Any]],
    work_id: str,
    fallback_claim_ids: Iterable[str] = (),
    origin: str,
) -> dict[str, Any] | None:
    if not isinstance(raw_value, Mapping):
        return None
    raw = copy.deepcopy(dict(raw_value))
    advantage_id = _advantage_reference(
        raw,
        definitions,
        work_id=work_id,
        origin=origin,
        fallback_claim_ids=fallback_claim_ids,
    )
    claim = raw.get("claim")
    if claim in (None, "", [], {}):
        claim = raw.get("statement") or raw.get("knowledge")
    if not advantage_id or claim in (None, "", [], {}):
        return None
    plane = _knowledge_plane(raw)
    reveal_stage = _clean_text(raw.get("reveal_stage")) or "initial"
    observer_id = _clean_text(
        raw.get("observer_entity_id") or raw.get("observer_id")
    ) or None
    knowledge_id = _clean_text(raw.get("knowledge_id")) or stable_id(
        "advknow",
        advantage_id,
        plane,
        reveal_stage,
        observer_id,
        claim,
    )
    confidence = raw.get("confidence", 1.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        **raw,
        "knowledge_id": knowledge_id,
        "advantage_id": advantage_id,
        "module_id": _clean_text(raw.get("module_id")) or None,
        "knowledge_plane": plane,
        "claim": copy.deepcopy(claim),
        "confidence": confidence,
        "reveal_stage": reveal_stage,
        "observer_entity_id": observer_id,
        "misread_of": _clean_text(raw.get("misread_of")) or None,
        "status": _status(raw),
        "source_claim_ids": _source_claim_ids(raw, fallback_claim_ids),
        "origin": _clean_text(raw.get("origin")) or origin,
    }


def _normalize_contract(
    raw_value: Any,
    *,
    definitions: dict[str, dict[str, Any]],
    work_id: str,
    fallback_claim_ids: Iterable[str] = (),
    origin: str,
) -> dict[str, Any] | None:
    if not isinstance(raw_value, Mapping):
        return None
    raw = copy.deepcopy(dict(raw_value))
    advantage_id = _advantage_reference(
        raw,
        definitions,
        work_id=work_id,
        origin=origin,
        fallback_claim_ids=fallback_claim_ids,
    )
    kind = _clean_text(
        raw.get("contract_kind") or raw.get("kind") or raw.get("name")
    )
    if not advantage_id or not kind:
        return None
    parties = _string_list(raw.get("parties") or raw.get("party_ids"))
    contract_id = _clean_text(raw.get("contract_id")) or stable_id(
        "advcontract",
        advantage_id,
        kind,
        parties,
    )
    return {
        **raw,
        "contract_id": contract_id,
        "advantage_id": advantage_id,
        "contract_kind": kind,
        "parties": parties,
        "terms": copy.deepcopy(raw.get("terms") or []),
        "agency": copy.deepcopy(raw.get("agency") or {}),
        "trust": copy.deepcopy(raw.get("trust") or {}),
        "debt": copy.deepcopy(raw.get("debt") or {}),
        "breach_effect": copy.deepcopy(raw.get("breach_effect") or []),
        "status": _status(raw),
        "source_claim_ids": _source_claim_ids(raw, fallback_claim_ids),
        "origin": _clean_text(raw.get("origin")) or origin,
    }


def _normalize_narrative_contract(
    raw_value: Any,
    *,
    definitions: dict[str, dict[str, Any]],
    work_id: str,
    fallback_claim_ids: Iterable[str] = (),
    origin: str,
) -> dict[str, Any] | None:
    if not isinstance(raw_value, Mapping):
        return None
    raw = copy.deepcopy(dict(raw_value))
    advantage_id = _advantage_reference(
        raw,
        definitions,
        work_id=work_id,
        origin=origin,
        fallback_claim_ids=fallback_claim_ids,
    )
    reading_promise = _clean_text(
        raw.get("reading_promise") or raw.get("promise")
    )
    if not advantage_id or not reading_promise:
        return None
    narrative_contract_id = _clean_text(
        raw.get("narrative_contract_id")
    ) or stable_id(
        "advnarr",
        advantage_id,
        reading_promise,
    )
    return {
        **raw,
        "narrative_contract_id": narrative_contract_id,
        "advantage_id": advantage_id,
        "reading_promise": reading_promise,
        "reward_loop": copy.deepcopy(raw.get("reward_loop") or []),
        "risk_loop": copy.deepcopy(raw.get("risk_loop") or []),
        "reveal_ladder": copy.deepcopy(raw.get("reveal_ladder") or []),
        "experience_binding": copy.deepcopy(
            raw.get("experience_binding") or {}
        ),
        "status": _status(raw),
        "source_claim_ids": _source_claim_ids(raw, fallback_claim_ids),
        "origin": _clean_text(raw.get("origin")) or origin,
    }


_NORMALIZERS = {
    "anchors": _normalize_anchor,
    "modules": _normalize_module,
    "runtime_slots": _normalize_runtime_slot,
    "runtime_bootstrap": _normalize_runtime,
    "ledger_bootstrap": _normalize_ledger,
    "knowledge": _normalize_knowledge,
    "contracts": _normalize_contract,
    "narrative_contracts": _normalize_narrative_contract,
}


def build_advantage_package(
    dossier: Mapping[str, Any],
    claims: Iterable[Mapping[str, Any]] = (),
    *,
    work_id: str,
    source_initialization_schema_version: str,
    source_snapshot_hash: str,
) -> dict[str, Any]:
    """Normalize explicit Advantage data into a deterministic sidecar package."""

    raw_dossier = copy.deepcopy(dict(dossier or {}))
    claim_values = [
        copy.deepcopy(dict(claim))
        for claim in claims
        if isinstance(claim, Mapping)
    ]
    definitions: dict[str, dict[str, Any]] = {}
    collections: dict[str, dict[str, dict[str, Any]]] = {
        field: {} for field in ADVANTAGE_PACKAGE_ARRAY_FIELDS
    }
    collections["definitions"] = definitions

    for raw in _raw_records(
        raw_dossier,
        "advantages",
        "advantage_definitions",
        "definitions",
    ):
        record = _normalize_definition(
            raw,
            work_id=work_id,
            origin="user_input",
        )
        if record is not None:
            _merge_unique(
                definitions,
                record,
                id_field="advantage_id",
            )

    definition_claims = [
        claim
        for claim in claim_values
        if _clean_text(claim.get("predicate")) == "advantage.definition"
    ]
    for claim in definition_claims:
        value = claim.get("object_or_value")
        if isinstance(value, Mapping):
            raw = copy.deepcopy(dict(value))
        else:
            raw = {"title": value or claim.get("subject")}
        raw.setdefault("advantage_name", claim.get("subject"))
        record = _normalize_definition(
            raw,
            work_id=work_id,
            fallback_claim_ids=[_clean_text(claim.get("claim_id"))],
            origin="source_extract",
        )
        if record is not None:
            _merge_unique(
                definitions,
                record,
                id_field="advantage_id",
            )

    dossier_keys = {
        "anchors": ("advantage_anchors", "anchors"),
        "modules": ("advantage_modules", "modules"),
        "runtime_slots": ("runtime_slots", "advantage_runtime_slots"),
        "runtime_bootstrap": ("advantage_runtime", "runtime_bootstrap"),
        "ledger_bootstrap": ("advantage_ledger", "ledger_bootstrap"),
        "knowledge": ("advantage_knowledge", "knowledge"),
        "contracts": ("advantage_contracts", "contracts"),
        "narrative_contracts": (
            "advantage_narrative_contracts",
            "narrative_contracts",
        ),
    }
    for collection, keys in dossier_keys.items():
        normalizer = _NORMALIZERS[collection]
        id_field = _COLLECTION_ID_FIELDS[collection]
        for raw in _raw_records(raw_dossier, *keys):
            record = normalizer(
                raw,
                definitions=definitions,
                work_id=work_id,
                origin="user_input",
            )
            if record is not None:
                _merge_unique(
                    collections[collection],
                    record,
                    id_field=id_field,
                )

    for claim in claim_values:
        predicate = _clean_text(claim.get("predicate"))
        collection = _CLAIM_PREDICATE_TO_COLLECTION.get(predicate)
        if not collection or collection == "definitions":
            continue
        value = claim.get("object_or_value")
        if isinstance(value, Mapping):
            raw = copy.deepcopy(dict(value))
        else:
            raw = {
                "advantage_name": claim.get("subject"),
                "claim": value,
            }
        raw.setdefault("advantage_name", claim.get("subject"))
        normalizer = _NORMALIZERS[collection]
        record = normalizer(
            raw,
            definitions=definitions,
            work_id=work_id,
            fallback_claim_ids=[_clean_text(claim.get("claim_id"))],
            origin="source_extract",
        )
        if record is not None:
            _merge_unique(
                collections[collection],
                record,
                id_field=_COLLECTION_ID_FIELDS[collection],
            )

    all_claim_ids = sorted(
        {
            _clean_text(claim.get("claim_id"))
            for claim in claim_values
            if _clean_text(claim.get("claim_id"))
            and _clean_text(claim.get("predicate")).startswith("advantage.")
        }
    )
    package: dict[str, Any] = {
        "schema_version": ADVANTAGE_SCHEMA_VERSION,
        "work_id": _clean_text(work_id),
        "source_initialization_schema_version": _clean_text(
            source_initialization_schema_version
        ),
        "source_snapshot_hash": _clean_text(source_snapshot_hash),
        **{
            field: sorted(
                values.values(),
                key=lambda item, id_field=_COLLECTION_ID_FIELDS[field]: str(
                    item[id_field]
                ),
            )
            for field, values in collections.items()
        },
        "provenance": {
            "source_claim_ids": all_claim_ids,
            "extractor": "plot-init-advantage-sidecar-v1",
            "profile_registry_schema_version": (
                PROFILE_REGISTRY_SCHEMA_VERSION
            ),
            "profile_registry_hash": advantage_profile_registry_hash(),
            "rules": {
                "stable_ids_are_local_deterministic": True,
                "future_states_are_not_promoted": True,
                "knowledge_planes_are_preserved": True,
                "cross_projection_objects_are_references": True,
                "remote_candidates_never_own_runtime_math": True,
            },
        },
    }
    package["package_hash"] = recompute_advantage_package_hash(package)
    validate_advantage_package(package)
    return package


def _validate_id_collection(
    package: Mapping[str, Any],
    collection: str,
    id_field: str,
) -> set[str]:
    values = package.get(collection)
    if not isinstance(values, list):
        raise PlotInitError(
            "ADVANTAGE_PACKAGE_STRUCTURE_INVALID",
            f"advantage sidecar field must be an array: {collection}",
            field=collection,
        )
    seen: set[str] = set()
    for index, record in enumerate(values):
        if not isinstance(record, Mapping):
            raise PlotInitError(
                "ADVANTAGE_PACKAGE_STRUCTURE_INVALID",
                "advantage sidecar records must be objects",
                field=collection,
                index=index,
            )
        record_id = _clean_text(record.get(id_field))
        if not record_id or record_id in seen:
            raise PlotInitError(
                "ADVANTAGE_PACKAGE_ID_INVALID",
                "advantage sidecar ids must be present and unique per collection",
                field=id_field,
                record_id=record_id,
            )
        seen.add(record_id)
        status = _clean_text(record.get("status"))
        if status not in ADVANTAGE_STATUSES:
            raise PlotInitError(
                "ADVANTAGE_STATUS_INVALID",
                "advantage record status is unsupported",
                collection=collection,
                record_id=record_id,
                status=status,
            )
        if not isinstance(record.get("source_claim_ids"), list):
            raise PlotInitError(
                "ADVANTAGE_SOURCE_CLAIMS_INVALID",
                "advantage record source_claim_ids must be an array",
                collection=collection,
                record_id=record_id,
            )
        if not _clean_text(record.get("origin")):
            raise PlotInitError(
                "ADVANTAGE_RECORD_ORIGIN_INVALID",
                "advantage record origin must be non-empty",
                collection=collection,
                record_id=record_id,
            )
    return seen


def validate_advantage_package(
    package: Mapping[str, Any],
) -> dict[str, Any]:
    value = copy.deepcopy(dict(package))
    _validate_advantage_json_schema_contract(value)
    unexpected = sorted(set(value) - _TOP_LEVEL_FIELDS)
    if unexpected:
        raise PlotInitError(
            "ADVANTAGE_PACKAGE_STRUCTURE_INVALID",
            "advantage sidecar contains unsupported top-level fields",
            fields=unexpected,
        )
    if value.get("schema_version") != ADVANTAGE_SCHEMA_VERSION:
        raise PlotInitError(
            "ADVANTAGE_PACKAGE_SCHEMA_MISMATCH",
            "advantage sidecar uses an unsupported schema",
            expected=ADVANTAGE_SCHEMA_VERSION,
            actual=value.get("schema_version"),
        )
    if not _clean_text(value.get("work_id")):
        raise PlotInitError(
            "ADVANTAGE_PACKAGE_STRUCTURE_INVALID",
            "advantage sidecar work_id must be non-empty",
            field="work_id",
        )
    if value.get("source_initialization_schema_version") not in {
        "plot-rag-init/v1",
        "plot-rag-init/v2",
    }:
        raise PlotInitError(
            "ADVANTAGE_PACKAGE_STRUCTURE_INVALID",
            "advantage sidecar source initialization schema is unsupported",
            field="source_initialization_schema_version",
        )
    if not re.fullmatch(
        r"[a-f0-9]{64}",
        _clean_text(value.get("source_snapshot_hash")),
    ):
        raise PlotInitError(
            "ADVANTAGE_PACKAGE_STRUCTURE_INVALID",
            "advantage source_snapshot_hash must be lowercase SHA-256",
            field="source_snapshot_hash",
        )
    ids = {
        collection: _validate_id_collection(value, collection, id_field)
        for collection, id_field in _COLLECTION_ID_FIELDS.items()
    }
    advantage_ids = ids["definitions"]
    module_ids = ids["modules"]
    anchor_ids = ids["anchors"]
    for record in value["definitions"]:
        profiles = _string_list(record.get("profiles"))
        if not profiles or any(
            profile not in ADVANTAGE_PROFILES for profile in profiles
        ):
            raise PlotInitError(
                "ADVANTAGE_PROFILE_INVALID",
                "advantage definition profiles are incomplete or unsupported",
                advantage_id=record["advantage_id"],
                profiles=profiles,
            )
        anchor_type = _clean_text(record.get("anchor_type"))
        if anchor_type not in ADVANTAGE_ANCHOR_TYPES:
            raise PlotInitError(
                "ADVANTAGE_ANCHOR_TYPE_INVALID",
                "advantage definition anchor type is unsupported",
                advantage_id=record["advantage_id"],
                anchor_type=anchor_type,
            )
    for collection in ADVANTAGE_PACKAGE_ARRAY_FIELDS:
        if collection == "definitions":
            continue
        for record in value[collection]:
            advantage_id = _clean_text(record.get("advantage_id"))
            if advantage_id not in advantage_ids:
                raise PlotInitError(
                    "ADVANTAGE_DEFINITION_REFERENCE_INVALID",
                    "advantage record references a missing definition",
                    collection=collection,
                    advantage_id=advantage_id,
                )
    for record in value["anchors"]:
        if _clean_text(record.get("anchor_type")) not in ADVANTAGE_ANCHOR_TYPES:
            raise PlotInitError(
                "ADVANTAGE_ANCHOR_TYPE_INVALID",
                "advantage anchor type is unsupported",
                anchor_id=record["anchor_id"],
            )
        if not _clean_text(record.get("anchor_ref_id")):
            raise PlotInitError(
                "ADVANTAGE_ANCHOR_REFERENCE_REQUIRED",
                "advantage anchor external reference is required",
                anchor_id=record["anchor_id"],
            )
        if _clean_text(record.get("binding_state")) not in (
            ANCHOR_BINDING_STATES
        ):
            raise PlotInitError(
                "ADVANTAGE_ANCHOR_STATE_INVALID",
                "advantage anchor binding state is unsupported",
                anchor_id=record["anchor_id"],
            )
    for record in value["modules"]:
        if _clean_text(record.get("module_status")) not in {
            "locked",
            "available",
            "enabled",
            "suppressed",
            "deprecated",
            "superseded",
        }:
            raise PlotInitError(
                "ADVANTAGE_MODULE_STATUS_INVALID",
                "advantage module runtime status is unsupported",
                module_id=record["module_id"],
            )
        profile = _clean_text(record.get("profile"))
        if profile:
            definition = next(
                item
                for item in value["definitions"]
                if item["advantage_id"] == record["advantage_id"]
            )
            if profile not in definition["profiles"]:
                raise PlotInitError(
                    "ADVANTAGE_MODULE_PROFILE_INVALID",
                    "advantage module profile is not declared",
                    module_id=record["module_id"],
                )
        for anchor_id in _string_list(record.get("anchor_ids")):
            if anchor_id not in anchor_ids:
                raise PlotInitError(
                    "ADVANTAGE_MODULE_ANCHOR_INVALID",
                    "advantage module references a missing anchor",
                    module_id=record["module_id"],
                    anchor_id=anchor_id,
                )
    for record in value["runtime_bootstrap"]:
        unlocked = _string_list(record.get("unlocked_modules"))
        if any(module_id not in module_ids for module_id in unlocked):
            raise PlotInitError(
                "ADVANTAGE_RUNTIME_MODULE_INVALID",
                "advantage runtime references a missing unlocked module",
                runtime_id=record["runtime_id"],
            )
        if not isinstance(record.get("resources"), Mapping):
            raise PlotInitError(
                "ADVANTAGE_RUNTIME_RESOURCES_INVALID",
                "advantage runtime resources must be an object",
                runtime_id=record["runtime_id"],
            )
        for resource, amount in record["resources"].items():
            if (
                not _clean_text(resource)
                or isinstance(amount, bool)
                or not isinstance(amount, (int, float))
                or not math.isfinite(float(amount))
                or float(amount) < 0
            ):
                raise PlotInitError(
                    "ADVANTAGE_RUNTIME_RESOURCES_INVALID",
                    "advantage runtime resources require finite non-negative numeric values",
                    runtime_id=record["runtime_id"],
                    resource=resource,
                )
        for field in ("charges", "max_charges"):
            amount = record.get(field)
            if amount is not None and (
                isinstance(amount, bool)
                or not isinstance(amount, (int, float))
                or not math.isfinite(float(amount))
                or float(amount) < 0
            ):
                raise PlotInitError(
                    "ADVANTAGE_RUNTIME_NUMBER_INVALID",
                    "advantage charges must be null or finite non-negative numbers",
                    runtime_id=record["runtime_id"],
                    field=field,
                )
        if (
            record.get("charges") is not None
            and record.get("max_charges") is not None
            and float(record["charges"]) > float(record["max_charges"])
        ):
            raise PlotInitError(
                "ADVANTAGE_RUNTIME_CHARGES_INVALID",
                "advantage runtime charges cannot exceed max_charges",
                runtime_id=record["runtime_id"],
            )
        cooldown = record.get("cooldown_until")
        if cooldown is not None:
            if (
                not isinstance(cooldown, Mapping)
                or not _clean_text(cooldown.get("calendar_id"))
                or isinstance(cooldown.get("ordinal"), bool)
                or not isinstance(cooldown.get("ordinal"), int)
            ):
                raise PlotInitError(
                    "ADVANTAGE_RUNTIME_COOLDOWN_INVALID",
                    "cooldown_until must be null or a story coordinate",
                    runtime_id=record["runtime_id"],
                )
        for field in ("pollution", "exposure", "debt"):
            amount = record.get(field)
            if (
                isinstance(amount, bool)
                or not isinstance(amount, (int, float))
                or not math.isfinite(float(amount))
                or float(amount) < 0
            ):
                raise PlotInitError(
                    "ADVANTAGE_RUNTIME_NUMBER_INVALID",
                    "advantage runtime counters must be finite non-negative numbers",
                    runtime_id=record["runtime_id"],
                    field=field,
                )
    for record in value["knowledge"]:
        plane = _clean_text(record.get("knowledge_plane"))
        if plane not in KNOWLEDGE_PLANES:
            raise PlotInitError(
                "ADVANTAGE_KNOWLEDGE_PLANE_INVALID",
                "advantage knowledge plane is unsupported",
                knowledge_id=record["knowledge_id"],
                knowledge_plane=plane,
            )
        confidence = record.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
        ):
            raise PlotInitError(
                "ADVANTAGE_KNOWLEDGE_CONFIDENCE_INVALID",
                "advantage knowledge confidence must be finite in [0,1]",
                knowledge_id=record["knowledge_id"],
            )
        module_id = _clean_text(record.get("module_id"))
        if module_id and module_id not in module_ids:
            raise PlotInitError(
                "ADVANTAGE_KNOWLEDGE_MODULE_INVALID",
                "advantage knowledge references a missing module",
                knowledge_id=record["knowledge_id"],
            )
        if record["status"] == "misread" and not _clean_text(
            record.get("misread_of")
        ):
            raise PlotInitError(
                "ADVANTAGE_MISREAD_REFERENCE_REQUIRED",
                "misread knowledge must identify the corrected claim",
                knowledge_id=record["knowledge_id"],
            )
    if not isinstance(value.get("provenance"), Mapping):
        raise PlotInitError(
            "ADVANTAGE_PACKAGE_PROVENANCE_INVALID",
            "advantage sidecar provenance must be an object",
        )
    provenance = value["provenance"]
    if provenance.get("profile_registry_schema_version") != (
        PROFILE_REGISTRY_SCHEMA_VERSION
    ):
        raise PlotInitError(
            "ADVANTAGE_PACKAGE_PROVENANCE_INVALID",
            "advantage sidecar profile registry version is unsupported",
        )
    if provenance.get("profile_registry_hash") != (
        advantage_profile_registry_hash()
    ):
        raise PlotInitError(
            "ADVANTAGE_PROFILE_REGISTRY_HASH_MISMATCH",
            "advantage sidecar was built against a different profile registry",
        )
    rules = provenance.get("rules")
    required_rules = {
        "stable_ids_are_local_deterministic",
        "future_states_are_not_promoted",
        "knowledge_planes_are_preserved",
        "cross_projection_objects_are_references",
        "remote_candidates_never_own_runtime_math",
    }
    if (
        not isinstance(rules, Mapping)
        or set(rules) != required_rules
        or any(flag is not True for flag in rules.values())
    ):
        raise PlotInitError(
            "ADVANTAGE_PACKAGE_PROVENANCE_INVALID",
            "advantage sidecar conservative guarantees are incomplete",
        )
    expected = _clean_text(value.get("package_hash"))
    actual = recompute_advantage_package_hash(value)
    if not expected or expected != actual:
        raise PlotInitError(
            "ADVANTAGE_PACKAGE_HASH_MISMATCH",
            "advantage sidecar package hash does not match its content",
            expected=expected,
            actual=actual,
        )
    return value


def render_advantage_package(package: Mapping[str, Any]) -> str:
    value = validate_advantage_package(package)
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def advantage_package_has_typed_content(
    package: Mapping[str, Any],
) -> bool:
    return bool(validate_advantage_package(package)["definitions"])


def build_advantage_sidecar_artifact(
    package: Mapping[str, Any],
    project_root: Path | None,
    *,
    relative_path: str = ADVANTAGE_SIDECAR_PATH,
) -> dict[str, Any]:
    value = validate_advantage_package(package)
    content = render_advantage_package(value)
    proposed_hash = sha256_bytes(content.encode("utf-8"))
    expected_old_hash: str | None = None
    existing = ""
    target_exists = False
    if project_root is not None:
        root = project_root.resolve(strict=False)
        target = (root / Path(relative_path)).resolve(strict=False)
        if not path_is_within(target, root):
            raise PlotInitError(
                "UNSAFE_TARGET_PATH",
                "advantage sidecar target escapes project root",
                path=relative_path,
            )
        if target.is_file():
            raw = target.read_bytes()
            target_exists = True
            expected_old_hash = sha256_bytes(raw)
            try:
                existing = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                existing = ""
    operation = (
        "create"
        if not target_exists
        else "noop"
        if expected_old_hash == proposed_hash
        else "update"
    )
    diff = ""
    if operation != "noop":
        diff = "".join(
            difflib.unified_diff(
                existing.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{relative_path}",
                tofile=f"b/{relative_path}",
            )
        )
    return {
        "artifact_id": stable_id("artifact", relative_path, proposed_hash),
        "path": relative_path,
        "logical_owner": ADVANTAGE_SIDECAR_OWNER,
        "operation": operation,
        "expected_old_hash": expected_old_hash,
        "proposed_new_hash": proposed_hash,
        "proposed_content": content,
        "unified_diff": diff,
        "materialized": False,
        "advantage_package_hash": value["package_hash"],
        "advantage_schema_version": ADVANTAGE_SCHEMA_VERSION,
    }


def advantage_sidecar_reference(
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    if _clean_text(artifact.get("logical_owner")) != ADVANTAGE_SIDECAR_OWNER:
        raise PlotInitError(
            "ADVANTAGE_SIDECAR_ARTIFACT_INVALID",
            "artifact is not the initialization Advantage sidecar",
        )
    content = str(artifact.get("proposed_content") or "")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise PlotInitError(
            "ADVANTAGE_SIDECAR_ARTIFACT_INVALID",
            "advantage sidecar artifact content is not valid JSON",
        ) from exc
    package = validate_advantage_package(payload)
    actual_content_hash = sha256_bytes(content.encode("utf-8"))
    expected_content_hash = _clean_text(artifact.get("proposed_new_hash"))
    if expected_content_hash != actual_content_hash:
        raise PlotInitError(
            "ADVANTAGE_SIDECAR_CONTENT_HASH_MISMATCH",
            "advantage sidecar bytes differ from the frozen hash",
            expected=expected_content_hash,
            actual=actual_content_hash,
        )
    artifact_package_hash = _clean_text(
        artifact.get("advantage_package_hash")
    )
    if artifact_package_hash and artifact_package_hash != package["package_hash"]:
        raise PlotInitError(
            "ADVANTAGE_SIDECAR_PACKAGE_HASH_MISMATCH",
            "advantage artifact package hash differs from its content",
        )
    return {
        "schema_version": ADVANTAGE_SCHEMA_VERSION,
        "path": _clean_text(artifact.get("path")),
        "artifact_id": _clean_text(artifact.get("artifact_id")),
        "package_hash": package["package_hash"],
        "content_hash": expected_content_hash,
    }


def advantage_package_from_artifact_manifest(
    artifact_manifest: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    candidates = [
        copy.deepcopy(dict(item))
        for item in artifact_manifest
        if isinstance(item, Mapping)
        and (
            _clean_text(item.get("logical_owner"))
            == ADVANTAGE_SIDECAR_OWNER
            or _clean_text(item.get("path")) == ADVANTAGE_SIDECAR_PATH
        )
    ]
    if not candidates:
        return None
    if len(candidates) != 1:
        raise PlotInitError(
            "ADVANTAGE_SIDECAR_DUPLICATE",
            "initialization artifact manifest contains multiple Advantage sidecars",
        )
    artifact = candidates[0]
    reference = advantage_sidecar_reference(artifact)
    payload = json.loads(str(artifact.get("proposed_content") or ""))
    return validate_advantage_package(payload), reference


def advantage_package_from_frozen_proposal(
    frozen_proposal: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    bundle = frozen_proposal.get("bundle")
    if not isinstance(bundle, Mapping):
        raise PlotInitError(
            "INVALID_INITIALIZATION_PROPOSAL",
            "Advantage sidecar verification requires an initialization bundle",
        )
    loaded = advantage_package_from_artifact_manifest(
        item
        for item in bundle.get("artifact_manifest") or []
        if isinstance(item, Mapping)
    )
    if loaded is None:
        raise PlotInitError(
            "ADVANTAGE_SIDECAR_MISSING",
            "frozen initialization proposal has no Advantage sidecar",
        )
    package, actual_reference = loaded
    apply_plan = frozen_proposal.get("apply_plan")
    apply_plan = apply_plan if isinstance(apply_plan, Mapping) else {}
    expected_reference = apply_plan.get("advantage_sidecar")
    if not isinstance(expected_reference, Mapping):
        expected_reference = (bundle.get("meta") or {}).get(
            "advantage_sidecar"
        )
    if not isinstance(expected_reference, Mapping):
        raise PlotInitError(
            "ADVANTAGE_SIDECAR_REFERENCE_MISSING",
            "initialization proposal must bind the Advantage sidecar hash",
        )
    comparable_fields = (
        "schema_version",
        "path",
        "artifact_id",
        "package_hash",
        "content_hash",
    )
    expected = {
        key: _clean_text(expected_reference.get(key))
        for key in comparable_fields
    }
    actual = {
        key: _clean_text(actual_reference.get(key))
        for key in comparable_fields
    }
    if expected != actual:
        raise PlotInitError(
            "ADVANTAGE_SIDECAR_REFERENCE_MISMATCH",
            "initialization Advantage reference changed after freeze",
            expected=expected,
            actual=actual,
        )
    return package, actual_reference


def assert_advantage_sidecar_target_baseline(
    frozen_proposal: Mapping[str, Any],
    project_root: Path | str,
) -> dict[str, Any]:
    _package, reference = advantage_package_from_frozen_proposal(
        frozen_proposal
    )
    bundle = frozen_proposal["bundle"]
    artifact = next(
        item
        for item in bundle.get("artifact_manifest") or []
        if isinstance(item, Mapping)
        and _clean_text(item.get("artifact_id")) == reference["artifact_id"]
    )
    root = Path(project_root).expanduser().resolve(strict=False)
    target = (root / Path(reference["path"])).resolve(strict=False)
    if not path_is_within(target, root):
        raise PlotInitError(
            "UNSAFE_TARGET_PATH",
            "advantage sidecar target escapes project root",
            path=reference["path"],
        )
    current_hash = sha256_bytes(target.read_bytes()) if target.is_file() else None
    expected = artifact.get("expected_old_hash")
    if current_hash != expected:
        raise PlotInitError(
            "ADVANTAGE_SIDECAR_TARGET_DRIFT",
            "advantage sidecar target changed after proposal review",
            path=reference["path"],
            expected=expected,
            actual=current_hash,
        )
    return {
        "status": "current",
        "path": reference["path"],
        "expected_old_hash": expected,
        "actual_hash": current_hash,
    }


def verify_materialized_advantage_sidecar(
    package_or_artifact: Mapping[str, Any],
    project_root: Path | str,
) -> dict[str, Any]:
    """Verify exact materialized bytes for a package or frozen artifact."""

    if isinstance(package_or_artifact.get("bundle"), Mapping):
        package, reference = advantage_package_from_frozen_proposal(
            package_or_artifact
        )
        artifact = next(
            item
            for item in package_or_artifact["bundle"].get(
                "artifact_manifest"
            )
            or []
            if isinstance(item, Mapping)
            and _clean_text(item.get("artifact_id"))
            == reference["artifact_id"]
        )
    elif "proposed_content" in package_or_artifact:
        artifact = copy.deepcopy(dict(package_or_artifact))
        reference = advantage_sidecar_reference(artifact)
        package = json.loads(str(artifact["proposed_content"]))
    else:
        package = validate_advantage_package(package_or_artifact)
        artifact = build_advantage_sidecar_artifact(package, None)
        reference = advantage_sidecar_reference(artifact)
    root = Path(project_root).expanduser().resolve(strict=False)
    target = (root / Path(reference["path"])).resolve(strict=False)
    if not path_is_within(target, root) or target.is_symlink():
        raise PlotInitError(
            "UNSAFE_TARGET_PATH",
            "materialized advantage sidecar is outside the project or a symlink",
            path=reference["path"],
        )
    if not target.is_file():
        raise PlotInitError(
            "ADVANTAGE_SIDECAR_NOT_MATERIALIZED",
            "materialized advantage sidecar file is missing",
            path=reference["path"],
        )
    raw = target.read_bytes()
    actual_content_hash = sha256_bytes(raw)
    if actual_content_hash != reference["content_hash"]:
        raise PlotInitError(
            "ADVANTAGE_SIDECAR_MATERIALIZED_HASH_MISMATCH",
            "materialized advantage sidecar bytes differ from approved bytes",
            expected=reference["content_hash"],
            actual=actual_content_hash,
        )
    materialized = json.loads(raw.decode("utf-8"))
    verified = validate_advantage_package(materialized)
    if verified["package_hash"] != package["package_hash"]:
        raise PlotInitError(
            "ADVANTAGE_SIDECAR_MATERIALIZED_PACKAGE_MISMATCH",
            "materialized Advantage package differs from the frozen package",
        )
    return {
        "status": "verified",
        "path": reference["path"],
        "artifact_id": reference["artifact_id"],
        "package_hash": verified["package_hash"],
        "content_hash": actual_content_hash,
    }



__all__ = [
    "ADVANTAGE_DOSSIER_KEYS",
    "ADVANTAGE_PACKAGE_ARRAY_FIELDS",
    "ADVANTAGE_SCHEMA_VERSION",
    "ADVANTAGE_SIDECAR_OWNER",
    "ADVANTAGE_SIDECAR_PATH",
    "ADVANTAGE_STATUSES",
    "KNOWLEDGE_PLANES",
    "advantage_package_has_typed_content",
    "advantage_package_from_artifact_manifest",
    "advantage_package_from_frozen_proposal",
    "advantage_sidecar_reference",
    "assert_advantage_sidecar_target_baseline",
    "build_advantage_package",
    "build_advantage_sidecar_artifact",
    "recompute_advantage_package_hash",
    "render_advantage_package",
    "validate_advantage_package",
    "verify_materialized_advantage_sidecar",
]
