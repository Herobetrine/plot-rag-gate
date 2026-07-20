"""Deterministic ``plot-rag-item/v1`` initialization sidecars.

Initialization bundle v1/v2 are strict, frozen protocols.  Item data therefore
stays outside those roots and is materialized as a hash-bound sidecar artifact.
This module deliberately performs only evidence-preserving normalization:

* a name plus holder remains a legacy inventory reference;
* functions require an explicit function declaration;
* a one-off effect remains an observation;
* missing uniqueness remains ``unknown``;
* legacy attributes are copied without interpretation.
"""

from __future__ import annotations

import copy
import difflib
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .canonical import (
    canonical_hash,
    canonical_json,
    path_is_within,
    sha256_bytes,
    stable_id,
)
from .errors import PlotInitError


ITEM_SCHEMA_VERSION = "plot-rag-item/v1"
ITEM_SIDECAR_PATH = ".plot-rag/items.v1.json"
ITEM_SIDECAR_OWNER = "item_sidecar"

STACK_POLICIES = frozenset(
    {"non_stackable", "homogeneous", "lot", "unknown"}
)
UNIQUENESS_POLICIES = frozenset(
    {"ordinary", "unique_instance", "unique_definition", "unknown"}
)
ACTIVATION_KINDS = frozenset(
    {"active", "passive", "toggle", "reaction", "triggered"}
)
EFFECT_OWNERS = frozenset({"inline", "ability_bridge"})
CUSTODY_STATUSES = frozenset(
    {
        "possessed",
        "stored",
        "loaned",
        "seized",
        "lost",
        "abandoned",
        "in_transit",
        "destroyed",
        "unknown",
    }
)
OBSERVATION_ACTIONS = frozenset(
    {"observe", "reveal", "claim", "misidentify", "correct"}
)
KNOWLEDGE_PLANES = frozenset(
    {
        "objective",
        "actor_belief",
        "public_narrative",
        "reader_disclosed",
        "author_plan",
    }
)
FUNCTION_UNLOCK_STATES = frozenset({"locked", "unlocked", "suppressed"})

ITEM_PACKAGE_ARRAY_FIELDS = (
    "item_definitions",
    "item_instances",
    "item_stacks",
    "item_functions",
    "item_function_bindings",
    "item_custody_bootstrap",
    "item_runtime_bootstrap",
    "item_function_runtime_bootstrap",
    "item_observations",
    "legacy_inventory",
)

ITEM_DOSSIER_KEYS = (
    "items",
    "item_definitions",
    "item_instances",
    "item_stacks",
    "item_functions",
    "item_function_definitions",
    "item_function_bindings",
    "item_custody_bootstrap",
    "item_runtime_bootstrap",
    "item_function_runtime_bootstrap",
    "item_observations",
)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "work_id",
        "source_initialization_schema_version",
        "source_snapshot_hash",
        *ITEM_PACKAGE_ARRAY_FIELDS,
        "provenance",
        "package_hash",
    }
)


def recompute_item_package_hash(package: Mapping[str, Any]) -> str:
    """Hash the complete semantic package, excluding only its own hash field."""

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


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {"legacy_raw": value}
        if isinstance(decoded, dict):
            return decoded
        return {"legacy_value": decoded}
    return {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        values = list(value)
    else:
        values = []
    return sorted({_clean_text(item) for item in values if _clean_text(item)})


def _source_claim_ids(raw: Mapping[str, Any], fallback: Iterable[str] = ()) -> list[str]:
    values = raw.get("source_claim_ids")
    if values is None:
        values = raw.get("evidence_claim_ids")
    return sorted(
        {
            *[_clean_text(item) for item in _as_records(values) if _clean_text(item)],
            *[_clean_text(item) for item in fallback if _clean_text(item)],
        }
    )


def _entity_id(entity_type: str, name: Any) -> str | None:
    cleaned = _clean_text(name)
    if not cleaned:
        return None
    return stable_id("ent", entity_type, cleaned.casefold())


def _unknown_unique(value: Any) -> bool | str:
    if isinstance(value, bool):
        return value
    if value in {0, 0.0}:
        return False
    if value in {1, 1.0}:
        return True
    text = _clean_text(value).casefold()
    if text in {"true", "yes", "unique", "唯一", "是"}:
        return True
    if text in {"false", "no", "ordinary", "非唯一", "否"}:
        return False
    return "unknown"


def _stack_policy(value: Any) -> str:
    text = _clean_text(value).casefold().replace("-", "_")
    aliases = {
        "nonstackable": "non_stackable",
        "non_stackable": "non_stackable",
        "不可堆叠": "non_stackable",
        "homogeneous": "homogeneous",
        "stackable": "homogeneous",
        "可堆叠": "homogeneous",
        "lot": "lot",
        "batch": "lot",
        "批次": "lot",
    }
    return aliases.get(text, text if text in set(aliases.values()) else "unknown")


def _uniqueness_policy(value: Any) -> str:
    if isinstance(value, bool):
        return "unique_instance" if value else "ordinary"
    text = _clean_text(value).casefold().replace("-", "_")
    aliases = {
        "ordinary": "ordinary",
        "普通": "ordinary",
        "unique": "unique_instance",
        "unique_instance": "unique_instance",
        "唯一实例": "unique_instance",
        "unique_definition": "unique_definition",
        "唯一定义": "unique_definition",
    }
    return aliases.get(text, "unknown")


def _normalized_enum(value: Any, *, default: str) -> str:
    text = _clean_text(value).casefold().replace("-", "_")
    return text or default


def _validate_optional_nonnegative_number(
    value: Any,
    *,
    code: str,
    field: str,
    record_id: str,
) -> float | int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise PlotInitError(
            code,
            f"{field} must be a finite non-negative number",
            field=field,
            record_id=record_id,
            value=value,
        )
    return value


def _validate_optional_boolean(
    value: Any,
    *,
    code: str,
    field: str,
    record_id: str,
) -> bool | None:
    if value is None:
        return None
    if type(value) is not bool:
        raise PlotInitError(
            code,
            f"{field} must be a JSON boolean",
            field=field,
            record_id=record_id,
            value=value,
        )
    return value


def _validate_optional_coordinate(
    value: Any,
    *,
    code: str,
    field: str,
    record_id: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise PlotInitError(
            code,
            f"{field} must be a story-coordinate object",
            field=field,
            record_id=record_id,
        )
    calendar_id = _clean_text(value.get("calendar_id"))
    ordinal = value.get("ordinal")
    if not calendar_id or type(ordinal) is not int:
        raise PlotInitError(
            code,
            f"{field} requires calendar_id and an exact integer ordinal",
            field=field,
            record_id=record_id,
            calendar_id=calendar_id,
            ordinal=ordinal,
        )
    return copy.deepcopy(dict(value))


def _legacy_attributes(raw: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("legacy_attributes", "attributes_json", "attributes"):
        if key in raw:
            return _json_object(raw.get(key))
    return {}


def _merge_unique(
    records: dict[str, dict[str, Any]],
    record: dict[str, Any],
    *,
    id_field: str,
) -> None:
    record_id = _clean_text(record.get(id_field))
    if not record_id:
        raise PlotInitError(
            "ITEM_RECORD_ID_REQUIRED",
            f"item sidecar record requires {id_field}",
            field=id_field,
        )
    previous = records.get(record_id)
    if previous is None:
        records[record_id] = record
        return
    if canonical_json(previous) == canonical_json(record):
        return
    raise PlotInitError(
        "ITEM_RECORD_ID_CONFLICT",
        "item sidecar id is bound to different immutable content",
        field=id_field,
        record_id=record_id,
    )


def _normalize_definition(
    raw_value: Any,
    *,
    fallback_claim_ids: Iterable[str] = (),
    origin: str,
) -> dict[str, Any] | None:
    raw = (
        copy.deepcopy(dict(raw_value))
        if isinstance(raw_value, Mapping)
        else {"name": _clean_text(raw_value)}
    )
    name = _clean_text(
        raw.get("name")
        or raw.get("canonical_name")
        or raw.get("item_name")
        or raw.get("definition_name")
    )
    if not name:
        return None
    item_kind = _clean_text(raw.get("item_kind") or raw.get("kind")) or "unknown"
    stack_policy = _stack_policy(raw.get("stack_policy"))
    uniqueness = _uniqueness_policy(
        raw.get("uniqueness_policy", raw.get("unique"))
    )
    discriminator = (
        _clean_text(raw.get("definition_key"))
        or _clean_text(raw.get("type_key"))
        or _clean_text(raw.get("namespace"))
        or item_kind
    )
    definition_id = _clean_text(
        raw.get("item_definition_id") or raw.get("definition_id")
    ) or stable_id(
        "itemdef",
        name.casefold(),
        discriminator.casefold(),
        stack_policy,
    )
    legacy = _legacy_attributes(raw)
    return {
        **copy.deepcopy(raw),
        "item_definition_id": definition_id,
        "item_entity_id": _clean_text(raw.get("item_entity_id"))
        or _entity_id("item", name),
        "name": name,
        "item_kind": item_kind,
        "tags": _string_list(raw.get("tags")),
        "material": copy.deepcopy(raw.get("material")),
        "quality": copy.deepcopy(raw.get("quality")),
        "rarity": copy.deepcopy(raw.get("rarity")),
        "stack_policy": stack_policy,
        "uniqueness_policy": uniqueness,
        "default_functions": _string_list(raw.get("default_functions")),
        "attributes": _json_object(raw.get("attributes")),
        "legacy_attributes": legacy,
        "source_claim_ids": _source_claim_ids(raw, fallback_claim_ids),
        "knowledge_plane": _clean_text(raw.get("knowledge_plane")) or "objective",
        "origin": _clean_text(raw.get("origin")) or origin,
    }


def _definition_lookup(
    definitions: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for definition_id, record in definitions.items():
        name = _clean_text(record.get("name")).casefold()
        if name and name not in result:
            result[name] = definition_id
    return result


def _ensure_definition(
    definitions: dict[str, dict[str, Any]],
    *,
    definition_id: Any = None,
    definition_name: Any = None,
    item_kind: Any = None,
    origin: str,
    source_claim_ids: Iterable[str] = (),
) -> str | None:
    explicit_id = _clean_text(definition_id)
    if explicit_id and explicit_id in definitions:
        return explicit_id
    name = _clean_text(definition_name)
    if name:
        by_name = _definition_lookup(definitions)
        found = by_name.get(name.casefold())
        if found:
            return found
        record = _normalize_definition(
            {
                "item_definition_id": explicit_id or None,
                "name": name,
                "item_kind": item_kind or "unknown",
                "source_claim_ids": list(source_claim_ids),
            },
            origin=origin,
        )
        if record is not None:
            _merge_unique(
                definitions,
                record,
                id_field="item_definition_id",
            )
            return str(record["item_definition_id"])
    return explicit_id or None


def _normalize_instance(
    raw_value: Any,
    *,
    definitions: dict[str, dict[str, Any]],
    fallback_claim_ids: Iterable[str] = (),
    origin: str,
) -> dict[str, Any] | None:
    raw = (
        copy.deepcopy(dict(raw_value))
        if isinstance(raw_value, Mapping)
        else {"instance_name": _clean_text(raw_value)}
    )
    definition_name = _clean_text(
        raw.get("definition_name")
        or raw.get("item_name")
        or raw.get("type_name")
    )
    definition_id = _ensure_definition(
        definitions,
        definition_id=raw.get("item_definition_id") or raw.get("definition_id"),
        definition_name=definition_name,
        item_kind=raw.get("item_kind"),
        origin=origin,
        source_claim_ids=fallback_claim_ids,
    )
    instance_name = _clean_text(
        raw.get("instance_name")
        or raw.get("name")
        or raw.get("canonical_name")
    )
    if not instance_name or not definition_id:
        return None
    serial = _clean_text(raw.get("serial_or_mark") or raw.get("serial"))
    discriminator = (
        serial
        or _clean_text(raw.get("instance_key"))
        or canonical_hash(raw.get("provenance") or {})[:16]
    )
    instance_id = _clean_text(
        raw.get("item_instance_id") or raw.get("instance_id")
    ) or stable_id(
        "iteminst",
        definition_id,
        instance_name.casefold(),
        discriminator,
    )
    return {
        **copy.deepcopy(raw),
        "item_instance_id": instance_id,
        "item_definition_id": definition_id,
        "item_entity_id": _clean_text(raw.get("item_entity_id"))
        or _entity_id("item", instance_name),
        "instance_name": instance_name,
        "serial_or_mark": serial or None,
        "unique": _unknown_unique(raw.get("unique")),
        "provenance": copy.deepcopy(raw.get("provenance") or {}),
        "legacy_attributes": _legacy_attributes(raw),
        "source_claim_ids": _source_claim_ids(raw, fallback_claim_ids),
        "origin": _clean_text(raw.get("origin")) or origin,
    }


def _normalize_stack(
    raw_value: Any,
    *,
    definitions: dict[str, dict[str, Any]],
    fallback_claim_ids: Iterable[str] = (),
    origin: str,
) -> dict[str, Any] | None:
    raw = (
        copy.deepcopy(dict(raw_value))
        if isinstance(raw_value, Mapping)
        else {"stack_name": _clean_text(raw_value)}
    )
    definition_id = _ensure_definition(
        definitions,
        definition_id=raw.get("item_definition_id") or raw.get("definition_id"),
        definition_name=(
            raw.get("definition_name")
            or raw.get("item_name")
            or raw.get("type_name")
        ),
        item_kind=raw.get("item_kind"),
        origin=origin,
        source_claim_ids=fallback_claim_ids,
    )
    if not definition_id:
        return None
    quantity = raw.get("quantity")
    if isinstance(quantity, bool):
        quantity = None
    if quantity is not None:
        try:
            quantity = float(quantity)
        except (TypeError, ValueError):
            quantity = None
        else:
            if quantity.is_integer():
                quantity = int(quantity)
    batch = _json_object(
        raw.get("batch_properties")
        if "batch_properties" in raw
        else raw.get("batch")
    )
    quality_band = copy.deepcopy(raw.get("quality_band"))
    stack_id = _clean_text(raw.get("stack_id")) or stable_id(
        "itemstack",
        definition_id,
        _clean_text(raw.get("stack_name")),
        quality_band,
        batch,
    )
    return {
        **copy.deepcopy(raw),
        "stack_id": stack_id,
        "item_definition_id": definition_id,
        "stack_name": _clean_text(raw.get("stack_name") or raw.get("name")) or None,
        "quantity": quantity,
        "quality_band": quality_band,
        "batch_properties": batch,
        "provenance": copy.deepcopy(raw.get("provenance") or {}),
        "legacy_attributes": _legacy_attributes(raw),
        "source_claim_ids": _source_claim_ids(raw, fallback_claim_ids),
        "origin": _clean_text(raw.get("origin")) or origin,
    }


def _normalize_function(
    raw_value: Any,
    *,
    definitions: dict[str, dict[str, Any]],
    fallback_claim_ids: Iterable[str] = (),
    origin: str,
    item_name_hint: Any = None,
) -> dict[str, Any] | None:
    if not isinstance(raw_value, Mapping):
        return None
    raw = copy.deepcopy(dict(raw_value))
    definition_id = _ensure_definition(
        definitions,
        definition_id=raw.get("item_definition_id") or raw.get("definition_id"),
        definition_name=(
            raw.get("item_name")
            or raw.get("definition_name")
            or item_name_hint
        ),
        item_kind=raw.get("item_kind"),
        origin=origin,
        source_claim_ids=fallback_claim_ids,
    )
    name = _clean_text(
        raw.get("name")
        or raw.get("function_name")
        or raw.get("function")
    )
    if not definition_id or not name:
        return None
    granted = _string_list(raw.get("granted_ability_ids"))
    effect_owner = _clean_text(raw.get("effect_owner"))
    if not effect_owner:
        effect_owner = "ability_bridge" if granted else "inline"
    effect_owner = _normalized_enum(effect_owner, default="inline")
    inline = _as_records(raw.get("inline_effects"))
    explicit_effect = raw.get("effect")
    if explicit_effect in (None, "", {}, []) and effect_owner == "inline":
        explicit_effect = raw.get("description")
    if (
        effect_owner == "inline"
        and explicit_effect not in (None, "", {}, [])
    ):
        inline.append(copy.deepcopy(explicit_effect))
    function_id = _clean_text(raw.get("function_id")) or stable_id(
        "itemfn",
        definition_id,
        name.casefold(),
        _clean_text(raw.get("function_kind") or "custom"),
    )
    return {
        **copy.deepcopy(raw),
        "function_id": function_id,
        "item_definition_id": definition_id,
        "name": name,
        "function_kind": _clean_text(raw.get("function_kind")) or "custom",
        "activation_kind": _normalized_enum(
            raw.get("activation_kind"),
            default="active",
        ),
        "effect_owner": effect_owner,
        "inline_effects": inline,
        "granted_ability_ids": granted,
        "targets": copy.deepcopy(raw.get("targets") or []),
        "range": copy.deepcopy(raw.get("range")),
        "conditions": copy.deepcopy(raw.get("conditions") or []),
        "costs": copy.deepcopy(raw.get("costs") or []),
        "cooldown": copy.deepcopy(raw.get("cooldown")),
        "charges": copy.deepcopy(raw.get("charges")),
        "durability_cost": copy.deepcopy(raw.get("durability_cost")),
        "capacity": copy.deepcopy(raw.get("capacity")),
        "limits": copy.deepcopy(raw.get("limits") or []),
        "failure_modes": copy.deepcopy(raw.get("failure_modes") or []),
        "side_effects": copy.deepcopy(raw.get("side_effects") or []),
        "counters": copy.deepcopy(raw.get("counters") or []),
        "observable_signatures": copy.deepcopy(
            raw.get("observable_signatures") or []
        ),
        "source_claim_ids": _source_claim_ids(raw, fallback_claim_ids),
        "knowledge_plane": _clean_text(raw.get("knowledge_plane")) or "objective",
        "origin": _clean_text(raw.get("origin")) or origin,
    }


def _resolve_subject(
    raw: Mapping[str, Any],
    *,
    instances: Mapping[str, Mapping[str, Any]],
    stacks: Mapping[str, Mapping[str, Any]],
    definitions: Mapping[str, Mapping[str, Any]],
) -> tuple[str | None, str | None]:
    instance_id = _clean_text(raw.get("item_instance_id") or raw.get("instance_id"))
    stack_id = _clean_text(raw.get("stack_id"))
    definition_id = _clean_text(
        raw.get("item_definition_id") or raw.get("definition_id")
    )
    subject_type = _clean_text(raw.get("subject_type"))
    subject_id = _clean_text(raw.get("subject_id"))
    if subject_type in {"item_instance", "item_stack", "item_definition"} and subject_id:
        return subject_type, subject_id
    if instance_id:
        return "item_instance", instance_id
    if stack_id:
        return "item_stack", stack_id
    if definition_id:
        return "item_definition", definition_id
    name = _clean_text(
        raw.get("instance_name")
        or raw.get("stack_name")
        or raw.get("item_name")
        or raw.get("name")
    ).casefold()
    if name:
        for candidate_id, record in instances.items():
            if _clean_text(record.get("instance_name")).casefold() == name:
                return "item_instance", candidate_id
        for candidate_id, record in stacks.items():
            if _clean_text(record.get("stack_name")).casefold() == name:
                return "item_stack", candidate_id
        for candidate_id, record in definitions.items():
            if _clean_text(record.get("name")).casefold() == name:
                return "item_definition", candidate_id
    return None, None


def _normalize_entity_reference(raw: Mapping[str, Any], id_key: str, name_key: str) -> str | None:
    explicit = _clean_text(raw.get(id_key))
    if explicit:
        return explicit
    name = raw.get(name_key)
    if name is None:
        return None
    entity_type = "location" if "location" in id_key else "character"
    return _entity_id(entity_type, name)


def _normalize_custody(
    raw_value: Any,
    *,
    definitions: Mapping[str, Mapping[str, Any]],
    instances: Mapping[str, Mapping[str, Any]],
    stacks: Mapping[str, Mapping[str, Any]],
    fallback_claim_ids: Iterable[str] = (),
    origin: str,
) -> dict[str, Any] | None:
    if not isinstance(raw_value, Mapping):
        return None
    raw = copy.deepcopy(dict(raw_value))
    subject_type, subject_id = _resolve_subject(
        raw,
        instances=instances,
        stacks=stacks,
        definitions=definitions,
    )
    if subject_type not in {"item_instance", "item_stack"} or not subject_id:
        return None
    return {
        **raw,
        "custody_key": _clean_text(raw.get("custody_key"))
        or stable_id("itemcustody", subject_type, subject_id),
        "subject_type": subject_type,
        "subject_id": subject_id,
        "legal_owner_entity_id": _normalize_entity_reference(
            raw, "legal_owner_entity_id", "legal_owner"
        ),
        "custodian_entity_id": _normalize_entity_reference(
            raw, "custodian_entity_id", "custodian"
        ),
        "carrier_entity_id": _normalize_entity_reference(
            raw, "carrier_entity_id", "carrier"
        ),
        "location_entity_id": _normalize_entity_reference(
            raw, "location_entity_id", "location"
        ),
        "container_instance_id": _clean_text(raw.get("container_instance_id"))
        or None,
        "access_controller_entity_id": _normalize_entity_reference(
            raw, "access_controller_entity_id", "access_controller"
        ),
        "custody_status": _normalized_enum(
            raw.get("custody_status"),
            default="unknown",
        ),
        "quantity": copy.deepcopy(raw.get("quantity")),
        "effective_from_coordinate": copy.deepcopy(
            raw.get("effective_from_coordinate")
            or raw.get("story_coordinate")
        ),
        "source_claim_ids": _source_claim_ids(raw, fallback_claim_ids),
        "origin": _clean_text(raw.get("origin")) or origin,
    }


def _normalize_runtime(
    raw_value: Any,
    *,
    definitions: Mapping[str, Mapping[str, Any]],
    instances: Mapping[str, Mapping[str, Any]],
    stacks: Mapping[str, Mapping[str, Any]],
    fallback_claim_ids: Iterable[str] = (),
    origin: str,
) -> dict[str, Any] | None:
    if not isinstance(raw_value, Mapping):
        return None
    raw = copy.deepcopy(dict(raw_value))
    subject_type, subject_id = _resolve_subject(
        raw,
        instances=instances,
        stacks=stacks,
        definitions=definitions,
    )
    if subject_type != "item_instance" or not subject_id:
        return None
    return {
        **raw,
        "item_instance_id": subject_id,
        "durability": copy.deepcopy(raw.get("durability")),
        "max_durability": copy.deepcopy(raw.get("max_durability")),
        "energy": copy.deepcopy(raw.get("energy")),
        "max_energy": copy.deepcopy(raw.get("max_energy")),
        "sealed": copy.deepcopy(raw.get("sealed")),
        "damaged": copy.deepcopy(raw.get("damaged")),
        "destroyed": copy.deepcopy(raw.get("destroyed")),
        "active": copy.deepcopy(raw.get("active")),
        "equipped_by_entity_id": _normalize_entity_reference(
            raw, "equipped_by_entity_id", "equipped_by"
        ),
        "slot_key": copy.deepcopy(raw.get("slot_key")),
        "bound_actor_entity_id": _normalize_entity_reference(
            raw, "bound_actor_entity_id", "bound_actor"
        ),
        "story_coordinate": copy.deepcopy(raw.get("story_coordinate")),
        "source_claim_ids": _source_claim_ids(raw, fallback_claim_ids),
        "origin": _clean_text(raw.get("origin")) or origin,
    }


def _normalize_function_runtime(
    raw_value: Any,
    *,
    definitions: Mapping[str, Mapping[str, Any]],
    instances: Mapping[str, Mapping[str, Any]],
    stacks: Mapping[str, Mapping[str, Any]],
    fallback_claim_ids: Iterable[str] = (),
    origin: str,
) -> dict[str, Any] | None:
    if not isinstance(raw_value, Mapping):
        return None
    raw = copy.deepcopy(dict(raw_value))
    subject_type, subject_id = _resolve_subject(
        raw,
        instances=instances,
        stacks=stacks,
        definitions=definitions,
    )
    function_id = _clean_text(raw.get("function_id"))
    if (
        subject_type not in {"item_instance", "item_stack"}
        or not subject_id
        or not function_id
    ):
        return None
    runtime_key = _clean_text(raw.get("function_runtime_key")) or stable_id(
        "itemfnrt",
        subject_type,
        subject_id,
        function_id,
    )
    remaining_charges = (
        raw.get("remaining_charges")
        if "remaining_charges" in raw
        else raw.get("charges")
    )
    state = (
        copy.deepcopy(raw.get("state"))
        if "state" in raw
        else copy.deepcopy(raw.get("state_json", {}))
    )
    typed_subject = (
        {"item_instance_id": subject_id}
        if subject_type == "item_instance"
        else {"stack_id": subject_id}
    )
    return {
        **raw,
        "function_runtime_key": runtime_key,
        "subject_type": subject_type,
        "subject_id": subject_id,
        **typed_subject,
        "function_id": function_id,
        "enabled": copy.deepcopy(raw.get("enabled")),
        "unlock_state": (
            _normalized_enum(raw.get("unlock_state"), default="unlocked")
            if raw.get("unlock_state") is not None
            else None
        ),
        "remaining_charges": copy.deepcopy(remaining_charges),
        "cooldown_until": copy.deepcopy(raw.get("cooldown_until")),
        "state": state,
        "story_coordinate": copy.deepcopy(raw.get("story_coordinate")),
        "source_claim_ids": _source_claim_ids(raw, fallback_claim_ids),
        "origin": _clean_text(raw.get("origin")) or origin,
    }


def _normalize_observation(
    raw_value: Any,
    *,
    definitions: Mapping[str, Mapping[str, Any]],
    instances: Mapping[str, Mapping[str, Any]],
    stacks: Mapping[str, Mapping[str, Any]],
    fallback_claim_ids: Iterable[str] = (),
    origin: str,
    item_name_hint: Any = None,
) -> dict[str, Any] | None:
    if not isinstance(raw_value, Mapping):
        return None
    raw = copy.deepcopy(dict(raw_value))
    if item_name_hint and not any(
        raw.get(key)
        for key in (
            "item_name",
            "name",
            "item_definition_id",
            "item_instance_id",
            "stack_id",
        )
    ):
        raw["item_name"] = item_name_hint
    subject_type, subject_id = _resolve_subject(
        raw,
        instances=instances,
        stacks=stacks,
        definitions=definitions,
    )
    if not subject_type or not subject_id:
        return None
    observer_id = _normalize_entity_reference(
        raw, "observer_entity_id", "observer"
    )
    observed = raw.get("observation")
    if observed in (None, "", {}, []):
        observed = raw.get("observed_effect") or raw.get("description")
    if observed in (None, "", {}, []):
        return None
    return {
        **raw,
        "observation_id": _clean_text(raw.get("observation_id"))
        or stable_id(
            "itemobs",
            subject_type,
            subject_id,
            observer_id,
            observed,
            list(fallback_claim_ids),
        ),
        "subject_type": subject_type,
        "subject_id": subject_id,
        "observer_entity_id": observer_id,
        "function_id": _clean_text(raw.get("function_id")) or None,
        "observation_action": _normalized_enum(
            raw.get("observation_action") or raw.get("action"),
            default="observe",
        ),
        "knowledge_plane": _normalized_enum(
            raw.get("knowledge_plane"),
            default=("actor_belief" if observer_id else "reader_disclosed"),
        ),
        "confidence": copy.deepcopy(raw.get("confidence", 1.0)),
        "observation": (
            copy.deepcopy(observed)
            if isinstance(observed, Mapping)
            else {"description": copy.deepcopy(observed)}
        ),
        "story_coordinate": copy.deepcopy(raw.get("story_coordinate")),
        "source_claim_ids": _source_claim_ids(raw, fallback_claim_ids),
        "origin": _clean_text(raw.get("origin")) or origin,
    }


def _legacy_inventory_record(
    *,
    holder: Any,
    item_name: Any,
    quantity: Any = None,
    unique: Any = None,
    legacy_attributes: Any = None,
    source_claim_ids: Iterable[str] = (),
    origin: str,
) -> dict[str, Any] | None:
    holder_name = _clean_text(holder)
    item = _clean_text(item_name)
    if not holder_name or not item:
        return None
    record = {
        "legacy_inventory_id": stable_id(
            "legacyinv",
            holder_name.casefold(),
            item.casefold(),
            quantity,
            list(source_claim_ids),
        ),
        "holder_entity_id": _entity_id("character", holder_name),
        "holder_name": holder_name,
        "item_name": item,
        "quantity": copy.deepcopy(quantity),
        "unique": _unknown_unique(unique),
        "legacy_attributes": _json_object(legacy_attributes),
        "source_claim_ids": sorted(
            {_clean_text(value) for value in source_claim_ids if _clean_text(value)}
        ),
        "origin": origin,
        "modeling_status": "legacy_inventory_only",
    }
    return record


def build_item_package(
    dossier: Mapping[str, Any] | None,
    claims: Iterable[Mapping[str, Any]] = (),
    *,
    work_id: str,
    source_initialization_schema_version: str,
    source_snapshot_hash: str,
) -> dict[str, Any]:
    """Build a deterministic item package without extending init v1/v2 roots."""

    dossier_value = copy.deepcopy(dict(dossier or {}))
    definitions: dict[str, dict[str, Any]] = {}
    instances: dict[str, dict[str, Any]] = {}
    stacks: dict[str, dict[str, Any]] = {}
    functions: dict[str, dict[str, Any]] = {}
    bindings: dict[str, dict[str, Any]] = {}
    custodies: dict[str, dict[str, Any]] = {}
    runtimes: dict[str, dict[str, Any]] = {}
    function_runtimes: dict[str, dict[str, Any]] = {}
    observations: dict[str, dict[str, Any]] = {}
    legacy_inventory: dict[str, dict[str, Any]] = {}

    raw_definitions = _as_records(dossier_value.get("item_definitions"))
    raw_instances = _as_records(dossier_value.get("item_instances"))
    raw_stacks = _as_records(dossier_value.get("item_stacks"))
    raw_functions = _as_records(
        dossier_value.get("item_functions")
        or dossier_value.get("item_function_definitions")
    )
    raw_bindings = _as_records(dossier_value.get("item_function_bindings"))
    raw_custodies = _as_records(dossier_value.get("item_custody_bootstrap"))
    raw_runtimes = _as_records(dossier_value.get("item_runtime_bootstrap"))
    raw_function_runtimes = _as_records(
        dossier_value.get("item_function_runtime_bootstrap")
    )
    raw_observations = _as_records(dossier_value.get("item_observations"))

    for raw_item in _as_records(dossier_value.get("items")):
        if not isinstance(raw_item, Mapping):
            raw_definitions.append(raw_item)
            continue
        kind = _clean_text(
            raw_item.get("record_type") or raw_item.get("subject_type")
        )
        if kind in {"item_instance", "instance"}:
            raw_instances.append(raw_item)
        elif kind in {"item_stack", "stack"}:
            raw_stacks.append(raw_item)
        else:
            raw_definitions.append(raw_item)

    for raw in raw_definitions:
        record = _normalize_definition(raw, origin="user_input")
        if record is not None:
            _merge_unique(definitions, record, id_field="item_definition_id")
    for raw in raw_instances:
        record = _normalize_instance(
            raw,
            definitions=definitions,
            origin="user_input",
        )
        if record is not None:
            _merge_unique(instances, record, id_field="item_instance_id")
    for raw in raw_stacks:
        record = _normalize_stack(
            raw,
            definitions=definitions,
            origin="user_input",
        )
        if record is not None:
            _merge_unique(stacks, record, id_field="stack_id")
    for raw in raw_functions:
        record = _normalize_function(
            raw,
            definitions=definitions,
            origin="user_input",
        )
        if record is not None:
            _merge_unique(functions, record, id_field="function_id")

    actor_system = dossier_value.get("actor_system")
    actor_system = actor_system if isinstance(actor_system, Mapping) else {}
    actors: list[Mapping[str, Any]] = []
    protagonist = actor_system.get("protagonist")
    if isinstance(protagonist, Mapping):
        actors.append(protagonist)
    for key in ("opponents", "third_parties"):
        actors.extend(
            item
            for item in _as_records(actor_system.get(key))
            if isinstance(item, Mapping)
        )
    for actor in actors:
        holder_name = _clean_text(actor.get("name") or actor.get("canonical_name"))
        for raw_resource in _as_records(actor.get("resources")):
            if not isinstance(raw_resource, Mapping):
                legacy = _legacy_inventory_record(
                    holder=holder_name,
                    item_name=raw_resource,
                    origin="user_input",
                )
                if legacy is not None:
                    legacy_inventory[legacy["legacy_inventory_id"]] = legacy
                continue
            raw = copy.deepcopy(dict(raw_resource))
            item_name = _clean_text(raw.get("name") or raw.get("item"))
            typed_markers = {
                "item_kind",
                "item_definition_id",
                "definition_id",
                "definition_name",
                "item_instance_id",
                "instance_id",
                "serial_or_mark",
                "stack_policy",
                "uniqueness_policy",
                "functions",
                "attributes",
                "attributes_json",
            }
            if not any(key in raw for key in typed_markers):
                legacy = _legacy_inventory_record(
                    holder=holder_name,
                    item_name=item_name,
                    quantity=raw.get("quantity"),
                    unique=raw.get("unique"),
                    legacy_attributes=raw,
                    origin="user_input",
                )
                if legacy is not None:
                    legacy_inventory[legacy["legacy_inventory_id"]] = legacy
                continue
            definition = _normalize_definition(
                {
                    **raw,
                    "name": raw.get("definition_name") or item_name,
                },
                origin="user_input",
            )
            if definition is None:
                continue
            _merge_unique(definitions, definition, id_field="item_definition_id")
            subject_type: str | None = None
            subject_id: str | None = None
            unique = _unknown_unique(raw.get("unique"))
            serial = _clean_text(raw.get("serial_or_mark"))
            quantity = raw.get("quantity")
            if unique is True or serial or raw.get("item_instance_id"):
                instance = _normalize_instance(
                    {
                        **raw,
                        "definition_id": definition["item_definition_id"],
                        "instance_name": item_name,
                    },
                    definitions=definitions,
                    origin="user_input",
                )
                if instance is not None:
                    _merge_unique(instances, instance, id_field="item_instance_id")
                    subject_type = "item_instance"
                    subject_id = str(instance["item_instance_id"])
            elif quantity not in (None, 1, 1.0) or _stack_policy(
                raw.get("stack_policy")
            ) in {"homogeneous", "lot"}:
                stack = _normalize_stack(
                    {
                        **raw,
                        "definition_id": definition["item_definition_id"],
                        "stack_name": item_name,
                    },
                    definitions=definitions,
                    origin="user_input",
                )
                if stack is not None:
                    _merge_unique(stacks, stack, id_field="stack_id")
                    subject_type = "item_stack"
                    subject_id = str(stack["stack_id"])
            for raw_function in _as_records(raw.get("functions")):
                function = _normalize_function(
                    raw_function,
                    definitions=definitions,
                    origin="user_input",
                    item_name_hint=definition["name"],
                )
                if function is not None:
                    _merge_unique(functions, function, id_field="function_id")
            if subject_type and subject_id and holder_name:
                custody = _normalize_custody(
                    {
                        "subject_type": subject_type,
                        "subject_id": subject_id,
                        "legal_owner": holder_name,
                        "custodian": holder_name,
                        "carrier": holder_name,
                        "custody_status": "possessed",
                        "quantity": 1 if subject_type == "item_instance" else quantity,
                    },
                    definitions=definitions,
                    instances=instances,
                    stacks=stacks,
                    origin="user_input",
                )
                if custody is not None:
                    custodies[custody["custody_key"]] = custody

    claim_values = [copy.deepcopy(dict(item)) for item in claims if isinstance(item, Mapping)]
    for claim in claim_values:
        claim_id = _clean_text(claim.get("claim_id"))
        predicate = _clean_text(claim.get("predicate"))
        subject = _clean_text(claim.get("subject"))
        value = copy.deepcopy(claim.get("object_or_value"))
        fallback_ids = [claim_id] if claim_id else []
        if predicate == "item.definition":
            payload = value if isinstance(value, Mapping) else {"name": value or subject}
            record = _normalize_definition(
                payload,
                fallback_claim_ids=fallback_ids,
                origin="source_extract",
            )
            if record is not None:
                _merge_unique(definitions, record, id_field="item_definition_id")
        elif predicate == "item.instance":
            payload = value if isinstance(value, Mapping) else {"instance_name": value}
            record = _normalize_instance(
                payload,
                definitions=definitions,
                fallback_claim_ids=fallback_ids,
                origin="source_extract",
            )
            if record is not None:
                _merge_unique(instances, record, id_field="item_instance_id")
        elif predicate == "item.stack":
            payload = value if isinstance(value, Mapping) else {"stack_name": value}
            record = _normalize_stack(
                payload,
                definitions=definitions,
                fallback_claim_ids=fallback_ids,
                origin="source_extract",
            )
            if record is not None:
                _merge_unique(stacks, record, id_field="stack_id")
        elif predicate == "item.function":
            # Only this explicit predicate may create a function.
            payload = value if isinstance(value, Mapping) else {
                "item_name": subject,
                "name": "明确用途",
                "description": value,
            }
            record = _normalize_function(
                payload,
                definitions=definitions,
                fallback_claim_ids=fallback_ids,
                origin="source_extract",
                item_name_hint=subject,
            )
            if record is not None:
                _merge_unique(functions, record, id_field="function_id")
        elif predicate == "item.custody":
            record = _normalize_custody(
                value,
                definitions=definitions,
                instances=instances,
                stacks=stacks,
                fallback_claim_ids=fallback_ids,
                origin="source_extract",
            )
            if record is not None:
                custodies[record["custody_key"]] = record
        elif predicate == "item.runtime":
            record = _normalize_runtime(
                value,
                definitions=definitions,
                instances=instances,
                stacks=stacks,
                fallback_claim_ids=fallback_ids,
                origin="source_extract",
            )
            if record is not None:
                runtimes[record["item_instance_id"]] = record
        elif predicate == "item.function_runtime":
            record = _normalize_function_runtime(
                value,
                definitions=definitions,
                instances=instances,
                stacks=stacks,
                fallback_claim_ids=fallback_ids,
                origin="source_extract",
            )
            if record is not None:
                function_runtimes[record["function_runtime_key"]] = record
        elif predicate == "item.observation":
            payload = value if isinstance(value, Mapping) else {
                "item_name": subject,
                "description": value,
            }
            record = _normalize_observation(
                payload,
                definitions=definitions,
                instances=instances,
                stacks=stacks,
                fallback_claim_ids=fallback_ids,
                origin="source_extract",
                item_name_hint=subject,
            )
            if record is not None:
                observations[record["observation_id"]] = record
        elif predicate == "inventory.holds":
            legacy = _legacy_inventory_record(
                holder=subject,
                item_name=value,
                source_claim_ids=fallback_ids,
                origin="source_extract",
            )
            if legacy is not None:
                legacy_inventory[legacy["legacy_inventory_id"]] = legacy

    for raw in raw_bindings:
        if not isinstance(raw, Mapping):
            continue
        record = copy.deepcopy(dict(raw))
        function_id = _clean_text(record.get("function_id"))
        if not function_id:
            continue
        target_type, target_id = _resolve_subject(
            record,
            instances=instances,
            stacks=stacks,
            definitions=definitions,
        )
        if target_type not in {"item_definition", "item_instance", "item_stack"}:
            continue
        binding_id = _clean_text(record.get("binding_id")) or stable_id(
            "itemfnbind", target_type, target_id, function_id
        )
        normalized = {
            **record,
            "binding_id": binding_id,
            "target_type": target_type,
            "target_id": target_id,
            "function_id": function_id,
            "source_claim_ids": _source_claim_ids(record),
            "origin": _clean_text(record.get("origin")) or "user_input",
        }
        _merge_unique(bindings, normalized, id_field="binding_id")
    for raw in raw_custodies:
        record = _normalize_custody(
            raw,
            definitions=definitions,
            instances=instances,
            stacks=stacks,
            origin="user_input",
        )
        if record is not None:
            custodies[record["custody_key"]] = record
    for raw in raw_runtimes:
        record = _normalize_runtime(
            raw,
            definitions=definitions,
            instances=instances,
            stacks=stacks,
            origin="user_input",
        )
        if record is not None:
            runtimes[record["item_instance_id"]] = record
    for raw in raw_function_runtimes:
        record = _normalize_function_runtime(
            raw,
            definitions=definitions,
            instances=instances,
            stacks=stacks,
            origin="user_input",
        )
        if record is not None:
            function_runtimes[record["function_runtime_key"]] = record
    for raw in raw_observations:
        record = _normalize_observation(
            raw,
            definitions=definitions,
            instances=instances,
            stacks=stacks,
            origin="user_input",
        )
        if record is not None:
            observations[record["observation_id"]] = record

    all_claim_ids = sorted(
        {
            _clean_text(claim.get("claim_id"))
            for claim in claim_values
            if _clean_text(claim.get("claim_id"))
            and _clean_text(claim.get("predicate")).startswith(
                ("item.", "inventory.")
            )
        }
    )
    package: dict[str, Any] = {
        "schema_version": ITEM_SCHEMA_VERSION,
        "work_id": _clean_text(work_id),
        "source_initialization_schema_version": _clean_text(
            source_initialization_schema_version
        ),
        "source_snapshot_hash": _clean_text(source_snapshot_hash),
        "item_definitions": sorted(
            definitions.values(), key=lambda item: str(item["item_definition_id"])
        ),
        "item_instances": sorted(
            instances.values(), key=lambda item: str(item["item_instance_id"])
        ),
        "item_stacks": sorted(
            stacks.values(), key=lambda item: str(item["stack_id"])
        ),
        "item_functions": sorted(
            functions.values(), key=lambda item: str(item["function_id"])
        ),
        "item_function_bindings": sorted(
            bindings.values(), key=lambda item: str(item["binding_id"])
        ),
        "item_custody_bootstrap": sorted(
            custodies.values(), key=lambda item: str(item["custody_key"])
        ),
        "item_runtime_bootstrap": sorted(
            runtimes.values(), key=lambda item: str(item["item_instance_id"])
        ),
        "item_function_runtime_bootstrap": sorted(
            function_runtimes.values(),
            key=lambda item: str(item["function_runtime_key"]),
        ),
        "item_observations": sorted(
            observations.values(), key=lambda item: str(item["observation_id"])
        ),
        "legacy_inventory": sorted(
            legacy_inventory.values(),
            key=lambda item: str(item["legacy_inventory_id"]),
        ),
        "provenance": {
            "source_claim_ids": all_claim_ids,
            "extractor": "plot-init-item-sidecar-v1",
            "rules": {
                "name_and_holder_remain_legacy_inventory": True,
                "functions_require_explicit_source": True,
                "single_effect_remains_observation": True,
                "unknown_uniqueness_is_preserved": True,
                "legacy_attributes_are_preserved": True,
            },
        },
    }
    package["package_hash"] = recompute_item_package_hash(package)
    validate_item_package(package)
    return package


def validate_item_package(package: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(package))
    unexpected = sorted(set(value) - _TOP_LEVEL_FIELDS)
    if unexpected:
        raise PlotInitError(
            "ITEM_PACKAGE_STRUCTURE_INVALID",
            "item sidecar contains unsupported top-level fields",
            fields=unexpected,
        )
    if value.get("schema_version") != ITEM_SCHEMA_VERSION:
        raise PlotInitError(
            "ITEM_PACKAGE_SCHEMA_MISMATCH",
            "item sidecar uses an unsupported schema",
            expected=ITEM_SCHEMA_VERSION,
            actual=value.get("schema_version"),
        )
    if not _clean_text(value.get("work_id")):
        raise PlotInitError(
            "ITEM_PACKAGE_STRUCTURE_INVALID",
            "item sidecar work_id must be a non-empty string",
            field="work_id",
        )
    if value.get("source_initialization_schema_version") not in {
        "plot-rag-init/v1",
        "plot-rag-init/v2",
    }:
        raise PlotInitError(
            "ITEM_PACKAGE_STRUCTURE_INVALID",
            "item sidecar source initialization schema is unsupported",
            field="source_initialization_schema_version",
        )
    if not re.fullmatch(
        r"[a-f0-9]{64}",
        _clean_text(value.get("source_snapshot_hash")),
    ):
        raise PlotInitError(
            "ITEM_PACKAGE_STRUCTURE_INVALID",
            "item sidecar source_snapshot_hash must be lowercase SHA-256",
            field="source_snapshot_hash",
        )
    for field in ITEM_PACKAGE_ARRAY_FIELDS:
        if not isinstance(value.get(field), list):
            raise PlotInitError(
                "ITEM_PACKAGE_STRUCTURE_INVALID",
                f"item sidecar field must be an array: {field}",
                field=field,
            )
    if not isinstance(value.get("provenance"), dict):
        raise PlotInitError(
            "ITEM_PACKAGE_STRUCTURE_INVALID",
            "item sidecar provenance must be an object",
        )
    required_rules = {
        "name_and_holder_remain_legacy_inventory",
        "functions_require_explicit_source",
        "single_effect_remains_observation",
        "unknown_uniqueness_is_preserved",
        "legacy_attributes_are_preserved",
    }
    rules = value["provenance"].get("rules")
    if (
        not isinstance(rules, dict)
        or set(rules) != required_rules
        or any(rule_value is not True for rule_value in rules.values())
    ):
        raise PlotInitError(
            "ITEM_PACKAGE_PROVENANCE_INVALID",
            "item sidecar provenance rules must preserve all conservative extraction guarantees",
            fields=sorted(rules) if isinstance(rules, dict) else [],
        )

    ids_by_collection: dict[str, set[str]] = {}
    collection_ids = {
        "item_definitions": "item_definition_id",
        "item_instances": "item_instance_id",
        "item_stacks": "stack_id",
        "item_functions": "function_id",
        "item_function_bindings": "binding_id",
        "item_custody_bootstrap": "custody_key",
        "item_runtime_bootstrap": "item_instance_id",
        "item_function_runtime_bootstrap": "function_runtime_key",
        "item_observations": "observation_id",
        "legacy_inventory": "legacy_inventory_id",
    }
    for collection, id_field in collection_ids.items():
        seen: set[str] = set()
        for index, record in enumerate(value[collection]):
            if not isinstance(record, dict):
                raise PlotInitError(
                    "ITEM_PACKAGE_STRUCTURE_INVALID",
                    f"{collection} records must be objects",
                    field=collection,
                    index=index,
                )
            record_id = _clean_text(record.get(id_field))
            if not record_id or record_id in seen:
                raise PlotInitError(
                    "ITEM_PACKAGE_ID_INVALID",
                    "item sidecar ids must be present and unique per collection",
                    field=id_field,
                    record_id=record_id,
                )
            seen.add(record_id)
        ids_by_collection[collection] = seen

    definition_ids = ids_by_collection["item_definitions"]
    instance_ids = ids_by_collection["item_instances"]
    stack_ids = ids_by_collection["item_stacks"]
    function_ids = ids_by_collection["item_functions"]
    for record in value["item_definitions"]:
        stack_policy = _clean_text(record.get("stack_policy"))
        if stack_policy not in STACK_POLICIES:
            raise PlotInitError(
                "ITEM_STACK_POLICY_INVALID",
                "item definition stack policy is invalid",
                item_definition_id=record["item_definition_id"],
                stack_policy=stack_policy,
            )
        uniqueness_policy = _clean_text(record.get("uniqueness_policy"))
        if uniqueness_policy not in UNIQUENESS_POLICIES:
            raise PlotInitError(
                "ITEM_UNIQUENESS_INVALID",
                "item definition uniqueness policy is invalid",
                item_definition_id=record["item_definition_id"],
                uniqueness_policy=uniqueness_policy,
            )
        if not isinstance(record.get("legacy_attributes") or {}, dict):
            raise PlotInitError(
                "ITEM_LEGACY_ATTRIBUTES_INVALID",
                "legacy item attributes must remain an object",
                item_definition_id=record["item_definition_id"],
            )
    for collection in ("item_instances", "item_stacks", "item_functions"):
        for record in value[collection]:
            if _clean_text(record.get("item_definition_id")) not in definition_ids:
                raise PlotInitError(
                    "ITEM_DEFINITION_REFERENCE_INVALID",
                    "item sidecar record references a missing definition",
                    collection=collection,
                )
    instance_by_id = {
        str(record["item_instance_id"]): record
        for record in value["item_instances"]
    }
    stack_by_id = {
        str(record["stack_id"]): record
        for record in value["item_stacks"]
    }
    definition_by_id = {
        str(record["item_definition_id"]): record
        for record in value["item_definitions"]
    }
    function_by_id = {
        str(record["function_id"]): record
        for record in value["item_functions"]
    }
    for record in value["item_instances"]:
        instance_id = str(record["item_instance_id"])
        unique = record.get("unique")
        if type(unique) is not bool and unique != "unknown":
            raise PlotInitError(
                "ITEM_INSTANCE_METADATA_INVALID",
                "item instance unique must be a JSON boolean or unknown",
                item_instance_id=instance_id,
                field="unique",
                value=unique,
            )
        if record.get("provenance") is not None and not isinstance(
            record.get("provenance"), Mapping
        ):
            raise PlotInitError(
                "ITEM_INSTANCE_METADATA_INVALID",
                "item instance provenance must be an object",
                item_instance_id=instance_id,
                field="provenance",
            )
        _validate_optional_coordinate(
            record.get("story_coordinate"),
            code="ITEM_INSTANCE_COORDINATE_INVALID",
            field="story_coordinate",
            record_id=instance_id,
        )
    for record in value["item_functions"]:
        effect_owner = _clean_text(record.get("effect_owner"))
        activation_kind = _clean_text(record.get("activation_kind"))
        if activation_kind not in ACTIVATION_KINDS:
            raise PlotInitError(
                "ITEM_FUNCTION_ACTIVATION_INVALID",
                "item function activation_kind is invalid",
                function_id=record["function_id"],
                activation_kind=activation_kind,
            )
        if effect_owner not in EFFECT_OWNERS:
            raise PlotInitError(
                "ITEM_FUNCTION_EFFECT_OWNER_INVALID",
                "item function effect_owner is invalid",
                function_id=record["function_id"],
                effect_owner=effect_owner,
            )
        granted = _string_list(record.get("granted_ability_ids"))
        inline = _as_records(record.get("inline_effects"))
        if effect_owner == "ability_bridge" and (
            not granted
            or inline
            or record.get("costs")
            or record.get("cooldown")
            or record.get("counters")
        ):
            raise PlotInitError(
                "ITEM_ABILITY_BRIDGE_DUPLICATE",
                "ability bridge functions may only reference the ability owner",
                function_id=record["function_id"],
            )
        if effect_owner == "inline" and granted:
            raise PlotInitError(
                "ITEM_ABILITY_BRIDGE_DUPLICATE",
                "inline functions cannot also grant bridged abilities",
                function_id=record["function_id"],
            )
        function_id = str(record["function_id"])
        _validate_optional_nonnegative_number(
            record.get("charges"),
            code="ITEM_FUNCTION_RUNTIME_VALUE_INVALID",
            field="charges",
            record_id=function_id,
        )
        _validate_optional_nonnegative_number(
            record.get("durability_cost"),
            code="ITEM_FUNCTION_RUNTIME_VALUE_INVALID",
            field="durability_cost",
            record_id=function_id,
        )
    for record in value["item_function_bindings"]:
        if _clean_text(record.get("function_id")) not in function_ids:
            raise PlotInitError(
                "ITEM_FUNCTION_REFERENCE_INVALID",
                "item function binding references a missing function",
                binding_id=record["binding_id"],
            )
        target_type = _clean_text(record.get("target_type"))
        target_id = _clean_text(record.get("target_id"))
        valid = (
            target_type == "item_definition" and target_id in definition_ids
        ) or (
            target_type == "item_instance" and target_id in instance_ids
        ) or (
            target_type == "item_stack" and target_id in stack_ids
        )
        if not valid:
            raise PlotInitError(
                "ITEM_BINDING_TARGET_INVALID",
                "item function binding target is missing or ambiguous",
                binding_id=record["binding_id"],
            )
    for record in value["item_custody_bootstrap"]:
        subject_type = _clean_text(record.get("subject_type"))
        subject_id = _clean_text(record.get("subject_id"))
        if not (
            (subject_type == "item_instance" and subject_id in instance_ids)
            or (subject_type == "item_stack" and subject_id in stack_ids)
        ):
            raise PlotInitError(
                "ITEM_CUSTODY_SUBJECT_INVALID",
                "item custody bootstrap references a missing typed subject",
                custody_key=record["custody_key"],
            )
        custody_status = _clean_text(record.get("custody_status"))
        if custody_status not in CUSTODY_STATUSES:
            raise PlotInitError(
                "ITEM_CUSTODY_STATUS_INVALID",
                "item custody bootstrap has an unsupported status",
                custody_key=record["custody_key"],
                custody_status=custody_status,
            )
        _validate_optional_nonnegative_number(
            record.get("quantity"),
            code="ITEM_CUSTODY_QUANTITY_INVALID",
            field="quantity",
            record_id=str(record["custody_key"]),
        )
        _validate_optional_coordinate(
            record.get("effective_from_coordinate")
            or record.get("story_coordinate"),
            code="ITEM_CUSTODY_COORDINATE_INVALID",
            field="effective_from_coordinate",
            record_id=str(record["custody_key"]),
        )
    for record in value["item_runtime_bootstrap"]:
        instance_id = _clean_text(record.get("item_instance_id"))
        if instance_id not in instance_ids:
            raise PlotInitError(
                "ITEM_RUNTIME_SUBJECT_INVALID",
                "item runtime bootstrap references a missing instance",
            )
        runtime_id = instance_id
        definition_id = _clean_text(
            instance_by_id[instance_id].get("item_definition_id")
        )
        definition = definition_by_id.get(definition_id, {})
        current_durability = _validate_optional_nonnegative_number(
            record.get("durability"),
            code="ITEM_RUNTIME_VALUE_INVALID",
            field="durability",
            record_id=runtime_id,
        )
        max_durability = _validate_optional_nonnegative_number(
            (
                record.get("max_durability")
                if record.get("max_durability") is not None
                else definition.get("max_durability")
            ),
            code="ITEM_RUNTIME_VALUE_INVALID",
            field="max_durability",
            record_id=runtime_id,
        )
        current_energy = _validate_optional_nonnegative_number(
            record.get("energy"),
            code="ITEM_RUNTIME_VALUE_INVALID",
            field="energy",
            record_id=runtime_id,
        )
        max_energy = _validate_optional_nonnegative_number(
            (
                record.get("max_energy")
                if record.get("max_energy") is not None
                else definition.get("max_energy")
            ),
            code="ITEM_RUNTIME_VALUE_INVALID",
            field="max_energy",
            record_id=runtime_id,
        )
        if (
            current_durability is not None
            and max_durability is not None
            and float(current_durability) > float(max_durability)
        ):
            raise PlotInitError(
                "ITEM_RUNTIME_CAPACITY_EXCEEDED",
                "item runtime durability exceeds max_durability",
                item_instance_id=instance_id,
                durability=current_durability,
                max_durability=max_durability,
            )
        if (
            current_energy is not None
            and max_energy is not None
            and float(current_energy) > float(max_energy)
        ):
            raise PlotInitError(
                "ITEM_RUNTIME_CAPACITY_EXCEEDED",
                "item runtime energy exceeds max_energy",
                item_instance_id=instance_id,
                energy=current_energy,
                max_energy=max_energy,
            )
        for field in ("sealed", "damaged", "destroyed", "active"):
            _validate_optional_boolean(
                record.get(field),
                code="ITEM_RUNTIME_VALUE_INVALID",
                field=field,
                record_id=runtime_id,
            )
        equipped = _clean_text(record.get("equipped_by_entity_id"))
        slot_key = _clean_text(record.get("slot_key"))
        if bool(equipped) != bool(slot_key):
            raise PlotInitError(
                "ITEM_RUNTIME_EQUIPMENT_INVALID",
                "equipped_by_entity_id and slot_key must be supplied together",
                item_instance_id=instance_id,
            )
        if record.get("state") is not None and not isinstance(
            record.get("state"), Mapping
        ):
            raise PlotInitError(
                "ITEM_RUNTIME_STATE_INVALID",
                "item runtime state must be an object",
                item_instance_id=instance_id,
            )
        _validate_optional_coordinate(
            record.get("story_coordinate"),
            code="ITEM_RUNTIME_COORDINATE_INVALID",
            field="story_coordinate",
            record_id=runtime_id,
        )
    for record in value["item_function_runtime_bootstrap"]:
        instance_id = _clean_text(record.get("item_instance_id"))
        stack_id = _clean_text(record.get("stack_id"))
        subject_type = _clean_text(record.get("subject_type"))
        subject_id = _clean_text(record.get("subject_id"))
        typed_subjects = [
            ("item_instance", instance_id, instance_by_id),
            ("item_stack", stack_id, stack_by_id),
        ]
        present = [
            (kind, identifier, lookup)
            for kind, identifier, lookup in typed_subjects
            if identifier
        ]
        if len(present) != 1:
            raise PlotInitError(
                "ITEM_FUNCTION_RUNTIME_REFERENCE_INVALID",
                "item function runtime requires exactly one instance or stack subject",
                function_runtime_key=record.get("function_runtime_key"),
            )
        expected_type, expected_id, subject_lookup = present[0]
        if subject_type and subject_type != expected_type:
            raise PlotInitError(
                "ITEM_FUNCTION_RUNTIME_REFERENCE_INVALID",
                "item function runtime subject_type does not match typed subject",
                function_runtime_key=record.get("function_runtime_key"),
                subject_type=subject_type,
                expected_subject_type=expected_type,
            )
        if subject_id and subject_id != expected_id:
            raise PlotInitError(
                "ITEM_FUNCTION_RUNTIME_REFERENCE_INVALID",
                "item function runtime subject_id does not match typed subject",
                function_runtime_key=record.get("function_runtime_key"),
                subject_id=subject_id,
                expected_subject_id=expected_id,
            )
        function_id = _clean_text(record.get("function_id"))
        if not function_id or function_id not in function_ids:
            raise PlotInitError(
                "ITEM_FUNCTION_RUNTIME_REFERENCE_INVALID",
                "item function runtime references missing function",
                function_runtime_key=record.get("function_runtime_key"),
                function_id=function_id,
            )
        runtime_key = str(record["function_runtime_key"])
        subject_record = subject_lookup[expected_id]
        subject_definition_id = _clean_text(
            subject_record.get("item_definition_id")
        )
        function = function_by_id[function_id]
        if _clean_text(function.get("item_definition_id")) != subject_definition_id:
            raise PlotInitError(
                "ITEM_FUNCTION_RUNTIME_REFERENCE_INVALID",
                "item function runtime crosses item definitions",
                function_runtime_key=runtime_key,
                subject_type=expected_type,
                subject_id=expected_id,
                function_id=function_id,
            )
        _validate_optional_boolean(
            record.get("enabled"),
            code="ITEM_FUNCTION_RUNTIME_VALUE_INVALID",
            field="enabled",
            record_id=runtime_key,
        )
        unlock_state = _clean_text(record.get("unlock_state"))
        if unlock_state and unlock_state not in FUNCTION_UNLOCK_STATES:
            raise PlotInitError(
                "ITEM_FUNCTION_RUNTIME_VALUE_INVALID",
                "item function runtime unlock_state is invalid",
                function_runtime_key=runtime_key,
                unlock_state=unlock_state,
            )
        remaining = _validate_optional_nonnegative_number(
            record.get("remaining_charges"),
            code="ITEM_FUNCTION_RUNTIME_VALUE_INVALID",
            field="remaining_charges",
            record_id=runtime_key,
        )
        default_charges = _validate_optional_nonnegative_number(
            function.get("charges"),
            code="ITEM_FUNCTION_RUNTIME_VALUE_INVALID",
            field="function.charges",
            record_id=runtime_key,
        )
        if (
            remaining is not None
            and default_charges is not None
            and float(remaining) > float(default_charges)
        ):
            raise PlotInitError(
                "ITEM_FUNCTION_RUNTIME_CAPACITY_EXCEEDED",
                "remaining_charges exceeds the function charge capacity",
                function_runtime_key=runtime_key,
                remaining_charges=remaining,
                charges=default_charges,
            )
        _validate_optional_coordinate(
            record.get("cooldown_until"),
            code="ITEM_FUNCTION_RUNTIME_COORDINATE_INVALID",
            field="cooldown_until",
            record_id=runtime_key,
        )
        _validate_optional_coordinate(
            record.get("story_coordinate"),
            code="ITEM_FUNCTION_RUNTIME_COORDINATE_INVALID",
            field="story_coordinate",
            record_id=runtime_key,
        )
        if record.get("state") is not None and not isinstance(
            record.get("state"), Mapping
        ):
            raise PlotInitError(
                "ITEM_FUNCTION_RUNTIME_STATE_INVALID",
                "item function runtime state must be an object",
                function_runtime_key=runtime_key,
            )
    for record in value["item_observations"]:
        subject_type = _clean_text(record.get("subject_type"))
        subject_id = _clean_text(record.get("subject_id"))
        if not (
            (subject_type == "item_definition" and subject_id in definition_ids)
            or (subject_type == "item_instance" and subject_id in instance_ids)
            or (subject_type == "item_stack" and subject_id in stack_ids)
        ):
            raise PlotInitError(
                "ITEM_OBSERVATION_SUBJECT_INVALID",
                "item observation references a missing subject",
                observation_id=record["observation_id"],
            )
        action = _clean_text(record.get("observation_action"))
        if action not in OBSERVATION_ACTIONS:
            raise PlotInitError(
                "ITEM_OBSERVATION_ACTION_INVALID",
                "item observation action is invalid",
                observation_id=record["observation_id"],
                observation_action=action,
            )
        plane = _clean_text(record.get("knowledge_plane"))
        if plane not in KNOWLEDGE_PLANES:
            raise PlotInitError(
                "ITEM_OBSERVATION_PLANE_INVALID",
                "item observation knowledge plane is invalid",
                observation_id=record["observation_id"],
                knowledge_plane=plane,
            )
        if plane == "actor_belief" and not _clean_text(
            record.get("observer_entity_id")
        ):
            raise PlotInitError(
                "ITEM_OBSERVATION_OBSERVER_REQUIRED",
                "actor-belief item observations require an observer entity",
                observation_id=record["observation_id"],
            )
        observation_function_id = _clean_text(record.get("function_id"))
        if observation_function_id:
            function = function_by_id.get(observation_function_id)
            if function is None:
                raise PlotInitError(
                    "ITEM_OBSERVATION_FUNCTION_INVALID",
                    "item observation references a missing function",
                    observation_id=record["observation_id"],
                    function_id=observation_function_id,
                )
            observed_definition_id = (
                subject_id
                if subject_type == "item_definition"
                else _clean_text(
                    instance_by_id[subject_id].get("item_definition_id")
                )
                if subject_type == "item_instance"
                else next(
                    (
                        _clean_text(stack.get("item_definition_id"))
                        for stack in value["item_stacks"]
                        if _clean_text(stack.get("stack_id")) == subject_id
                    ),
                    "",
                )
            )
            if _clean_text(function.get("item_definition_id")) != (
                observed_definition_id
            ):
                raise PlotInitError(
                    "ITEM_OBSERVATION_FUNCTION_INVALID",
                    "item observation function belongs to another definition",
                    observation_id=record["observation_id"],
                    function_id=observation_function_id,
                )
        confidence = _validate_optional_nonnegative_number(
            record.get("confidence", 1.0),
            code="ITEM_OBSERVATION_CONFIDENCE_INVALID",
            field="confidence",
            record_id=str(record["observation_id"]),
        )
        if confidence is None or float(confidence) > 1:
            raise PlotInitError(
                "ITEM_OBSERVATION_CONFIDENCE_INVALID",
                "item observation confidence must be between zero and one",
                observation_id=record["observation_id"],
                confidence=confidence,
            )
        observation = record.get("observation")
        if not isinstance(observation, Mapping) or not observation:
            raise PlotInitError(
                "ITEM_OBSERVATION_VALUE_INVALID",
                "item observation must be a non-empty object",
                observation_id=record["observation_id"],
            )
        _validate_optional_coordinate(
            record.get("story_coordinate"),
            code="ITEM_OBSERVATION_COORDINATE_INVALID",
            field="story_coordinate",
            record_id=str(record["observation_id"]),
        )

    expected = _clean_text(value.get("package_hash"))
    actual = recompute_item_package_hash(value)
    if not expected or expected != actual:
        raise PlotInitError(
            "ITEM_PACKAGE_HASH_MISMATCH",
            "item sidecar package hash does not match its content",
            expected=expected,
            actual=actual,
        )
    return value


def render_item_package(package: Mapping[str, Any]) -> str:
    value = validate_item_package(package)
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def item_package_has_typed_content(package: Mapping[str, Any]) -> bool:
    """Return whether the package contains anything beyond legacy inventory."""

    value = validate_item_package(package)
    return any(
        bool(value[field])
        for field in ITEM_PACKAGE_ARRAY_FIELDS
        if field != "legacy_inventory"
    )


def build_item_sidecar_artifact(
    package: Mapping[str, Any],
    project_root: Path | None,
    *,
    relative_path: str = ITEM_SIDECAR_PATH,
) -> dict[str, Any]:
    value = validate_item_package(package)
    content = render_item_package(value)
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
                "item sidecar target escapes project root",
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
        "logical_owner": ITEM_SIDECAR_OWNER,
        "operation": operation,
        "expected_old_hash": expected_old_hash,
        "proposed_new_hash": proposed_hash,
        "proposed_content": content,
        "unified_diff": diff,
        "materialized": False,
        "item_package_hash": value["package_hash"],
        "item_schema_version": ITEM_SCHEMA_VERSION,
    }


def item_sidecar_reference(artifact: Mapping[str, Any]) -> dict[str, Any]:
    if _clean_text(artifact.get("logical_owner")) != ITEM_SIDECAR_OWNER:
        raise PlotInitError(
            "ITEM_SIDECAR_ARTIFACT_INVALID",
            "artifact is not the initialization item sidecar",
        )
    content = str(artifact.get("proposed_content") or "")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise PlotInitError(
            "ITEM_SIDECAR_ARTIFACT_INVALID",
            "item sidecar artifact content is not valid JSON",
        ) from exc
    package = validate_item_package(payload)
    actual_content_hash = sha256_bytes(content.encode("utf-8"))
    expected_content_hash = _clean_text(artifact.get("proposed_new_hash"))
    if expected_content_hash != actual_content_hash:
        raise PlotInitError(
            "ITEM_SIDECAR_CONTENT_HASH_MISMATCH",
            "item sidecar artifact bytes differ from the frozen hash",
            expected=expected_content_hash,
            actual=actual_content_hash,
        )
    artifact_package_hash = _clean_text(artifact.get("item_package_hash"))
    if artifact_package_hash and artifact_package_hash != package["package_hash"]:
        raise PlotInitError(
            "ITEM_SIDECAR_PACKAGE_HASH_MISMATCH",
            "item sidecar artifact package hash differs from its content",
        )
    return {
        "schema_version": ITEM_SCHEMA_VERSION,
        "path": _clean_text(artifact.get("path")),
        "artifact_id": _clean_text(artifact.get("artifact_id")),
        "package_hash": package["package_hash"],
        "content_hash": expected_content_hash,
    }


def item_package_from_artifact_manifest(
    artifact_manifest: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    candidates = [
        copy.deepcopy(dict(item))
        for item in artifact_manifest
        if isinstance(item, Mapping)
        and (
            _clean_text(item.get("logical_owner")) == ITEM_SIDECAR_OWNER
            or _clean_text(item.get("path")) == ITEM_SIDECAR_PATH
        )
    ]
    if not candidates:
        return None
    if len(candidates) != 1:
        raise PlotInitError(
            "ITEM_SIDECAR_DUPLICATE",
            "initialization artifact manifest contains multiple item sidecars",
        )
    artifact = candidates[0]
    reference = item_sidecar_reference(artifact)
    payload = json.loads(str(artifact.get("proposed_content") or ""))
    return validate_item_package(payload), reference


def item_package_from_frozen_proposal(
    frozen_proposal: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    bundle = frozen_proposal.get("bundle")
    if not isinstance(bundle, Mapping):
        raise PlotInitError(
            "INVALID_INITIALIZATION_PROPOSAL",
            "item sidecar verification requires an initialization bundle",
        )
    loaded = item_package_from_artifact_manifest(
        item
        for item in bundle.get("artifact_manifest") or []
        if isinstance(item, Mapping)
    )
    if loaded is None:
        raise PlotInitError(
            "ITEM_SIDECAR_MISSING",
            "frozen initialization proposal does not contain an item sidecar",
        )
    package, actual_reference = loaded
    apply_plan = frozen_proposal.get("apply_plan")
    apply_plan = apply_plan if isinstance(apply_plan, Mapping) else {}
    expected_reference = apply_plan.get("item_sidecar")
    if not isinstance(expected_reference, Mapping):
        expected_reference = (bundle.get("meta") or {}).get("item_sidecar")
    if not isinstance(expected_reference, Mapping):
        raise PlotInitError(
            "ITEM_SIDECAR_REFERENCE_MISSING",
            "initialization proposal must bind the item sidecar hash",
        )
    comparable_fields = (
        "schema_version",
        "path",
        "artifact_id",
        "package_hash",
        "content_hash",
    )
    expected = {
        key: _clean_text(expected_reference.get(key)) for key in comparable_fields
    }
    actual = {
        key: _clean_text(actual_reference.get(key)) for key in comparable_fields
    }
    if expected != actual:
        raise PlotInitError(
            "ITEM_SIDECAR_REFERENCE_MISMATCH",
            "initialization proposal item sidecar reference changed after freeze",
            expected=expected,
            actual=actual,
        )
    return package, actual_reference


def assert_item_sidecar_target_baseline(
    frozen_proposal: Mapping[str, Any],
    project_root: Path | str,
) -> dict[str, Any]:
    """Verify the reviewed target hash before a materialization attempt."""

    _package, reference = item_package_from_frozen_proposal(frozen_proposal)
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
            "item sidecar target escapes project root",
            path=reference["path"],
        )
    current_hash = sha256_bytes(target.read_bytes()) if target.is_file() else None
    expected = artifact.get("expected_old_hash")
    if current_hash != expected:
        raise PlotInitError(
            "ITEM_SIDECAR_TARGET_DRIFT",
            "item sidecar target changed after proposal review",
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


def verify_materialized_item_sidecar(
    frozen_proposal: Mapping[str, Any],
    project_root: Path | str,
) -> dict[str, Any]:
    package, reference = item_package_from_frozen_proposal(frozen_proposal)
    root = Path(project_root).expanduser().resolve(strict=False)
    target = (root / Path(reference["path"])).resolve(strict=False)
    if not path_is_within(target, root) or target.is_symlink():
        raise PlotInitError(
            "UNSAFE_TARGET_PATH",
            "materialized item sidecar is outside the project or is a symlink",
            path=reference["path"],
        )
    if not target.is_file():
        raise PlotInitError(
            "ITEM_SIDECAR_NOT_MATERIALIZED",
            "materialized item sidecar file is missing",
            path=reference["path"],
        )
    raw = target.read_bytes()
    actual_content_hash = sha256_bytes(raw)
    if actual_content_hash != reference["content_hash"]:
        raise PlotInitError(
            "ITEM_SIDECAR_MATERIALIZED_HASH_MISMATCH",
            "materialized item sidecar bytes differ from the approved artifact",
            expected=reference["content_hash"],
            actual=actual_content_hash,
        )
    try:
        materialized = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlotInitError(
            "ITEM_SIDECAR_MATERIALIZED_INVALID",
            "materialized item sidecar is not valid UTF-8 JSON",
        ) from exc
    verified = validate_item_package(materialized)
    if verified["package_hash"] != package["package_hash"]:
        raise PlotInitError(
            "ITEM_SIDECAR_MATERIALIZED_PACKAGE_MISMATCH",
            "materialized item package differs from the frozen proposal",
        )
    return {
        "status": "verified",
        "path": reference["path"],
        "artifact_id": reference["artifact_id"],
        "package_hash": verified["package_hash"],
        "content_hash": actual_content_hash,
    }
