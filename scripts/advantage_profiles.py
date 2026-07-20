"""Declarative registry for web-novel Advantage profiles.

The registry is deliberately independent from continuity storage.  It gives
initialization, retrieval, and presentation code one stable vocabulary for the
sixteen generic golden-finger shapes exposed by the public Advantage schema.
Profile matching is recall-only: a match never grants a capability or promotes
an unreviewed claim into canon.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping


PROFILE_REGISTRY_SCHEMA_VERSION = "plot-rag-advantage-profile-registry/v1"
PROFILE_REGISTRY_VERSION = "1.0.0"

ADVANTAGE_PROFILES = (
    "appraisal_copy",
    "bloodline_constitution",
    "companion_mentor",
    "contract_summon",
    "foreknowledge",
    "growth_relic",
    "inheritance",
    "pocket_domain",
    "resource_transformer",
    "reward_market",
    "sign_in_lottery",
    "simulator_branch",
    "social_currency",
    "system_panel",
    "task_reward",
    "time_causality",
)

ADVANTAGE_UPPER_CLASSES = frozenset(
    {"resource_device", "ability_endowment", "knowledge_guidance"}
)
ADVANTAGE_ANCHOR_TYPES = frozenset(
    {
        "item_instance",
        "item_stack",
        "body_or_vessel",
        "actor",
        "virtual_system",
        "knowledge_set",
        "temporal_rule",
        "contract",
        "location",
        "power_source",
        "social_graph",
    }
)


class AdvantageProfileError(ValueError):
    """A deterministic profile-registry contract error."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "failed",
            "code": self.code,
            "reason": self.message,
        }
        if self.details:
            payload["details"] = copy.deepcopy(self.details)
        return payload


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        values = list(value)
    else:
        values = []
    return tuple(
        sorted({str(item).strip() for item in values if str(item).strip()})
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return "\n".join(
            f"{key}\n{_flatten_text(child)}"
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return "\n".join(_flatten_text(child) for child in value)
    return "" if value is None else str(value)


@dataclass(frozen=True)
class AdvantageProfileSpec:
    profile: str
    display_name: str
    aliases: tuple[str, ...]
    upper_classes: tuple[str, ...]
    anchor_types: tuple[str, ...]
    detection_terms: tuple[str, ...]
    module_kinds: tuple[str, ...]
    runtime_dimensions: tuple[str, ...]
    ledger_entry_kinds: tuple[str, ...]
    knowledge_requirements: tuple[str, ...]
    contract_kinds: tuple[str, ...]
    narrative_contract: dict[str, Any]
    compatibility: dict[str, bool]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AdvantageProfileSpec":
        profile = str(value.get("profile") or "").strip()
        if profile not in ADVANTAGE_PROFILES:
            raise AdvantageProfileError(
                "ADVANTAGE_PROFILE_UNSUPPORTED",
                "advantage profile is not registered",
                profile=profile,
            )
        display_name = str(value.get("display_name") or "").strip()
        if not display_name:
            raise AdvantageProfileError(
                "ADVANTAGE_PROFILE_INVALID",
                "advantage profile requires a display name",
                profile=profile,
            )
        upper_classes = _string_tuple(value.get("upper_classes"))
        invalid_upper = sorted(set(upper_classes) - ADVANTAGE_UPPER_CLASSES)
        if not upper_classes or invalid_upper:
            raise AdvantageProfileError(
                "ADVANTAGE_PROFILE_INVALID",
                "advantage profile has invalid upper classes",
                profile=profile,
                invalid=invalid_upper,
            )
        anchor_types = _string_tuple(value.get("anchor_types"))
        invalid_anchors = sorted(set(anchor_types) - ADVANTAGE_ANCHOR_TYPES)
        if not anchor_types or invalid_anchors:
            raise AdvantageProfileError(
                "ADVANTAGE_PROFILE_INVALID",
                "advantage profile has invalid anchor types",
                profile=profile,
                invalid=invalid_anchors,
            )
        narrative = copy.deepcopy(dict(value.get("narrative_contract") or {}))
        narrative_keys = {
            "reading_promise",
            "reward_loop",
            "risk_loop",
            "reveal_ladder",
        }
        if set(narrative) != narrative_keys:
            raise AdvantageProfileError(
                "ADVANTAGE_PROFILE_INVALID",
                "profile narrative contract is incomplete",
                profile=profile,
                fields=sorted(narrative),
            )
        if not str(narrative.get("reading_promise") or "").strip():
            raise AdvantageProfileError(
                "ADVANTAGE_PROFILE_INVALID",
                "profile narrative promise must be non-empty",
                profile=profile,
            )
        for field in ("reward_loop", "risk_loop", "reveal_ladder"):
            normalized = list(_string_tuple(narrative.get(field)))
            if not normalized:
                raise AdvantageProfileError(
                    "ADVANTAGE_PROFILE_INVALID",
                    "profile narrative loop must be non-empty",
                    profile=profile,
                    field=field,
                )
            narrative[field] = normalized
        compatibility = copy.deepcopy(dict(value.get("compatibility") or {}))
        compatibility_keys = {
            "item_projection",
            "power_bridge",
            "branch_isolation",
            "story_time_required",
        }
        if set(compatibility) != compatibility_keys or any(
            not isinstance(flag, bool) for flag in compatibility.values()
        ):
            raise AdvantageProfileError(
                "ADVANTAGE_PROFILE_INVALID",
                "profile compatibility flags are incomplete",
                profile=profile,
                fields=sorted(compatibility),
            )
        required_vectors = {
            "aliases": _string_tuple(value.get("aliases")),
            "detection_terms": _string_tuple(value.get("detection_terms")),
            "module_kinds": _string_tuple(value.get("module_kinds")),
            "runtime_dimensions": _string_tuple(
                value.get("runtime_dimensions")
            ),
            "ledger_entry_kinds": _string_tuple(
                value.get("ledger_entry_kinds")
            ),
            "knowledge_requirements": _string_tuple(
                value.get("knowledge_requirements")
            ),
            "contract_kinds": _string_tuple(value.get("contract_kinds")),
        }
        missing = sorted(
            field for field, items in required_vectors.items() if not items
        )
        if missing:
            raise AdvantageProfileError(
                "ADVANTAGE_PROFILE_INVALID",
                "profile executable dimensions must be non-empty",
                profile=profile,
                fields=missing,
            )
        return cls(
            profile=profile,
            display_name=display_name,
            aliases=required_vectors["aliases"],
            upper_classes=upper_classes,
            anchor_types=anchor_types,
            detection_terms=required_vectors["detection_terms"],
            module_kinds=required_vectors["module_kinds"],
            runtime_dimensions=required_vectors["runtime_dimensions"],
            ledger_entry_kinds=required_vectors["ledger_entry_kinds"],
            knowledge_requirements=required_vectors[
                "knowledge_requirements"
            ],
            contract_kinds=required_vectors["contract_kinds"],
            narrative_contract=narrative,
            compatibility={
                str(key): bool(flag)
                for key, flag in sorted(compatibility.items())
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "display_name": self.display_name,
            "aliases": list(self.aliases),
            "upper_classes": list(self.upper_classes),
            "anchor_types": list(self.anchor_types),
            "detection_terms": list(self.detection_terms),
            "module_kinds": list(self.module_kinds),
            "runtime_dimensions": list(self.runtime_dimensions),
            "ledger_entry_kinds": list(self.ledger_entry_kinds),
            "knowledge_requirements": list(self.knowledge_requirements),
            "contract_kinds": list(self.contract_kinds),
            "narrative_contract": copy.deepcopy(self.narrative_contract),
            "compatibility": copy.deepcopy(self.compatibility),
        }


class AdvantageProfileRegistry:
    def __init__(
        self,
        profiles: Iterable[AdvantageProfileSpec],
        *,
        schema_version: str = PROFILE_REGISTRY_SCHEMA_VERSION,
        registry_version: str = PROFILE_REGISTRY_VERSION,
    ) -> None:
        if schema_version != PROFILE_REGISTRY_SCHEMA_VERSION:
            raise AdvantageProfileError(
                "ADVANTAGE_PROFILE_SCHEMA_UNSUPPORTED",
                "unsupported advantage profile registry schema",
                actual=schema_version,
            )
        if registry_version != PROFILE_REGISTRY_VERSION:
            raise AdvantageProfileError(
                "ADVANTAGE_PROFILE_REGISTRY_VERSION_UNSUPPORTED",
                "unsupported advantage profile registry version",
                actual=registry_version,
            )
        values = tuple(profiles)
        names = [item.profile for item in values]
        duplicates = sorted(
            name for name in set(names) if names.count(name) > 1
        )
        if duplicates:
            raise AdvantageProfileError(
                "ADVANTAGE_PROFILE_DUPLICATE",
                "profile registry contains duplicate profile ids",
                duplicates=duplicates,
            )
        missing = sorted(set(ADVANTAGE_PROFILES) - set(names))
        extra = sorted(set(names) - set(ADVANTAGE_PROFILES))
        if missing or extra:
            raise AdvantageProfileError(
                "ADVANTAGE_PROFILE_SET_INCOMPLETE",
                "profile registry must contain all sixteen declared profiles",
                missing=missing,
                extra=extra,
            )
        self.schema_version = schema_version
        self.registry_version = registry_version
        self._by_profile = {item.profile: item for item in values}

    def get(self, profile: str) -> AdvantageProfileSpec:
        key = str(profile or "").strip()
        result = self._by_profile.get(key)
        if result is None:
            raise AdvantageProfileError(
                "ADVANTAGE_PROFILE_NOT_FOUND",
                "advantage profile is not registered",
                profile=key,
            )
        return result

    def profiles(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_profile))

    def all(self) -> tuple[AdvantageProfileSpec, ...]:
        return tuple(self._by_profile[key] for key in self.profiles())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "registry_version": self.registry_version,
            "profiles": [item.to_dict() for item in self.all()],
        }

    @property
    def registry_hash(self) -> str:
        return _canonical_hash(self.to_dict())

    def detect(
        self,
        value: Any,
        *,
        explicit_profiles: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> tuple[str, ...]:
        if explicit_profiles is not None:
            selected = tuple(
                sorted(
                    {
                        self.get(str(profile)).profile
                        for profile in explicit_profiles
                    }
                )
            )
            return selected[:limit] if limit is not None else selected
        text = _flatten_text(value).casefold()
        scores: dict[str, int] = {}
        for spec in self.all():
            score = 0
            for term in (
                spec.profile,
                spec.display_name,
                *spec.aliases,
                *spec.detection_terms,
            ):
                normalized = str(term).casefold().strip()
                if normalized and normalized in text:
                    score += max(1, len(normalized))
            if score:
                scores[spec.profile] = score
        ordered = tuple(
            profile
            for profile, _score in sorted(
                scores.items(),
                key=lambda item: (-item[1], item[0]),
            )
        )
        return ordered[:limit] if limit is not None else ordered


def _registry_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "templates"
        / "advantage_profiles.v1.json"
    )


@lru_cache(maxsize=1)
def advantage_profile_registry() -> AdvantageProfileRegistry:
    path = _registry_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdvantageProfileError(
            "ADVANTAGE_PROFILE_REGISTRY_UNREADABLE",
            "advantage profile registry is missing or invalid JSON",
            path=str(path),
        ) from exc
    if not isinstance(payload, Mapping):
        raise AdvantageProfileError(
            "ADVANTAGE_PROFILE_REGISTRY_INVALID",
            "advantage profile registry root must be an object",
            path=str(path),
        )
    values = payload.get("profiles")
    if not isinstance(values, list):
        raise AdvantageProfileError(
            "ADVANTAGE_PROFILE_REGISTRY_INVALID",
            "advantage profile registry profiles must be an array",
            path=str(path),
        )
    return AdvantageProfileRegistry(
        [
            AdvantageProfileSpec.from_mapping(item)
            for item in values
            if isinstance(item, Mapping)
        ],
        schema_version=str(payload.get("schema_version") or ""),
        registry_version=str(payload.get("registry_version") or ""),
    )


def get_advantage_profile(profile: str) -> AdvantageProfileSpec:
    return advantage_profile_registry().get(profile)


def detect_advantage_profiles(
    value: Any,
    *,
    explicit_profiles: Iterable[str] | None = None,
    limit: int | None = None,
) -> tuple[str, ...]:
    return advantage_profile_registry().detect(
        value,
        explicit_profiles=explicit_profiles,
        limit=limit,
    )


def compile_advantage_query_terms(
    profiles: Iterable[str],
    *,
    include_dimensions: bool = True,
    project_terms: Iterable[str] = (),
) -> list[str]:
    result = {
        str(item).strip()
        for item in project_terms
        if str(item).strip()
    }
    for profile in profiles:
        spec = get_advantage_profile(str(profile))
        result.update(
            {
                spec.profile,
                spec.display_name,
                *spec.aliases,
                *spec.detection_terms,
            }
        )
        if include_dimensions:
            result.update(spec.module_kinds)
            result.update(spec.runtime_dimensions)
            result.update(spec.ledger_entry_kinds)
    return sorted(result)


def advantage_profile_registry_hash() -> str:
    return advantage_profile_registry().registry_hash


# Concise aliases for integration code that follows the power-adapter naming
# convention.
profile_registry = advantage_profile_registry
get_profile = get_advantage_profile
detect_profiles = detect_advantage_profiles


__all__ = [
    "ADVANTAGE_ANCHOR_TYPES",
    "ADVANTAGE_PROFILES",
    "ADVANTAGE_UPPER_CLASSES",
    "PROFILE_REGISTRY_SCHEMA_VERSION",
    "PROFILE_REGISTRY_VERSION",
    "AdvantageProfileError",
    "AdvantageProfileRegistry",
    "AdvantageProfileSpec",
    "advantage_profile_registry",
    "advantage_profile_registry_hash",
    "compile_advantage_query_terms",
    "detect_advantage_profiles",
    "detect_profiles",
    "get_advantage_profile",
    "get_profile",
    "profile_registry",
]
