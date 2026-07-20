"""Lossless normalized export and re-import helpers."""

from __future__ import annotations

import copy
import json
from typing import Any, Mapping

from .canonical import canonical_hash
from .constants import FIELD_STATUSES, PROTOCOL_V1, PROTOCOL_V2
from .errors import PlotInitError
from .advantages import advantage_package_from_artifact_manifest
from .items import item_package_from_artifact_manifest


NORMALIZED_EXPORT_FORMAT = "plot-rag-init-normalized/v1"
_BUNDLE_HASH_VOLATILE_KEYS = (
    "real_path",
    "normalized_real_path",
    "unified_diff",
)
_NORMALIZED_PAYLOAD_KEYS = (
    "schema_version",
    "genre_contract",
    "world_model",
    "actor_system",
    "story_engine",
    "serialization_contract",
    "entities",
    "relations",
    "timeline",
    "open_loops",
    "field_states",
    "source_manifest",
    "source_ownership",
    "conflicts",
    "gaps",
    "decisions",
    "provenance",
    "power_systems",
    "progression_tracks",
    "rank_nodes",
    "rank_edges",
    "ability_definitions",
    "resource_definitions",
    "status_definitions",
    "qualification_definitions",
    "counter_rules",
    "bridge_rules",
    "conversion_rules",
    "actor_power_bootstrap",
    "power_model",
    "advantage_package",
)
_REQUIRED_OBJECT_KEYS = (
    "meta",
    "genre_contract",
    "world_model",
    "actor_system",
    "story_engine",
    "serialization_contract",
    "field_states",
    "source_ownership",
    "provenance",
    "validation",
)
_REQUIRED_LIST_KEYS = (
    "entities",
    "relations",
    "timeline",
    "open_loops",
    "source_manifest",
    "conflicts",
    "gaps",
    "decisions",
    "artifact_manifest",
)


def recompute_bundle_hash(bundle: Mapping[str, Any]) -> str:
    return canonical_hash(
        dict(bundle),
        extra_volatile_keys=_BUNDLE_HASH_VOLATILE_KEYS,
        strip_default_volatile=True,
    )


def normalized_payload(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Return the path-owned, provenance-bearing semantic representation."""

    payload = {
        key: copy.deepcopy(bundle.get(key))
        for key in _NORMALIZED_PAYLOAD_KEYS
    }
    loaded_item_sidecar = item_package_from_artifact_manifest(
        item
        for item in bundle.get("artifact_manifest") or []
        if isinstance(item, Mapping)
    )
    if loaded_item_sidecar is not None:
        item_package, _reference = loaded_item_sidecar
        payload["item_sidecars"] = [item_package]
    loaded_advantage_sidecar = advantage_package_from_artifact_manifest(
        item
        for item in bundle.get("artifact_manifest") or []
        if isinstance(item, Mapping)
    )
    if loaded_advantage_sidecar is not None:
        advantage_package, _reference = loaded_advantage_sidecar
        payload["advantage_package"] = advantage_package
    return payload


def normalized_hash(bundle: Mapping[str, Any]) -> str:
    return canonical_hash(
        normalized_payload(bundle),
        extra_volatile_keys=(
            "real_path",
            "normalized_real_path",
            "mtime_ns",
        ),
        strip_default_volatile=True,
    )


def _validate_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(bundle))
    schema_version = value.get("schema_version")
    if schema_version not in {PROTOCOL_V1, PROTOCOL_V2}:
        raise PlotInitError(
            "NORMALIZED_EXPORT_SCHEMA_MISMATCH",
            "normalized export contains an unsupported initialization schema",
            expected=[PROTOCOL_V1, PROTOCOL_V2],
            actual=schema_version,
        )
    for key in _REQUIRED_OBJECT_KEYS:
        if not isinstance(value.get(key), dict):
            raise PlotInitError(
                "NORMALIZED_EXPORT_STRUCTURE_INVALID",
                f"normalized export bundle field must be an object: {key}",
                field=key,
            )
    for key in _REQUIRED_LIST_KEYS:
        if not isinstance(value.get(key), list):
            raise PlotInitError(
                "NORMALIZED_EXPORT_STRUCTURE_INVALID",
                f"normalized export bundle field must be an array: {key}",
                field=key,
            )
    loaded_item_sidecar = item_package_from_artifact_manifest(
        item
        for item in value.get("artifact_manifest") or []
        if isinstance(item, Mapping)
    )
    provenance_sidecars = value["provenance"].get("item_sidecars")
    if loaded_item_sidecar is None:
        if provenance_sidecars not in (None, []):
            raise PlotInitError(
                "NORMALIZED_ITEM_SIDECAR_REFERENCE_INVALID",
                "normalized provenance references a missing item sidecar",
            )
    else:
        _package, actual_reference = loaded_item_sidecar
        if provenance_sidecars != [actual_reference]:
            raise PlotInitError(
                "NORMALIZED_ITEM_SIDECAR_REFERENCE_INVALID",
                "normalized item sidecar reference differs from its artifact",
                expected=provenance_sidecars,
                actual=[actual_reference],
            )
    loaded_advantage_sidecar = advantage_package_from_artifact_manifest(
        item
        for item in value.get("artifact_manifest") or []
        if isinstance(item, Mapping)
    )
    advantage_sidecars = value["provenance"].get("advantage_sidecars")
    if loaded_advantage_sidecar is None:
        if advantage_sidecars not in (None, []):
            raise PlotInitError(
                "NORMALIZED_ADVANTAGE_SIDECAR_REFERENCE_INVALID",
                "normalized provenance references a missing Advantage sidecar",
            )
    else:
        _package, actual_reference = loaded_advantage_sidecar
        if advantage_sidecars != [actual_reference]:
            raise PlotInitError(
                "NORMALIZED_ADVANTAGE_SIDECAR_REFERENCE_INVALID",
                "normalized Advantage sidecar reference differs from its artifact",
                expected=advantage_sidecars,
                actual=[actual_reference],
            )
    if schema_version == PROTOCOL_V2:
        if not isinstance(value.get("power_model"), dict):
            raise PlotInitError(
                "NORMALIZED_EXPORT_STRUCTURE_INVALID",
                "v2 normalized export requires a power_model object",
                field="power_model",
            )
        for key in (
            "power_systems",
            "progression_tracks",
            "rank_nodes",
            "rank_edges",
            "ability_definitions",
            "resource_definitions",
            "status_definitions",
            "qualification_definitions",
            "counter_rules",
            "bridge_rules",
            "conversion_rules",
            "actor_power_bootstrap",
        ):
            if not isinstance(value.get(key), list):
                raise PlotInitError(
                    "NORMALIZED_EXPORT_STRUCTURE_INVALID",
                    f"v2 normalized export field must be an array: {key}",
                    field=key,
                )
    for path, state in value["field_states"].items():
        if not isinstance(path, str) or not path.startswith("/"):
            raise PlotInitError(
                "NORMALIZED_EXPORT_FIELD_STATE_INVALID",
                "normalized field state paths must be JSON pointers",
                path=path,
            )
        if not isinstance(state, dict) or state.get("field_status") not in FIELD_STATUSES:
            raise PlotInitError(
                "NORMALIZED_EXPORT_FIELD_STATE_INVALID",
                "normalized field state has an unsupported status",
                path=path,
                field_status=(
                    state.get("field_status")
                    if isinstance(state, dict)
                    else None
                ),
            )
    expected = str(value.get("bundle_hash") or "")
    actual = recompute_bundle_hash(value)
    if not expected or expected != actual:
        raise PlotInitError(
            "NORMALIZED_EXPORT_BUNDLE_HASH_MISMATCH",
            "normalized export bundle hash does not match its content",
            expected=expected,
            actual=actual,
        )
    return value


def export_normalized_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Create a transport envelope without changing ownership or provenance."""

    value = _validate_bundle(bundle)
    return {
        "format": NORMALIZED_EXPORT_FORMAT,
        "schema_version": str(value["schema_version"]),
        "normalization_hash": normalized_hash(value),
        "source_bundle_hash": str(value["bundle_hash"]),
        "initialization_bundle": value,
    }


def render_normalized_bundle(bundle: Mapping[str, Any]) -> str:
    return (
        json.dumps(
            export_normalized_bundle(bundle),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def parse_normalized_export(payload: Any) -> dict[str, Any] | None:
    """Validate a normalized envelope, returning ``None`` for ordinary JSON."""

    if not isinstance(payload, dict):
        return None
    if payload.get("format") != NORMALIZED_EXPORT_FORMAT:
        return None
    if payload.get("schema_version") not in {PROTOCOL_V1, PROTOCOL_V2}:
        raise PlotInitError(
            "NORMALIZED_EXPORT_SCHEMA_MISMATCH",
            "normalized export envelope schema is unsupported",
            expected=[PROTOCOL_V1, PROTOCOL_V2],
            actual=payload.get("schema_version"),
        )
    raw_bundle = payload.get("initialization_bundle")
    if not isinstance(raw_bundle, dict):
        raise PlotInitError(
            "NORMALIZED_EXPORT_BUNDLE_MISSING",
            "normalized export requires an initialization_bundle object",
        )
    bundle = _validate_bundle(raw_bundle)
    if payload.get("schema_version") != bundle.get("schema_version"):
        raise PlotInitError(
            "NORMALIZED_EXPORT_SCHEMA_MISMATCH",
            "normalized export envelope and bundle versions differ",
            envelope=payload.get("schema_version"),
            bundle=bundle.get("schema_version"),
        )
    expected_source_hash = str(payload.get("source_bundle_hash") or "")
    if expected_source_hash != str(bundle["bundle_hash"]):
        raise PlotInitError(
            "NORMALIZED_EXPORT_SOURCE_HASH_MISMATCH",
            "normalized export source bundle hash is inconsistent",
            expected=expected_source_hash,
            actual=bundle["bundle_hash"],
        )
    expected_normalized_hash = str(payload.get("normalization_hash") or "")
    actual_normalized_hash = normalized_hash(bundle)
    if expected_normalized_hash != actual_normalized_hash:
        raise PlotInitError(
            "NORMALIZED_EXPORT_HASH_MISMATCH",
            "normalized export semantic hash does not match its content",
            expected=expected_normalized_hash,
            actual=actual_normalized_hash,
        )
    return bundle


def _diff_values(before: Any, after: Any, path: str, result: list[dict[str, Any]]) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after), key=str):
            child_path = f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}"
            if key not in before:
                result.append(
                    {"path": child_path, "operation": "add", "after": copy.deepcopy(after[key])}
                )
            elif key not in after:
                result.append(
                    {
                        "path": child_path,
                        "operation": "remove",
                        "before": copy.deepcopy(before[key]),
                    }
                )
            else:
                _diff_values(before[key], after[key], child_path, result)
        return
    if isinstance(before, list) and isinstance(after, list):
        if before != after:
            result.append(
                {
                    "path": path or "/",
                    "operation": "replace",
                    "before": copy.deepcopy(before),
                    "after": copy.deepcopy(after),
                }
            )
        return
    if before != after:
        result.append(
            {
                "path": path or "/",
                "operation": "replace",
                "before": copy.deepcopy(before),
                "after": copy.deepcopy(after),
            }
        )


def normalization_diff(
    before_bundle: Mapping[str, Any],
    after_bundle: Mapping[str, Any],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    _diff_values(
        normalized_payload(before_bundle),
        normalized_payload(after_bundle),
        "",
        result,
    )
    return result
