"""Deterministic, genre-neutral power-system value model.

The module intentionally accepts and returns plain dictionaries at the
serialization boundary.  Frozen dataclasses provide a stable Python API while
``normalize_power_package`` remains the canonical ingestion path.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


POWER_SCHEMA_VERSION = "plot-rag-power/v1"
TRACK_KINDS = frozenset(
    {
        "ordered_rank",
        "numeric_level",
        "branch_tree",
        "dag",
        "state_machine",
        "open_ended",
        "none",
    }
)
MAPPING_QUALITIES = frozenset({"lossless", "partial", "unmapped"})
POWER_PROFILES = frozenset(
    {
        "cultivation",
        "magic",
        "skill_tree",
        "game",
        "martial",
        "superpower",
        "bloodline",
        "technology",
        "contract_summoning",
        "system_assist",
        "hybrid",
        "mundane",
    }
)
POWER_COLLECTIONS = (
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
POWER_SOURCE_ENTITY_TYPES = frozenset(
    {
        "item",
        "ability",
        "bloodline",
        "contract",
        "faction",
        "role",
        "system",
        "summon",
    }
)


class PowerModelError(ValueError):
    """Stable validation error for power-system packages."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        self.code = str(code)
        self.details = copy.deepcopy(details)
        super().__init__(message)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_power_hash(value: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(value))
    payload.pop("power_package_hash", None)
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def stable_power_id(entity_type: str, namespace: str, name: Any) -> str:
    digest = hashlib.sha256(
        _canonical_json(
            {
                "entity_type": str(entity_type),
                "namespace": str(namespace),
                "name": name,
            }
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"ent-{digest}"


@dataclass(frozen=True)
class NativeTermBinding:
    native_term: str
    canonical_type: str
    canonical_id: str | None = None
    adapter_id: str = ""
    adapter_version: str = ""
    mapping_quality: str = "lossless"
    semantic_loss: tuple[str, ...] = ()
    source_claim_ids: tuple[str, ...] = ()
    binding_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["semantic_loss"] = list(self.semantic_loss)
        value["source_claim_ids"] = list(self.source_claim_ids)
        value["binding_id"] = self.binding_id or stable_power_id(
            "native_term_binding",
            self.adapter_id or "project",
            [self.native_term, self.canonical_type, self.canonical_id],
        )
        return value


@dataclass(frozen=True)
class PowerSystemSpec:
    power_system_id: str
    namespace: str
    name: str
    profile: str
    adapter_id: str
    adapter_version: str
    axioms: tuple[Any, ...] = ()
    visibility: str = "objective"
    cross_system_policy: str = "unknown"
    model_status: str = "modeled"
    native_term_bindings: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["axioms"] = list(self.axioms)
        value["native_term_bindings"] = [
            copy.deepcopy(item) for item in self.native_term_bindings
        ]
        return value


@dataclass(frozen=True)
class ProgressionTrack:
    track_id: str
    power_system_id: str
    namespace: str
    name: str
    track_kind: str
    description: str = ""
    native_term_bindings: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["native_term_bindings"] = [
            copy.deepcopy(item) for item in self.native_term_bindings
        ]
        return value


@dataclass(frozen=True)
class RankNode:
    rank_node_id: str
    track_id: str
    name: str
    order: float | None = None
    state: str = "defined"
    prerequisites: dict[str, Any] = field(default_factory=dict)
    consequences: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RankEdge:
    rank_edge_id: str
    track_id: str
    from_node_ids: tuple[str, ...]
    to_node_id: str
    prerequisites: dict[str, Any] = field(default_factory=dict)
    resource_costs: tuple[dict[str, Any], ...] = ()
    risks: tuple[Any, ...] = ()
    failure_outcomes: tuple[Any, ...] = ()
    allows_skip: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["from_node_ids"] = list(self.from_node_ids)
        value["resource_costs"] = [copy.deepcopy(item) for item in self.resource_costs]
        value["risks"] = list(self.risks)
        value["failure_outcomes"] = list(self.failure_outcomes)
        return value


@dataclass(frozen=True)
class AbilityDefinition:
    ability_id: str
    power_system_id: str
    name: str
    ability_kind: str = "active"
    effects: tuple[Any, ...] = ()
    source_bindings: tuple[Any, ...] = ()
    costs: tuple[Any, ...] = ()
    conditions: tuple[Any, ...] = ()
    limits: tuple[Any, ...] = ()
    counters: tuple[Any, ...] = ()
    cooldown: Any = None
    evidence_claim_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in (
            "effects",
            "source_bindings",
            "costs",
            "conditions",
            "limits",
            "counters",
            "evidence_claim_ids",
        ):
            value[key] = list(value[key])
        return value


@dataclass(frozen=True)
class ResourcePoolDefinition:
    resource_id: str
    power_system_id: str
    name: str
    resource_kind: str = "stock"
    unit: str = ""
    acquisition: tuple[Any, ...] = ()
    consumption: tuple[Any, ...] = ()
    recovery: tuple[Any, ...] = ()
    capacity: Any = None
    debt_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("acquisition", "consumption", "recovery"):
            value[key] = list(value[key])
        return value


@dataclass(frozen=True)
class StatusEffectDefinition:
    status_id: str
    power_system_id: str
    name: str
    status_kind: str = "effect"
    stack_policy: str = "replace"
    max_stacks: int | None = None
    duration: Any = None
    effects: tuple[Any, ...] = ()
    removal_conditions: tuple[Any, ...] = ()
    native_term_bindings: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["effects"] = list(self.effects)
        value["removal_conditions"] = list(self.removal_conditions)
        value["native_term_bindings"] = [
            copy.deepcopy(item) for item in self.native_term_bindings
        ]
        return value


@dataclass(frozen=True)
class QualificationDefinition:
    qualification_id: str
    power_system_id: str
    name: str
    qualification_kind: str = "permission"
    grant_sources: tuple[Any, ...] = ()
    consumption_rules: tuple[Any, ...] = ()
    expiry_rules: tuple[Any, ...] = ()
    prerequisites: tuple[Any, ...] = ()
    effects: tuple[Any, ...] = ()
    slot_key: str = ""
    max_quantity: float | None = None
    native_term_bindings: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in (
            "grant_sources",
            "consumption_rules",
            "expiry_rules",
            "prerequisites",
            "effects",
        ):
            value[key] = list(value[key])
        value["native_term_bindings"] = [
            copy.deepcopy(item) for item in self.native_term_bindings
        ]
        return value


@dataclass(frozen=True)
class CounterRule:
    counter_rule_id: str
    power_system_id: str
    name: str
    source_tags: tuple[str, ...] = ()
    target_tags: tuple[str, ...] = ()
    relation: str = "counter"
    conditions: tuple[Any, ...] = ()
    priority: int = 0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_tags"] = list(self.source_tags)
        value["target_tags"] = list(self.target_tags)
        value["conditions"] = list(self.conditions)
        return value


@dataclass(frozen=True)
class BridgeRule:
    bridge_rule_id: str
    source_namespace: str
    target_namespace: str
    direction: str = "one_way"
    reversible: bool = False
    conditions: tuple[Any, ...] = ()
    conversion: dict[str, Any] = field(default_factory=dict)
    conflict_policy: str = "narrower_wins"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["conditions"] = list(self.conditions)
        return value


@dataclass(frozen=True)
class ConversionRule:
    conversion_rule_id: str
    source_resource_id: str
    target_resource_id: str
    ratio: float
    source_system_id: str = ""
    target_system_id: str = ""
    fixed_cost: float = 0.0
    loss_ratio: float = 0.0
    capacity: Any = None
    rounding: str = "exact"
    reversible: bool = False
    conditions: tuple[Any, ...] = ()
    conflict_policy: str = "narrower_wins"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["conditions"] = list(self.conditions)
        return value


@dataclass(frozen=True)
class ActorPowerBootstrap:
    actor_id: str
    actor_name: str
    progression_states: tuple[dict[str, Any], ...] = ()
    ability_ownerships: tuple[dict[str, Any], ...] = ()
    resources: tuple[dict[str, Any], ...] = ()
    statuses: tuple[dict[str, Any], ...] = ()
    bindings: tuple[dict[str, Any], ...] = ()
    qualifications: tuple[dict[str, Any], ...] = ()
    observed_capabilities: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in (
            "progression_states",
            "ability_ownerships",
            "resources",
            "statuses",
            "bindings",
            "qualifications",
            "observed_capabilities",
        ):
            value[key] = [copy.deepcopy(item) for item in value[key]]
        return value


@dataclass(frozen=True)
class PowerSpec:
    schema_version: str
    power_systems: tuple[dict[str, Any], ...]
    progression_tracks: tuple[dict[str, Any], ...]
    rank_nodes: tuple[dict[str, Any], ...]
    rank_edges: tuple[dict[str, Any], ...]
    ability_definitions: tuple[dict[str, Any], ...]
    resource_definitions: tuple[dict[str, Any], ...]
    status_definitions: tuple[dict[str, Any], ...]
    qualification_definitions: tuple[dict[str, Any], ...]
    counter_rules: tuple[dict[str, Any], ...]
    bridge_rules: tuple[dict[str, Any], ...]
    conversion_rules: tuple[dict[str, Any], ...]
    actor_power_bootstrap: tuple[dict[str, Any], ...]
    power_model_status: str = "modeled"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in POWER_COLLECTIONS:
            value[key] = [copy.deepcopy(item) for item in value[key]]
        value["power_package_hash"] = canonical_power_hash(value)
        return value


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return copy.deepcopy(value)
    if isinstance(value, tuple):
        return [copy.deepcopy(item) for item in value]
    return [copy.deepcopy(value)]


def _name(value: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        candidate = str(value.get(key) or "").strip()
        if candidate:
            return candidate
    return ""


def _normalize_binding(
    raw: Mapping[str, Any],
    *,
    adapter_id: str,
    adapter_version: str,
) -> dict[str, Any]:
    term = _name(raw, "native_term", "term", "name")
    canonical_type = _name(raw, "canonical_type", "object_type") or "unknown"
    quality = _name(raw, "mapping_quality", "quality") or "lossless"
    if quality not in MAPPING_QUALITIES:
        quality = "partial"
    binding = NativeTermBinding(
        native_term=term,
        canonical_type=canonical_type,
        canonical_id=str(raw.get("canonical_id") or "") or None,
        adapter_id=str(raw.get("adapter_id") or adapter_id),
        adapter_version=str(raw.get("adapter_version") or adapter_version),
        mapping_quality=quality,
        semantic_loss=tuple(str(item) for item in _list(raw.get("semantic_loss"))),
        source_claim_ids=tuple(
            str(item) for item in _list(raw.get("source_claim_ids")) if str(item)
        ),
        binding_id=str(raw.get("binding_id") or ""),
    )
    return binding.to_dict()


def normalize_power_package(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize a power package and generate deterministic entity ids."""

    source = copy.deepcopy(dict(raw))
    systems: list[dict[str, Any]] = []
    system_ids: dict[str, str] = {}
    for index, item in enumerate(_list(source.get("power_systems"))):
        if not isinstance(item, dict):
            continue
        namespace = _name(item, "namespace") or f"power.system.{index + 1}"
        profile = _name(item, "profile") or "hybrid"
        name = _name(item, "name", "display_name") or namespace
        adapter_id = _name(item, "adapter_id") or f"plot-rag-power.{profile}"
        adapter_version = _name(item, "adapter_version") or "1.0.0"
        system_id = _name(item, "power_system_id", "system_id") or stable_power_id(
            "power_system", namespace, name
        )
        bindings = [
            _normalize_binding(
                binding,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
            )
            for binding in _list(item.get("native_term_bindings"))
            if isinstance(binding, dict)
        ]
        if not any(
            binding.get("native_term") == name
            and binding.get("canonical_type") == "power_system"
            for binding in bindings
        ):
            bindings.append(
                _normalize_binding(
                    {
                        "native_term": name,
                        "canonical_type": "power_system",
                        "canonical_id": system_id,
                        "mapping_quality": "lossless",
                    },
                    adapter_id=adapter_id,
                    adapter_version=adapter_version,
                )
            )
        normalized = copy.deepcopy(item)
        normalized.update(
            {
                "power_system_id": system_id,
                "namespace": namespace,
                "name": name,
                "profile": profile,
                "adapter_id": adapter_id,
                "adapter_version": adapter_version,
                "axioms": _list(item.get("axioms")),
                "visibility": _name(item, "visibility") or "objective",
                "cross_system_policy": _name(item, "cross_system_policy") or "unknown",
                "model_status": _name(item, "model_status") or "modeled",
                "native_term_bindings": bindings,
            }
        )
        systems.append(normalized)
        system_ids[namespace] = system_id
        system_ids[system_id] = system_id

    default_system_id = systems[0]["power_system_id"] if systems else ""
    tracks: list[dict[str, Any]] = []
    track_ids: dict[str, str] = {}
    for index, item in enumerate(_list(source.get("progression_tracks"))):
        if not isinstance(item, dict):
            continue
        namespace = _name(item, "namespace") or f"power.track.{index + 1}"
        name = _name(item, "name", "display_name") or namespace
        system_ref = _name(item, "power_system_id", "system_id", "system_namespace")
        system_id = system_ids.get(system_ref, system_ref) or default_system_id
        kind = _name(item, "track_kind") or "open_ended"
        track_id = _name(item, "track_id", "progression_track_id") or stable_power_id(
            "progression_track", namespace, name
        )
        track_bindings = [
            _normalize_binding(
                binding,
                adapter_id=(
                    systems[0]["adapter_id"] if systems else "plot-rag-power.hybrid"
                ),
                adapter_version=(
                    systems[0]["adapter_version"] if systems else "1.0.0"
                ),
            )
            for binding in _list(item.get("native_term_bindings"))
            if isinstance(binding, dict)
        ]
        if not any(
            binding.get("native_term") == name
            and binding.get("canonical_type") == "progression_track"
            for binding in track_bindings
        ):
            track_bindings.append(
                _normalize_binding(
                    {
                        "native_term": name,
                        "canonical_type": "progression_track",
                        "canonical_id": track_id,
                    },
                    adapter_id=(
                        systems[0]["adapter_id"] if systems else "plot-rag-power.hybrid"
                    ),
                    adapter_version=(
                        systems[0]["adapter_version"] if systems else "1.0.0"
                    ),
                )
            )
        normalized = copy.deepcopy(item)
        normalized.update(
            {
                "track_id": track_id,
                "power_system_id": system_id,
                "namespace": namespace,
                "name": name,
                "track_kind": kind,
                "native_term_bindings": track_bindings,
            }
        )
        tracks.append(normalized)
        track_ids[namespace] = track_id
        track_ids[name] = track_id
        track_ids[track_id] = track_id

    nodes: list[dict[str, Any]] = []
    node_ids: dict[str, str] = {}
    for index, item in enumerate(_list(source.get("rank_nodes"))):
        if not isinstance(item, dict):
            continue
        track_ref = _name(item, "track_id", "progression_track_id", "track_namespace")
        track_id = track_ids.get(track_ref, track_ref)
        name = _name(item, "name", "native_term", "label") or f"rank-{index + 1}"
        node_id = _name(item, "rank_node_id", "node_id") or stable_power_id(
            "rank_node", track_id or "unbound", name
        )
        node_bindings = [
            _normalize_binding(
                binding,
                adapter_id=(
                    systems[0]["adapter_id"] if systems else "plot-rag-power.hybrid"
                ),
                adapter_version=(
                    systems[0]["adapter_version"] if systems else "1.0.0"
                ),
            )
            for binding in _list(item.get("native_term_bindings"))
            if isinstance(binding, dict)
        ]
        if not any(
            binding.get("native_term") == name
            and binding.get("canonical_type") == "rank_node"
            for binding in node_bindings
        ):
            node_bindings.append(
                _normalize_binding(
                    {
                        "native_term": name,
                        "canonical_type": "rank_node",
                        "canonical_id": node_id,
                    },
                    adapter_id=(
                        systems[0]["adapter_id"] if systems else "plot-rag-power.hybrid"
                    ),
                    adapter_version=(
                        systems[0]["adapter_version"] if systems else "1.0.0"
                    ),
                )
            )
        normalized = copy.deepcopy(item)
        normalized.update(
            {
                "rank_node_id": node_id,
                "track_id": track_id,
                "name": name,
                "state": _name(item, "state") or "defined",
                "native_term_bindings": node_bindings,
            }
        )
        nodes.append(normalized)
        node_ids[name] = node_id
        node_ids[node_id] = node_id

    edges: list[dict[str, Any]] = []
    for item in _list(source.get("rank_edges")):
        if not isinstance(item, dict):
            continue
        track_ref = _name(item, "track_id", "progression_track_id", "track_namespace")
        track_id = track_ids.get(track_ref, track_ref)
        from_ids = [
            node_ids.get(str(value), str(value))
            for value in _list(
                item.get("from_node_ids", item.get("from_rank_entity_ids"))
            )
            if str(value)
        ]
        to_ref = _name(item, "to_node_id", "to_rank_entity_id", "to")
        to_id = node_ids.get(to_ref, to_ref)
        edge_id = _name(item, "rank_edge_id", "edge_id") or stable_power_id(
            "rank_edge", track_id or "unbound", [from_ids, to_id]
        )
        normalized = copy.deepcopy(item)
        normalized.update(
            {
                "rank_edge_id": edge_id,
                "track_id": track_id,
                "from_node_ids": from_ids,
                "to_node_id": to_id,
                "prerequisites": copy.deepcopy(item.get("prerequisites") or {}),
                "resource_costs": _list(
                    item.get("resource_costs", item.get("costs"))
                ),
                "risks": _list(item.get("risks")),
                "failure_outcomes": _list(item.get("failure_outcomes")),
                "allows_skip": bool(item.get("allows_skip", False)),
            }
        )
        edges.append(normalized)

    def normalize_definition(
        collection: str,
        id_key: str,
        entity_type: str,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for index, item in enumerate(_list(source.get(collection))):
            if not isinstance(item, dict):
                continue
            name = _name(item, "name", "native_term", "label") or f"{entity_type}-{index + 1}"
            system_ref = _name(item, "power_system_id", "system_id", "system_namespace")
            system_id = system_ids.get(system_ref, system_ref) or default_system_id
            identifier = _name(item, id_key, "entity_id") or stable_power_id(
                entity_type, system_id or "unbound", name
            )
            definition_bindings = [
                _normalize_binding(
                    binding,
                    adapter_id=(
                        systems[0]["adapter_id"]
                        if systems
                        else "plot-rag-power.hybrid"
                    ),
                    adapter_version=(
                        systems[0]["adapter_version"] if systems else "1.0.0"
                    ),
                )
                for binding in _list(item.get("native_term_bindings"))
                if isinstance(binding, dict)
            ]
            if not any(
                binding.get("native_term") == name
                and binding.get("canonical_type") == entity_type
                for binding in definition_bindings
            ):
                definition_bindings.append(
                    _normalize_binding(
                        {
                            "native_term": name,
                            "canonical_type": entity_type,
                            "canonical_id": identifier,
                        },
                        adapter_id=(
                            systems[0]["adapter_id"]
                            if systems
                            else "plot-rag-power.hybrid"
                        ),
                        adapter_version=(
                            systems[0]["adapter_version"]
                            if systems
                            else "1.0.0"
                        ),
                    )
                )
            normalized = copy.deepcopy(item)
            normalized.update(
                {
                    id_key: identifier,
                    "power_system_id": system_id,
                    "name": name,
                    "native_term_bindings": definition_bindings,
                }
            )
            result.append(normalized)
        return result

    abilities = normalize_definition("ability_definitions", "ability_id", "ability")
    resources = normalize_definition(
        "resource_definitions", "resource_id", "resource_pool"
    )
    statuses = normalize_definition(
        "status_definitions", "status_id", "status_effect"
    )
    qualifications = normalize_definition(
        "qualification_definitions",
        "qualification_id",
        "qualification",
    )
    counters = normalize_definition(
        "counter_rules", "counter_rule_id", "counter_rule"
    )
    resource_ids = {
        str(item.get("name") or ""): str(item.get("resource_id") or "")
        for item in resources
    }
    resource_ids.update(
        {
            str(item.get("resource_id") or ""): str(item.get("resource_id") or "")
            for item in resources
        }
    )
    status_ids = {
        str(item.get("name") or ""): str(item.get("status_id") or "")
        for item in statuses
    }
    status_ids.update(
        {
            str(item.get("status_id") or ""): str(item.get("status_id") or "")
            for item in statuses
        }
    )
    qualification_ids = {
        str(item.get("name") or ""): str(item.get("qualification_id") or "")
        for item in qualifications
    }
    qualification_ids.update(
        {
            str(item.get("qualification_id") or ""): str(
                item.get("qualification_id") or ""
            )
            for item in qualifications
        }
    )

    bridges: list[dict[str, Any]] = []
    for index, item in enumerate(_list(source.get("bridge_rules"))):
        if not isinstance(item, dict):
            continue
        src = _name(item, "source_namespace", "source")
        dst = _name(item, "target_namespace", "target")
        identifier = _name(item, "bridge_rule_id", "entity_id") or stable_power_id(
            "bridge_rule", src or "unknown", [dst, index]
        )
        normalized = copy.deepcopy(item)
        normalized.update(
            {
                "bridge_rule_id": identifier,
                "source_namespace": src,
                "target_namespace": dst,
                "direction": _name(item, "direction") or "one_way",
                "reversible": bool(item.get("reversible", False)),
                "conditions": _list(item.get("conditions")),
                "conversion": copy.deepcopy(item.get("conversion") or {}),
                "conflict_policy": _name(item, "conflict_policy")
                or "narrower_wins",
            }
        )
        bridges.append(normalized)

    conversions: list[dict[str, Any]] = []
    for index, item in enumerate(_list(source.get("conversion_rules"))):
        if not isinstance(item, dict):
            continue
        source_resource_ref = _name(
            item,
            "source_resource_id",
            "source_resource_entity_id",
            "source_resource",
        )
        target_resource_ref = _name(
            item,
            "target_resource_id",
            "target_resource_entity_id",
            "target_resource",
        )
        source_resource_id = resource_ids.get(
            source_resource_ref,
            source_resource_ref,
        )
        target_resource_id = resource_ids.get(
            target_resource_ref,
            target_resource_ref,
        )
        source_system_ref = _name(
            item,
            "source_system_id",
            "source_system_entity_id",
            "source_namespace",
        )
        target_system_ref = _name(
            item,
            "target_system_id",
            "target_system_entity_id",
            "target_namespace",
        )
        source_system_id = (
            system_ids.get(source_system_ref, source_system_ref)
            or default_system_id
        )
        target_system_id = (
            system_ids.get(target_system_ref, target_system_ref)
            or default_system_id
        )
        identifier = _name(
            item,
            "conversion_rule_id",
            "rule_entity_id",
            "entity_id",
        ) or stable_power_id(
            "conversion_rule",
            source_system_id or "unbound",
            [source_resource_id, target_resource_id, index],
        )
        normalized = copy.deepcopy(item)
        normalized.update(
            {
                "conversion_rule_id": identifier,
                "source_system_id": source_system_id,
                "target_system_id": target_system_id,
                "source_resource_id": source_resource_id,
                "target_resource_id": target_resource_id,
                "ratio": item.get("ratio"),
                "fixed_cost": item.get("fixed_cost", 0),
                "loss_ratio": item.get("loss_ratio", item.get("loss", 0)),
                "capacity": copy.deepcopy(item.get("capacity")),
                "rounding": _name(item, "rounding") or "exact",
                "reversible": bool(item.get("reversible", False)),
                "conditions": _list(item.get("conditions")),
                "conflict_policy": _name(item, "conflict_policy")
                or "narrower_wins",
            }
        )
        conversions.append(normalized)

    ability_ids = {
        str(item.get("name") or ""): str(item.get("ability_id") or "")
        for item in abilities
    }
    ability_ids.update(
        {
            str(item.get("ability_id") or ""): str(item.get("ability_id") or "")
            for item in abilities
        }
    )
    bootstraps: list[dict[str, Any]] = []
    for item in _list(source.get("actor_power_bootstrap")):
        if not isinstance(item, dict):
            continue
        actor_name = _name(item, "actor_name", "name")
        actor_id = _name(item, "actor_id", "actor_entity_id") or stable_power_id(
            "character", "actor", actor_name
        )
        normalized = copy.deepcopy(item)
        progression_states: list[dict[str, Any]] = []
        for state in _list(item.get("progression_states")):
            if not isinstance(state, dict):
                continue
            track_ref = _name(
                state,
                "track_id",
                "track_entity_id",
                "track_namespace",
                "track_name",
            )
            rank_ref = _name(
                state,
                "rank_node_id",
                "to_rank_entity_id",
                "current_rank_id",
                "rank_name",
                "current_rank",
            )
            normalized_state = copy.deepcopy(state)
            normalized_state["track_id"] = track_ids.get(track_ref, track_ref)
            normalized_state["rank_node_id"] = node_ids.get(rank_ref, rank_ref)
            progression_states.append(normalized_state)
        ownerships: list[dict[str, Any]] = []
        for ownership in _list(item.get("ability_ownerships")):
            if not isinstance(ownership, dict):
                continue
            ability_ref = _name(
                ownership,
                "ability_id",
                "ability_entity_id",
                "ability_name",
                "name",
            )
            normalized_ownership = copy.deepcopy(ownership)
            normalized_ownership["ability_id"] = ability_ids.get(
                ability_ref,
                ability_ref,
            )
            normalized_ownership["ownership_id"] = str(
                ownership.get("ownership_id")
                or stable_power_id(
                    "ability_ownership",
                    actor_id,
                    normalized_ownership["ability_id"],
                )
            )
            normalized_ownership["unlock_state"] = (
                _name(ownership, "unlock_state", "state") or "unlocked"
            )
            ownerships.append(normalized_ownership)
        actor_resources: list[dict[str, Any]] = []
        for state in _list(item.get("resources")):
            if not isinstance(state, dict):
                continue
            resource_ref = _name(
                state,
                "resource_id",
                "resource_entity_id",
                "resource_name",
                "name",
            )
            normalized_state = copy.deepcopy(state)
            normalized_state["resource_id"] = resource_ids.get(
                resource_ref,
                resource_ref,
            )
            normalized_state["resource_state_id"] = str(
                state.get("resource_state_id")
                or stable_power_id(
                    "actor_resource_state",
                    actor_id,
                    normalized_state["resource_id"],
                )
            )
            normalized_state["amount"] = state.get(
                "amount",
                state.get("current", state.get("balance", 0)),
            )
            actor_resources.append(normalized_state)
        actor_statuses: list[dict[str, Any]] = []
        for state in _list(item.get("statuses")):
            if not isinstance(state, dict):
                continue
            status_ref = _name(
                state,
                "status_id",
                "status_entity_id",
                "status_name",
                "name",
            )
            normalized_state = copy.deepcopy(state)
            normalized_state["status_id"] = status_ids.get(
                status_ref,
                status_ref,
            )
            normalized_state["status_state_id"] = str(
                state.get("status_state_id")
                or stable_power_id(
                    "actor_status_state",
                    actor_id,
                    normalized_state["status_id"],
                )
            )
            normalized_state["stacks"] = state.get("stacks", 1)
            actor_statuses.append(normalized_state)
        actor_bindings: list[dict[str, Any]] = []
        for binding in _list(item.get("bindings")):
            if not isinstance(binding, dict):
                continue
            source_type = _name(
                binding,
                "source_entity_type",
                "source_type",
            ) or "item"
            source_name = _name(
                binding,
                "source_name",
                "name",
            ) or source_type
            source_id = _name(binding, "source_entity_id") or stable_power_id(
                source_type,
                "power-source",
                source_name,
            )
            raw_ability_ids = _list(
                binding.get("ability_entity_ids", binding.get("ability_ids"))
            )
            resolved_ability_ids = [
                ability_ids.get(str(value), str(value))
                for value in raw_ability_ids
                if str(value)
            ]
            normalized_binding = copy.deepcopy(binding)
            normalized_binding.update(
                {
                    "binding_id": str(
                        binding.get("binding_id")
                        or stable_power_id(
                            "power_binding",
                            actor_id,
                            [source_id, binding.get("slot_key")],
                        )
                    ),
                    "source_entity_id": source_id,
                    "source_entity_type": source_type,
                    "source_name": source_name,
                    "ability_entity_ids": list(
                        dict.fromkeys(resolved_ability_ids)
                    ),
                    "action": _name(binding, "action") or "bind",
                    "unique": bool(binding.get("unique", False)),
                    "inactive_behavior": (
                        _name(binding, "inactive_behavior")
                        or "disable_linked_abilities"
                    ),
                    "source_claim_ids": [
                        str(value)
                        for value in _list(binding.get("source_claim_ids"))
                        if str(value)
                    ],
                }
            )
            actor_bindings.append(normalized_binding)
        actor_qualifications: list[dict[str, Any]] = []
        for qualification in _list(item.get("qualifications")):
            if not isinstance(qualification, dict):
                continue
            qualification_ref = _name(
                qualification,
                "qualification_id",
                "qualification_entity_id",
                "qualification_name",
                "qualification",
                "name",
            )
            qualification_id = qualification_ids.get(
                qualification_ref,
                qualification_ref,
            )
            normalized_qualification = copy.deepcopy(qualification)
            normalized_qualification.update(
                {
                    "qualification_id": qualification_id,
                    "qualification_state_id": str(
                        qualification.get("qualification_state_id")
                        or stable_power_id(
                            "qualification_state",
                            actor_id,
                            qualification_id,
                        )
                    ),
                    "action": _name(qualification, "action") or "grant",
                    "quantity": qualification.get("quantity", 1),
                    "source_claim_ids": [
                        str(value)
                        for value in _list(
                            qualification.get("source_claim_ids")
                        )
                        if str(value)
                    ],
                }
            )
            actor_qualifications.append(normalized_qualification)
        observations: list[dict[str, Any]] = []
        for observation in _list(item.get("observed_capabilities")):
            if not isinstance(observation, dict):
                continue
            ability_ref = _name(
                observation,
                "ability_id",
                "ability_entity_id",
                "ability_name",
            )
            ability_id = ability_ids.get(ability_ref, ability_ref)
            subject_id = _name(
                observation,
                "subject_entity_id",
                "subject_id",
            ) or actor_id
            observer_id = _name(
                observation,
                "observer_entity_id",
                "observer_id",
            ) or actor_id
            normalized_observation = copy.deepcopy(observation)
            normalized_observation.update(
                {
                    "observation_id": str(
                        observation.get("observation_id")
                        or stable_power_id(
                            "observed_capability",
                            observer_id,
                            [
                                subject_id,
                                ability_id,
                                observation.get("observed_fields"),
                            ],
                        )
                    ),
                    "observer_entity_id": observer_id,
                    "subject_entity_id": subject_id,
                    "ability_id": ability_id or None,
                    "action": _name(observation, "action") or "observe",
                    "knowledge_plane": (
                        _name(observation, "knowledge_plane")
                        or "actor_belief"
                    ),
                    "observed_fields": [
                        str(value)
                        for value in _list(
                            observation.get("observed_fields")
                        )
                        if str(value)
                    ],
                    "confidence": observation.get("confidence", 0.5),
                    "source_claim_ids": [
                        str(value)
                        for value in _list(
                            observation.get(
                                "source_claim_ids",
                                observation.get("evidence_claim_ids"),
                            )
                        )
                        if str(value)
                    ],
                }
            )
            observations.append(normalized_observation)
        normalized.update(
            {
                "actor_id": actor_id,
                "actor_name": actor_name,
                "progression_states": progression_states,
                "ability_ownerships": ownerships,
                "resources": actor_resources,
                "statuses": actor_statuses,
                "bindings": actor_bindings,
                "qualifications": actor_qualifications,
                "observed_capabilities": observations,
            }
        )
        bootstraps.append(normalized)

    package = {
        "schema_version": POWER_SCHEMA_VERSION,
        "power_model_status": str(
            source.get("power_model_status")
            or ("modeled" if systems else "unmodeled")
        ),
        "adapter_versions": copy.deepcopy(source.get("adapter_versions") or {}),
        "claim_bindings": _list(source.get("claim_bindings")),
        "semantic_losses": _list(source.get("semantic_losses")),
        "power_systems": systems,
        "progression_tracks": tracks,
        "rank_nodes": nodes,
        "rank_edges": edges,
        "ability_definitions": abilities,
        "resource_definitions": resources,
        "status_definitions": statuses,
        "qualification_definitions": qualifications,
        "counter_rules": counters,
        "bridge_rules": bridges,
        "conversion_rules": conversions,
        "actor_power_bootstrap": bootstraps,
    }
    for key in POWER_COLLECTIONS:
        package[key] = sorted(
            package[key],
            key=lambda value: _canonical_json(value),
        )
    validate_power_package(package)
    package["power_package_hash"] = canonical_power_hash(package)
    return package


def validate_power_package(package: Mapping[str, Any]) -> None:
    if package.get("schema_version") != POWER_SCHEMA_VERSION:
        raise PowerModelError(
            "POWER_SCHEMA_VERSION_UNSUPPORTED",
            "unsupported power-system schema version",
            actual=package.get("schema_version"),
        )
    for collection in POWER_COLLECTIONS:
        if not isinstance(package.get(collection), list):
            raise PowerModelError(
                "POWER_COLLECTION_INVALID",
                f"{collection} must be an array",
                collection=collection,
            )
    systems = {
        str(item.get("power_system_id")): item
        for item in package["power_systems"]
        if isinstance(item, dict)
    }
    for system_id, system in systems.items():
        if not system_id:
            raise PowerModelError(
                "POWER_ID_REQUIRED", "power system id is required"
            )
        if system.get("profile") not in POWER_PROFILES:
            raise PowerModelError(
                "POWER_PROFILE_UNSUPPORTED",
                "power-system profile is not registered",
                profile=system.get("profile"),
            )
    tracks = {
        str(item.get("track_id")): item
        for item in package["progression_tracks"]
        if isinstance(item, dict)
    }
    for track_id, track in tracks.items():
        if track.get("track_kind") not in TRACK_KINDS:
            raise PowerModelError(
                "POWER_TRACK_KIND_UNSUPPORTED",
                "progression track kind is unsupported",
                track_id=track_id,
                track_kind=track.get("track_kind"),
            )
        if systems and str(track.get("power_system_id")) not in systems:
            raise PowerModelError(
                "POWER_ENDPOINT_UNRESOLVED",
                "progression track references an unknown power system",
                track_id=track_id,
            )
    nodes = {
        str(item.get("rank_node_id")): item
        for item in package["rank_nodes"]
        if isinstance(item, dict)
    }
    for node_id, node in nodes.items():
        if tracks and str(node.get("track_id")) not in tracks:
            raise PowerModelError(
                "POWER_ENDPOINT_UNRESOLVED",
                "rank node references an unknown progression track",
                rank_node_id=node_id,
            )
    for edge in package["rank_edges"]:
        if not isinstance(edge, dict):
            continue
        track_id = str(edge.get("track_id") or "")
        if tracks and track_id not in tracks:
            raise PowerModelError(
                "POWER_ENDPOINT_UNRESOLVED",
                "rank edge references an unknown progression track",
                rank_edge_id=edge.get("rank_edge_id"),
            )
        endpoint_ids = [
            *[str(value) for value in edge.get("from_node_ids") or []],
            str(edge.get("to_node_id") or ""),
        ]
        for endpoint in endpoint_ids:
            if nodes and endpoint not in nodes:
                raise PowerModelError(
                    "POWER_ENDPOINT_UNRESOLVED",
                    "rank edge references an unknown rank node",
                    rank_edge_id=edge.get("rank_edge_id"),
                    endpoint=endpoint,
                )
            if endpoint in nodes and str(nodes[endpoint].get("track_id")) != track_id:
                raise PowerModelError(
                    "POWER_TRACK_MISMATCH",
                    "rank edge endpoints must belong to the same track",
                    rank_edge_id=edge.get("rank_edge_id"),
                    endpoint=endpoint,
                )
    for collection in (
        "ability_definitions",
        "resource_definitions",
        "status_definitions",
        "qualification_definitions",
        "counter_rules",
    ):
        for definition in package[collection]:
            if (
                systems
                and isinstance(definition, dict)
                and str(definition.get("power_system_id")) not in systems
            ):
                raise PowerModelError(
                    "POWER_ENDPOINT_UNRESOLVED",
                    f"{collection} entry references an unknown power system",
                    collection=collection,
                )
    ability_ids = {
        str(item.get("ability_id"))
        for item in package["ability_definitions"]
        if isinstance(item, dict)
    }
    resource_ids = {
        str(item.get("resource_id"))
        for item in package["resource_definitions"]
        if isinstance(item, dict)
    }
    status_ids = {
        str(item.get("status_id"))
        for item in package["status_definitions"]
        if isinstance(item, dict)
    }
    qualification_ids = {
        str(item.get("qualification_id"))
        for item in package["qualification_definitions"]
        if isinstance(item, dict)
    }
    for definition in package["qualification_definitions"]:
        if not isinstance(definition, dict):
            continue
        qualification_id = str(definition.get("qualification_id") or "")
        if not qualification_id:
            raise PowerModelError(
                "POWER_ID_REQUIRED",
                "qualification definition id is required",
            )
        max_quantity = definition.get("max_quantity")
        if max_quantity is not None:
            try:
                normalized_max_quantity = float(max_quantity)
            except (TypeError, ValueError) as error:
                raise PowerModelError(
                    "POWER_QUALIFICATION_INVALID",
                    "qualification max_quantity must be a finite number",
                    qualification_id=qualification_id,
                ) from error
            if (
                not math.isfinite(normalized_max_quantity)
                or normalized_max_quantity <= 0
            ):
                raise PowerModelError(
                    "POWER_QUALIFICATION_INVALID",
                    "qualification max_quantity must be positive",
                    qualification_id=qualification_id,
                    max_quantity=max_quantity,
                )
    for rule in package["conversion_rules"]:
        if not isinstance(rule, dict):
            continue
        rule_id = str(rule.get("conversion_rule_id") or "")
        source_resource_id = str(rule.get("source_resource_id") or "")
        target_resource_id = str(rule.get("target_resource_id") or "")
        if not rule_id:
            raise PowerModelError(
                "POWER_ID_REQUIRED",
                "conversion rule id is required",
            )
        if (
            not source_resource_id
            or not target_resource_id
            or (resource_ids and source_resource_id not in resource_ids)
            or (resource_ids and target_resource_id not in resource_ids)
        ):
            raise PowerModelError(
                "POWER_ENDPOINT_UNRESOLVED",
                "conversion rule references an unknown resource",
                conversion_rule_id=rule_id,
                source_resource_id=source_resource_id,
                target_resource_id=target_resource_id,
            )
        try:
            ratio = float(rule.get("ratio"))
            fixed_cost = float(rule.get("fixed_cost", 0))
            loss_ratio = float(rule.get("loss_ratio", 0))
        except (TypeError, ValueError) as error:
            raise PowerModelError(
                "POWER_CONVERSION_INVALID",
                "conversion ratio and costs must be finite numbers",
                conversion_rule_id=rule_id,
            ) from error
        if (
            not math.isfinite(ratio)
            or not math.isfinite(fixed_cost)
            or not math.isfinite(loss_ratio)
            or not ratio > 0
            or fixed_cost < 0
            or not 0 <= loss_ratio < 1
        ):
            raise PowerModelError(
                "POWER_CONVERSION_INVALID",
                "conversion ratio must be positive and losses must be bounded",
                conversion_rule_id=rule_id,
                ratio=ratio,
                fixed_cost=fixed_cost,
                loss_ratio=loss_ratio,
            )
        for field in ("source_system_id", "target_system_id"):
            system_id = str(rule.get(field) or "")
            if systems and system_id not in systems:
                raise PowerModelError(
                    "POWER_ENDPOINT_UNRESOLVED",
                    "conversion rule references an unknown power system",
                    conversion_rule_id=rule_id,
                    field=field,
                    system_id=system_id,
                )
    for actor in package["actor_power_bootstrap"]:
        if not isinstance(actor, dict):
            continue
        actor_id = str(actor.get("actor_id") or "")
        for progression in actor.get("progression_states") or []:
            if not isinstance(progression, dict):
                continue
            track_id = str(progression.get("track_id") or "")
            rank_node_id = str(progression.get("rank_node_id") or "")
            if track_id and tracks and track_id not in tracks:
                raise PowerModelError(
                    "POWER_ENDPOINT_UNRESOLVED",
                    "actor bootstrap references an unknown progression track",
                    actor_id=actor_id,
                    track_id=track_id,
                )
            if rank_node_id and nodes and rank_node_id not in nodes:
                raise PowerModelError(
                    "POWER_ENDPOINT_UNRESOLVED",
                    "actor bootstrap references an unknown rank node",
                    actor_id=actor_id,
                    rank_node_id=rank_node_id,
                )
            if (
                rank_node_id in nodes
                and track_id
                and str(nodes[rank_node_id].get("track_id") or "")
                != track_id
            ):
                raise PowerModelError(
                    "POWER_TRACK_MISMATCH",
                    "actor bootstrap rank must belong to its progression track",
                    actor_id=actor_id,
                    track_id=track_id,
                    rank_node_id=rank_node_id,
                )
        for ownership in actor.get("ability_ownerships") or []:
            ability_id = str((ownership or {}).get("ability_id") or "")
            if ability_id and ability_ids and ability_id not in ability_ids:
                raise PowerModelError(
                    "POWER_ENDPOINT_UNRESOLVED",
                    "actor bootstrap references an unknown ability",
                    actor_id=actor.get("actor_id"),
                    ability_id=ability_id,
                )
        for state in actor.get("resources") or []:
            resource_id = str((state or {}).get("resource_id") or "")
            if resource_id and resource_ids and resource_id not in resource_ids:
                raise PowerModelError(
                    "POWER_ENDPOINT_UNRESOLVED",
                    "actor bootstrap references an unknown resource",
                    actor_id=actor.get("actor_id"),
                    resource_id=resource_id,
                )
        for state in actor.get("statuses") or []:
            status_id = str((state or {}).get("status_id") or "")
            if status_id and status_ids and status_id not in status_ids:
                raise PowerModelError(
                    "POWER_ENDPOINT_UNRESOLVED",
                    "actor bootstrap references an unknown status definition",
                    actor_id=actor.get("actor_id"),
                    status_id=status_id,
                )
        for binding in actor.get("bindings") or []:
            if not isinstance(binding, dict):
                continue
            binding_id = str(binding.get("binding_id") or "")
            source_entity_id = str(binding.get("source_entity_id") or "")
            source_entity_type = str(
                binding.get("source_entity_type") or ""
            )
            if not binding_id or not source_entity_id:
                raise PowerModelError(
                    "POWER_BINDING_INVALID",
                    "power binding requires stable binding and source ids",
                    actor_id=actor_id,
                )
            if source_entity_type not in POWER_SOURCE_ENTITY_TYPES:
                raise PowerModelError(
                    "POWER_BINDING_SOURCE_TYPE_UNSUPPORTED",
                    "power binding source type is unsupported",
                    actor_id=actor_id,
                    binding_id=binding_id,
                    source_entity_type=source_entity_type,
                )
            for ability_id in binding.get("ability_entity_ids") or []:
                resolved = str(ability_id or "")
                if resolved and ability_ids and resolved not in ability_ids:
                    raise PowerModelError(
                        "POWER_ENDPOINT_UNRESOLVED",
                        "power binding references an unknown ability",
                        actor_id=actor_id,
                        binding_id=binding_id,
                        ability_id=resolved,
                    )
        for qualification in actor.get("qualifications") or []:
            if not isinstance(qualification, dict):
                continue
            qualification_id = str(
                qualification.get("qualification_id")
                or qualification.get("qualification_entity_id")
                or ""
            )
            if (
                qualification_id
                and qualification_ids
                and qualification_id not in qualification_ids
            ):
                raise PowerModelError(
                    "POWER_ENDPOINT_UNRESOLVED",
                    "actor bootstrap references an unknown qualification",
                    actor_id=actor_id,
                    qualification_id=qualification_id,
                )
            try:
                quantity = float(qualification.get("quantity", 1))
            except (TypeError, ValueError) as error:
                raise PowerModelError(
                    "POWER_QUALIFICATION_INVALID",
                    "qualification quantity must be a finite number",
                    actor_id=actor_id,
                    qualification_id=qualification_id,
                ) from error
            if not math.isfinite(quantity) or quantity <= 0:
                raise PowerModelError(
                    "POWER_QUALIFICATION_INVALID",
                    "qualification quantity must be positive",
                    actor_id=actor_id,
                    qualification_id=qualification_id,
                    quantity=quantity,
                )
        for observation in actor.get("observed_capabilities") or []:
            if not isinstance(observation, dict):
                continue
            ability_id = str(
                observation.get("ability_id")
                or observation.get("ability_entity_id")
                or ""
            )
            if ability_id and ability_ids and ability_id not in ability_ids:
                raise PowerModelError(
                    "POWER_ENDPOINT_UNRESOLVED",
                    "observed capability references an unknown ability",
                    actor_id=actor_id,
                    ability_id=ability_id,
                )
            knowledge_plane = str(
                observation.get("knowledge_plane") or ""
            )
            if knowledge_plane not in KNOWLEDGE_PLANES:
                raise PowerModelError(
                    "POWER_KNOWLEDGE_PLANE_INVALID",
                    "observed capability uses an unsupported knowledge plane",
                    actor_id=actor_id,
                    knowledge_plane=knowledge_plane,
                )
            try:
                confidence = float(observation.get("confidence", 0.5))
            except (TypeError, ValueError) as error:
                raise PowerModelError(
                    "POWER_OBSERVATION_INVALID",
                    "observed capability confidence must be numeric",
                    actor_id=actor_id,
                ) from error
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise PowerModelError(
                    "POWER_OBSERVATION_INVALID",
                    "observed capability confidence must be between zero and one",
                    actor_id=actor_id,
                    confidence=confidence,
                )
