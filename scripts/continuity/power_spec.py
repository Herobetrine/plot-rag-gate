"""Pure compiler for standalone ``plot-rag-power/v1`` specification imports.

The initialization lifecycle already knows how to turn a normalized power
model into ``power_spec`` events.  This module exposes the same deterministic
mapping without requiring an initialization session, a project path, SQLite,
or a clock.  Integration layers may preview the result, persist the returned
package as a ``power_spec_change`` proposal, and require an
``accept_power_spec`` grant later.
"""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any, Mapping, Sequence

try:
    from ..power_system import (
        PowerModelError,
        canonical_power_hash,
        normalize_power_package,
        validate_power_package,
    )
except ImportError:  # Direct ``continuity`` import with scripts on sys.path.
    from power_system import (  # type: ignore[no-redef]
        PowerModelError,
        canonical_power_hash,
        normalize_power_package,
        validate_power_package,
    )
from .validators import canonical_json, stable_hash


POWER_SPEC_LIFECYCLE_SCHEMA = "plot-rag-lifecycle/power-spec-package-v1"
POWER_SPEC_PROPOSAL_KIND = "power_spec_change"
POWER_SPEC_REQUIRED_OPERATION = "accept_power_spec"
POWER_SPEC_SCOPE = "timeless"
POWER_SPEC_ARTIFACT_STAGE = "bootstrap"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PACKAGE_FIELDS = frozenset(
    {
        "schema_version",
        "proposal_id",
        "proposal_kind",
        "required_operation",
        "scope",
        "entities",
        "events",
        "power_package_hash",
        "package_hash",
    }
)
_ENTITY_FIELDS = frozenset(
    {
        "entity_id",
        "entity_type",
        "canonical_name",
        "aliases",
    }
)
_EVENT_FIELDS = frozenset(
    {
        "event_type",
        "event_id",
        "scope",
        "artifact_stage",
        "action",
        "spec_type",
        "spec_entity_id",
        "definition",
        "evidence",
    }
)

# collection, normalized id field, lifecycle spec type, continuity entity type
POWER_SPEC_COLLECTIONS: tuple[tuple[str, str, str, str], ...] = (
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
    ("bridge_rules", "bridge_rule_id", "bridge_rule", "bridge_rule"),
    (
        "conversion_rules",
        "conversion_rule_id",
        "conversion_rule",
        "conversion_rule",
    ),
)

_SPEC_TYPE_TO_ENTITY_TYPE = {
    spec_type: entity_type
    for _, _, spec_type, entity_type in POWER_SPEC_COLLECTIONS
}


class PowerSpecImportError(PowerModelError):
    """Stable error raised by the standalone PowerSpec compiler."""


def _error(
    code: str,
    message: str,
    **details: Any,
) -> PowerSpecImportError:
    return PowerSpecImportError(code, message, **details)


def _stable_id(prefix: str, *parts: Any, length: int = 24) -> str:
    digest = hashlib.sha256(
        "\x1f".join(canonical_json(part) for part in parts).encode("utf-8")
    ).hexdigest()
    return f"{prefix}-{digest[:length]}"


def _wrap_model_error(error: PowerModelError) -> PowerSpecImportError:
    if isinstance(error, PowerSpecImportError):
        return error
    return PowerSpecImportError(
        error.code,
        str(error),
        **copy.deepcopy(error.details),
    )


def _require_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(
            "POWER_SPEC_INPUT_INVALID",
            f"{field} must be an object",
            field=field,
            actual_type=type(value).__name__,
        )
    return value


def _require_list(value: Any, *, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise _error(
            "POWER_SPEC_PACKAGE_INVALID",
            f"{field} must be an array",
            field=field,
            actual_type=type(value).__name__,
        )
    return value


def _normalized_records(
    package: Mapping[str, Any],
) -> list[tuple[str, str, str, str, dict[str, Any]]]:
    records: list[tuple[str, str, str, str, dict[str, Any]]] = []
    seen: dict[str, dict[str, Any]] = {}
    for collection, id_key, spec_type, entity_type in POWER_SPEC_COLLECTIONS:
        for index, raw_record in enumerate(package.get(collection) or []):
            if not isinstance(raw_record, Mapping):
                raise _error(
                    "POWER_SPEC_RECORD_INVALID",
                    "normalized power specification record must be an object",
                    collection=collection,
                    index=index,
                    actual_type=type(raw_record).__name__,
                )
            record = copy.deepcopy(dict(raw_record))
            entity_id = str(record.get(id_key) or "").strip()
            if not entity_id:
                raise _error(
                    "POWER_ID_REQUIRED",
                    "power specification record requires a stable id",
                    collection=collection,
                    index=index,
                    id_field=id_key,
                )
            first = seen.get(entity_id)
            if first is not None:
                raise _error(
                    "POWER_SPEC_DUPLICATE_ID",
                    "power specification ids must be globally unique",
                    entity_id=entity_id,
                    first=first,
                    duplicate={
                        "collection": collection,
                        "index": index,
                        "spec_type": spec_type,
                    },
                )
            seen[entity_id] = {
                "collection": collection,
                "index": index,
                "spec_type": spec_type,
            }
            records.append(
                (
                    collection,
                    entity_id,
                    spec_type,
                    entity_type,
                    record,
                )
            )
    return records


def _raise_unresolved(
    *,
    collection: str,
    entity_id: str,
    field: str,
    reference: Any,
    expected_entity_type: str,
) -> None:
    raise _error(
        "POWER_ENDPOINT_UNRESOLVED",
        "power specification references an unknown entity",
        collection=collection,
        entity_id=entity_id,
        field=field,
        reference=reference,
        expected_entity_type=expected_entity_type,
    )


def _validate_normalized_references(
    package: Mapping[str, Any],
    records: Sequence[tuple[str, str, str, str, Mapping[str, Any]]],
) -> None:
    ids_by_type: dict[str, set[str]] = {}
    for _, entity_id, _, entity_type, _ in records:
        ids_by_type.setdefault(entity_type, set()).add(entity_id)

    system_ids = ids_by_type.get("power_system", set())
    track_ids = ids_by_type.get("progression_track", set())
    rank_ids = ids_by_type.get("rank_node", set())
    resource_ids = ids_by_type.get("resource_pool", set())
    system_refs: dict[str, str] = {}
    for raw_system in package.get("power_systems") or []:
        if not isinstance(raw_system, Mapping):
            continue
        system_id = str(raw_system.get("power_system_id") or "").strip()
        namespace = str(raw_system.get("namespace") or "").strip()
        if system_id:
            system_refs[system_id] = system_id
        if namespace:
            system_refs[namespace] = system_id

    system_bound = {
        "progression_tracks",
        "ability_definitions",
        "resource_definitions",
        "status_definitions",
        "qualification_definitions",
        "counter_rules",
    }
    for collection, entity_id, spec_type, _, record in records:
        if collection in system_bound:
            reference = str(record.get("power_system_id") or "").strip()
            if reference and reference not in system_ids:
                _raise_unresolved(
                    collection=collection,
                    entity_id=entity_id,
                    field="power_system_id",
                    reference=reference,
                    expected_entity_type="power_system",
                )

        if spec_type == "rank_node":
            reference = str(record.get("track_id") or "").strip()
            if not reference or reference not in track_ids:
                _raise_unresolved(
                    collection=collection,
                    entity_id=entity_id,
                    field="track_id",
                    reference=reference,
                    expected_entity_type="progression_track",
                )

        elif spec_type == "rank_edge":
            track_reference = str(record.get("track_id") or "").strip()
            if not track_reference or track_reference not in track_ids:
                _raise_unresolved(
                    collection=collection,
                    entity_id=entity_id,
                    field="track_id",
                    reference=track_reference,
                    expected_entity_type="progression_track",
                )
            from_ids = [
                str(value).strip()
                for value in record.get("from_node_ids") or []
                if str(value).strip()
            ]
            if not from_ids:
                _raise_unresolved(
                    collection=collection,
                    entity_id=entity_id,
                    field="from_node_ids",
                    reference=from_ids,
                    expected_entity_type="rank_node",
                )
            for reference in from_ids:
                if reference not in rank_ids:
                    _raise_unresolved(
                        collection=collection,
                        entity_id=entity_id,
                        field="from_node_ids",
                        reference=reference,
                        expected_entity_type="rank_node",
                    )
            to_reference = str(record.get("to_node_id") or "").strip()
            if not to_reference or to_reference not in rank_ids:
                _raise_unresolved(
                    collection=collection,
                    entity_id=entity_id,
                    field="to_node_id",
                    reference=to_reference,
                    expected_entity_type="rank_node",
                )

        elif spec_type == "bridge_rule":
            for field in ("source_namespace", "target_namespace"):
                reference = str(record.get(field) or "").strip()
                if not reference or reference not in system_refs:
                    _raise_unresolved(
                        collection=collection,
                        entity_id=entity_id,
                        field=field,
                        reference=reference,
                        expected_entity_type="power_system",
                    )

        elif spec_type == "conversion_rule":
            for field in ("source_resource_id", "target_resource_id"):
                reference = str(record.get(field) or "").strip()
                if not reference or reference not in resource_ids:
                    _raise_unresolved(
                        collection=collection,
                        entity_id=entity_id,
                        field=field,
                        reference=reference,
                        expected_entity_type="resource_pool",
                    )
            for field in ("source_system_id", "target_system_id"):
                reference = str(record.get(field) or "").strip()
                if reference and reference not in system_ids:
                    _raise_unresolved(
                        collection=collection,
                        entity_id=entity_id,
                        field=field,
                        reference=reference,
                        expected_entity_type="power_system",
                    )


def normalize_power_spec_import(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize and strictly validate a raw ``plot-rag-power/v1`` aggregate."""

    source = _require_mapping(raw, field="power_spec_import")
    incoming_hash = source.get("power_package_hash")
    if incoming_hash is not None:
        declared_incoming_hash = str(incoming_hash or "")
        actual_incoming_hash = canonical_power_hash(source)
        if (
            not declared_incoming_hash
            or declared_incoming_hash != actual_incoming_hash
        ):
            raise _error(
                "POWER_PACKAGE_HASH_MISMATCH",
                "declared power package hash does not match its content",
                expected_power_package_hash=declared_incoming_hash or None,
                actual_power_package_hash=actual_incoming_hash,
            )
    try:
        normalized = normalize_power_package(source)
        validate_power_package(normalized)
    except PowerModelError as error:
        raise _wrap_model_error(error) from error

    declared_hash = str(normalized.get("power_package_hash") or "")
    actual_hash = canonical_power_hash(normalized)
    if not declared_hash or declared_hash != actual_hash:
        raise _error(
            "POWER_PACKAGE_HASH_MISMATCH",
            "normalized power package hash is invalid",
            expected_power_package_hash=declared_hash or None,
            actual_power_package_hash=actual_hash,
        )

    runtime_records = normalized.get("actor_power_bootstrap") or []
    if runtime_records:
        raise _error(
            "POWER_SPEC_RUNTIME_NOT_SUPPORTED",
            "standalone PowerSpec imports accept definitions only",
            collection="actor_power_bootstrap",
            record_count=len(runtime_records),
            required_proposal_kind="story_delta",
        )

    records = _normalized_records(normalized)
    _validate_normalized_references(normalized, records)
    return copy.deepcopy(normalized)


def validate_power_spec_import(raw: Mapping[str, Any]) -> None:
    """Validate a raw aggregate without retaining or writing any state."""

    normalize_power_spec_import(raw)


def _record_name(
    record: Mapping[str, Any],
    *,
    spec_type: str,
    entity_id: str,
) -> str:
    return str(
        record.get("name")
        or record.get("native_term")
        or f"{spec_type}:{entity_id}"
    ).strip()


def _record_aliases(record: Mapping[str, Any]) -> list[str]:
    return sorted(
        {
            str(binding.get("native_term") or "").strip()
            for binding in record.get("native_term_bindings") or []
            if isinstance(binding, Mapping)
            and str(binding.get("native_term") or "").strip()
        }
    )


def _event_definition(
    record: Mapping[str, Any],
    *,
    spec_type: str,
    system_refs: Mapping[str, str],
) -> dict[str, Any]:
    definition = copy.deepcopy(dict(record))
    if spec_type in {
        "progression_track",
        "ability_definition",
        "resource_definition",
        "status_definition",
        "qualification_definition",
        "counter_rule",
    }:
        definition["system_entity_id"] = str(
            record.get("power_system_id") or ""
        )
    elif spec_type == "rank_node":
        definition["track_entity_id"] = str(record.get("track_id") or "")
    elif spec_type == "rank_edge":
        definition.update(
            {
                "track_entity_id": str(record.get("track_id") or ""),
                "from_rank_entity_ids": [
                    str(value)
                    for value in record.get("from_node_ids") or []
                    if str(value)
                ],
                "to_rank_entity_id": str(record.get("to_node_id") or ""),
            }
        )
    elif spec_type == "bridge_rule":
        definition.update(
            {
                "source_system_entity_id": system_refs.get(
                    str(record.get("source_namespace") or ""),
                    "",
                ),
                "target_system_entity_id": system_refs.get(
                    str(record.get("target_namespace") or ""),
                    "",
                ),
            }
        )
    elif spec_type == "conversion_rule":
        definition.update(
            {
                "source_resource_entity_id": str(
                    record.get("source_resource_id") or ""
                ),
                "target_resource_entity_id": str(
                    record.get("target_resource_id") or ""
                ),
                "source_system_entity_id": str(
                    record.get("source_system_id") or ""
                ),
                "target_system_entity_id": str(
                    record.get("target_system_id") or ""
                ),
            }
        )
    return definition


def _event_id(
    proposal_id: str,
    event: Mapping[str, Any],
) -> str:
    payload = {
        key: copy.deepcopy(value)
        for key, value in event.items()
        if key not in {"event_id", "created_at"}
    }
    return _stable_id(
        "powerspecevt",
        proposal_id,
        "power_spec",
        payload,
    )


def _compile_normalized(
    normalized: Mapping[str, Any],
) -> dict[str, Any]:
    records = _normalized_records(normalized)
    if not records:
        raise _error(
            "POWER_SPEC_EVENTS_EMPTY",
            "power specification import produced no definition events",
        )

    power_package_hash = str(normalized.get("power_package_hash") or "")
    proposal_id = _stable_id(
        "power-spec-import",
        power_package_hash,
    )
    system_refs: dict[str, str] = {}
    for raw_system in normalized.get("power_systems") or []:
        if not isinstance(raw_system, Mapping):
            continue
        system_id = str(raw_system.get("power_system_id") or "")
        namespace = str(raw_system.get("namespace") or "")
        if system_id:
            system_refs[system_id] = system_id
        if namespace:
            system_refs[namespace] = system_id

    entities: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    collection_ordinals: dict[str, int] = {}
    for collection, entity_id, spec_type, entity_type, record in records:
        ordinal = collection_ordinals.get(collection, 0)
        collection_ordinals[collection] = ordinal + 1
        entities.append(
            {
                "entity_id": entity_id,
                "entity_type": entity_type,
                "canonical_name": _record_name(
                    record,
                    spec_type=spec_type,
                    entity_id=entity_id,
                ),
                "aliases": _record_aliases(record),
            }
        )
        event: dict[str, Any] = {
            "event_type": "power_spec",
            "scope": POWER_SPEC_SCOPE,
            "artifact_stage": POWER_SPEC_ARTIFACT_STAGE,
            "action": "define",
            "spec_type": spec_type,
            "spec_entity_id": entity_id,
            "definition": _event_definition(
                record,
                spec_type=spec_type,
                system_refs=system_refs,
            ),
            "evidence": {
                "kind": "power_spec_import",
                "collection": collection,
                "normalized_index": ordinal,
                "power_package_hash": power_package_hash,
            },
        }
        event["event_id"] = _event_id(proposal_id, event)
        events.append(event)

    entities.sort(
        key=lambda entity: (
            str(entity["entity_type"]),
            str(entity["entity_id"]),
            canonical_json(entity),
        )
    )
    events.sort(
        key=lambda event: (
            str(event["spec_type"]),
            str(event["spec_entity_id"]),
            str(event["event_id"]),
        )
    )

    package: dict[str, Any] = {
        "schema_version": POWER_SPEC_LIFECYCLE_SCHEMA,
        "proposal_id": proposal_id,
        "proposal_kind": POWER_SPEC_PROPOSAL_KIND,
        "required_operation": POWER_SPEC_REQUIRED_OPERATION,
        "scope": POWER_SPEC_SCOPE,
        "entities": entities,
        "events": events,
        "power_package_hash": power_package_hash,
    }
    package["package_hash"] = stable_hash(package)
    return package


def build_power_spec_lifecycle_package(
    raw: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile a raw aggregate into a frozen standalone lifecycle package."""

    normalized = normalize_power_spec_import(raw)
    package = _compile_normalized(normalized)
    validate_power_spec_lifecycle_package(package)
    return copy.deepcopy(package)


def compile_power_spec_change(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Alias with proposal-oriented naming for integration layers."""

    return build_power_spec_lifecycle_package(raw)


def preview_power_spec_import(
    raw: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a deterministic, read-only preview of normalization and mapping."""

    normalized = normalize_power_spec_import(raw)
    package = _compile_normalized(normalized)
    validate_power_spec_lifecycle_package(package)
    return {
        "status": "ready",
        "read_only": True,
        "normalized_power_package": copy.deepcopy(normalized),
        "lifecycle_package": copy.deepcopy(package),
        "summary": {
            "entity_count": len(package["entities"]),
            "event_count": len(package["events"]),
            "power_package_hash": package["power_package_hash"],
            "package_hash": package["package_hash"],
        },
    }


def _validate_reference(
    *,
    entity_types: Mapping[str, str],
    spec_type: str,
    spec_entity_id: str,
    field: str,
    reference: Any,
    expected_entity_type: str,
    required: bool = True,
) -> None:
    values = (
        [str(value).strip() for value in reference or [] if str(value).strip()]
        if isinstance(reference, list)
        else [str(reference or "").strip()]
    )
    if required and not values:
        values = [""]
    for value in values:
        if not value:
            if not required:
                continue
            _raise_unresolved(
                collection=spec_type,
                entity_id=spec_entity_id,
                field=field,
                reference=value,
                expected_entity_type=expected_entity_type,
            )
        if entity_types.get(value) != expected_entity_type:
            _raise_unresolved(
                collection=spec_type,
                entity_id=spec_entity_id,
                field=field,
                reference=value,
                expected_entity_type=expected_entity_type,
            )


def _validate_event_references(
    *,
    event: Mapping[str, Any],
    entity_types: Mapping[str, str],
) -> None:
    spec_type = str(event.get("spec_type") or "")
    spec_entity_id = str(event.get("spec_entity_id") or "")
    definition = event.get("definition")
    if not isinstance(definition, Mapping):
        raise _error(
            "POWER_SPEC_EVENT_INVALID",
            "power specification event requires a definition object",
            spec_type=spec_type,
            spec_entity_id=spec_entity_id,
        )

    if spec_type in {
        "progression_track",
        "ability_definition",
        "resource_definition",
        "status_definition",
        "qualification_definition",
        "counter_rule",
    }:
        _validate_reference(
            entity_types=entity_types,
            spec_type=spec_type,
            spec_entity_id=spec_entity_id,
            field="system_entity_id",
            reference=definition.get("system_entity_id"),
            expected_entity_type="power_system",
            required=spec_type == "progression_track",
        )
    elif spec_type == "rank_node":
        _validate_reference(
            entity_types=entity_types,
            spec_type=spec_type,
            spec_entity_id=spec_entity_id,
            field="track_entity_id",
            reference=definition.get("track_entity_id"),
            expected_entity_type="progression_track",
        )
    elif spec_type == "rank_edge":
        _validate_reference(
            entity_types=entity_types,
            spec_type=spec_type,
            spec_entity_id=spec_entity_id,
            field="track_entity_id",
            reference=definition.get("track_entity_id"),
            expected_entity_type="progression_track",
        )
        _validate_reference(
            entity_types=entity_types,
            spec_type=spec_type,
            spec_entity_id=spec_entity_id,
            field="from_rank_entity_ids",
            reference=definition.get("from_rank_entity_ids"),
            expected_entity_type="rank_node",
        )
        _validate_reference(
            entity_types=entity_types,
            spec_type=spec_type,
            spec_entity_id=spec_entity_id,
            field="to_rank_entity_id",
            reference=definition.get("to_rank_entity_id"),
            expected_entity_type="rank_node",
        )
    elif spec_type == "bridge_rule":
        for field in (
            "source_system_entity_id",
            "target_system_entity_id",
        ):
            _validate_reference(
                entity_types=entity_types,
                spec_type=spec_type,
                spec_entity_id=spec_entity_id,
                field=field,
                reference=definition.get(field),
                expected_entity_type="power_system",
            )
    elif spec_type == "conversion_rule":
        for field in (
            "source_resource_entity_id",
            "target_resource_entity_id",
        ):
            _validate_reference(
                entity_types=entity_types,
                spec_type=spec_type,
                spec_entity_id=spec_entity_id,
                field=field,
                reference=definition.get(field),
                expected_entity_type="resource_pool",
            )
        for field in (
            "source_system_entity_id",
            "target_system_entity_id",
        ):
            _validate_reference(
                entity_types=entity_types,
                spec_type=spec_type,
                spec_entity_id=spec_entity_id,
                field=field,
                reference=definition.get(field),
                expected_entity_type="power_system",
                required=False,
            )


def validate_power_spec_lifecycle_package(
    package: Mapping[str, Any],
) -> None:
    """Validate a frozen standalone lifecycle package without side effects."""

    value = _require_mapping(package, field="power_spec_lifecycle_package")
    unexpected_package_fields = sorted(set(value) - _PACKAGE_FIELDS)
    if unexpected_package_fields:
        raise _error(
            "POWER_SPEC_PACKAGE_FIELDS_UNSUPPORTED",
            "power specification lifecycle package contains unsupported fields",
            unexpected_fields=unexpected_package_fields,
        )
    if str(value.get("schema_version") or "") != POWER_SPEC_LIFECYCLE_SCHEMA:
        raise _error(
            "POWER_SPEC_PACKAGE_SCHEMA_UNSUPPORTED",
            "power specification lifecycle package schema is unsupported",
            actual=value.get("schema_version"),
            supported=POWER_SPEC_LIFECYCLE_SCHEMA,
        )
    if str(value.get("proposal_kind") or "") != POWER_SPEC_PROPOSAL_KIND:
        raise _error(
            "POWER_SPEC_PACKAGE_INVALID",
            "power specification lifecycle package has an invalid proposal kind",
            actual=value.get("proposal_kind"),
            expected=POWER_SPEC_PROPOSAL_KIND,
        )
    if (
        str(value.get("required_operation") or "")
        != POWER_SPEC_REQUIRED_OPERATION
    ):
        raise _error(
            "POWER_SPEC_PACKAGE_INVALID",
            "power specification lifecycle package has an invalid grant operation",
            actual=value.get("required_operation"),
            expected=POWER_SPEC_REQUIRED_OPERATION,
        )
    if str(value.get("scope") or "") != POWER_SPEC_SCOPE:
        raise _error(
            "POWER_SPEC_PACKAGE_INVALID",
            "power specification lifecycle package must be timeless",
            actual=value.get("scope"),
            expected=POWER_SPEC_SCOPE,
        )

    power_package_hash = str(value.get("power_package_hash") or "")
    if not _SHA256_RE.fullmatch(power_package_hash):
        raise _error(
            "POWER_PACKAGE_HASH_INVALID",
            "power_package_hash must be a lowercase SHA-256 digest",
            power_package_hash=power_package_hash or None,
        )
    proposal_id = str(value.get("proposal_id") or "")
    expected_proposal_id = _stable_id(
        "power-spec-import",
        power_package_hash,
    )
    if proposal_id != expected_proposal_id:
        raise _error(
            "POWER_SPEC_PROPOSAL_ID_MISMATCH",
            "power specification proposal id is not deterministic",
            expected=expected_proposal_id,
            actual=proposal_id or None,
        )

    entities = _require_list(value.get("entities"), field="entities")
    events = _require_list(value.get("events"), field="events")
    if not events:
        raise _error(
            "POWER_SPEC_EVENTS_EMPTY",
            "power specification lifecycle package requires definition events",
        )

    declared_package_hash = str(value.get("package_hash") or "")
    hash_payload = copy.deepcopy(dict(value))
    hash_payload.pop("package_hash", None)
    actual_package_hash = stable_hash(hash_payload)
    if (
        not declared_package_hash
        or declared_package_hash != actual_package_hash
    ):
        raise _error(
            "POWER_SPEC_PACKAGE_HASH_MISMATCH",
            "power specification lifecycle package hash is invalid",
            expected_package_hash=declared_package_hash or None,
            actual_package_hash=actual_package_hash,
        )

    entity_types: dict[str, str] = {}
    for index, raw_entity in enumerate(entities):
        if not isinstance(raw_entity, Mapping):
            raise _error(
                "POWER_SPEC_ENTITY_INVALID",
                "power specification entity must be an object",
                index=index,
            )
        unexpected_entity_fields = sorted(set(raw_entity) - _ENTITY_FIELDS)
        if unexpected_entity_fields:
            raise _error(
                "POWER_SPEC_ENTITY_FIELDS_UNSUPPORTED",
                "power specification entity contains unsupported fields",
                index=index,
                unexpected_fields=unexpected_entity_fields,
            )
        entity_id = str(raw_entity.get("entity_id") or "").strip()
        entity_type = str(raw_entity.get("entity_type") or "").strip()
        canonical_name = str(
            raw_entity.get("canonical_name") or ""
        ).strip()
        if not entity_id or not entity_type or not canonical_name:
            raise _error(
                "POWER_SPEC_ENTITY_INVALID",
                "power specification entity requires id, type, and name",
                index=index,
                entity_id=entity_id or None,
                entity_type=entity_type or None,
            )
        aliases = raw_entity.get("aliases")
        if (
            not isinstance(aliases, list)
            or any(
                not isinstance(alias, str) or not alias.strip()
                for alias in aliases
            )
        ):
            raise _error(
                "POWER_SPEC_ENTITY_INVALID",
                "power specification entity aliases must be non-empty strings",
                index=index,
                entity_id=entity_id,
            )
        if entity_id in entity_types:
            raise _error(
                "POWER_SPEC_DUPLICATE_ID",
                "power specification entity ids must be unique",
                entity_id=entity_id,
                first_entity_type=entity_types[entity_id],
                duplicate_entity_type=entity_type,
            )
        entity_types[entity_id] = entity_type

    event_ids: set[str] = set()
    event_specs: set[tuple[str, str]] = set()
    referenced_entity_ids: set[str] = set()
    for index, raw_event in enumerate(events):
        if not isinstance(raw_event, Mapping):
            raise _error(
                "POWER_SPEC_EVENT_INVALID",
                "power specification event must be an object",
                index=index,
            )
        event = dict(raw_event)
        unexpected_event_fields = sorted(set(event) - _EVENT_FIELDS)
        if unexpected_event_fields:
            raise _error(
                "POWER_SPEC_EVENT_FIELDS_UNSUPPORTED",
                "power specification event contains unsupported fields",
                index=index,
                unexpected_fields=unexpected_event_fields,
            )
        if (
            str(event.get("event_type") or "") != "power_spec"
            or str(event.get("action") or "") != "define"
            or str(event.get("scope") or "") != POWER_SPEC_SCOPE
            or str(event.get("artifact_stage") or "")
            != POWER_SPEC_ARTIFACT_STAGE
        ):
            raise _error(
                "POWER_SPEC_EVENT_INVALID",
                "standalone imports may contain only bootstrap timeless power_spec define events",
                index=index,
                event_type=event.get("event_type"),
                action=event.get("action"),
                scope=event.get("scope"),
                artifact_stage=event.get("artifact_stage"),
            )
        if not isinstance(event.get("evidence"), Mapping):
            raise _error(
                "POWER_SPEC_EVENT_INVALID",
                "power specification event requires an evidence object",
                index=index,
            )
        spec_type = str(event.get("spec_type") or "")
        spec_entity_id = str(event.get("spec_entity_id") or "").strip()
        expected_entity_type = _SPEC_TYPE_TO_ENTITY_TYPE.get(spec_type)
        if (
            expected_entity_type is None
            or not spec_entity_id
            or entity_types.get(spec_entity_id) != expected_entity_type
        ):
            raise _error(
                "POWER_SPEC_EVENT_INVALID",
                "power specification event does not match its entity descriptor",
                index=index,
                spec_type=spec_type or None,
                spec_entity_id=spec_entity_id or None,
                expected_entity_type=expected_entity_type,
                actual_entity_type=entity_types.get(spec_entity_id),
            )
        spec_key = (spec_type, spec_entity_id)
        if spec_key in event_specs:
            raise _error(
                "POWER_SPEC_DUPLICATE_ID",
                "power specification events may define each entity only once",
                spec_type=spec_type,
                entity_id=spec_entity_id,
            )
        event_specs.add(spec_key)
        referenced_entity_ids.add(spec_entity_id)

        event_id = str(event.get("event_id") or "")
        if not event_id or event_id in event_ids:
            raise _error(
                "POWER_SPEC_DUPLICATE_ID",
                "power specification event ids must be present and unique",
                event_id=event_id or None,
                index=index,
            )
        expected_event_id = _event_id(proposal_id, event)
        if event_id != expected_event_id:
            raise _error(
                "POWER_SPEC_EVENT_ID_MISMATCH",
                "power specification event id is not deterministic",
                index=index,
                expected=expected_event_id,
                actual=event_id,
            )
        event_ids.add(event_id)
        _validate_event_references(
            event=event,
            entity_types=entity_types,
        )

    unreferenced = sorted(set(entity_types) - referenced_entity_ids)
    if unreferenced:
        raise _error(
            "POWER_SPEC_ENTITY_UNREFERENCED",
            "every power specification entity requires one define event",
            entity_ids=unreferenced,
        )


__all__ = [
    "POWER_SPEC_ARTIFACT_STAGE",
    "POWER_SPEC_COLLECTIONS",
    "POWER_SPEC_LIFECYCLE_SCHEMA",
    "POWER_SPEC_PROPOSAL_KIND",
    "POWER_SPEC_REQUIRED_OPERATION",
    "POWER_SPEC_SCOPE",
    "PowerSpecImportError",
    "build_power_spec_lifecycle_package",
    "compile_power_spec_change",
    "normalize_power_spec_import",
    "preview_power_spec_import",
    "validate_power_spec_import",
    "validate_power_spec_lifecycle_package",
]
