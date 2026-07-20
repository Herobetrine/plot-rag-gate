"""Versioned registry for declarative genre adapters."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from .model import (
    KNOWLEDGE_PLANES,
    POWER_PROFILES,
    PowerModelError,
    stable_power_id,
)


ADAPTER_SCHEMA_VERSION = "plot-rag-power-adapter/v1"
ADAPTER_CONTRACT_OPERATIONS = (
    "detect_native_terms",
    "normalize_definition",
    "normalize_actor_state",
    "normalize_event",
    "validate_transition",
    "render_native_projection",
    "compile_query_terms",
    "report_semantic_loss",
)

_DEFINITION_IDS = {
    "power_system": "power_system_id",
    "progression_track": "track_id",
    "rank_node": "rank_node_id",
    "rank_edge": "rank_edge_id",
    "ability": "ability_id",
    "resource_pool": "resource_id",
    "status_effect": "status_id",
    "qualification": "qualification_id",
    "counter_rule": "counter_rule_id",
    "bridge_rule": "bridge_rule_id",
    "conversion_rule": "conversion_rule_id",
}
_EVENT_TYPE_ALIASES = {
    "progression": "progression",
    "rank": "progression",
    "境界": "progression",
    "等级": "progression",
    "突破": "progression",
    "升级": "progression",
    "ability": "ability",
    "skill": "ability",
    "能力": "ability",
    "技能": "ability",
    "resource": "resource",
    "资源": "resource",
    "status": "status_effect",
    "status_effect": "status_effect",
    "状态": "status_effect",
    "binding": "power_binding",
    "power_binding": "power_binding",
    "绑定": "power_binding",
    "qualification": "qualification",
    "资格": "qualification",
    "observation": "power_observation",
    "power_observation": "power_observation",
    "观察": "power_observation",
}
_ACTION_ALIASES = {
    "突破": "advance",
    "晋升": "advance",
    "升级": "advance",
    "advance": "advance",
    "获得": "gain",
    "学会": "gain",
    "解锁": "unlock",
    "使用": "use",
    "发动": "use",
    "施放": "use",
    "冷却": "cooldown",
    "失去": "lose",
    "消耗": "spend",
    "恢复": "recover",
    "转换": "convert",
    "施加": "apply",
    "移除": "remove",
    "绑定": "bind",
    "解除": "unbind",
    "装备": "equip",
    "卸下": "unequip",
    "授予": "grant",
    "撤销": "revoke",
    "观察": "observe",
    "确认": "confirm",
}


@dataclass(frozen=True)
class AdapterSpec:
    adapter_id: str
    version: str
    profile: str
    display_name: str
    aliases: tuple[str, ...]
    detection_terms: tuple[str, ...]
    track_kinds: tuple[str, ...]
    native_terms: dict[str, tuple[str, ...]]
    required_dimensions: tuple[str, ...]
    question_prompts: tuple[str, ...]
    no_rank_generation: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AdapterSpec":
        if value.get("schema_version") != ADAPTER_SCHEMA_VERSION:
            raise PowerModelError(
                "POWER_ADAPTER_SCHEMA_UNSUPPORTED",
                "unsupported adapter schema version",
                actual=value.get("schema_version"),
            )
        profile = str(value.get("profile") or "")
        if profile not in POWER_PROFILES:
            raise PowerModelError(
                "POWER_PROFILE_UNSUPPORTED",
                "adapter profile is not registered",
                profile=profile,
            )
        native_terms = value.get("native_terms") or {}
        if not isinstance(native_terms, dict):
            raise PowerModelError(
                "POWER_ADAPTER_INVALID",
                "native_terms must be an object",
                profile=profile,
            )
        return cls(
            adapter_id=str(value.get("adapter_id") or f"plot-rag-power.{profile}"),
            version=str(value.get("version") or "1.0.0"),
            profile=profile,
            display_name=str(value.get("display_name") or profile),
            aliases=tuple(str(item) for item in value.get("aliases") or []),
            detection_terms=tuple(
                str(item) for item in value.get("detection_terms") or []
            ),
            track_kinds=tuple(str(item) for item in value.get("track_kinds") or []),
            native_terms={
                str(key): tuple(str(item) for item in terms or [])
                for key, terms in native_terms.items()
            },
            required_dimensions=tuple(
                str(item) for item in value.get("required_dimensions") or []
            ),
            question_prompts=tuple(
                str(item) for item in value.get("question_prompts") or []
            ),
            no_rank_generation=bool(value.get("no_rank_generation", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "adapter_id": self.adapter_id,
            "version": self.version,
            "profile": self.profile,
            "display_name": self.display_name,
            "aliases": list(self.aliases),
            "detection_terms": list(self.detection_terms),
            "track_kinds": list(self.track_kinds),
            "native_terms": {
                key: list(value) for key, value in self.native_terms.items()
            },
            "required_dimensions": list(self.required_dimensions),
            "question_prompts": list(self.question_prompts),
            "no_rank_generation": self.no_rank_generation,
        }

    @property
    def contract_operations(self) -> tuple[str, ...]:
        return ADAPTER_CONTRACT_OPERATIONS

    def detect_native_terms(self, value: Any) -> dict[str, Any]:
        return detect_native_terms(self.profile, value)

    def normalize_definition(
        self,
        canonical_type: str,
        value: Mapping[str, Any],
    ) -> dict[str, Any]:
        return normalize_definition(self.profile, canonical_type, value)

    def normalize_actor_state(
        self,
        value: Mapping[str, Any],
    ) -> dict[str, Any]:
        return normalize_actor_state(self.profile, value)

    def normalize_event(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return normalize_event(self.profile, value)

    def validate_transition(
        self,
        event: Mapping[str, Any],
        *,
        actor_state: Mapping[str, Any] | None = None,
        power_spec: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return validate_transition(
            self.profile,
            event,
            actor_state=actor_state,
            power_spec=power_spec,
        )


class AdapterRegistry:
    def __init__(self, adapters: Iterable[AdapterSpec]) -> None:
        values = list(adapters)
        profiles = [item.profile for item in values]
        adapter_ids = [item.adapter_id for item in values]
        if len(set(profiles)) != len(profiles):
            duplicates = sorted(
                profile for profile in set(profiles) if profiles.count(profile) > 1
            )
            raise PowerModelError(
                "POWER_ADAPTER_DUPLICATE_PROFILE",
                "adapter registry contains duplicate profiles",
                duplicates=duplicates,
            )
        if len(set(adapter_ids)) != len(adapter_ids):
            duplicates = sorted(
                adapter_id
                for adapter_id in set(adapter_ids)
                if adapter_ids.count(adapter_id) > 1
            )
            raise PowerModelError(
                "POWER_ADAPTER_DUPLICATE_ID",
                "adapter registry contains duplicate adapter ids",
                duplicates=duplicates,
            )
        self._by_profile = {item.profile: item for item in values}
        self._by_id = {item.adapter_id: item for item in values}
        if set(self._by_profile) != set(POWER_PROFILES):
            raise PowerModelError(
                "POWER_ADAPTER_SET_INCOMPLETE",
                "adapter registry must contain all declared profiles",
                missing=sorted(set(POWER_PROFILES) - set(self._by_profile)),
                extra=sorted(set(self._by_profile) - set(POWER_PROFILES)),
            )

    def get(self, profile_or_id: str) -> AdapterSpec:
        key = str(profile_or_id or "")
        adapter = self._by_profile.get(key) or self._by_id.get(key)
        if adapter is None:
            raise PowerModelError(
                "POWER_ADAPTER_NOT_FOUND",
                "power adapter is not registered",
                adapter=key,
            )
        return adapter

    def profiles(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_profile))

    def all(self) -> tuple[AdapterSpec, ...]:
        return tuple(self._by_profile[key] for key in sorted(self._by_profile))

    def detect(self, value: Any, *, default: str = "mundane") -> str:
        text = _flatten_text(value).casefold()
        scores: dict[str, int] = {}
        for adapter in self.all():
            score = 0
            for term in (*adapter.aliases, *adapter.detection_terms):
                normalized = term.casefold().strip()
                if normalized and normalized in text:
                    score += max(1, len(normalized))
            if score:
                scores[adapter.profile] = score
        if not scores:
            return default
        best = max(scores.values())
        winners = sorted(profile for profile, score in scores.items() if score == best)
        if len(winners) > 1:
            return "hybrid"
        return winners[0]


def _flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return "\n".join(
            f"{key}\n{_flatten_text(child)}"
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return "\n".join(_flatten_text(child) for child in value)
    return "" if value is None else str(value)


def _adapter_root() -> Path:
    return Path(__file__).resolve().parents[2] / "knowledge" / "power_adapters"


@lru_cache(maxsize=1)
def adapter_registry() -> AdapterRegistry:
    adapters: list[AdapterSpec] = []
    for path in sorted(_adapter_root().glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise PowerModelError(
                "POWER_ADAPTER_INVALID",
                "adapter file must contain an object",
                path=str(path),
            )
        adapters.append(AdapterSpec.from_mapping(payload))
    return AdapterRegistry(adapters)


def get_adapter(profile_or_id: str) -> AdapterSpec:
    return adapter_registry().get(profile_or_id)


def detect_power_profile(
    value: Any,
    *,
    explicit_profile: str | None = None,
    default: str = "mundane",
) -> str:
    if explicit_profile:
        return get_adapter(explicit_profile).profile
    return adapter_registry().detect(value, default=default)


def detect_native_terms(profile: str, value: Any) -> dict[str, Any]:
    """Return deterministic native-term matches without assigning authority."""

    adapter = get_adapter(profile)
    text = _flatten_text(value).casefold()
    matches: list[dict[str, str]] = []
    for canonical_type, terms in sorted(adapter.native_terms.items()):
        for native_term in sorted(set(terms)):
            normalized = native_term.casefold().strip()
            if normalized and normalized in text:
                matches.append(
                    {
                        "native_term": native_term,
                        "canonical_type": canonical_type,
                        "mapping_quality": "lossless",
                    }
                )
    return {
        "adapter_id": adapter.adapter_id,
        "adapter_version": adapter.version,
        "profile": adapter.profile,
        "matches": matches,
    }


def normalize_definition(
    profile: str,
    canonical_type: str,
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize one project-native definition into the shared ontology."""

    adapter = get_adapter(profile)
    object_type = str(canonical_type or "").strip()
    id_key = _DEFINITION_IDS.get(object_type)
    if id_key is None:
        raise PowerModelError(
            "POWER_DEFINITION_TYPE_UNSUPPORTED",
            "adapter cannot normalize this definition type",
            canonical_type=object_type,
        )
    raw = copy.deepcopy(dict(value))
    name = str(
        raw.get("name")
        or raw.get("native_term")
        or raw.get("display_name")
        or ""
    ).strip()
    if not name:
        raise PowerModelError(
            "POWER_DEFINITION_NAME_REQUIRED",
            "power definition requires a project-native name",
            canonical_type=object_type,
        )
    namespace = str(
        raw.get("namespace")
        or raw.get("power_system_id")
        or f"adapter.{adapter.profile}"
    ).strip()
    identifier = str(
        raw.get(id_key)
        or raw.get("entity_id")
        or stable_power_id(object_type, namespace, name)
    )
    binding = {
        "binding_id": stable_power_id(
            "native_term_binding",
            adapter.adapter_id,
            [name, object_type, identifier],
        ),
        "native_term": name,
        "canonical_type": object_type,
        "canonical_id": identifier,
        "adapter_id": adapter.adapter_id,
        "adapter_version": adapter.version,
        "mapping_quality": "lossless",
        "semantic_loss": [],
        "source_claim_ids": [
            str(item)
            for item in raw.get("source_claim_ids") or []
            if str(item)
        ],
    }
    bindings = [
        copy.deepcopy(item)
        for item in raw.get("native_term_bindings") or []
        if isinstance(item, Mapping)
    ]
    if not any(
        item.get("native_term") == name
        and item.get("canonical_type") == object_type
        for item in bindings
    ):
        bindings.append(binding)
    raw.update(
        {
            id_key: identifier,
            "name": name,
            "native_term_bindings": sorted(
                bindings,
                key=lambda item: json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
            "adapter_id": adapter.adapter_id,
            "adapter_version": adapter.version,
        }
    )
    if object_type == "power_system":
        raw.setdefault("profile", adapter.profile)
        raw.setdefault("namespace", namespace)
        raw.setdefault("model_status", "modeled")
        raw.setdefault("cross_system_policy", "unknown")
    return raw


def _canonical_reference(
    value: Mapping[str, Any],
    keys: tuple[str, ...],
    *,
    entity_type: str,
    namespace: str,
) -> str:
    for key in keys:
        candidate = str(value.get(key) or "").strip()
        if candidate:
            if candidate.startswith("ent-"):
                return candidate
            return stable_power_id(entity_type, namespace, candidate)
    return ""


def normalize_actor_state(
    profile: str,
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize definition references while preserving unknown state fields."""

    adapter = get_adapter(profile)
    raw = copy.deepcopy(dict(value))
    actor_name = str(raw.get("actor_name") or raw.get("name") or "").strip()
    if not actor_name:
        raise PowerModelError(
            "POWER_ACTOR_NAME_REQUIRED",
            "actor power state requires an actor name",
        )
    actor_id = str(
        raw.get("actor_id")
        or raw.get("actor_entity_id")
        or stable_power_id("character", "actor", actor_name)
    )
    namespace = str(
        raw.get("power_namespace") or f"adapter.{adapter.profile}"
    )

    def records(field: str) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(dict(item))
            for item in raw.get(field) or []
            if isinstance(item, Mapping)
        ]

    progression_states = records("progression_states")
    for state in progression_states:
        state["track_id"] = _canonical_reference(
            state,
            ("track_id", "track_entity_id", "track_name", "track_namespace"),
            entity_type="progression_track",
            namespace=namespace,
        )
        state["rank_node_id"] = _canonical_reference(
            state,
            (
                "rank_node_id",
                "rank_entity_id",
                "rank_name",
                "current_rank",
            ),
            entity_type="rank_node",
            namespace=state["track_id"] or namespace,
        )

    ownerships = records("ability_ownerships")
    for ownership in ownerships:
        ability_id = _canonical_reference(
            ownership,
            ("ability_id", "ability_entity_id", "ability_name", "name"),
            entity_type="ability",
            namespace=namespace,
        )
        ownership.update(
            {
                "ability_id": ability_id,
                "ownership_id": str(
                    ownership.get("ownership_id")
                    or stable_power_id(
                        "ability_ownership",
                        actor_id,
                        ability_id,
                    )
                ),
                "unlock_state": str(
                    ownership.get("unlock_state") or "unlocked"
                ),
            }
        )

    resources = records("resources")
    for state in resources:
        resource_id = _canonical_reference(
            state,
            ("resource_id", "resource_entity_id", "resource_name", "name"),
            entity_type="resource_pool",
            namespace=namespace,
        )
        state.update(
            {
                "resource_id": resource_id,
                "resource_state_id": str(
                    state.get("resource_state_id")
                    or stable_power_id(
                        "actor_resource_state",
                        actor_id,
                        resource_id,
                    )
                ),
                "amount": state.get(
                    "amount",
                    state.get("current", state.get("balance", 0)),
                ),
            }
        )

    statuses = records("statuses")
    for state in statuses:
        status_id = _canonical_reference(
            state,
            ("status_id", "status_entity_id", "status_name", "name"),
            entity_type="status_effect",
            namespace=namespace,
        )
        state.update(
            {
                "status_id": status_id,
                "status_state_id": str(
                    state.get("status_state_id")
                    or stable_power_id(
                        "actor_status_state",
                        actor_id,
                        status_id,
                    )
                ),
                "stacks": state.get("stacks", 1),
            }
        )

    qualifications = records("qualifications")
    for state in qualifications:
        qualification_id = _canonical_reference(
            state,
            (
                "qualification_id",
                "qualification_entity_id",
                "qualification_name",
                "qualification",
                "name",
            ),
            entity_type="qualification",
            namespace=namespace,
        )
        state.update(
            {
                "qualification_id": qualification_id,
                "qualification_state_id": str(
                    state.get("qualification_state_id")
                    or stable_power_id(
                        "qualification_state",
                        actor_id,
                        qualification_id,
                    )
                ),
                "action": str(state.get("action") or "grant"),
                "quantity": state.get("quantity", 1),
            }
        )

    result = {
        **raw,
        "actor_id": actor_id,
        "actor_name": actor_name,
        "adapter_id": adapter.adapter_id,
        "adapter_version": adapter.version,
        "progression_states": progression_states,
        "ability_ownerships": ownerships,
        "resources": resources,
        "statuses": statuses,
        "bindings": records("bindings"),
        "qualifications": qualifications,
        "observed_capabilities": records("observed_capabilities"),
    }
    return result


def normalize_event(
    profile: str,
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize project-native event labels while retaining original terms."""

    adapter = get_adapter(profile)
    raw = copy.deepcopy(dict(value))
    native_event_type = str(
        raw.get("event_type")
        or raw.get("category")
        or raw.get("type")
        or ""
    ).strip()
    event_type = _EVENT_TYPE_ALIASES.get(
        native_event_type.casefold(),
        native_event_type,
    )
    native_action = str(raw.get("action") or raw.get("verb") or "").strip()
    action = _ACTION_ALIASES.get(native_action.casefold(), native_action)
    if not event_type:
        raise PowerModelError(
            "POWER_EVENT_TYPE_REQUIRED",
            "power event requires an event type",
        )
    normalized = {
        **raw,
        "event_type": event_type,
        "action": action,
        "adapter_id": adapter.adapter_id,
        "adapter_version": adapter.version,
        "native_event_type": native_event_type,
        "native_action": native_action,
    }
    if not normalized.get("knowledge_plane"):
        normalized["knowledge_plane"] = "objective"
    if normalized["knowledge_plane"] not in KNOWLEDGE_PLANES:
        raise PowerModelError(
            "POWER_KNOWLEDGE_PLANE_INVALID",
            "power event uses an unsupported knowledge plane",
            knowledge_plane=normalized["knowledge_plane"],
        )
    return normalized


def _has_unknown(value: Any) -> bool:
    if isinstance(value, Mapping):
        if str(value.get("field_status") or "").casefold() in {
            "unknown",
            "conflicted",
            "deferred",
        }:
            return True
        return any(_has_unknown(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_unknown(child) for child in value)
    return False


def validate_transition(
    profile: str,
    event: Mapping[str, Any],
    *,
    actor_state: Mapping[str, Any] | None = None,
    power_spec: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the adapter-neutral fail-closed transition contract."""

    adapter = get_adapter(profile)
    normalized = normalize_event(profile, event)
    state = copy.deepcopy(dict(actor_state or {}))
    spec = copy.deepcopy(dict(power_spec or {}))
    reason_codes: list[str] = []
    status = "allowed"
    if _has_unknown(normalized) or _has_unknown(state) or _has_unknown(spec):
        status = "unknown"
        reason_codes.append("POWER_MODEL_STATE_UNKNOWN")
    elif str(spec.get("power_model_status") or "modeled") in {
        "unmodeled",
        "conflicted",
    }:
        status = "unknown"
        reason_codes.append("POWER_MODEL_STATE_UNKNOWN")
    else:
        event_type = str(normalized.get("event_type") or "")
        action = str(normalized.get("action") or "")
        if event_type == "progression" and action in {
            "advance",
            "regress",
            "branch",
            "prestige",
        }:
            current_rank = str(
                normalized.get("from_rank_entity_id")
                or state.get("rank_node_id")
                or state.get("current_rank_id")
                or ""
            )
            target_rank = str(
                normalized.get("to_rank_entity_id")
                or normalized.get("rank_node_id")
                or ""
            )
            edges = [
                item
                for item in spec.get("rank_edges") or []
                if isinstance(item, Mapping)
            ]
            matching = [
                item
                for item in edges
                if current_rank
                in {
                    str(value)
                    for value in (
                        item.get("from_node_ids")
                        or item.get("from_rank_entity_ids")
                        or []
                    )
                }
                and target_rank
                == str(
                    item.get("to_node_id")
                    or item.get("to_rank_entity_id")
                    or ""
                )
            ]
            if not current_rank or not target_rank or not edges:
                status = "unknown"
                reason_codes.append("POWER_TRANSITION_EDGE_MISSING")
            elif not matching:
                status = "blocked"
                reason_codes.append("POWER_TRANSITION_EDGE_MISSING")
        elif event_type == "ability" and action in {
            "use",
            "activate",
            "charge",
        }:
            ability_id = str(
                normalized.get("ability_entity_id")
                or normalized.get("ability_id")
                or ""
            )
            ownerships = [
                item
                for item in state.get("ability_ownerships") or []
                if isinstance(item, Mapping)
                and str(
                    item.get("ability_id")
                    or item.get("ability_entity_id")
                    or ""
                )
                == ability_id
            ]
            if not ownerships:
                status = "blocked"
                reason_codes.append("POWER_ABILITY_NOT_ACQUIRED")
            else:
                ownership = dict(ownerships[0])
                if (
                    ownership.get("acquired") is False
                    or str(ownership.get("unlock_state") or "")
                    in {"locked", "lost", "suppressed"}
                ):
                    status = "blocked"
                    reason_codes.append("POWER_ABILITY_NOT_ACQUIRED")
                elif (
                    ownership.get("available") is False
                    or ownership.get("cooldown_active") is True
                ):
                    status = "blocked"
                    reason_codes.append("POWER_COOLDOWN_ACTIVE")
                elif ownership.get("source_active") is False:
                    status = "blocked"
                    reason_codes.append("POWER_SOURCE_INACTIVE")
        elif event_type == "resource" and action in {"spend", "reserve"}:
            resource_id = str(
                normalized.get("resource_entity_id")
                or normalized.get("resource_id")
                or ""
            )
            matches = [
                item
                for item in state.get("resources") or []
                if isinstance(item, Mapping)
                and str(
                    item.get("resource_id")
                    or item.get("resource_entity_id")
                    or ""
                )
                == resource_id
            ]
            if not matches:
                status = "unknown"
                reason_codes.append("POWER_RESOURCE_STATE_UNKNOWN")
            else:
                available = float(
                    matches[0].get(
                        "available",
                        matches[0].get(
                            "amount",
                            matches[0].get("balance", 0),
                        ),
                    )
                )
                amount = float(normalized.get("amount") or 0)
                if amount > available:
                    status = "blocked"
                    reason_codes.append("POWER_RESOURCE_INSUFFICIENT")
        elif event_type == "resource" and action == "convert":
            rule_id = str(
                normalized.get("conversion_rule_entity_id")
                or normalized.get("conversion_rule_id")
                or ""
            )
            rules = {
                str(
                    item.get("conversion_rule_id")
                    or item.get("rule_entity_id")
                    or ""
                )
                for item in spec.get("conversion_rules") or []
                if isinstance(item, Mapping)
            }
            if not rule_id or rule_id not in rules:
                status = "unknown"
                reason_codes.append("POWER_INTERACTION_UNKNOWN")
        elif event_type == "status_effect":
            status_id = str(
                normalized.get("status_entity_id")
                or normalized.get("status_id")
                or ""
            )
            definitions = {
                str(item.get("status_id") or item.get("status_entity_id") or "")
                for item in spec.get("status_definitions") or []
                if isinstance(item, Mapping)
            }
            if not status_id or status_id not in definitions:
                status = "unknown"
                reason_codes.append("POWER_STATUS_DEFINITION_UNKNOWN")
        elif event_type == "qualification":
            qualification_id = str(
                normalized.get("qualification_entity_id")
                or normalized.get("qualification_id")
                or ""
            )
            definitions = {
                str(
                    item.get("qualification_id")
                    or item.get("qualification_entity_id")
                    or ""
                )
                for item in spec.get("qualification_definitions") or []
                if isinstance(item, Mapping)
            }
            if not qualification_id or qualification_id not in definitions:
                status = "unknown"
                reason_codes.append("POWER_QUALIFICATION_DEFINITION_UNKNOWN")
        elif event_type == "power_binding" and action in {
            "unbind",
            "unequip",
            "dismiss",
            "suppress",
        }:
            binding_id = str(normalized.get("binding_id") or "")
            bindings = {
                str(item.get("binding_id") or "")
                for item in state.get("bindings") or []
                if isinstance(item, Mapping)
            }
            if not binding_id or binding_id not in bindings:
                status = "unknown"
                reason_codes.append("POWER_BINDING_STATE_UNKNOWN")
    return {
        "adapter_id": adapter.adapter_id,
        "adapter_version": adapter.version,
        "profile": adapter.profile,
        "status": status,
        "reason_codes": reason_codes,
        "normalized_event": normalized,
    }


def compile_query_terms(
    profile: str,
    categories: Iterable[str] | None = None,
    *,
    project_terms: Mapping[str, Iterable[str]] | None = None,
) -> list[str]:
    adapter = get_adapter(profile)
    selected = set(categories or adapter.native_terms.keys())
    result: set[str] = {
        adapter.display_name,
        adapter.profile,
        *adapter.aliases,
    }
    for category in selected:
        result.update(adapter.native_terms.get(str(category), ()))
        result.update(
            str(item)
            for item in (project_terms or {}).get(str(category), ())
            if str(item)
        )
    return sorted(result)


def render_native_projection(
    profile: str,
    canonical_type: str,
    fallback: str,
) -> str:
    adapter = get_adapter(profile)
    terms = adapter.native_terms.get(canonical_type) or ()
    return str(terms[0] if terms else fallback)


def report_semantic_loss(
    profile: str,
    canonical_type: str,
    native_term: str,
) -> dict[str, Any]:
    adapter = get_adapter(profile)
    supported = native_term in adapter.native_terms.get(canonical_type, ())
    return {
        "adapter_id": adapter.adapter_id,
        "adapter_version": adapter.version,
        "native_term": native_term,
        "canonical_type": canonical_type,
        "mapping_quality": "lossless" if supported else "partial",
        "semantic_loss": [] if supported else ["project term requires explicit binding"],
    }
