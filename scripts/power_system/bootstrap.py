"""Initialization-v2 power package construction and sufficiency gate."""

from __future__ import annotations

import copy
import re
from typing import Any, Iterable, Mapping

from .adapters import detect_power_profile, get_adapter
from .model import (
    POWER_COLLECTIONS,
    POWER_SCHEMA_VERSION,
    canonical_power_hash,
    normalize_power_package,
    stable_power_id,
)


INIT_SCHEMA_V1 = "plot-rag-init/v1"
INIT_SCHEMA_V2 = "plot-rag-init/v2"
INIT_SCHEMA_AUTO = "auto"
SUPPORTED_INIT_SCHEMAS = frozenset(
    {INIT_SCHEMA_V1, INIT_SCHEMA_V2, INIT_SCHEMA_AUTO}
)
POWER_PREDICATE_PREFIXES = (
    "power.",
    "progression.",
    "rank.",
    "ability.",
    "resource.",
    "counter.",
    "bridge.",
    "status.",
    "binding.",
    "qualification.",
    "conversion.",
    "observation.",
)
PLACEHOLDER_TERMS = (
    "主线相关能力",
    "核心能力",
    "关键资源",
    "某种能力",
    "待定",
    "unknown",
    "tbd",
)
BARE_ABILITY_LABEL_TERMS = frozenset(
    {
        "能力",
        "技能",
        "法术",
        "功法",
        "神通",
        "异能",
        "术式",
        "招式",
        "天赋",
        "秘术",
        "战技",
    }
)


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return copy.deepcopy(value)
    if isinstance(value, tuple):
        return [copy.deepcopy(item) for item in value]
    return [copy.deepcopy(value)]


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return "\n".join(str(key) + "\n" + _text(child) for key, child in value.items())
    if isinstance(value, (list, tuple)):
        return "\n".join(_text(child) for child in value)
    return "" if value is None else str(value)


def _explicit_profile(dossier: Mapping[str, Any]) -> str | None:
    candidates = (
        dossier.get("power_profile"),
        dossier.get("power_system_profile"),
        (dossier.get("power_system") or {}).get("profile")
        if isinstance(dossier.get("power_system"), dict)
        else None,
    )
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    systems = dossier.get("power_systems")
    if isinstance(systems, list) and systems and isinstance(systems[0], dict):
        value = str(systems[0].get("profile") or "").strip()
        return value or None
    return None


def has_structured_power_model(
    dossier: Mapping[str, Any] | None,
    claims: Iterable[Mapping[str, Any]] = (),
) -> bool:
    value = dossier or {}
    if any(key in value for key in (*POWER_COLLECTIONS, "power_system", "power_profile")):
        if any(value.get(key) not in (None, "", [], {}) for key in value):
            return True
    for claim in claims:
        predicate = str(claim.get("predicate") or "")
        if predicate.startswith(POWER_PREDICATE_PREFIXES):
            return True
    return False


def negotiate_initialization_schema(
    requested: str | None,
    dossier: Mapping[str, Any] | None,
    claims: Iterable[Mapping[str, Any]] = (),
) -> str:
    value = str(requested or INIT_SCHEMA_AUTO)
    if value not in SUPPORTED_INIT_SCHEMAS:
        raise ValueError(f"unsupported initialization schema: {value}")
    if value != INIT_SCHEMA_AUTO:
        return value
    return (
        INIT_SCHEMA_V2
        if has_structured_power_model(dossier, claims)
        else INIT_SCHEMA_V1
    )


def read_power_model_from_bundle(
    bundle: Mapping[str, Any],
) -> dict[str, Any]:
    """Return an explicit v2 model or a lossless v1 compatibility view."""

    if bundle.get("schema_version") == INIT_SCHEMA_V2 and isinstance(
        bundle.get("power_model"),
        dict,
    ):
        return {
            "compatibility": "native_v2",
            "power_model_status": bundle["power_model"].get(
                "power_model_status",
                "unmodeled",
            ),
            "power_model": copy.deepcopy(bundle["power_model"]),
        }
    provenance = bundle.get("provenance")
    legacy_payload = (
        provenance.get("legacy_power_payload")
        if isinstance(provenance, dict)
        else {}
    )
    return {
        "compatibility": "v1_fallback",
        "power_model_status": "unmodeled",
        "power_model": None,
        "legacy_power_payload": copy.deepcopy(legacy_payload or {}),
    }


def _claim_value_name(claim: Mapping[str, Any]) -> str:
    value = claim.get("object_or_value")
    if isinstance(value, dict):
        for key in (
            "name",
            "ability",
            "skill",
            "spell",
            "technique",
            "track",
            "rank",
            "resource",
            "status",
            "qualification",
            "value",
        ):
            candidate = str(value.get(key) or "").strip()
            if candidate:
                return candidate
        return ""
    return str(value or "").strip()


def _actor_records(actor_system: Any) -> list[dict[str, Any]]:
    if not isinstance(actor_system, dict):
        return []
    result: list[dict[str, Any]] = []
    if isinstance(actor_system.get("protagonist"), dict):
        result.append(copy.deepcopy(actor_system["protagonist"]))
    for key in ("opponents", "third_parties"):
        result.extend(
            copy.deepcopy(item)
            for item in actor_system.get(key) or []
            if isinstance(item, dict)
        )
    return result


def _meaningful(value: Any) -> bool:
    if value in (None, "", [], {}):
        return False
    text = _text(value).strip().casefold()
    return bool(text) and not any(term.casefold() == text for term in PLACEHOLDER_TERMS)


def _adapter_native_bare_ability_scaffold(record: Mapping[str, Any]) -> bool:
    name = str(record.get("name") or "").strip()
    ability_id = str(record.get("ability_id") or "").strip()
    if (
        name not in BARE_ABILITY_LABEL_TERMS
        or not ability_id
        or record.get("evidence_claim_ids")
    ):
        return False
    return any(
        isinstance(binding, Mapping)
        and bool(str(binding.get("adapter_id") or "").strip())
        and binding.get("canonical_type") == "ability"
        and str(binding.get("canonical_id") or "").strip() == ability_id
        and str(binding.get("native_term") or "").strip() == name
        and binding.get("mapping_quality") == "lossless"
        and not binding.get("source_claim_ids")
        for binding in record.get("native_term_bindings") or []
    )


def _placeholder_bare_ability(record: Mapping[str, Any]) -> bool:
    name = str(record.get("name") or "").strip()
    return (
        name in BARE_ABILITY_LABEL_TERMS
        and not _adapter_native_bare_ability_scaffold(record)
    )


def _merge_unique(records: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        identifier = str(record.get(key) or "")
        if not identifier:
            result[f"__unresolved__{index}"] = copy.deepcopy(record)
            continue
        result.setdefault(identifier, copy.deepcopy(record))
        if identifier in result:
            existing = result[identifier]
            for field, value in record.items():
                if field not in existing or existing[field] in (None, "", [], {}):
                    existing[field] = copy.deepcopy(value)
                elif isinstance(existing[field], list) and isinstance(value, list):
                    for child in value:
                        if child not in existing[field]:
                            existing[field].append(copy.deepcopy(child))
    return list(result.values())


def build_power_package(
    dossier: Mapping[str, Any] | None,
    claims: Iterable[Mapping[str, Any]] = (),
    *,
    mode: str = "new",
) -> dict[str, Any]:
    """Build a deterministic PowerSpec without inventing rank chains."""

    value = copy.deepcopy(dict(dossier or {}))
    claim_list = [copy.deepcopy(dict(claim)) for claim in claims]
    raw: dict[str, Any] = {
        key: copy.deepcopy(value.get(key) or []) for key in POWER_COLLECTIONS
    }
    nested = value.get("power_system")
    if isinstance(nested, dict):
        for key in POWER_COLLECTIONS:
            if not raw[key] and nested.get(key):
                raw[key] = copy.deepcopy(nested[key])
    explicit = _explicit_profile(value)
    profile = detect_power_profile(
        {
            "genre": value.get("genre_contract"),
            "world": value.get("world_model"),
            "power": {key: raw[key] for key in POWER_COLLECTIONS},
            "claims": claim_list,
        },
        explicit_profile=explicit,
        default="mundane",
    )
    declared_power = bool(
        explicit
        or any(raw[key] for key in POWER_COLLECTIONS)
        or any(
            str(claim.get("predicate") or "").startswith(
                POWER_PREDICATE_PREFIXES
            )
            for claim in claim_list
        )
    )
    if profile == "mundane" and not declared_power:
        # Absence of evidence is not an explicit "no power system" decision.
        # Use the neutral hybrid shell so the initialization gate asks the
        # user instead of silently inventing a mundane contract.
        profile = "hybrid"
    adapter = get_adapter(profile)
    if not raw["power_systems"]:
        name = {
            "mundane": "现实技能与社会资格",
            "hybrid": "复合力量体系",
        }.get(profile, adapter.display_name)
        raw["power_systems"] = [
            {
                "namespace": f"project.{profile}",
                "name": name,
                "profile": profile,
                "adapter_id": adapter.adapter_id,
                "adapter_version": adapter.version,
                "model_status": (
                    "not_applicable" if profile == "mundane" else "partial"
                ),
                "cross_system_policy": "unknown",
                "axioms": [],
                "native_term_bindings": [],
                "no_resource_pool": profile == "mundane",
            }
        ]
    else:
        for system in raw["power_systems"]:
            if isinstance(system, dict):
                system.setdefault("profile", profile)
                system.setdefault("adapter_id", adapter.adapter_id)
                system.setdefault("adapter_version", adapter.version)

    namespace = str(raw["power_systems"][0].get("namespace") or f"project.{profile}")
    system_id = str(
        raw["power_systems"][0].get("power_system_id")
        or stable_power_id(
            "power_system",
            namespace,
            raw["power_systems"][0].get("name") or profile,
        )
    )
    raw["power_systems"][0]["power_system_id"] = system_id

    ability_records: list[dict[str, Any]] = [
        item for item in raw["ability_definitions"] if isinstance(item, dict)
    ]
    bootstrap_by_actor: dict[str, dict[str, Any]] = {}
    for item in raw["actor_power_bootstrap"]:
        if not isinstance(item, dict):
            continue
        actor_name = str(item.get("actor_name") or item.get("name") or "")
        bootstrap_by_actor[actor_name.casefold()] = copy.deepcopy(item)

    def actor_bootstrap(actor_name: str) -> dict[str, Any]:
        key = actor_name.casefold()
        record = bootstrap_by_actor.setdefault(
            key,
            {
                "actor_name": actor_name,
                "actor_id": stable_power_id("character", "actor", actor_name),
                "progression_states": [],
                "ability_ownerships": [],
                "resources": [],
                "statuses": [],
                "bindings": [],
                "qualifications": [],
                "observed_capabilities": [],
            },
        )
        return record

    for actor in _actor_records(value.get("actor_system")):
        actor_name = str(actor.get("name") or actor.get("canonical_name") or "").strip()
        if not actor_name:
            continue
        capabilities = actor.get("capabilities") or []
        if not capabilities:
            continue
        bootstrap = actor_bootstrap(actor_name)
        for capability in capabilities:
            if isinstance(capability, dict):
                ability_name = str(
                    capability.get("name") or capability.get("ability") or ""
                ).strip()
                definition = copy.deepcopy(capability)
            else:
                ability_name = str(capability).strip()
                definition = {"name": ability_name}
            if not ability_name:
                continue
            ability_id = str(
                definition.get("ability_id")
                or stable_power_id("ability", namespace, ability_name)
            )
            definition.update(
                {
                    "ability_id": ability_id,
                    "power_system_id": system_id,
                    "name": ability_name,
                    "ability_kind": str(
                        definition.get("ability_kind") or definition.get("kind") or "active"
                    ),
                }
            )
            ability_records.append(definition)
            bootstrap["ability_ownerships"].append(
                {
                    "ability_id": ability_id,
                    "ownership_status": "owned",
                    "source": copy.deepcopy(definition.get("source")),
                    "level": copy.deepcopy(definition.get("level")),
                    "costs": _list(
                        definition.get("costs", definition.get("cost"))
                    ),
                    "limits": _list(
                        definition.get("limits", definition.get("limit"))
                    ),
                    "source_claim_ids": [],
                }
            )

    claim_bindings: list[dict[str, Any]] = []
    definition_by_name: dict[str, dict[str, Any]] = {
        str(item.get("name") or "").casefold(): item
        for item in ability_records
        if str(item.get("name") or "")
    }
    ownership_suffixes = {
        "owns",
        "owned",
        "has",
        "gain",
        "gained",
        "learned",
        "unlocked",
        "capability",
        "skill",
        "spell",
        "technique",
    }
    field_map = {
        "cost": "costs",
        "costs": "costs",
        "limit": "limits",
        "limits": "limits",
        "counter": "counters",
        "counters": "counters",
        "source": "source_bindings",
        "effect": "effects",
        "effects": "effects",
        "condition": "conditions",
        "conditions": "conditions",
        "cooldown": "cooldown",
        "trigger": "trigger",
        "level": "level",
        "rank": "rank",
    }

    def claim_payload(claim: Mapping[str, Any]) -> dict[str, Any]:
        payload = claim.get("object_or_value")
        return copy.deepcopy(dict(payload)) if isinstance(payload, Mapping) else {}

    def add_claim_provenance(
        record: dict[str, Any],
        claim: Mapping[str, Any],
    ) -> None:
        claim_id = str(claim.get("claim_id") or "")
        if claim_id:
            values = record.setdefault("source_claim_ids", [])
            if claim_id not in values:
                values.append(claim_id)
        record.setdefault(
            "knowledge_plane",
            str(claim.get("knowledge_plane") or "objective"),
        )
        record.setdefault(
            "field_status",
            str(claim.get("field_status") or "known"),
        )
        record.setdefault(
            "native_predicate",
            str(claim.get("predicate") or ""),
        )

    def append_definition(
        *,
        collection: str,
        id_key: str,
        entity_type: str,
        name: str,
        payload: Mapping[str, Any],
        claim: Mapping[str, Any],
        system_bound: bool = True,
        id_namespace: str | None = None,
    ) -> str:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            return ""
        record = copy.deepcopy(dict(payload))
        identifier = str(
            record.get(id_key)
            or stable_power_id(
                entity_type,
                id_namespace or namespace,
                normalized_name,
            )
        )
        record[id_key] = identifier
        record.setdefault("name", normalized_name)
        if system_bound:
            record.setdefault("power_system_id", system_id)
        add_claim_provenance(record, claim)
        raw[collection].append(record)
        return identifier

    for claim in claim_list:
        predicate = str(claim.get("predicate") or "")
        claim_id = str(claim.get("claim_id") or "")
        if not predicate.startswith(POWER_PREDICATE_PREFIXES):
            continue
        suffix = predicate.rsplit(".", 1)[-1]
        binding = {
            "claim_id": claim_id,
            "predicate": predicate,
            "status": "preserved",
            "target_ids": [],
        }
        if predicate.startswith("ability."):
            subject = str(claim.get("subject") or "").strip()
            ability_name = _claim_value_name(claim) if suffix in ownership_suffixes else subject
            if not ability_name:
                ability_name = _claim_value_name(claim)
            ability_id = stable_power_id("ability", namespace, ability_name)
            definition = definition_by_name.setdefault(
                ability_name.casefold(),
                {
                    "ability_id": ability_id,
                    "power_system_id": system_id,
                    "name": ability_name,
                    "ability_kind": "active",
                    "effects": [],
                    "source_bindings": [],
                    "costs": [],
                    "conditions": [],
                    "limits": [],
                    "counters": [],
                    "evidence_claim_ids": [],
                },
            )
            if claim_id and claim_id not in definition.setdefault(
                "evidence_claim_ids", []
            ):
                definition["evidence_claim_ids"].append(claim_id)
            if suffix in ownership_suffixes:
                owner = subject
                bootstrap = actor_bootstrap(owner)
                bootstrap["ability_ownerships"].append(
                    {
                        "ability_id": ability_id,
                        "ownership_status": "owned",
                        "source_claim_ids": [claim_id] if claim_id else [],
                        "native_predicate": predicate,
                        "native_value": copy.deepcopy(claim.get("object_or_value")),
                    }
                )
                binding["target_ids"].extend([bootstrap["actor_id"], ability_id])
            elif suffix in field_map:
                field = field_map[suffix]
                payload = copy.deepcopy(claim.get("object_or_value"))
                if field in {
                    "effects",
                    "source_bindings",
                    "costs",
                    "conditions",
                    "limits",
                    "counters",
                }:
                    definition.setdefault(field, [])
                    for item in _list(payload):
                        if item not in definition[field]:
                            definition[field].append(item)
                else:
                    definition[field] = payload
                binding["target_ids"].append(ability_id)
            else:
                definition.setdefault("native_claims", []).append(
                    {
                        "predicate": predicate,
                        "value": copy.deepcopy(claim.get("object_or_value")),
                        "claim_id": claim_id,
                    }
                )
                binding["target_ids"].append(ability_id)
        elif predicate == "power.system":
            payload = claim_payload(claim)
            system_name = str(
                payload.get("name")
                or _claim_value_name(claim)
                or claim.get("subject")
                or ""
            ).strip()
            if system_name:
                system = raw["power_systems"][0]
                system["name"] = system_name
                for key, value in payload.items():
                    if key not in {"power_system_id", "name"}:
                        system[key] = copy.deepcopy(value)
                add_claim_provenance(system, claim)
                binding["target_ids"].append(system_id)
        elif predicate == "progression.track":
            payload = claim_payload(claim)
            track_name = str(
                payload.get("name")
                or _claim_value_name(claim)
                or claim.get("subject")
                or ""
            ).strip()
            if track_name and payload.get("track_kind"):
                track_id = append_definition(
                    collection="progression_tracks",
                    id_key="track_id",
                    entity_type="progression_track",
                    name=track_name,
                    payload={
                        **payload,
                        "namespace": str(
                            payload.get("namespace")
                            or f"{namespace}.track"
                        ),
                    },
                    claim=claim,
                )
                binding["target_ids"].append(track_id)
        elif predicate == "rank.node":
            payload = claim_payload(claim)
            rank_name = str(
                payload.get("name") or _claim_value_name(claim) or ""
            ).strip()
            track_ref = str(
                payload.get("track_id")
                or payload.get("track_entity_id")
                or payload.get("track_namespace")
                or payload.get("track_name")
                or claim.get("subject")
                or ""
            ).strip()
            if rank_name and track_ref:
                rank_id = append_definition(
                    collection="rank_nodes",
                    id_key="rank_node_id",
                    entity_type="rank_node",
                    name=rank_name,
                    payload={
                        **payload,
                        "track_id": track_ref,
                    },
                    claim=claim,
                    system_bound=False,
                    id_namespace=track_ref,
                )
                binding["target_ids"].append(rank_id)
        elif predicate == "rank.edge":
            payload = claim_payload(claim)
            track_ref = str(
                payload.get("track_id")
                or payload.get("track_entity_id")
                or payload.get("track_namespace")
                or payload.get("track_name")
                or claim.get("subject")
                or ""
            ).strip()
            from_nodes = _list(
                payload.get(
                    "from_node_ids",
                    payload.get("from_rank_entity_ids"),
                )
            )
            to_node = str(
                payload.get("to_node_id")
                or payload.get("to_rank_entity_id")
                or ""
            ).strip()
            if track_ref and from_nodes and to_node:
                edge_name = str(
                    payload.get("name")
                    or f"{','.join(str(item) for item in from_nodes)}->{to_node}"
                )
                edge_id = append_definition(
                    collection="rank_edges",
                    id_key="rank_edge_id",
                    entity_type="rank_edge",
                    name=edge_name,
                    payload={
                        **payload,
                        "track_id": track_ref,
                        "from_node_ids": from_nodes,
                        "to_node_id": to_node,
                    },
                    claim=claim,
                    system_bound=False,
                    id_namespace=track_ref,
                )
                binding["target_ids"].append(edge_id)
        elif predicate == "resource.definition":
            payload = claim_payload(claim)
            resource_name = str(
                payload.get("name")
                or _claim_value_name(claim)
                or claim.get("subject")
                or ""
            ).strip()
            resource_id = append_definition(
                collection="resource_definitions",
                id_key="resource_id",
                entity_type="resource_pool",
                name=resource_name,
                payload=payload,
                claim=claim,
            )
            if resource_id:
                binding["target_ids"].append(resource_id)
        elif predicate == "status.definition":
            payload = claim_payload(claim)
            status_name = str(
                payload.get("name")
                or _claim_value_name(claim)
                or claim.get("subject")
                or ""
            ).strip()
            status_id = append_definition(
                collection="status_definitions",
                id_key="status_id",
                entity_type="status_effect",
                name=status_name,
                payload=payload,
                claim=claim,
            )
            if status_id:
                binding["target_ids"].append(status_id)
        elif predicate == "qualification.definition":
            payload = claim_payload(claim)
            qualification_name = str(
                payload.get("name")
                or _claim_value_name(claim)
                or claim.get("subject")
                or ""
            ).strip()
            qualification_id = append_definition(
                collection="qualification_definitions",
                id_key="qualification_id",
                entity_type="qualification",
                name=qualification_name,
                payload=payload,
                claim=claim,
            )
            if qualification_id:
                binding["target_ids"].append(qualification_id)
        elif predicate == "counter.rule":
            payload = claim_payload(claim)
            rule_name = str(
                payload.get("name")
                or _claim_value_name(claim)
                or claim.get("subject")
                or ""
            ).strip()
            rule_id = append_definition(
                collection="counter_rules",
                id_key="counter_rule_id",
                entity_type="counter_rule",
                name=rule_name,
                payload=payload,
                claim=claim,
            )
            if rule_id:
                binding["target_ids"].append(rule_id)
        elif predicate == "bridge.rule":
            payload = claim_payload(claim)
            source_namespace = str(
                payload.get("source_namespace") or ""
            ).strip()
            target_namespace = str(
                payload.get("target_namespace") or ""
            ).strip()
            if source_namespace and target_namespace:
                rule_name = str(
                    payload.get("name")
                    or f"{source_namespace}->{target_namespace}"
                )
                rule_id = append_definition(
                    collection="bridge_rules",
                    id_key="bridge_rule_id",
                    entity_type="bridge_rule",
                    name=rule_name,
                    payload=payload,
                    claim=claim,
                    system_bound=False,
                    id_namespace=source_namespace,
                )
                binding["target_ids"].append(rule_id)
        elif predicate == "conversion.rule":
            payload = claim_payload(claim)
            source_resource = str(
                payload.get("source_resource_id")
                or payload.get("source_resource")
                or ""
            ).strip()
            target_resource = str(
                payload.get("target_resource_id")
                or payload.get("target_resource")
                or ""
            ).strip()
            ratio = payload.get("ratio")
            if source_resource and target_resource and ratio is not None:
                rule_name = str(
                    payload.get("name")
                    or f"{source_resource}->{target_resource}"
                )
                rule_id = append_definition(
                    collection="conversion_rules",
                    id_key="conversion_rule_id",
                    entity_type="conversion_rule",
                    name=rule_name,
                    payload=payload,
                    claim=claim,
                    system_bound=False,
                    id_namespace=namespace,
                )
                binding["target_ids"].append(rule_id)
        elif predicate == "progression.state":
            payload = claim_payload(claim)
            actor_name = str(claim.get("subject") or "").strip()
            track_ref = str(
                payload.get("track_id")
                or payload.get("track_entity_id")
                or payload.get("track_name")
                or payload.get("track_namespace")
                or ""
            ).strip()
            rank_ref = str(
                payload.get("rank_node_id")
                or payload.get("rank_entity_id")
                or payload.get("rank_name")
                or payload.get("current_rank")
                or ""
            ).strip()
            if actor_name and track_ref and rank_ref:
                bootstrap = actor_bootstrap(actor_name)
                record = {
                    **payload,
                    "track_id": track_ref,
                    "rank_node_id": rank_ref,
                }
                add_claim_provenance(record, claim)
                bootstrap["progression_states"].append(record)
                binding["target_ids"].append(bootstrap["actor_id"])
        elif predicate == "resource.state":
            payload = claim_payload(claim)
            actor_name = str(claim.get("subject") or "").strip()
            resource_ref = str(
                payload.get("resource_id")
                or payload.get("resource_entity_id")
                or payload.get("resource_name")
                or payload.get("name")
                or ""
            ).strip()
            if actor_name and resource_ref and any(
                key in payload for key in ("amount", "current", "balance")
            ):
                bootstrap = actor_bootstrap(actor_name)
                record = {
                    **payload,
                    "resource_id": resource_ref,
                }
                add_claim_provenance(record, claim)
                bootstrap["resources"].append(record)
                binding["target_ids"].append(bootstrap["actor_id"])
        elif predicate == "status.state":
            payload = claim_payload(claim)
            actor_name = str(claim.get("subject") or "").strip()
            status_ref = str(
                payload.get("status_id")
                or payload.get("status_entity_id")
                or payload.get("status_name")
                or payload.get("name")
                or ""
            ).strip()
            if actor_name and status_ref:
                bootstrap = actor_bootstrap(actor_name)
                record = {**payload, "status_id": status_ref}
                add_claim_provenance(record, claim)
                bootstrap["statuses"].append(record)
                binding["target_ids"].append(bootstrap["actor_id"])
        elif predicate == "binding.state":
            payload = claim_payload(claim)
            actor_name = str(claim.get("subject") or "").strip()
            source_name = str(
                payload.get("source_name")
                or payload.get("name")
                or ""
            ).strip()
            source_type = str(
                payload.get("source_entity_type")
                or payload.get("source_type")
                or ""
            ).strip()
            if actor_name and source_name and source_type:
                bootstrap = actor_bootstrap(actor_name)
                record = copy.deepcopy(payload)
                add_claim_provenance(record, claim)
                bootstrap["bindings"].append(record)
                binding["target_ids"].append(bootstrap["actor_id"])
        elif predicate == "qualification.state":
            payload = claim_payload(claim)
            actor_name = str(claim.get("subject") or "").strip()
            qualification_ref = str(
                payload.get("qualification_id")
                or payload.get("qualification_entity_id")
                or payload.get("qualification_name")
                or payload.get("name")
                or ""
            ).strip()
            if actor_name and qualification_ref:
                bootstrap = actor_bootstrap(actor_name)
                record = {
                    **payload,
                    "qualification_id": qualification_ref,
                }
                add_claim_provenance(record, claim)
                bootstrap["qualifications"].append(record)
                binding["target_ids"].append(bootstrap["actor_id"])
        elif predicate == "observation.capability":
            payload = claim_payload(claim)
            observer_name = str(claim.get("subject") or "").strip()
            subject_ref = str(
                payload.get("subject_entity_id")
                or payload.get("subject_id")
                or ""
            ).strip()
            ability_ref = str(
                payload.get("ability_id")
                or payload.get("ability_entity_id")
                or payload.get("ability_name")
                or payload.get("name")
                or ""
            ).strip()
            if observer_name and (subject_ref or ability_ref):
                bootstrap = actor_bootstrap(observer_name)
                record = copy.deepcopy(payload)
                record.setdefault(
                    "knowledge_plane",
                    str(claim.get("knowledge_plane") or "actor_belief"),
                )
                add_claim_provenance(record, claim)
                bootstrap["observed_capabilities"].append(record)
                binding["target_ids"].append(bootstrap["actor_id"])
        elif predicate == "power.profile":
            if claim_id:
                raw["power_systems"][0].setdefault(
                    "evidence_claim_ids",
                    [],
                ).append(claim_id)
            binding["target_ids"].append(system_id)
        claim_bindings.append(binding)

    ability_records = _merge_unique(list(definition_by_name.values()), "ability_id")
    raw["ability_definitions"] = ability_records
    for collection, id_key in (
        ("progression_tracks", "track_id"),
        ("rank_nodes", "rank_node_id"),
        ("rank_edges", "rank_edge_id"),
        ("resource_definitions", "resource_id"),
        ("status_definitions", "status_id"),
        ("qualification_definitions", "qualification_id"),
        ("counter_rules", "counter_rule_id"),
        ("bridge_rules", "bridge_rule_id"),
        ("conversion_rules", "conversion_rule_id"),
    ):
        raw[collection] = _merge_unique(
            [
                item
                for item in raw[collection]
                if isinstance(item, dict)
            ],
            id_key,
        )
    raw["actor_power_bootstrap"] = list(bootstrap_by_actor.values())
    raw["claim_bindings"] = claim_bindings
    raw["adapter_versions"] = {adapter.adapter_id: adapter.version}
    raw["semantic_losses"] = [
        {
            "claim_id": item["claim_id"],
            "reason": "predicate preserved without a typed target",
        }
        for item in claim_bindings
        if not item["target_ids"]
    ]
    raw["power_model_status"] = (
        "not_applicable"
        if profile == "mundane"
        else (
            "modeled"
            if raw["progression_tracks"]
            or raw["ability_definitions"]
            or raw["resource_definitions"]
            else "partial"
        )
    )
    package = normalize_power_package(raw)
    package["power_package_hash"] = canonical_power_hash(package)
    return package


def power_sufficiency(
    package: Mapping[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    systems = package.get("power_systems") or []
    profile = str((systems[0] if systems else {}).get("profile") or "")
    mundane = profile == "mundane"
    abilities = package.get("ability_definitions") or []
    resources = package.get("resource_definitions") or []
    tracks = package.get("progression_tracks") or []
    nodes = package.get("rank_nodes") or []
    edges = package.get("rank_edges") or []
    checks = {
        "profile_declared": profile
        in {
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
        },
        "source_traceable": mundane
        or bool(
            any(
                (item.get("source_bindings") or item.get("source"))
                for item in abilities
                if isinstance(item, dict)
            )
            or package.get("claim_bindings")
        ),
        "current_progression_identified": mundane
        or bool(
            tracks
            and (
                nodes
                or any(
                    item.get("track_kind") in {"open_ended", "numeric_level", "none"}
                    for item in tracks
                    if isinstance(item, dict)
                )
            )
        ),
        "resource_cycle": mundane
        or bool(
            any(
                isinstance(item, dict)
                and item.get("acquisition")
                and item.get("consumption")
                and item.get("recovery")
                for item in resources
            )
            or any(
                isinstance(item, dict) and item.get("no_resource_pool")
                for item in systems
            )
        ),
        "ability_contract": mundane
        or bool(
            any(
                isinstance(item, dict)
                and _meaningful(item.get("name"))
                and (item.get("source_bindings") or item.get("source"))
                and (item.get("costs") or item.get("cost"))
                and (item.get("limits") or item.get("limit"))
                and (item.get("counters") or item.get("counter"))
                for item in abilities
            )
        ),
        "advancement_failure_modeled": mundane
        or not edges
        or bool(
            all(
                isinstance(item, dict)
                and item.get("prerequisites")
                and item.get("failure_outcomes")
                for item in edges
            )
        ),
        "social_consequence": mundane
        or bool(
            any(
                isinstance(item, dict)
                and (
                    item.get("social_consequences")
                    or item.get("institutional_effects")
                    or item.get("qualification_effects")
                )
                for item in [*systems, *nodes]
            )
        ),
        "no_placeholder_only": mundane
        or not any(
            (
                str(item.get("name") or "").strip().casefold()
                in {term.casefold() for term in PLACEHOLDER_TERMS}
                or _placeholder_bare_ability(item)
            )
            for item in abilities
            if isinstance(item, dict)
        ),
        "mundane_has_no_generated_rank_chain": not mundane
        or not tracks
        or all(
            isinstance(item, dict) and item.get("track_kind") == "none"
            for item in tracks
        ),
    }
    blocking = [
        key
        for key, passed in checks.items()
        if not passed and mode in {"new", "hybrid"}
    ]
    return {
        "profile": profile,
        "power_model_status": package.get("power_model_status"),
        "checks": checks,
        "sufficient": not blocking,
        "blocking_checks": blocking,
    }
