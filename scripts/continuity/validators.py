"""Pure validation, canonicalization, and event-normalization helpers."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .schema import (
    ARTIFACT_STAGES,
    CANON_STATUSES,
    EVENT_TYPES,
    FACT_SCOPES,
    SOURCE_ROLES,
)

POWER_SPEC_TYPES = (
    "power_system",
    "progression_track",
    "rank_node",
    "rank_edge",
    "ability_definition",
    "resource_definition",
    "status_definition",
    "qualification_definition",
    "counter_rule",
    "bridge_rule",
    "conversion_rule",
)

KNOWLEDGE_PLANES = (
    "objective",
    "actor_belief",
    "public_narrative",
    "reader_disclosed",
    "author_plan",
)

ITEM_EVENT_TYPES = (
    "item_spec",
    "item_instance",
    "item_custody",
    "item_runtime",
    "item_function_runtime",
    "item_use",
    "item_observation",
    "item_correction",
)

ITEM_SPEC_TYPES = (
    "item_definition",
    "function_definition",
    "function_binding",
)

ITEM_SUBJECT_TYPES = ("item_instance", "item_stack")
LEGACY_DELTA_SCHEMA_VERSION = "plot-rag-delta/v3"
ITEM_DELTA_SCHEMA_VERSION = "plot-rag-delta/v4"
ADVANTAGE_SCHEMA_VERSION = "plot-rag-advantage/v1"

ADVANTAGE_EVENT_TYPES = (
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
)

ADVANTAGE_ANCHOR_TYPES = (
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
)

ADVANTAGE_AUTHORITY_STATUSES = (
    "canon",
    "planned",
    "rumor",
    "misread",
)

_ITEM_RUNTIME_OUTPUT_FIELDS = frozenset(
    {
        "before",
        "after",
        "before_state",
        "after_state",
        "resulting_state",
        "computed_state",
    }
)

_ADVANTAGE_RUNTIME_OUTPUT_FIELDS = _ITEM_RUNTIME_OUTPUT_FIELDS

_ADVANTAGE_COMMON_FIELDS = frozenset(
    {
        "schema_version",
        "event_type",
        "event_id",
        "scope",
        "branch_id",
        "chapter_no",
        "scene_index",
        "story_time",
        "story_coordinate",
        "narrative_mode",
        "evidence",
        "knowledge_plane",
        "confidence",
        "effective_at",
        "ambiguity",
        "entity_id",
        "subject_entity_id",
        "target_entity_id",
        "supersedes",
        "retracts",
        "caused_by",
        "source_claim_ids",
        "advantage_id",
        "actor_entity_id",
        "experience_contract_id",
        "experience_contract",
        "experience_contract_hash",
        "narrative_contract",
        "causal_provenance",
        "provenance",
        "causal_event_id",
    }
)

_ADVANTAGE_FIELDS_BY_EVENT = {
    "advantage_spec": frozenset(
        {
            "action",
            "spec_type",
            "spec_id",
            "title",
            "profiles",
            "anchor_type",
            "acquisition_mode",
            "uniqueness",
            "status",
            "authority_status",
            "promise",
            "counterplay",
            "definition",
            "slot_id",
            "module_id",
            "stage",
            "capacity",
            "unlock_graph",
            "set_membership",
            "slot_status",
            "narrative_contract_id",
            "contract_id",
            "contract_status",
            "reading_promise",
            "reward_loop",
            "risk_loop",
            "reveal_ladder",
            "experience_binding",
            "name",
            "origin",
            "slot_kind",
        }
    ),
    "advantage_anchor": frozenset(
        {
            "action",
            "anchor_id",
            "anchor_type",
            "anchor_ref_id",
            "subject_id",
            "owner_entity_id",
            "binding_state",
            "transfer_rule",
            "anchor_status",
            "authority_status",
            "status",
            "attributes",
            "anchor_name",
            "origin",
        }
    ),
    "advantage_module": frozenset(
        {
            "action",
            "module_id",
            "title",
            "kind",
            "module_kind",
            "status",
            "authority_status",
            "module_status",
            "stage",
            "trigger",
            "preconditions",
            "targets",
            "costs",
            "effects",
            "side_effects",
            "failure_modes",
            "counters",
            "definition",
            "anchor_ids",
            "granted_ability_ids",
            "name",
            "origin",
            "profile",
            "range",
            "reveal_stage",
        }
    ),
    "advantage_bind": frozenset(
        {
            "action",
            "anchor_id",
            "owner_entity_id",
        }
    ),
    "advantage_activate": frozenset(
        {
            "action",
            "owner_entity_id",
            "stage",
            "charges",
            "max_charges",
            "resources",
            "pollution",
            "exposure",
            "debt",
            "cooldown_until",
            "runtime_metadata",
        }
    ),
    "advantage_trigger": frozenset(
        {
            "action",
            "module_id",
            "entry_id",
            "costs",
            "rewards",
            "output",
            "effects",
            "side_effects",
            "cooldown",
            "pollution_delta",
            "exposure_delta",
            "debt_delta",
            "actor",
            "target",
            "target_kind",
            "target_filter",
            "preconditions_satisfied",
        }
    ),
    "advantage_use": frozenset(
        {
            "action",
            "module_id",
            "entry_id",
            "costs",
            "rewards",
            "output",
            "effects",
            "side_effects",
            "cooldown",
            "pollution_delta",
            "exposure_delta",
            "debt_delta",
            "actor",
            "target",
            "target_kind",
            "target_filter",
            "preconditions_satisfied",
        }
    ),
    "advantage_reward": frozenset(
        {
            "action",
            "module_id",
            "entry_id",
            "record_only",
            "ledger_entry_kind",
            "input",
            "loss",
            "costs",
            "rewards",
            "output",
            "effects",
            "side_effects",
            "cooldown",
            "pollution_delta",
            "exposure_delta",
            "debt_delta",
            "actor",
            "target",
            "target_kind",
            "target_filter",
            "preconditions_satisfied",
        }
    ),
    "advantage_cost": frozenset(
        {
            "action",
            "module_id",
            "entry_id",
            "record_only",
            "ledger_entry_kind",
            "input",
            "loss",
            "costs",
            "rewards",
            "output",
            "effects",
            "side_effects",
            "cooldown",
            "pollution_delta",
            "exposure_delta",
            "debt_delta",
            "actor",
            "target",
            "target_kind",
            "target_filter",
            "preconditions_satisfied",
        }
    ),
    "advantage_upgrade": frozenset(
        {
            "to_stage",
            "stage",
            "unlock_modules",
            "max_charges",
            "entry_id",
        }
    ),
    "advantage_reveal": frozenset(
        {
            "knowledge_id",
            "module_id",
            "observer_entity_id",
            "status",
            "claim",
            "reveal_stage",
            "misread_of",
            "record_ledger",
            "entry_id",
            "origin",
        }
    ),
    "advantage_contract": frozenset(
        {
            "action",
            "contract_id",
            "narrative_contract_id",
            "actor_entity_id",
            "counterparty_entity_id",
            "status",
            "authority_status",
            "contract_status",
            "terms",
            "agency",
            "trust_delta",
            "debt_delta",
            "breach_effect",
            "reading_promise",
            "reward_loop",
            "risk_loop",
            "reveal_ladder",
            "experience_binding",
            "entry_id",
            "contract_kind",
            "origin",
            "parties",
        }
    ),
    "advantage_correction": frozenset(
        {
            "action",
            "target_event_id",
            "replacement",
        }
    ),
}

_ADVANTAGE_COORDINATE_REQUIRED_EVENTS = frozenset(
    {
        "advantage_bind",
        "advantage_activate",
        "advantage_trigger",
        "advantage_use",
        "advantage_reward",
        "advantage_cost",
        "advantage_upgrade",
        "advantage_reveal",
        "advantage_contract",
    }
)

_ITEM_COMMON_FIELDS = frozenset(
    {
        "schema_version",
        "event_type",
        "scope",
        "branch_id",
        "chapter_no",
        "scene_index",
        "story_time",
        "story_coordinate",
        "narrative_mode",
        "evidence",
        "knowledge_plane",
        "confidence",
        "effective_at",
        "ambiguity",
        "entity_id",
        "subject_entity_id",
        "target_entity_id",
        "supersedes",
        "retracts",
        "caused_by",
    }
)

_ITEM_FIELDS_BY_EVENT = {
    "item_spec": frozenset(
        {
            "action",
            "spec_type",
            "spec_id",
            "item_definition_id",
            "item_instance_id",
            "stack_id",
            "function_id",
            "binding_id",
            "definition",
            "supersedes_spec_id",
        }
    ),
    "item_instance": frozenset(
        {
            "action",
            "subject_type",
            "subject_id",
            "item_instance_id",
            "stack_id",
            "item_definition_id",
            "definition_id",
            "item_entity_id",
            "instance_name",
            "serial_or_mark",
            "unique",
            "provenance",
            "quantity",
            "batch",
            "attributes",
            "source_stack_id",
            "target_stack_id",
            "target_batch",
            "actor_entity_id",
        }
    ),
    "item_custody": frozenset(
        {
            "action",
            "subject_type",
            "subject_id",
            "item_instance_id",
            "stack_id",
            "quantity",
            "actor_entity_id",
            "legal_owner_entity_id",
            "custodian_entity_id",
            "carrier_entity_id",
            "location_entity_id",
            "container_instance_id",
            "access_controller_entity_id",
            "from_legal_owner_entity_id",
            "to_legal_owner_entity_id",
            "from_custodian_entity_id",
            "to_custodian_entity_id",
            "from_carrier_entity_id",
            "to_carrier_entity_id",
            "from_location_entity_id",
            "to_location_entity_id",
            "from_container_instance_id",
            "to_container_instance_id",
            "from_access_controller_entity_id",
            "to_access_controller_entity_id",
            "custody_status",
        }
    ),
    "item_runtime": frozenset(
        {
            "action",
            "subject_type",
            "subject_id",
            "item_instance_id",
            "stack_id",
            "actor_entity_id",
            "function_id",
            "slot_key",
            "delta",
            "quantity",
            "current_mode",
            "suppressed_by_entity_id",
            "cooldown_until",
            "last_used_coordinate",
            "durability",
            "max_durability",
            "energy",
            "max_energy",
            "sealed",
            "damaged",
            "destroyed",
            "active",
            "equipped_by_entity_id",
            "bound_actor_entity_id",
            "state",
        }
    ),
    "item_function_runtime": frozenset(
        {
            "action",
            "subject_type",
            "subject_id",
            "item_instance_id",
            "stack_id",
            "function_id",
            "enabled",
            "unlock_state",
            "remaining_charges",
            "cooldown_until",
            "state",
            "delta",
            "reason",
        }
    ),
    "item_use": frozenset(
        {
            "action",
            "subject_type",
            "subject_id",
            "item_instance_id",
            "stack_id",
            "actor_entity_id",
            "function_id",
            "target_entity_id",
            "location_entity_id",
            "resource_entity_id",
            "delta",
            "quantity",
        }
    ),
    "item_observation": frozenset(
        {
            "action",
            "subject_type",
            "subject_id",
            "item_instance_id",
            "stack_id",
            "item_definition_id",
            "observer_entity_id",
            "function_id",
            "source_entity_id",
            "target_entity_id",
            "observation",
            "observed_fields",
        }
    ),
    "item_correction": frozenset(
        {
            "action",
            "target_event_id",
            "replacement",
        }
    ),
}

# Action-specific mutable payloads. Identity fields (subject/function/actor
# ids) stay valid for every action; these maps close only state-bearing fields
# so a model cannot smuggle an absolute/computed value alongside a delta.
_ITEM_RUNTIME_ACTION_FIELDS = {
    "bootstrap": frozenset(
        {
            "slot_key",
            "durability",
            "max_durability",
            "energy",
            "max_energy",
            "sealed",
            "damaged",
            "destroyed",
            "active",
            "equipped_by_entity_id",
            "bound_actor_entity_id",
            "state",
        }
    ),
    "equip": frozenset({"slot_key"}),
    "unequip": frozenset(),
    "bind": frozenset(),
    "unbind": frozenset(),
    "activate": frozenset(),
    "deactivate": frozenset(),
    "consume": frozenset(),
    "charge": frozenset({"delta"}),
    "discharge": frozenset({"delta"}),
    "repair": frozenset({"delta"}),
    "damage": frozenset({"delta"}),
    "break": frozenset(),
    "destroy": frozenset(),
    "seal": frozenset(),
    "unseal": frozenset(),
    "unlock_function": frozenset(),
    "suppress_function": frozenset(),
}

_ITEM_FUNCTION_RUNTIME_ACTION_FIELDS = {
    "bootstrap": frozenset(
        {
            "enabled",
            "unlock_state",
            "remaining_charges",
            "cooldown_until",
            "state",
        }
    ),
    "enable": frozenset(),
    "disable": frozenset(),
    "unlock": frozenset(),
    "lock": frozenset(),
    "suppress": frozenset({"reason"}),
    "set_charges": frozenset({"remaining_charges", "delta"}),
    "set_cooldown": frozenset({"cooldown_until"}),
    "clear_cooldown": frozenset(),
}

_ITEM_ACTION_IDENTITY_FIELDS = {
    "item_runtime": frozenset(
        {
            "action",
            "subject_type",
            "subject_id",
            "item_instance_id",
            "stack_id",
            "actor_entity_id",
            "function_id",
        }
    ),
    "item_function_runtime": frozenset(
        {
            "action",
            "subject_type",
            "subject_id",
            "item_instance_id",
            "stack_id",
            "function_id",
        }
    ),
}


class ContinuityError(RuntimeError):
    """Machine-readable lifecycle failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def stable_hash(value: Any, *, prefix: str = "") -> str:
    raw = value if isinstance(value, str) else canonical_json(value)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{prefix}{digest}"


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold().strip()
    return re.sub(r"\s+", " ", normalized)


def parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContinuityError(
            "INVALID_TIMESTAMP", f"invalid UTC timestamp: {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_positive_int(
    value: Any,
    field: str,
    *,
    allow_none: bool,
    minimum: int,
) -> int | None:
    if value is None and allow_none:
        return None
    if type(value) is not int:
        raise ContinuityError("INVALID_FIELD", f"{field} must be an integer")
    integer = value
    if integer < minimum:
        raise ContinuityError(
            "INVALID_FIELD", f"{field} must be >= {minimum}"
        )
    return integer


def validate_finite_number(
    value: Any,
    field: str,
    *,
    minimum: float | None = None,
    allow_zero: bool = True,
) -> float:
    if isinstance(value, bool):
        raise ContinuityError("INVALID_FIELD", f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContinuityError(
            "INVALID_FIELD", f"{field} must be numeric"
        ) from exc
    if not math.isfinite(number):
        raise ContinuityError("INVALID_FIELD", f"{field} must be finite")
    if minimum is not None and number < minimum:
        raise ContinuityError(
            "INVALID_FIELD", f"{field} must be >= {minimum}"
        )
    if not allow_zero and number == 0:
        raise ContinuityError(
            "INVALID_FIELD", f"{field} must be greater than zero"
        )
    return number


def normalize_story_coordinate(
    value: Any,
    field: str = "story_coordinate",
) -> dict[str, Any] | None:
    """Normalize a comparable in-world coordinate.

    Chapter/scene numbers and wall-clock timestamps deliberately do not
    qualify.  A coordinate is comparable only inside one project calendar and
    therefore always carries ``calendar_id`` plus a monotonic integer ordinal.
    """

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ContinuityError(
            "POWER_STORY_COORDINATE_UNKNOWN",
            f"{field} must be an object",
            details={"field": field, "value": value},
        )
    coordinate = dict(value)
    calendar_id = str(coordinate.get("calendar_id") or "").strip()
    if not calendar_id:
        raise ContinuityError(
            "POWER_STORY_COORDINATE_UNKNOWN",
            f"{field}.calendar_id is required",
            details={"field": field},
        )
    ordinal = coordinate.get("ordinal")
    if type(ordinal) is not int:
        raise ContinuityError(
            "POWER_STORY_COORDINATE_UNKNOWN",
            f"{field}.ordinal must be an integer",
            details={"field": field},
        )
    normalized: dict[str, Any] = {
        "calendar_id": calendar_id,
        "ordinal": ordinal,
    }
    for optional in ("label", "precision", "source_event_id"):
        if coordinate.get(optional) is not None:
            normalized[optional] = str(coordinate[optional]).strip()
    return normalized


def _normalize_power_prerequisites(
    value: Any,
    field: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContinuityError(
            "INVALID_FIELD",
            f"{field} must be an object",
            details={"field": field},
        )
    prerequisites = dict(value)
    if prerequisites.get("minimum_story_coordinate") is not None:
        prerequisites["minimum_story_coordinate"] = normalize_story_coordinate(
            prerequisites["minimum_story_coordinate"],
            f"{field}.minimum_story_coordinate",
        )
    return prerequisites


def normalize_stage(value: Any) -> str:
    # Fail closed when callers cannot classify generated content.
    stage = normalize_text(str(value or ""))
    return stage if stage in ARTIFACT_STAGES else "brainstorm"


def require_choice(value: Any, field: str, choices: Iterable[str]) -> str:
    normalized = normalize_text(str(value or ""))
    allowed = tuple(choices)
    if normalized not in allowed:
        raise ContinuityError(
            "INVALID_FIELD",
            f"{field} must be one of {', '.join(allowed)}",
            details={"field": field, "value": value},
        )
    return normalized


def normalize_source_role(value: Any) -> str:
    role = normalize_text(str(value or "draft"))
    if role not in SOURCE_ROLES:
        return "draft"
    return role


def default_scope_for_stage(stage: str) -> str:
    if stage == "outline":
        return "planned"
    if stage in {"brainstorm", "draft"}:
        return "planned"
    return "current"


def _require_entity(event: Mapping[str, Any], key: str) -> str:
    value = str(event.get(key) or "").strip()
    if not value:
        raise ContinuityError(
            "EVENT_ENTITY_REQUIRED",
            f"{event.get('event_type', 'event')} requires {key}",
            details={"event": dict(event), "field": key},
        )
    return value


def _require_mapping(
    value: Any,
    field: str,
    *,
    allow_empty: bool = True,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContinuityError(
            "INVALID_FIELD",
            f"{field} must be an object",
            details={"field": field},
        )
    normalized = dict(value)
    if not allow_empty and not normalized:
        raise ContinuityError(
            "EVENT_VALUE_REQUIRED",
            f"{field} cannot be empty",
            details={"field": field},
        )
    return normalized


def _normalize_string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ContinuityError(
            "INVALID_FIELD",
            f"{field} must be a list of non-empty strings",
            details={"field": field},
        )
    return list(dict.fromkeys(item.strip() for item in value))


def _first_event_link(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and value:
        first = value[0]
        return first.strip() if isinstance(first, str) else ""
    return ""


def _reject_item_runtime_outputs(event: Mapping[str, Any]) -> None:
    forbidden = sorted(_ITEM_RUNTIME_OUTPUT_FIELDS.intersection(event))
    if forbidden:
        raise ContinuityError(
            "ITEM_COMPUTED_STATE_FORBIDDEN",
            "item before/after state is computed by the local replay runtime",
            details={"fields": forbidden},
        )


def _reject_unknown_item_fields(
    event: Mapping[str, Any],
    event_type: str,
) -> None:
    allowed = (
        _ITEM_COMMON_FIELDS
        | _ITEM_FIELDS_BY_EVENT[event_type]
        | _ITEM_RUNTIME_OUTPUT_FIELDS
    )
    unknown = sorted(set(event) - allowed)
    if unknown:
        raise ContinuityError(
            "ITEM_EVENT_FIELD_UNSUPPORTED",
            "typed item event contains unsupported top-level fields",
            details={"event_type": event_type, "fields": unknown},
        )


def _reject_item_action_fields(
    event: Mapping[str, Any],
    *,
    event_type: str,
    action: str,
    allowed_payload_fields: frozenset[str],
) -> None:
    identity_fields = _ITEM_ACTION_IDENTITY_FIELDS[event_type]
    event_fields = _ITEM_FIELDS_BY_EVENT[event_type]
    unsupported = sorted(
        field
        for field in event
        if field in event_fields
        and field not in identity_fields
        and field not in allowed_payload_fields
        and not (
            field == "delta"
            and isinstance(event.get(field), Mapping)
            and not event.get(field)
        )
    )
    if unsupported:
        raise ContinuityError(
            "ITEM_ACTION_FIELD_UNSUPPORTED",
            "typed item action contains fields outside its closed payload contract",
            details={
                "event_type": event_type,
                "action": action,
                "fields": unsupported,
            },
        )


def _normalize_item_envelope_fields(event: dict[str, Any]) -> None:
    """Enforce the v4-only evidence and knowledge contract for item events."""

    if event.get("schema_version") != ITEM_DELTA_SCHEMA_VERSION:
        raise ContinuityError(
            "ITEM_DELTA_SCHEMA_REQUIRED",
            "typed item events require schema_version=plot-rag-delta/v4",
            details={"schema_version": event.get("schema_version")},
        )
    if event.get("story_coordinate") is None:
        raise ContinuityError(
            "ITEM_STORY_COORDINATE_REQUIRED",
            "typed item events require a comparable story_coordinate",
        )

    event["knowledge_plane"] = require_choice(
        event.get("knowledge_plane"),
        "knowledge_plane",
        KNOWLEDGE_PLANES,
    )
    if (
        event.get("event_type")
        not in {"item_observation", "item_correction"}
        and event["knowledge_plane"]
        in {"actor_belief", "public_narrative", "reader_disclosed"}
    ):
        raise ContinuityError(
            "ITEM_KNOWLEDGE_PLANE_REQUIRES_OBSERVATION",
            (
                "non-objective item claims must use item_observation "
                "instead of mutating the objective item projection"
            ),
            details={
                "event_type": event.get("event_type"),
                "knowledge_plane": event["knowledge_plane"],
            },
        )
    raw_evidence = event.get("evidence")
    if isinstance(raw_evidence, str):
        quote = raw_evidence
        evidence: dict[str, Any] = {"quote": raw_evidence}
    elif isinstance(raw_evidence, Mapping):
        evidence = dict(raw_evidence)
        quote = evidence.get("quote")
        if quote is None:
            quote = evidence.get("verbatim_quote")
            if quote is not None:
                evidence["quote"] = quote
    else:
        quote = None
        evidence = {}
    if (
        not isinstance(quote, str)
        or not quote
        or quote != quote.strip()
        or len(quote) > 8192
    ):
        raise ContinuityError(
            "ITEM_EVIDENCE_REQUIRED",
            "typed item events require one non-empty contiguous verbatim quote",
        )
    evidence["quote"] = quote
    event["evidence"] = evidence


def _normalize_item_subject(
    event: dict[str, Any],
    *,
    field_prefix: str = "",
) -> tuple[str, str]:
    prefix = f"{field_prefix}_" if field_prefix else ""
    subject_type_key = f"{prefix}subject_type"
    subject_id_key = f"{prefix}subject_id"
    subject_type = normalize_text(str(event.get(subject_type_key) or ""))
    subject_id = str(event.get(subject_id_key) or "").strip()

    instance_key = f"{prefix}item_instance_id"
    stack_key = f"{prefix}stack_id"
    instance_id = str(event.get(instance_key) or "").strip()
    stack_id = str(event.get(stack_key) or "").strip()
    if instance_id and stack_id:
        raise ContinuityError(
            "ITEM_SUBJECT_AMBIGUOUS",
            "an item event must address one instance or one stack",
            details={"field_prefix": field_prefix},
        )
    if subject_type == "item_instance" and stack_id:
        raise ContinuityError(
            "ITEM_SUBJECT_MISMATCH",
            "item_instance subject_type cannot address a stack",
            details={"field_prefix": field_prefix},
        )
    if subject_type == "item_stack" and instance_id:
        raise ContinuityError(
            "ITEM_SUBJECT_MISMATCH",
            "item_stack subject_type cannot address an instance",
            details={"field_prefix": field_prefix},
        )
    if not subject_type:
        subject_type = (
            "item_instance"
            if instance_id
            else "item_stack"
            if stack_id
            else ""
        )
    if not subject_id:
        subject_id = instance_id or stack_id
    if subject_type not in ITEM_SUBJECT_TYPES or not subject_id:
        raise ContinuityError(
            "ITEM_SUBJECT_REQUIRED",
            "item event requires an item_instance or item_stack subject",
            details={"field_prefix": field_prefix},
        )
    expected_id = instance_id if subject_type == "item_instance" else stack_id
    if expected_id and expected_id != subject_id:
        raise ContinuityError(
            "ITEM_SUBJECT_MISMATCH",
            "subject_id conflicts with the typed item id",
            details={
                "subject_type": subject_type,
                "subject_id": subject_id,
                "typed_id": expected_id,
            },
        )
    event[subject_type_key] = subject_type
    event[subject_id_key] = subject_id
    event[instance_key if subject_type == "item_instance" else stack_key] = (
        subject_id
    )
    return subject_type, subject_id


def _normalize_item_delta(
    value: Any,
    field: str = "delta",
    *,
    allow_zero_fields: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    delta = _require_mapping(value or {}, field)
    numeric_fields = {
        "quantity",
        "charges",
        "durability",
        "energy",
        "cooldown",
    }
    unsupported = sorted(set(delta) - numeric_fields)
    if unsupported:
        raise ContinuityError(
            "ITEM_DELTA_FIELD_UNSUPPORTED",
            "item delta contains unsupported fields",
            details={"field": field, "fields": unsupported},
        )
    normalized: dict[str, Any] = {}
    for key, raw_value in delta.items():
        normalized[key] = validate_finite_number(
            raw_value,
            f"{field}.{key}",
            minimum=0,
            allow_zero=key in allow_zero_fields,
        )
        if key == "cooldown" and not normalized[key].is_integer():
            raise ContinuityError(
                "INVALID_FIELD",
                f"{field}.cooldown must be a whole story-coordinate delta",
                details={"field": f"{field}.cooldown"},
            )
        if key == "cooldown":
            normalized[key] = int(normalized[key])
    return normalized


def _normalize_optional_item_bool(
    event: dict[str, Any],
    field: str,
) -> None:
    if field not in event:
        return
    if type(event[field]) is not bool:
        raise ContinuityError(
            "INVALID_FIELD",
            f"{field} must be a boolean",
            details={"field": field},
        )


def _normalize_optional_item_number(
    event: dict[str, Any],
    field: str,
    *,
    allow_none: bool = True,
) -> None:
    if field not in event:
        return
    if event[field] is None and allow_none:
        return
    event[field] = validate_finite_number(
        event[field],
        field,
        minimum=0,
    )


def _normalize_item_spec_event(event: dict[str, Any]) -> None:
    action = require_choice(
        event.get("action") or "define",
        "action",
        ("define", "deprecate", "supersede"),
    )
    spec_type = require_choice(
        event.get("spec_type"),
        "spec_type",
        ITEM_SPEC_TYPES,
    )
    id_field = {
        "item_definition": "item_definition_id",
        "function_definition": "function_id",
        "function_binding": "binding_id",
    }[spec_type]
    spec_id = str(
        event.get("spec_id") or event.get(id_field) or ""
    ).strip()
    if not spec_id:
        raise ContinuityError(
            "EVENT_ENTITY_REQUIRED",
            f"item_spec {spec_type} requires {id_field}",
        )
    definition = _require_mapping(
        event.get("definition") or {},
        "definition",
        allow_empty=action == "deprecate",
    )

    if spec_type == "item_definition":
        definition.setdefault("item_definition_id", spec_id)
        definition["stack_policy"] = require_choice(
            definition.get("stack_policy") or "non_stackable",
            "definition.stack_policy",
            ("non_stackable", "homogeneous", "lot", "unknown"),
        )
        definition["uniqueness_policy"] = require_choice(
            definition.get("uniqueness_policy") or "ordinary",
            "definition.uniqueness_policy",
            (
                "ordinary",
                "unique_instance",
                "unique_definition",
                "unknown",
            ),
        )
        for field in (
            "capacity",
            "unit_bulk",
            "max_durability",
            "max_energy",
        ):
            if definition.get(field) is not None:
                definition[field] = validate_finite_number(
                    definition[field],
                    f"definition.{field}",
                    minimum=0,
                )
        definition["default_functions"] = _normalize_string_list(
            definition.get("default_functions"),
            "definition.default_functions",
        )

    elif spec_type == "function_definition":
        definition.setdefault("function_id", spec_id)
        item_definition_id = str(
            definition.get("item_definition_id")
            or event.get("item_definition_id")
            or ""
        ).strip()
        if not item_definition_id:
            raise ContinuityError(
                "EVENT_ENTITY_REQUIRED",
                "function_definition requires item_definition_id",
            )
        definition["item_definition_id"] = item_definition_id
        effect_owner = require_choice(
            definition.get("effect_owner") or "inline",
            "definition.effect_owner",
            ("inline", "ability_bridge"),
        )
        definition["effect_owner"] = effect_owner
        ability_ids = _normalize_string_list(
            definition.get("granted_ability_ids"),
            "definition.granted_ability_ids",
        )
        definition["granted_ability_ids"] = ability_ids
        inline_effects = definition.get("inline_effects") or []
        if not isinstance(inline_effects, list):
            raise ContinuityError(
                "INVALID_FIELD",
                "definition.inline_effects must be a list",
            )
        definition["inline_effects"] = list(inline_effects)
        if definition.get("charges") is not None:
            definition["charges"] = validate_finite_number(
                definition["charges"],
                "definition.charges",
                minimum=0,
            )
        if definition.get("durability_cost") is not None:
            definition["durability_cost"] = validate_finite_number(
                definition["durability_cost"],
                "definition.durability_cost",
                minimum=0,
            )

    else:
        definition.setdefault("binding_id", spec_id)
        function_id = str(
            definition.get("function_id") or event.get("function_id") or ""
        ).strip()
        if not function_id:
            raise ContinuityError(
                "EVENT_ENTITY_REQUIRED",
                "function_binding requires function_id",
            )
        definition["function_id"] = function_id
        definition_id = str(
            definition.get("item_definition_id")
            or event.get("item_definition_id")
            or ""
        ).strip()
        instance_id = str(
            definition.get("item_instance_id")
            or event.get("item_instance_id")
            or ""
        ).strip()
        stack_id = str(
            definition.get("stack_id") or event.get("stack_id") or ""
        ).strip()
        if sum(bool(value) for value in (definition_id, instance_id, stack_id)) != 1:
            raise ContinuityError(
                "ITEM_BINDING_TARGET_REQUIRED",
                "function_binding requires exactly one definition, instance, or stack target",
            )
        if definition_id:
            definition["item_definition_id"] = definition_id
        if instance_id:
            definition["item_instance_id"] = instance_id
        if stack_id:
            definition["stack_id"] = stack_id

    event["action"] = action
    event["spec_type"] = spec_type
    event["spec_id"] = spec_id
    event[id_field] = spec_id
    event["definition"] = definition
    if action == "supersede":
        superseded = str(
            event.get("supersedes_spec_id")
            or definition.get("supersedes_spec_id")
            or ""
        ).strip()
        if not superseded or superseded == spec_id:
            raise ContinuityError(
                "ITEM_SPEC_SUPERSESSION_REQUIRED",
                "item spec supersede requires a different supersedes_spec_id",
            )
        event["supersedes_spec_id"] = superseded


def _normalize_item_instance_event(event: dict[str, Any]) -> None:
    action = require_choice(
        event.get("action") or "instantiate",
        "action",
        ("instantiate", "retire", "split", "merge"),
    )
    event["action"] = action
    if action in {"split", "merge"}:
        source_id = str(
            event.get("source_stack_id")
            or event.get("stack_id")
            or event.get("subject_id")
            or ""
        ).strip()
        target_id = str(event.get("target_stack_id") or "").strip()
        if not source_id or not target_id or source_id == target_id:
            raise ContinuityError(
                "ITEM_STACK_ENDPOINTS_REQUIRED",
                f"{action} requires distinct source_stack_id and target_stack_id",
            )
        quantity = validate_finite_number(
            event.get("quantity"),
            "quantity",
            minimum=0,
            allow_zero=False,
        )
        event.update(
            {
                "subject_type": "item_stack",
                "subject_id": source_id,
                "stack_id": source_id,
                "source_stack_id": source_id,
                "target_stack_id": target_id,
                "quantity": quantity,
            }
        )
        if action == "split":
            batch = event.get("target_batch")
            if batch is not None:
                event["target_batch"] = _require_mapping(
                    batch, "target_batch"
                )
        return

    subject_type, _ = _normalize_item_subject(event)
    if action == "instantiate":
        definition_id = str(
            event.get("item_definition_id")
            or event.get("definition_id")
            or ""
        ).strip()
        if not definition_id:
            raise ContinuityError(
                "EVENT_ENTITY_REQUIRED",
                "item instantiate requires item_definition_id",
            )
        event["item_definition_id"] = definition_id
        if subject_type == "item_stack":
            event["quantity"] = validate_finite_number(
                event.get("quantity"),
                "quantity",
                minimum=0,
                allow_zero=False,
            )
            event["batch"] = _require_mapping(
                event.get("batch") or {}, "batch"
            )
        elif isinstance(event.get("quantity"), bool) or event.get(
            "quantity"
        ) not in {None, 1, 1.0}:
            raise ContinuityError(
                "INVALID_QUANTITY",
                "an item instance always has quantity 1",
            )
        event["attributes"] = _require_mapping(
            event.get("attributes") or {}, "attributes"
        )
        if event.get("item_entity_id") is not None:
            event["item_entity_id"] = (
                str(event["item_entity_id"]).strip() or None
            )
        for field in ("instance_name", "serial_or_mark"):
            if event.get(field) is not None:
                event[field] = str(event[field]).strip() or None
        if "unique" in event:
            unique = event["unique"]
            if type(unique) is not bool and unique != "unknown":
                raise ContinuityError(
                    "INVALID_FIELD",
                    "unique must be a boolean or 'unknown'",
                    details={"field": "unique"},
                )
        if event.get("provenance") is not None:
            event["provenance"] = _require_mapping(
                event["provenance"],
                "provenance",
            )


def _normalize_item_custody_event(event: dict[str, Any]) -> None:
    action = require_choice(
        event.get("action") or "acquire",
        "action",
        (
            "acquire",
            "transfer_title",
            "handover",
            "loan",
            "return",
            "seize",
            "store",
            "retrieve",
            "lose",
            "recover",
            "abandon",
        ),
    )
    subject_type, _ = _normalize_item_subject(event)
    event["action"] = action
    if subject_type == "item_instance":
        if isinstance(event.get("quantity"), bool) or event.get(
            "quantity"
        ) not in {None, 1, 1.0}:
            raise ContinuityError(
                "INVALID_QUANTITY",
                "an item instance custody event always has quantity 1",
            )
        event["quantity"] = 1.0
    elif event.get("quantity") is not None:
        event["quantity"] = validate_finite_number(
            event["quantity"],
            "quantity",
            minimum=0,
            allow_zero=False,
        )
    if event.get("custody_status") is not None:
        event["custody_status"] = require_choice(
            event["custody_status"],
            "custody_status",
            (
                "possessed",
                "stored",
                "loaned",
                "seized",
                "lost",
                "abandoned",
                "in_transit",
                "destroyed",
                "unknown",
            ),
        )

    owner_fields = (
        "from_legal_owner_entity_id",
        "to_legal_owner_entity_id",
    )
    if action == "transfer_title":
        from_owner = str(event.get(owner_fields[0]) or "").strip()
        to_owner = str(event.get(owner_fields[1]) or "").strip()
        if not from_owner or not to_owner or from_owner == to_owner:
            raise ContinuityError(
                "ITEM_TITLE_ENDPOINTS_REQUIRED",
                "transfer_title requires distinct from/to legal owners",
            )
    elif action != "acquire" and any(event.get(field) for field in owner_fields):
        raise ContinuityError(
            "ITEM_TITLE_REQUIRES_SEPARATE_EVENT",
            "legal ownership changes require transfer_title",
            details={"action": action},
        )

    to_anchor_fields = (
        "to_custodian_entity_id",
        "to_carrier_entity_id",
        "to_container_instance_id",
        "to_location_entity_id",
    )
    if action in {
        "acquire",
        "handover",
        "loan",
        "return",
        "seize",
        "store",
        "retrieve",
        "recover",
    } and not any(event.get(field) for field in to_anchor_fields) and not (
        action == "acquire" and event.get("custody_status") == "unknown"
    ):
        raise ContinuityError(
            "ITEM_CUSTODY_ANCHOR_REQUIRED",
            (
                f"{action} requires a destination custodian, carrier, "
                "container, or location"
            ),
        )


def _normalize_item_runtime_event(event: dict[str, Any]) -> None:
    action = require_choice(
        event.get("action"),
        "action",
        (
            "bootstrap",
            "equip",
            "unequip",
            "bind",
            "unbind",
            "activate",
            "deactivate",
            "consume",
            "charge",
            "discharge",
            "repair",
            "damage",
            "break",
            "destroy",
            "seal",
            "unseal",
            "unlock_function",
            "suppress_function",
        ),
    )
    subject_type, _ = _normalize_item_subject(event)
    if subject_type != "item_instance":
        raise ContinuityError(
            "ITEM_RUNTIME_INSTANCE_REQUIRED",
            "item runtime actions require an item_instance",
        )
    _reject_item_action_fields(
        event,
        event_type="item_runtime",
        action=action,
        allowed_payload_fields=_ITEM_RUNTIME_ACTION_FIELDS[action],
    )
    raw_delta_present = "delta" in event
    delta = _normalize_item_delta(event.get("delta") or {})
    if action == "bootstrap":
        for field in (
            "durability",
            "max_durability",
            "energy",
            "max_energy",
        ):
            _normalize_optional_item_number(event, field)
        for field in ("sealed", "damaged", "destroyed", "active"):
            _normalize_optional_item_bool(event, field)
        equipped_by = str(
            event.get("equipped_by_entity_id") or ""
        ).strip()
        slot_key = str(event.get("slot_key") or "").strip()
        if bool(equipped_by) != bool(slot_key):
            raise ContinuityError(
                "ITEM_EQUIPMENT_PAIR_REQUIRED",
                "bootstrap equipped_by_entity_id and slot_key must appear together",
            )
        if equipped_by:
            event["equipped_by_entity_id"] = equipped_by
            event["slot_key"] = slot_key
        if event.get("bound_actor_entity_id") is not None:
            event["bound_actor_entity_id"] = (
                str(event["bound_actor_entity_id"]).strip() or None
            )
        event["state"] = _require_mapping(
            event.get("state") or {},
            "state",
        )
    required_delta = {
        "charge": "energy",
        "discharge": "energy",
        "repair": "durability",
        "damage": "durability",
    }.get(action)
    if required_delta and required_delta not in delta:
        raise ContinuityError(
            "ITEM_DELTA_REQUIRED",
            f"{action} requires delta.{required_delta}",
        )
    if action in {"equip", "bind"}:
        _require_entity(event, "actor_entity_id")
    if action == "equip" and not str(event.get("slot_key") or "").strip():
        raise ContinuityError(
            "EVENT_FIELD_REQUIRED",
            "equip requires slot_key",
        )
    if action in {"unlock_function", "suppress_function"}:
        function_id = str(event.get("function_id") or "").strip()
        if not function_id:
            raise ContinuityError(
                "EVENT_ENTITY_REQUIRED",
                f"{action} requires function_id",
            )
        event["function_id"] = function_id
    event["action"] = action
    # Do not manufacture an empty delta on actions whose state is carried by
    # the action itself. This keeps normalize_event idempotent and prevents a
    # second pass from seeing a field outside the action contract.
    if raw_delta_present and delta:
        event["delta"] = delta
    else:
        event.pop("delta", None)


def _normalize_item_function_runtime_event(event: dict[str, Any]) -> None:
    action = require_choice(
        event.get("action"),
        "action",
        (
            "bootstrap",
            "enable",
            "disable",
            "unlock",
            "lock",
            "suppress",
            "set_charges",
            "set_cooldown",
            "clear_cooldown",
        ),
    )
    _normalize_item_subject(event)
    function_id = str(event.get("function_id") or "").strip()
    if not function_id:
        raise ContinuityError(
            "EVENT_ENTITY_REQUIRED",
            "item_function_runtime requires function_id",
        )
    event["action"] = action
    event["function_id"] = function_id

    _reject_item_action_fields(
        event,
        event_type="item_function_runtime",
        action=action,
        allowed_payload_fields=_ITEM_FUNCTION_RUNTIME_ACTION_FIELDS[action],
    )
    raw_delta_present = "delta" in event
    raw_delta = event.get("delta") or {}
    delta = _normalize_item_delta(
        raw_delta,
        allow_zero_fields=(
            frozenset({"charges"}) if action == "set_charges" else frozenset()
        ),
    )
    unsupported_delta = sorted(set(delta) - {"charges", "cooldown"})
    if unsupported_delta:
        raise ContinuityError(
            "ITEM_DELTA_FIELD_UNSUPPORTED",
            "item function runtime delta contains unsupported fields",
            details={"fields": unsupported_delta},
        )
    if raw_delta_present and delta:
        event["delta"] = delta
    else:
        event.pop("delta", None)

    if event.get("enabled") is not None or "enabled" in event:
        _normalize_optional_item_bool(event, "enabled")
    if event.get("unlock_state") is not None:
        event["unlock_state"] = require_choice(
            event["unlock_state"],
            "unlock_state",
            ("locked", "unlocked", "suppressed"),
        )
    if "remaining_charges" in event:
        _normalize_optional_item_number(event, "remaining_charges")
    if event.get("cooldown_until") is not None:
        event["cooldown_until"] = normalize_story_coordinate(
            event["cooldown_until"],
            "cooldown_until",
        )
    if event.get("state") is not None:
        event["state"] = _require_mapping(event["state"], "state")

    if action == "set_charges":
        has_remaining = "remaining_charges" in event
        has_delta_charges = "charges" in delta
        if has_remaining == has_delta_charges:
            raise ContinuityError(
                "ITEM_CHARGES_EXPRESSION_CONFLICT",
                (
                    "set_charges requires exactly one of "
                    "remaining_charges or delta.charges"
                ),
            )
        if has_remaining:
            if event.get("remaining_charges") is None:
                raise ContinuityError(
                    "ITEM_DELTA_REQUIRED",
                    "set_charges remaining_charges must be numeric",
                )
        else:
            # Canonicalize the relative spelling to the reducer's absolute
            # runtime field and remove the transient delta.
            event["remaining_charges"] = delta["charges"]
            event.pop("delta", None)
    elif action == "set_cooldown":
        if event.get("cooldown_until") is None:
            raise ContinuityError(
                "ITEM_STORY_COORDINATE_REQUIRED",
                "set_cooldown requires cooldown_until",
            )
    elif action == "clear_cooldown":
        if "cooldown_until" in event:
            raise ContinuityError(
                "ITEM_ACTION_FIELD_UNSUPPORTED",
                "clear_cooldown cannot carry cooldown_until",
                details={"field": "cooldown_until"},
            )
    elif action == "suppress" and event.get("reason") is not None:
        event["reason"] = str(event["reason"]).strip()


def _normalize_item_use_event(event: dict[str, Any]) -> None:
    action = require_choice(
        event.get("action") or "use",
        "action",
        ("use", "trigger", "consume"),
    )
    _normalize_item_subject(event)
    _require_entity(event, "actor_entity_id")
    function_id = str(event.get("function_id") or "").strip()
    if not function_id:
        raise ContinuityError(
            "EVENT_ENTITY_REQUIRED",
            "item_use requires function_id",
        )
    event["action"] = action
    event["function_id"] = function_id
    for field in (
        "target_entity_id",
        "location_entity_id",
        "resource_entity_id",
    ):
        if event.get(field) is not None:
            event[field] = _require_entity(event, field)
    event["delta"] = _normalize_item_delta(event.get("delta") or {})


def _normalize_item_observation_event(event: dict[str, Any]) -> None:
    subject_type = normalize_text(str(event.get("subject_type") or ""))
    if subject_type == "item_definition":
        subject_id = str(
            event.get("subject_id")
            or event.get("item_definition_id")
            or ""
        ).strip()
        definition_id = str(
            event.get("item_definition_id") or ""
        ).strip()
        if not subject_id:
            raise ContinuityError(
                "ITEM_SUBJECT_REQUIRED",
                "item observation requires an item definition, instance, or stack",
            )
        if definition_id and definition_id != subject_id:
            raise ContinuityError(
                "ITEM_SUBJECT_MISMATCH",
                "subject_id conflicts with item_definition_id",
            )
        event["subject_type"] = "item_definition"
        event["subject_id"] = subject_id
        event["item_definition_id"] = subject_id
    else:
        _normalize_item_subject(event)
    event["action"] = require_choice(
        event.get("action") or "observe",
        "action",
        ("observe", "reveal", "claim", "misidentify", "correct"),
    )
    event["knowledge_plane"] = require_choice(
        event.get("knowledge_plane") or "actor_belief",
        "knowledge_plane",
        KNOWLEDGE_PLANES,
    )
    observer = str(event.get("observer_entity_id") or "").strip()
    if event["knowledge_plane"] == "actor_belief" and not observer:
        raise ContinuityError(
            "EVENT_ENTITY_REQUIRED",
            "actor_belief item observation requires observer_entity_id",
        )
    event["observer_entity_id"] = observer or None
    event["confidence"] = validate_finite_number(
        event.get("confidence", 1.0),
        "confidence",
        minimum=0,
    )
    if event["confidence"] > 1:
        raise ContinuityError(
            "INVALID_FIELD",
            "confidence must be <= 1",
        )
    event["observation"] = _require_mapping(
        event.get("observation")
        or event.get("observed_fields")
        or {},
        "observation",
        allow_empty=False,
    )
    if event.get("function_id") is not None:
        event["function_id"] = str(event["function_id"]).strip() or None
    for field in ("source_entity_id", "target_entity_id"):
        if event.get(field) is not None:
            event[field] = _require_entity(event, field)


def _reject_unknown_advantage_fields(
    event: Mapping[str, Any],
    event_type: str,
) -> None:
    allowed = (
        _ADVANTAGE_COMMON_FIELDS
        | _ADVANTAGE_FIELDS_BY_EVENT[event_type]
        | _ADVANTAGE_RUNTIME_OUTPUT_FIELDS
    )
    unknown = sorted(set(event) - allowed)
    if unknown:
        raise ContinuityError(
            "ADVANTAGE_EVENT_FIELD_UNSUPPORTED",
            "typed advantage event contains unsupported top-level fields",
            details={"event_type": event_type, "fields": unknown},
        )


def _reject_advantage_runtime_outputs(event: Mapping[str, Any]) -> None:
    forbidden = sorted(_ADVANTAGE_RUNTIME_OUTPUT_FIELDS.intersection(event))
    if forbidden:
        raise ContinuityError(
            "ADVANTAGE_COMPUTED_STATE_FORBIDDEN",
            "advantage before/after state is computed by the local reducer",
            details={"fields": forbidden},
        )


def _advantage_text(
    event: Mapping[str, Any],
    field: str,
    *,
    required: bool = False,
) -> str | None:
    value = event.get(field)
    if value is None:
        if required:
            raise ContinuityError(
                "EVENT_ENTITY_REQUIRED",
                f"{event.get('event_type', 'advantage event')} requires {field}",
                details={"field": field},
            )
        return None
    text = str(value).strip()
    if not text:
        raise ContinuityError(
            "INVALID_FIELD",
            f"{field} must be a non-empty string",
            details={"field": field},
        )
    return text


def _normalize_advantage_origin(
    event: dict[str, Any],
    field: str = "origin",
) -> None:
    if field not in event or event[field] is None:
        return
    event[field] = _advantage_text(event, field)


def _validate_advantage_json_tree(
    value: Any,
    *,
    field: str,
    depth: int = 0,
) -> None:
    if depth > 16:
        raise ContinuityError(
            "INVALID_FIELD",
            f"{field} exceeds the maximum nesting depth",
            details={"field": field},
        )
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ContinuityError(
                    "INVALID_FIELD",
                    f"{field} contains an invalid object key",
                    details={"field": field},
                )
            _validate_advantage_json_tree(
                child,
                field=f"{field}.{key}",
                depth=depth + 1,
            )
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_advantage_json_tree(
                child,
                field=f"{field}[{index}]",
                depth=depth + 1,
            )
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ContinuityError(
            "INVALID_FIELD",
            f"{field} must contain only finite numbers",
            details={"field": field},
        )
    if value is not None and not isinstance(
        value,
        (str, int, float, bool),
    ):
        raise ContinuityError(
            "INVALID_FIELD",
            f"{field} contains a non-JSON value",
            details={"field": field},
        )


def _normalize_advantage_payload_field(
    event: dict[str, Any],
    field: str,
) -> None:
    if field not in event:
        return
    _validate_advantage_json_tree(event[field], field=field)
    event[field] = deepcopy(event[field])


def _normalize_advantage_number(
    event: dict[str, Any],
    field: str,
    *,
    minimum: float | None = None,
) -> None:
    if field not in event or event[field] is None:
        return
    event[field] = validate_finite_number(
        event[field],
        field,
        minimum=minimum,
    )


def _normalize_advantage_cost_amounts(value: Any, field: str) -> None:
    if isinstance(value, Mapping):
        numeric_values = (
            bool(value)
            and "amount" not in value
            and all(
                isinstance(item, (int, float)) and not isinstance(item, bool)
                for item in value.values()
            )
        )
        if numeric_values:
            for key, amount in value.items():
                validate_finite_number(
                    amount,
                    f"{field}.{key}",
                    minimum=0,
                )
        for key, child in value.items():
            if key == "amount":
                validate_finite_number(
                    child,
                    f"{field}.amount",
                    minimum=0,
                )
            elif isinstance(child, (Mapping, list)):
                _normalize_advantage_cost_amounts(
                    child,
                    f"{field}.{key}",
                )
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _normalize_advantage_cost_amounts(
                child,
                f"{field}[{index}]",
            )


def _normalize_advantage_links(value: Any, field: str) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise ContinuityError(
            "INVALID_FIELD",
            f"{field} must be a string or list of strings",
            details={"field": field},
        )
    if not all(isinstance(item, str) and item.strip() for item in values):
        raise ContinuityError(
            "INVALID_FIELD",
            f"{field} must contain non-empty event ids",
            details={"field": field},
        )
    return list(dict.fromkeys(item.strip() for item in values))


def _normalize_advantage_envelope_fields(
    event: dict[str, Any],
) -> None:
    event_type = str(event["event_type"])
    if event.get("schema_version") != ADVANTAGE_SCHEMA_VERSION:
        raise ContinuityError(
            "ADVANTAGE_SCHEMA_REQUIRED",
            "typed advantage events require schema_version=plot-rag-advantage/v1",
            details={"schema_version": event.get("schema_version")},
        )
    event["advantage_id"] = _advantage_text(
        event,
        "advantage_id",
        required=True,
    )
    raw_evidence = event.get("evidence")
    if isinstance(raw_evidence, str):
        if not raw_evidence.strip():
            evidence: dict[str, Any] = {}
        else:
            evidence = {"quote": raw_evidence.strip()}
    elif isinstance(raw_evidence, Mapping):
        evidence = dict(raw_evidence)
    else:
        evidence = {}
    if not evidence:
        raise ContinuityError(
            "ADVANTAGE_EVIDENCE_REQUIRED",
            "typed advantage events require non-empty evidence",
        )
    quote = evidence.get("quote")
    if quote is None:
        quote = evidence.get("verbatim_quote")
        if quote is not None:
            evidence["quote"] = quote
    if quote is not None and (
        not isinstance(quote, str)
        or not quote
        or quote != quote.strip()
        or len(quote) > 8192
    ):
        raise ContinuityError(
            "ADVANTAGE_EVIDENCE_REQUIRED",
            "advantage evidence quote must be non-empty and contiguous",
        )
    _validate_advantage_json_tree(evidence, field="evidence")
    event["evidence"] = evidence

    if (
        event_type in _ADVANTAGE_COORDINATE_REQUIRED_EVENTS
        and event.get("story_coordinate") is None
    ):
        raise ContinuityError(
            "ADVANTAGE_STORY_COORDINATE_REQUIRED",
            f"{event_type} requires a comparable story_coordinate",
        )
    if event.get("knowledge_plane") is not None:
        event["knowledge_plane"] = require_choice(
            event["knowledge_plane"],
            "knowledge_plane",
            KNOWLEDGE_PLANES,
        )
    if event.get("confidence") is not None:
        event["confidence"] = validate_finite_number(
            event["confidence"],
            "confidence",
            minimum=0,
        )
        if event["confidence"] > 1:
            raise ContinuityError(
                "INVALID_FIELD",
                "confidence must be <= 1",
            )
    if event.get("source_claim_ids") is not None:
        event["source_claim_ids"] = _normalize_string_list(
            event["source_claim_ids"],
            "source_claim_ids",
        )
    else:
        event["source_claim_ids"] = []
    for field in (
        "actor_entity_id",
        "target_entity_id",
        "experience_contract_id",
        "experience_contract_hash",
        "caused_by",
        "causal_event_id",
    ):
        if event.get(field) is not None:
            event[field] = str(event[field]).strip() or None
    _normalize_advantage_origin(event)
    for field in ("causal_provenance", "provenance"):
        if event.get(field) is not None:
            event[field] = _require_mapping(event[field], field)
    # Preserve the frozen event-experience contract snapshot alongside its
    # stable ID.  The contract is control metadata, not reducer-computed
    # runtime state, so it remains an accepted JSON payload without granting
    # the remote model ownership of any derived values.
    for field in ("experience_contract", "narrative_contract"):
        if event.get(field) is not None:
            _normalize_advantage_payload_field(event, field)
    for field in ("supersedes", "retracts"):
        if event.get(field) is not None:
            event[field] = _normalize_advantage_links(
                event[field],
                field,
            )


def _normalize_advantage_spec_event(event: dict[str, Any]) -> None:
    action = require_choice(
        event.get("action"),
        "action",
        ("define", "update", "deprecate", "supersede"),
    )
    spec_type = require_choice(
        event.get("spec_type"),
        "spec_type",
        (
            "advantage_definition",
            "runtime_slot",
            "narrative_contract",
        ),
    )
    event["action"] = action
    event["spec_type"] = spec_type
    if event.get("definition") is not None:
        event["definition"] = _require_mapping(
            event["definition"],
            "definition",
        )
    else:
        event["definition"] = {}
    definition = event["definition"]

    if spec_type == "advantage_definition":
        if action == "define":
            title = (
                _advantage_text(event, "title")
                or str(definition.get("title") or "").strip()
            )
            if not title:
                raise ContinuityError(
                    "EVENT_VALUE_REQUIRED",
                    "advantage definition requires title",
                )
            event["title"] = title
            anchor_type = (
                event.get("anchor_type")
                if event.get("anchor_type") is not None
                else definition.get("anchor_type")
            )
            event["anchor_type"] = require_choice(
                anchor_type,
                "anchor_type",
                ADVANTAGE_ANCHOR_TYPES,
            )
        elif event.get("anchor_type") is not None:
            event["anchor_type"] = require_choice(
                event["anchor_type"],
                "anchor_type",
                ADVANTAGE_ANCHOR_TYPES,
            )
        if event.get("profiles") is not None:
            event["profiles"] = _normalize_string_list(
                event["profiles"],
                "profiles",
            )
        for field in ("status", "authority_status"):
            if event.get(field) is not None:
                event[field] = require_choice(
                    event[field],
                    field,
                    ADVANTAGE_AUTHORITY_STATUSES,
                )
        for field in ("title", "acquisition_mode", "uniqueness"):
            if event.get(field) is not None:
                event[field] = _advantage_text(event, field)
        for field in ("name", "origin", "slot_kind"):
            if event.get(field) is not None:
                event[field] = _advantage_text(event, field)
        for field in ("promise", "counterplay"):
            _normalize_advantage_payload_field(event, field)
        return

    if spec_type == "runtime_slot":
        slot_id = str(
            event.get("slot_id")
            or event.get("spec_id")
            or definition.get("slot_id")
            or ""
        ).strip()
        if not slot_id:
            raise ContinuityError(
                "EVENT_ENTITY_REQUIRED",
                "runtime_slot advantage spec requires slot_id",
            )
        event["slot_id"] = slot_id
        if event.get("spec_id") is None:
            event["spec_id"] = slot_id
        for field in ("module_id", "stage"):
            if event.get(field) is not None:
                event[field] = _advantage_text(event, field)
        for field in ("name", "origin", "slot_kind"):
            if event.get(field) is not None:
                event[field] = _advantage_text(event, field)
        _normalize_advantage_number(event, "capacity", minimum=0)
        if event.get("set_membership") is not None:
            event["set_membership"] = _normalize_string_list(
                event["set_membership"],
                "set_membership",
            )
        _normalize_advantage_payload_field(event, "unlock_graph")
        if event.get("slot_status") is not None:
            event["slot_status"] = require_choice(
                event["slot_status"],
                "slot_status",
                ("locked", "available", "filled", "disabled"),
            )
        return

    contract_id = str(
        event.get("narrative_contract_id")
        or event.get("spec_id")
        or event.get("contract_id")
        or definition.get("narrative_contract_id")
        or ""
    ).strip()
    if not contract_id:
        raise ContinuityError(
            "EVENT_ENTITY_REQUIRED",
            "narrative_contract advantage spec requires an id",
        )
    event["narrative_contract_id"] = contract_id
    if event.get("spec_id") is None:
        event["spec_id"] = contract_id
    if event.get("contract_status") is not None:
        event["contract_status"] = require_choice(
            event["contract_status"],
            "contract_status",
            ("active", "planned", "retired"),
        )
    for field in (
        "reading_promise",
        "reward_loop",
        "risk_loop",
        "reveal_ladder",
        "experience_binding",
    ):
        _normalize_advantage_payload_field(event, field)


def _normalize_advantage_anchor_event(event: dict[str, Any]) -> None:
    action = require_choice(
        event.get("action"),
        "action",
        ("define", "update", "deprecate", "supersede"),
    )
    event["action"] = action
    event["anchor_id"] = _advantage_text(
        event,
        "anchor_id",
        required=True,
    )
    if event.get("anchor_type") is not None:
        event["anchor_type"] = require_choice(
            event["anchor_type"],
            "anchor_type",
            ADVANTAGE_ANCHOR_TYPES,
        )
    if action == "define" and not (
        str(event.get("anchor_ref_id") or "").strip()
        or str(event.get("subject_id") or "").strip()
    ):
        raise ContinuityError(
            "EVENT_ENTITY_REQUIRED",
            "advantage anchor define requires anchor_ref_id or subject_id",
        )
    for field in (
        "anchor_ref_id",
        "subject_id",
        "owner_entity_id",
        "anchor_name",
    ):
        if event.get(field) is not None:
            event[field] = _advantage_text(event, field)
    _normalize_advantage_origin(event)
    if event.get("binding_state") is not None:
        event["binding_state"] = require_choice(
            event["binding_state"],
            "binding_state",
            (
                "unbound",
                "bound",
                "dormant",
                "sealed",
                "contested",
                "released",
            ),
        )
    if event.get("anchor_status") is not None:
        event["anchor_status"] = require_choice(
            event["anchor_status"],
            "anchor_status",
            ("active", "deprecated", "superseded"),
        )
    for field in ("authority_status", "status"):
        if event.get(field) is not None:
            event[field] = require_choice(
                event[field],
                field,
                ADVANTAGE_AUTHORITY_STATUSES,
            )
    if event.get("transfer_rule") is not None:
        transfer_rule = event["transfer_rule"]
        if isinstance(transfer_rule, str):
            transfer_rule = transfer_rule.strip()
            if not transfer_rule:
                raise ContinuityError(
                    "INVALID_FIELD",
                    "transfer_rule must be a non-empty string or object",
                    details={"field": "transfer_rule"},
                )
            event["transfer_rule"] = transfer_rule
        else:
            event["transfer_rule"] = _require_mapping(
                transfer_rule,
                "transfer_rule",
            )
    if event.get("attributes") is not None:
        event["attributes"] = _require_mapping(
            event["attributes"],
            "attributes",
        )


def _normalize_advantage_module_event(event: dict[str, Any]) -> None:
    action = require_choice(
        event.get("action"),
        "action",
        (
            "define",
            "update",
            "unlock",
            "enable",
            "lock",
            "suppress",
            "deprecate",
        ),
    )
    event["action"] = action
    event["module_id"] = _advantage_text(
        event,
        "module_id",
        required=True,
    )
    definition = (
        _require_mapping(event["definition"], "definition")
        if event.get("definition") is not None
        else {}
    )
    event["definition"] = definition
    if action == "define":
        title = str(
            event.get("title") or definition.get("title") or ""
        ).strip()
        kind = str(
            event.get("kind")
            or event.get("module_kind")
            or definition.get("kind")
            or definition.get("module_kind")
            or ""
        ).strip()
        if not title or not kind:
            raise ContinuityError(
                "EVENT_VALUE_REQUIRED",
                "advantage module define requires title and kind",
            )
        event["title"] = title
        event["kind"] = kind
    for field in ("title", "kind", "module_kind", "stage"):
        if event.get(field) is not None:
            event[field] = _advantage_text(event, field)
    for field in ("name", "profile", "origin", "reveal_stage"):
        if event.get(field) is not None:
            event[field] = _advantage_text(event, field)
    for field in ("anchor_ids", "granted_ability_ids"):
        if event.get(field) is not None:
            event[field] = _normalize_string_list(
                event[field],
                field,
            )
    if event.get("range") is not None:
        _normalize_advantage_payload_field(event, "range")
    raw_status = event.get("status")
    if raw_status in ADVANTAGE_AUTHORITY_STATUSES:
        if (
            event.get("authority_status") is not None
            and event["authority_status"] != raw_status
        ):
            raise ContinuityError(
                "INVALID_FIELD",
                "status conflicts with authority_status",
                details={
                    "status": raw_status,
                    "authority_status": event["authority_status"],
                },
            )
        event["authority_status"] = raw_status
        event.pop("status", None)
    if event.get("authority_status") is not None:
        event["authority_status"] = require_choice(
            event["authority_status"],
            "authority_status",
            ADVANTAGE_AUTHORITY_STATUSES,
        )
    for field in ("status", "module_status"):
        if event.get(field) is not None:
            event[field] = require_choice(
                event[field],
                field,
                (
                    "locked",
                    "available",
                    "enabled",
                    "suppressed",
                    "deprecated",
                    "superseded",
                ),
            )
    for field in (
        "trigger",
        "preconditions",
        "targets",
        "costs",
        "effects",
        "side_effects",
        "failure_modes",
        "counters",
    ):
        _normalize_advantage_payload_field(event, field)
def _normalize_advantage_runtime_event(
    event: dict[str, Any],
) -> None:
    event_type = str(event["event_type"])
    if event_type == "advantage_bind":
        event["action"] = require_choice(
            event.get("action"),
            "action",
            ("bind", "unbind", "release", "seal", "contest"),
        )
        event["anchor_id"] = _advantage_text(
            event,
            "anchor_id",
            required=True,
        )
        if event.get("owner_entity_id") is not None:
            event["owner_entity_id"] = _advantage_text(
                event,
                "owner_entity_id",
            )
        return

    if event_type == "advantage_activate":
        event["action"] = require_choice(
            event.get("action"),
            "action",
            ("activate", "deactivate", "seal", "unseal"),
        )
        for field in ("owner_entity_id", "stage"):
            if event.get(field) is not None:
                event[field] = _advantage_text(event, field)
        _normalize_advantage_number(event, "charges", minimum=0)
        _normalize_advantage_number(event, "max_charges", minimum=0)
        for field in ("pollution", "exposure", "debt"):
            _normalize_advantage_number(event, field, minimum=0)
        if event.get("resources") is not None:
            resources = _require_mapping(event["resources"], "resources")
            normalized_resources: dict[str, float] = {}
            for key, value in resources.items():
                resource = str(key).strip()
                if not resource:
                    raise ContinuityError(
                        "INVALID_FIELD",
                        "resources keys must be non-empty strings",
                        details={"field": "resources"},
                    )
                normalized_resources[resource] = validate_finite_number(
                    value,
                    f"resources.{resource}",
                    minimum=0,
                )
            event["resources"] = normalized_resources
        if "cooldown_until" in event:
            event["cooldown_until"] = normalize_story_coordinate(
                event.get("cooldown_until"),
                "cooldown_until",
            )
        if event.get("runtime_metadata") is not None:
            event["runtime_metadata"] = _require_mapping(
                event["runtime_metadata"],
                "runtime_metadata",
            )
        if (
            event.get("charges") is not None
            and event.get("max_charges") is not None
            and float(event["charges"]) > float(event["max_charges"])
        ):
            raise ContinuityError(
                "ADVANTAGE_CONSERVATION_VIOLATION",
                "charges exceed max_charges",
            )
        return

    if event_type in {
        "advantage_trigger",
        "advantage_use",
        "advantage_reward",
        "advantage_cost",
    }:
        if event_type in {"advantage_trigger", "advantage_use"}:
            event["module_id"] = _advantage_text(
                event,
                "module_id",
                required=True,
            )
        elif event.get("module_id") is not None:
            event["module_id"] = _advantage_text(event, "module_id")
        for field in ("entry_id", "actor", "target"):
            if event.get(field) is not None:
                event[field] = _advantage_text(event, field)
        if event_type in {"advantage_reward", "advantage_cost"}:
            if "record_only" in event and type(event["record_only"]) is not bool:
                raise ContinuityError(
                    "INVALID_FIELD",
                    "record_only must be a boolean",
                    details={"field": "record_only"},
                )
            if event.get("ledger_entry_kind") is not None:
                event["ledger_entry_kind"] = _advantage_text(
                    event,
                    "ledger_entry_kind",
                )
            for field in ("input", "loss"):
                _normalize_advantage_payload_field(event, field)
        for field in (
            "costs",
            "rewards",
            "output",
            "effects",
            "side_effects",
        ):
            _normalize_advantage_payload_field(event, field)
        for field in ("costs", "rewards", "output"):
            if field in event:
                _normalize_advantage_cost_amounts(
                    event[field],
                    field,
                )
        if event.get("cooldown") is not None:
            if isinstance(event["cooldown"], Mapping):
                event["cooldown"] = normalize_story_coordinate(
                    event["cooldown"],
                    "cooldown",
                )
            else:
                cooldown = validate_finite_number(
                    event["cooldown"],
                    "cooldown",
                    minimum=0,
                )
                if not cooldown.is_integer():
                    raise ContinuityError(
                        "INVALID_FIELD",
                        "cooldown must be a whole coordinate delta",
                    )
                event["cooldown"] = int(cooldown)
        for field in (
            "pollution_delta",
            "exposure_delta",
            "debt_delta",
        ):
            _normalize_advantage_number(event, field, minimum=0)
        return

    if event_type == "advantage_upgrade":
        to_stage = str(
            event.get("to_stage") or event.get("stage") or ""
        ).strip()
        if not to_stage:
            raise ContinuityError(
                "EVENT_FIELD_REQUIRED",
                "advantage_upgrade requires to_stage or stage",
            )
        event["to_stage"] = to_stage
        if event.get("stage") is not None:
            event["stage"] = _advantage_text(event, "stage")
        if event.get("unlock_modules") is not None:
            event["unlock_modules"] = _normalize_string_list(
                event["unlock_modules"],
                "unlock_modules",
            )
        else:
            event["unlock_modules"] = []
        _normalize_advantage_number(event, "max_charges", minimum=0)
        if event.get("entry_id") is not None:
            event["entry_id"] = _advantage_text(event, "entry_id")
        return

    raise ContinuityError(
        "UNSUPPORTED_EVENT_TYPE",
        f"unsupported advantage runtime event: {event_type}",
    )


def _normalize_advantage_reveal_event(event: dict[str, Any]) -> None:
    if event.get("knowledge_plane") is None:
        raise ContinuityError(
            "INVALID_FIELD",
            "advantage_reveal requires knowledge_plane",
        )
    event["knowledge_plane"] = require_choice(
        event["knowledge_plane"],
        "knowledge_plane",
        KNOWLEDGE_PLANES,
    )
    claim = event.get("claim")
    if claim is None or claim == "" or claim == {} or claim == []:
        raise ContinuityError(
            "EVENT_VALUE_REQUIRED",
            "advantage_reveal requires claim",
        )
    _normalize_advantage_payload_field(event, "claim")
    event["reveal_stage"] = _advantage_text(
        event,
        "reveal_stage",
        required=True,
    )
    status = require_choice(
        event.get("status") or "canon",
        "status",
        ADVANTAGE_AUTHORITY_STATUSES,
    )
    event["status"] = status
    if status == "misread":
        event["misread_of"] = _advantage_text(
            event,
            "misread_of",
            required=True,
        )
    elif event.get("misread_of") is not None:
        event["misread_of"] = _advantage_text(event, "misread_of")
    if event.get("confidence") is None:
        event["confidence"] = 1.0
    for field in (
        "knowledge_id",
        "module_id",
        "observer_entity_id",
        "entry_id",
    ):
        if event.get(field) is not None:
            event[field] = _advantage_text(event, field)
    if (
        event["knowledge_plane"] == "actor_belief"
        and not event.get("observer_entity_id")
    ):
        raise ContinuityError(
            "EVENT_ENTITY_REQUIRED",
            "actor_belief advantage reveal requires observer_entity_id",
        )
    if "record_ledger" in event and type(event["record_ledger"]) is not bool:
        raise ContinuityError(
            "INVALID_FIELD",
            "record_ledger must be a boolean",
        )


def _normalize_advantage_contract_event(event: dict[str, Any]) -> None:
    action = require_choice(
        event.get("action"),
        "action",
        (
            "define",
            "update",
            "activate",
            "suspend",
            "breach",
            "fulfill",
            "terminate",
            "narrative",
        ),
    )
    event["action"] = action
    if action == "narrative":
        contract_id = str(
            event.get("narrative_contract_id")
            or event.get("contract_id")
            or ""
        ).strip()
        if not contract_id:
            raise ContinuityError(
                "EVENT_ENTITY_REQUIRED",
                "narrative advantage contract requires an id",
            )
        event["narrative_contract_id"] = contract_id
        # The reducer's narrative path accepts either spelling but its
        # definition lookup is keyed by contract_id.  Canonicalize both so
        # replay and readable projections observe one stable identifier.
        event["contract_id"] = contract_id
    else:
        event["contract_id"] = _advantage_text(
            event,
            "contract_id",
            required=True,
        )
    for field in (
        "actor_entity_id",
        "counterparty_entity_id",
        "entry_id",
        "contract_kind",
    ):
        if event.get(field) is not None:
            event[field] = _advantage_text(event, field)
    _normalize_advantage_origin(event)
    if event.get("parties") is not None:
        event["parties"] = _normalize_string_list(
            event["parties"],
            "parties",
        )
    if event.get("authority_status") is not None:
        event["authority_status"] = require_choice(
            event["authority_status"],
            "authority_status",
            ADVANTAGE_AUTHORITY_STATUSES,
        )
    for field in ("trust_delta", "debt_delta"):
        _normalize_advantage_number(event, field)
    for field in (
        "terms",
        "agency",
        "breach_effect",
        "reading_promise",
        "reward_loop",
        "risk_loop",
        "reveal_ladder",
        "experience_binding",
    ):
        _normalize_advantage_payload_field(event, field)


def _normalize_advantage_correction_event(
    event: dict[str, Any],
    *,
    artifact_stage: str,
) -> None:
    replacement = event.get("replacement")
    action = require_choice(
        event.get("action") or ("correct" if replacement is not None else ""),
        "action",
        ("correct", "supersede", "retract"),
    )
    targets: list[str] = []
    explicit_target = str(event.get("target_event_id") or "").strip()
    if explicit_target:
        targets.append(explicit_target)
    for field in ("supersedes", "retracts"):
        targets.extend(
            str(value)
            for value in event.get(field) or []
            if str(value).strip()
        )
    targets = list(dict.fromkeys(value.strip() for value in targets))
    if len(targets) != 1:
        raise ContinuityError(
            "ADVANTAGE_CORRECTION_TARGET_REQUIRED",
            "advantage_correction requires exactly one target event",
            details={"target_event_ids": targets},
        )
    target_event_id = targets[0]
    event["action"] = action
    event["target_event_id"] = target_event_id
    if action == "retract":
        if replacement is not None:
            raise ContinuityError(
                "INVALID_FIELD",
                "retract advantage_correction cannot contain replacement",
            )
        event["retracts"] = [target_event_id]
        return
    if not isinstance(replacement, Mapping):
        raise ContinuityError(
            "CORRECTION_REPLACEMENT_REQUIRED",
            "advantage_correction requires a replacement advantage event",
        )
    normalized = normalize_event(
        replacement,
        artifact_stage=artifact_stage,
        branch_id=str(event["branch_id"]),
        chapter_no=event["chapter_no"],
        scene_index=event["scene_index"],
    )
    if normalized["event_type"] not in ADVANTAGE_EVENT_TYPES[:-1]:
        raise ContinuityError(
            "ADVANTAGE_CORRECTION_REPLACEMENT_INVALID",
            "advantage correction replacement must be an advantage event",
        )
    if normalized["advantage_id"] != event["advantage_id"]:
        raise ContinuityError(
            "ADVANTAGE_CORRECTION_ID_MISMATCH",
            "advantage correction and replacement must share advantage_id",
        )
    event["replacement"] = normalized
    event["supersedes"] = [target_event_id]


def _normalize_advantage_event(
    event: dict[str, Any],
    *,
    artifact_stage: str,
) -> None:
    event_type = str(event["event_type"])
    if event_type == "advantage_spec":
        _normalize_advantage_spec_event(event)
        if artifact_stage in {"bootstrap", "final", "published"}:
            event["scope"] = "timeless"
    elif event_type == "advantage_anchor":
        _normalize_advantage_anchor_event(event)
    elif event_type == "advantage_module":
        _normalize_advantage_module_event(event)
    elif event_type in {
        "advantage_bind",
        "advantage_activate",
        "advantage_trigger",
        "advantage_use",
        "advantage_reward",
        "advantage_cost",
        "advantage_upgrade",
    }:
        _normalize_advantage_runtime_event(event)
    elif event_type == "advantage_reveal":
        _normalize_advantage_reveal_event(event)
    elif event_type == "advantage_contract":
        _normalize_advantage_contract_event(event)
    elif event_type == "advantage_correction":
        _normalize_advantage_correction_event(
            event,
            artifact_stage=artifact_stage,
        )


def validate_advantage_experience_contract_bindings(
    events: Sequence[Mapping[str, Any]],
    *,
    required: bool,
    allowed_contract_ids: Sequence[str] | None = None,
    allowed_contract_bindings: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate EventExperience bindings on frozen Advantage leaf events.

    EventSeed contracts live in the control plane while Advantage deltas live
    in immutable continuity proposals.  A proposal-level lifecycle identity
    proves which locked manifest was used, but it does not by itself identify
    which contract owns each generated Advantage fact.  This helper keeps that
    event-level edge explicit without imposing the control-plane contract on
    legacy, initialization, or direct reducer callers.

    Correction envelopes are traversed to their deterministic leaf.  Retraction
    corrections contribute no replacement fact and therefore do not require a
    leaf binding.  When an envelope also carries a contract id, it must agree
    with the replacement rather than presenting two conflicting identities.
    """

    allowed: frozenset[str] | None = None
    if allowed_contract_ids is not None:
        normalized_allowed = {
            str(value).strip()
            for value in allowed_contract_ids
            if str(value).strip()
        }
        allowed = frozenset(normalized_allowed)
    expected_bindings: dict[str, dict[str, Any]] = {}
    for raw_binding in allowed_contract_bindings or ():
        if not isinstance(raw_binding, Mapping):
            continue
        contract_id = str(
            raw_binding.get("contract_id")
            or raw_binding.get("experience_contract_id")
            or ""
        ).strip()
        if not contract_id:
            continue
        expected = {
            "experience_contract_hash": str(
                raw_binding.get("contract_hash")
                or raw_binding.get("experience_contract_hash")
                or ""
            ).strip(),
            "event_seed_id": str(
                raw_binding.get("event_seed_id") or ""
            ).strip(),
            "event_seed_revision": raw_binding.get(
                "event_seed_revision"
            ),
        }
        previous = expected_bindings.get(contract_id)
        if previous is not None and previous != expected:
            raise ContinuityError(
                "ADVANTAGE_EXPERIENCE_CONTRACT_MISMATCH",
                "locked lifecycle manifest contains conflicting identities "
                "for one experience contract",
                details={
                    "experience_contract_id": contract_id,
                    "expected_bindings": [previous, expected],
                },
            )
        expected_bindings[contract_id] = expected
    if allowed is None and expected_bindings:
        allowed = frozenset(expected_bindings)

    checked = 0
    bound_ids: set[str] = set()
    correction_types = {
        "correction",
        "advantage_correction",
        "item_correction",
    }
    for event_index, raw_event in enumerate(events):
        if not isinstance(raw_event, Mapping):
            continue
        current: Mapping[str, Any] = raw_event
        envelope_contract_ids: list[str] = []
        envelope_contract_hashes: list[str] = []
        seen: set[int] = set()
        depth = 0
        while True:
            identity = id(current)
            if identity in seen:
                raise ContinuityError(
                    "CORRECTION_REPLACEMENT_CYCLE",
                    "correction replacement contains a cycle",
                    details={
                        "event_index": event_index,
                        "depth": depth,
                    },
                )
            seen.add(identity)
            event_type = normalize_text(
                str(
                    current.get("event_type")
                    or current.get("type")
                    or "fact"
                )
            )
            contract_id = str(
                current.get("experience_contract_id") or ""
            ).strip()
            if event_type in correction_types:
                if contract_id:
                    envelope_contract_ids.append(contract_id)
                contract_hash = str(
                    current.get("experience_contract_hash") or ""
                ).strip()
                if contract_hash:
                    envelope_contract_hashes.append(contract_hash)
                if event_type.endswith("_correction") and normalize_text(
                    str(current.get("action") or "")
                ) == "retract":
                    current = {}
                    break
                replacement = current.get("replacement")
                if not isinstance(replacement, Mapping):
                    # The ordinary correction validator owns the structural
                    # error.  Do not replace its more specific diagnostic.
                    current = {}
                    break
                current = replacement
                depth += 1
                continue
            break

        leaf_type = normalize_text(
            str(
                current.get("event_type")
                or current.get("type")
                or "fact"
            )
        )
        if leaf_type not in ADVANTAGE_EVENT_TYPES:
            continue
        checked += 1
        contract_id = str(
            current.get("experience_contract_id") or ""
        ).strip()
        if required and not contract_id:
            raise ContinuityError(
                "ADVANTAGE_EXPERIENCE_CONTRACT_REQUIRED",
                "lifecycle-bound Advantage events require "
                "experience_contract_id",
                details={
                    "event_index": event_index,
                    "event_type": leaf_type,
                    "correction_depth": depth,
                },
            )
        if not contract_id:
            continue
        conflicting_envelopes = sorted(
            {
                value
                for value in envelope_contract_ids
                if value != contract_id
            }
        )
        if conflicting_envelopes:
            raise ContinuityError(
                "ADVANTAGE_EXPERIENCE_CONTRACT_MISMATCH",
                "Advantage correction envelope and replacement bind "
                "different experience contracts",
                details={
                    "event_index": event_index,
                    "event_type": leaf_type,
                    "experience_contract_id": contract_id,
                    "envelope_contract_ids": conflicting_envelopes,
                },
            )
        contract_hash = str(
            current.get("experience_contract_hash") or ""
        ).strip()
        conflicting_envelope_hashes = sorted(
            {
                value
                for value in envelope_contract_hashes
                if contract_hash and value != contract_hash
            }
        )
        if conflicting_envelope_hashes:
            raise ContinuityError(
                "ADVANTAGE_EXPERIENCE_CONTRACT_MISMATCH",
                "Advantage correction envelope and replacement bind "
                "different experience contract hashes",
                details={
                    "event_index": event_index,
                    "event_type": leaf_type,
                    "experience_contract_id": contract_id,
                    "experience_contract_hash": contract_hash,
                    "envelope_contract_hashes": (
                        conflicting_envelope_hashes
                    ),
                },
            )
        if allowed is not None and contract_id not in allowed:
            raise ContinuityError(
                "ADVANTAGE_EXPERIENCE_CONTRACT_MISMATCH",
                "Advantage event experience_contract_id is not present in "
                "the locked lifecycle manifest",
                details={
                    "event_index": event_index,
                    "event_type": leaf_type,
                    "experience_contract_id": contract_id,
                    "allowed_contract_ids": sorted(allowed),
                },
            )
        expected = expected_bindings.get(contract_id)
        if expected is not None:
            expected_hash = str(
                expected["experience_contract_hash"] or ""
            )
            supplied_hashes = {
                value
                for value in [contract_hash, *envelope_contract_hashes]
                if value
            }
            if expected_hash and any(
                value != expected_hash for value in supplied_hashes
            ):
                raise ContinuityError(
                    "ADVANTAGE_EXPERIENCE_CONTRACT_MISMATCH",
                    "Advantage event experience contract hash differs from "
                    "the locked lifecycle manifest",
                    details={
                        "event_index": event_index,
                        "event_type": leaf_type,
                        "experience_contract_id": contract_id,
                        "expected_experience_contract_hash": expected_hash,
                        "actual_experience_contract_hashes": sorted(
                            supplied_hashes
                        ),
                    },
                )
            provenance = current.get("causal_provenance")
            if isinstance(provenance, Mapping):
                supplied_seed_id = provenance.get("event_seed_id")
                expected_seed_id = str(expected["event_seed_id"] or "")
                if (
                    supplied_seed_id not in (None, "")
                    and expected_seed_id
                    and str(supplied_seed_id).strip() != expected_seed_id
                ):
                    raise ContinuityError(
                        "ADVANTAGE_EXPERIENCE_CONTRACT_MISMATCH",
                        "Advantage event seed provenance differs from the "
                        "locked lifecycle manifest",
                        details={
                            "event_index": event_index,
                            "event_type": leaf_type,
                            "experience_contract_id": contract_id,
                            "identity_field": "event_seed_id",
                            "expected": expected_seed_id,
                            "actual": str(supplied_seed_id).strip(),
                        },
                    )
                supplied_seed_revision = provenance.get(
                    "event_seed_revision"
                )
                expected_seed_revision = expected[
                    "event_seed_revision"
                ]
                if (
                    supplied_seed_revision is not None
                    and expected_seed_revision is not None
                    and (
                        type(supplied_seed_revision)
                        is not type(expected_seed_revision)
                        or supplied_seed_revision
                        != expected_seed_revision
                    )
                ):
                    raise ContinuityError(
                        "ADVANTAGE_EXPERIENCE_CONTRACT_MISMATCH",
                        "Advantage event seed provenance differs from the "
                        "locked lifecycle manifest",
                        details={
                            "event_index": event_index,
                            "event_type": leaf_type,
                            "experience_contract_id": contract_id,
                            "identity_field": "event_seed_revision",
                            "expected": expected_seed_revision,
                            "actual": supplied_seed_revision,
                        },
                    )
            snapshot = current.get("experience_contract")
            if isinstance(snapshot, Mapping):
                snapshot_identity = {
                    "contract_id": str(
                        snapshot.get("contract_id")
                        or snapshot.get("experience_contract_id")
                        or ""
                    ).strip(),
                    "contract_hash": str(
                        snapshot.get("contract_hash")
                        or snapshot.get("experience_contract_hash")
                        or ""
                    ).strip(),
                    "event_seed_id": str(
                        snapshot.get("event_seed_id") or ""
                    ).strip(),
                    "event_seed_revision": snapshot.get(
                        "event_seed_revision"
                    ),
                }
                expected_snapshot = {
                    "contract_id": contract_id,
                    "contract_hash": expected_hash,
                    "event_seed_id": str(expected["event_seed_id"] or ""),
                    "event_seed_revision": expected[
                        "event_seed_revision"
                    ],
                }
                mismatches = {
                    field: {
                        "expected": expected_value,
                        "actual": snapshot_identity[field],
                    }
                    for field, expected_value in expected_snapshot.items()
                    if snapshot_identity[field] not in (None, "")
                    and expected_value not in (None, "")
                    and (
                        snapshot_identity[field] != expected_value
                        or (
                            field == "event_seed_revision"
                            and type(snapshot_identity[field])
                            is not type(expected_value)
                        )
                    )
                }
                if mismatches:
                    raise ContinuityError(
                        "ADVANTAGE_EXPERIENCE_CONTRACT_MISMATCH",
                        "frozen Advantage experience contract identity "
                        "differs from the locked lifecycle manifest",
                        details={
                            "event_index": event_index,
                            "event_type": leaf_type,
                            "experience_contract_id": contract_id,
                            "mismatches": mismatches,
                        },
                    )
        bound_ids.add(contract_id)

    return {
        "checked_advantage_event_count": checked,
        "bound_contract_ids": sorted(bound_ids),
    }


def _promote_advantage_correction_wrapper(event: dict[str, Any]) -> None:
    event_type = normalize_text(
        str(event.get("event_type") or event.get("type") or "")
    )
    if event_type != "correction":
        return
    replacement = event.get("replacement")
    if not isinstance(replacement, Mapping):
        return
    replacement_type = normalize_text(
        str(replacement.get("event_type") or replacement.get("type") or "")
    )
    if replacement_type not in ADVANTAGE_EVENT_TYPES:
        return

    targets: list[str] = []
    explicit_target = str(event.get("target_event_id") or "").strip()
    if explicit_target:
        targets.append(explicit_target)
    for field in ("supersedes", "retracts"):
        raw_links = event.get(field)
        if isinstance(raw_links, str):
            if raw_links.strip():
                targets.append(raw_links.strip())
        elif isinstance(raw_links, list):
            targets.extend(
                value.strip()
                for value in raw_links
                if isinstance(value, str) and value.strip()
            )
    targets = list(dict.fromkeys(targets))
    if len(targets) > 1:
        raise ContinuityError(
            "ADVANTAGE_CORRECTION_TARGET_AMBIGUOUS",
            "an advantage correction may supersede exactly one event",
            details={"target_event_ids": targets},
        )

    event.pop("type", None)
    event["event_type"] = "advantage_correction"
    event["action"] = str(event.get("action") or "correct")
    if targets:
        event["target_event_id"] = targets[0]
        event["supersedes"] = [targets[0]]
    for field in (
        "schema_version",
        "scope",
        "branch_id",
        "chapter_no",
        "scene_index",
        "story_time",
        "story_coordinate",
        "narrative_mode",
        "evidence",
        "knowledge_plane",
        "confidence",
        "effective_at",
        "ambiguity",
        "source_claim_ids",
        "advantage_id",
        "experience_contract_id",
        "experience_contract",
        "experience_contract_hash",
        "narrative_contract",
        "causal_provenance",
        "provenance",
        "caused_by",
        "causal_event_id",
    ):
        if event.get(field) is None and replacement.get(field) is not None:
            event[field] = deepcopy(replacement[field])


def _promote_item_correction_wrapper(event: dict[str, Any]) -> None:
    """Expose a generic correction around an item event to strict item replay.

    Older callers may use the generic ``correction`` envelope even when the
    replacement is a v4 item event.  Leaving that wrapper generic bypasses the
    item proposal contract and makes the replacement invisible to the
    independent item replay.  Promotion happens before event-family
    validation, so the resulting event must satisfy the exact item-correction
    schema rather than receiving a compatibility bypass.
    """

    event_type = normalize_text(
        str(event.get("event_type") or event.get("type") or "")
    )
    if event_type != "correction":
        return
    replacement = event.get("replacement")
    if not isinstance(replacement, Mapping):
        return
    replacement_type = normalize_text(
        str(replacement.get("event_type") or replacement.get("type") or "")
    )
    if replacement_type not in ITEM_EVENT_TYPES:
        return
    # Keep a generic wrapper around an already-typed correction. The generic
    # correction normalizer/replay path is responsible for recursively
    # expanding this nested replacement; promoting it here would make the
    # item_correction validator reject a correction replacement.
    if replacement_type == "item_correction":
        return

    targets: list[str] = []
    explicit_target = str(event.get("target_event_id") or "").strip()
    if explicit_target:
        targets.append(explicit_target)
    raw_supersedes = event.get("supersedes")
    if isinstance(raw_supersedes, str):
        if raw_supersedes.strip():
            targets.append(raw_supersedes.strip())
    elif isinstance(raw_supersedes, list):
        targets.extend(
            value.strip()
            for value in raw_supersedes
            if isinstance(value, str) and value.strip()
        )
    targets = list(dict.fromkeys(targets))
    if len(targets) > 1:
        raise ContinuityError(
            "ITEM_CORRECTION_TARGET_AMBIGUOUS",
            "an item correction may supersede exactly one item event",
            details={"target_event_ids": targets},
        )

    event.pop("type", None)
    event["event_type"] = "item_correction"
    event["action"] = str(event.get("action") or "correct")
    if targets:
        event["target_event_id"] = targets[0]
        event["supersedes"] = [targets[0]]

    # The wrapper may predate item_correction while its replacement already
    # carries the complete v4 evidence envelope.  Copy only missing explicit
    # values; an explicitly conflicting wrapper value remains visible to the
    # strict validator and fails closed.
    for field in (
        "schema_version",
        "scope",
        "branch_id",
        "chapter_no",
        "scene_index",
        "story_time",
        "story_coordinate",
        "narrative_mode",
        "evidence",
        "knowledge_plane",
        "confidence",
        "effective_at",
        "ambiguity",
    ):
        if event.get(field) is None and replacement.get(field) is not None:
            event[field] = deepcopy(replacement[field])


def normalize_event(
    raw_event: Mapping[str, Any],
    *,
    artifact_stage: str,
    branch_id: str,
    chapter_no: int | None,
    scene_index: int | None,
) -> dict[str, Any]:
    if not isinstance(raw_event, Mapping):
        raise ContinuityError("INVALID_EVENT", "event must be an object")

    event = deepcopy(dict(raw_event))
    _promote_item_correction_wrapper(event)
    _promote_advantage_correction_wrapper(event)
    event_type = normalize_text(
        str(event.pop("type", event.get("event_type", "fact")))
    )
    if (
        event_type not in EVENT_TYPES
        and event_type not in ITEM_EVENT_TYPES
        and event_type not in ADVANTAGE_EVENT_TYPES
    ):
        raise ContinuityError(
            "UNSUPPORTED_EVENT_TYPE",
            f"unsupported event type: {event_type}",
        )
    event["event_type"] = event_type
    if event_type in ITEM_EVENT_TYPES:
        _reject_unknown_item_fields(event, event_type)
    elif event_type in ADVANTAGE_EVENT_TYPES:
        _reject_unknown_advantage_fields(event, event_type)

    scope = normalize_text(
        str(event.get("scope") or default_scope_for_stage(artifact_stage))
    )
    if scope not in FACT_SCOPES:
        raise ContinuityError(
            "INVALID_SCOPE", f"unsupported fact scope: {scope}"
        )
    # Accepted outlines are authoritative plans only.  Even a model-supplied
    # timeless/world-rule scope must not let an outline mutate active canon.
    if artifact_stage == "outline":
        scope = "planned"
    event["scope"] = scope

    event["branch_id"] = str(event.get("branch_id") or branch_id or "main")
    event["chapter_no"] = validate_positive_int(
        event.get("chapter_no", chapter_no),
        "chapter_no",
        allow_none=True,
        minimum=1,
    )
    event["scene_index"] = validate_positive_int(
        event.get("scene_index", scene_index),
        "scene_index",
        allow_none=True,
        minimum=0,
    )
    event["story_time"] = (
        str(event["story_time"]).strip()
        if event.get("story_time") is not None
        else None
    )
    event["story_coordinate"] = normalize_story_coordinate(
        event.get("story_coordinate")
    )
    narrative_mode = normalize_text(str(event.get("narrative_mode") or "linear"))
    if narrative_mode not in {"linear", "flashback", "parallel", "summary"}:
        raise ContinuityError(
            "INVALID_NARRATIVE_MODE",
            f"unsupported narrative mode: {narrative_mode}",
        )
    event["narrative_mode"] = narrative_mode
    raw_evidence = event.get("evidence")
    event["evidence"] = (
        dict(raw_evidence)
        if isinstance(raw_evidence, Mapping)
        else raw_evidence
        if isinstance(raw_evidence, str)
        else {}
    )
    if event_type in ITEM_EVENT_TYPES:
        _reject_item_runtime_outputs(event)
        _normalize_item_envelope_fields(event)
    elif event_type in ADVANTAGE_EVENT_TYPES:
        _reject_advantage_runtime_outputs(event)
        _normalize_advantage_envelope_fields(event)
    elif (
        event_type == "inventory"
        and event.get("schema_version") is not None
        and event.get("schema_version") != LEGACY_DELTA_SCHEMA_VERSION
    ):
        raise ContinuityError(
            "INVENTORY_DELTA_SCHEMA_UNSUPPORTED",
            "legacy inventory events require schema_version=plot-rag-delta/v3",
            details={"schema_version": event.get("schema_version")},
        )

    if event_type in {"fact", "state", "world_rule", "time"}:
        if event_type not in {"world_rule", "time"}:
            _require_entity(event, "entity_id")
        field_name = str(
            event.get("field")
            or event.get("field_name")
            or (
                event.get("rule_id")
                if event_type == "world_rule"
                else event.get("time_id")
                if event_type == "time"
                else ""
            )
            or (
                event.get("statement")
                if event_type == "world_rule"
                else event.get("label")
                if event_type == "time"
                else ""
            )
        ).strip()
        if not field_name:
            raise ContinuityError(
                "EVENT_FIELD_REQUIRED", f"{event_type} requires field"
            )
        event["field"] = field_name
        if "value" not in event:
            if event_type == "world_rule":
                event["value"] = {
                    key: value
                    for key, value in event.items()
                    if key
                    not in {
                        "event_type",
                        "scope",
                        "branch_id",
                        "chapter_no",
                        "scene_index",
                        "story_time",
                        "narrative_mode",
                        "evidence",
                        "field",
                    }
                }
            elif event_type == "time":
                event["value"] = event.get("label") or event.get("story_time")
            else:
                raise ContinuityError(
                    "EVENT_VALUE_REQUIRED", f"{event_type} requires value"
                )

    elif event_type == "entity":
        _require_entity(event, "entity_id")
        event["entity_type"] = str(event.get("entity_type") or "unknown")
        event["canonical_name"] = str(
            event.get("canonical_name") or event.get("name") or event["entity_id"]
        )

    elif event_type == "relation":
        _require_entity(event, "source_entity_id")
        _require_entity(event, "target_entity_id")
        dimension = str(
            event.get("dimension") or event.get("relation_type") or ""
        ).strip()
        if not dimension:
            raise ContinuityError(
                "RELATION_DIMENSION_REQUIRED", "relation requires dimension"
            )
        event["dimension"] = dimension
        if "value" not in event:
            event["value"] = dict(event.get("dimensions") or {})

    elif event_type == "movement":
        _require_entity(event, "actor_entity_id")
        action = normalize_text(str(event.get("action") or "move"))
        if action == "initialize_at":
            action = "arrive"
        if action not in {
            "move",
            "depart",
            "arrive",
            "teleport",
            "enter",
            "leave",
        }:
            raise ContinuityError(
                "INVALID_MOVEMENT_ACTION", f"unsupported movement action: {action}"
            )
        event["action"] = action
        destination = event.get("to_location_entity_id")
        if action in {"move", "arrive", "teleport", "enter"} and not destination:
            raise ContinuityError(
                "MOVEMENT_DESTINATION_REQUIRED",
                f"{action} requires to_location_entity_id",
            )
        if action in {"leave", "depart"} and destination is None:
            event["to_location_entity_id"] = None
        if action in {"leave", "depart"} and event.get("location_entity_id"):
            raise ContinuityError(
                "LEAVE_CANNOT_SET_LOCATION",
                "leaving a place cannot set that place as the current location",
            )
        route = event.get("route")
        if route is not None and not isinstance(route, (list, str, Mapping)):
            raise ContinuityError(
                "INVALID_MOVEMENT_ROUTE",
                "movement route must be text, an object, or a list",
            )

    elif event_type == "inventory":
        _require_entity(event, "item_entity_id")
        action = normalize_text(str(event.get("action") or "acquire"))
        if action == "initialize_owner":
            action = "set"
        if action not in {
            "acquire",
            "transfer",
            "consume",
            "lose",
            "set",
        }:
            raise ContinuityError(
                "INVALID_INVENTORY_ACTION",
                f"unsupported inventory action: {action}",
            )
        event["action"] = action
        event["unique"] = bool(event.get("unique", False))
        if action in {"acquire", "transfer", "set"}:
            _require_entity(event, "to_owner_entity_id")
        if (
            not event["unique"]
            and action in {"transfer", "consume", "lose"}
        ):
            _require_entity(event, "from_owner_entity_id")
        if (
            not event["unique"]
            and action == "transfer"
            and str(event.get("from_owner_entity_id"))
            == str(event.get("to_owner_entity_id"))
        ):
            raise ContinuityError(
                "INVENTORY_TRANSFER_SAME_OWNER",
                "non-unique inventory transfer requires distinct owners",
            )
        quantity = event.get("quantity", 1)
        if isinstance(quantity, bool):
            raise ContinuityError(
                "INVALID_QUANTITY", "inventory quantity must be numeric"
            )
        try:
            quantity_number = float(quantity)
        except (TypeError, ValueError) as exc:
            raise ContinuityError(
                "INVALID_QUANTITY", "inventory quantity must be numeric"
            ) from exc
        if not math.isfinite(quantity_number):
            raise ContinuityError(
                "INVALID_QUANTITY", "inventory quantity must be finite"
            )
        if action == "set":
            invalid_quantity = quantity_number < 0
        else:
            invalid_quantity = quantity_number <= 0
        if invalid_quantity:
            raise ContinuityError(
                "INVALID_QUANTITY",
                (
                    "inventory set quantity cannot be negative"
                    if action == "set"
                    else "inventory quantity must be greater than zero"
                ),
            )
        event["quantity"] = quantity_number

    elif event_type == "ability":
        _require_entity(event, "owner_entity_id")
        _require_entity(event, "ability_entity_id")
        action = normalize_text(str(event.get("action") or "set"))
        if action == "initialize":
            action = "set"
        if action not in {
            "gain",
            "set",
            "use",
            "cooldown",
            "breakthrough",
            "lose",
            "unlock",
            "upgrade",
            "charge",
            "activate",
            "deactivate",
            "refresh",
        }:
            raise ContinuityError(
                "INVALID_ABILITY_ACTION",
                f"unsupported ability action: {action}",
            )
        event["action"] = action
        state = dict(event.get("state") or {})
        # The top-level action is the sole action truth.  Keeping a stale
        # nested action caused legacy use/cooldown events to overwrite durable
        # ownership state during replay.
        state.pop("action", None)
        if "use_count" in state:
            raise ContinuityError(
                "INVALID_FIELD",
                "state.use_count is managed by the continuity runtime",
                details={"field": "state.use_count"},
            )
        if state.get("cooldown_until") is not None:
            state["cooldown_until"] = normalize_story_coordinate(
                state.get("cooldown_until"),
                "state.cooldown_until",
            )
        event["state"] = state
        if event.get("prerequisites") is not None:
            event["prerequisites"] = _normalize_power_prerequisites(
                event["prerequisites"],
                "prerequisites",
            )
        if event.get("cooldown_until") is not None:
            event["cooldown_until"] = normalize_story_coordinate(
                event.get("cooldown_until"), "cooldown_until"
            )

    elif event_type == "power_spec":
        action = require_choice(
            event.get("action") or "define",
            "action",
            ("define", "amend", "deprecate"),
        )
        spec_type = require_choice(
            event.get("spec_type"),
            "spec_type",
            POWER_SPEC_TYPES,
        )
        spec_entity_id = str(
            event.get("spec_entity_id")
            or event.get(
                {
                    "power_system": "system_entity_id",
                    "progression_track": "track_entity_id",
                    "rank_node": "rank_entity_id",
                    "rank_edge": "edge_entity_id",
                    "ability_definition": "ability_entity_id",
                    "resource_definition": "resource_entity_id",
                    "status_definition": "status_entity_id",
                    "qualification_definition": (
                        "qualification_entity_id"
                    ),
                    "counter_rule": "rule_entity_id",
                    "bridge_rule": "rule_entity_id",
                    "conversion_rule": "rule_entity_id",
                }[spec_type]
            )
            or ""
        ).strip()
        if not spec_entity_id:
            raise ContinuityError(
                "EVENT_ENTITY_REQUIRED",
                "power_spec requires spec_entity_id",
            )
        raw_definition = event.get("definition")
        if not isinstance(raw_definition, Mapping):
            raise ContinuityError(
                "EVENT_VALUE_REQUIRED",
                "power_spec requires a definition object",
            )
        definition = dict(raw_definition)
        if spec_type == "rank_edge":
            if (
                "from_rank_entity_ids" not in definition
                and definition.get("from_node_ids") is not None
            ):
                definition["from_rank_entity_ids"] = list(
                    definition.get("from_node_ids") or []
                )
            if (
                "to_rank_entity_id" not in definition
                and definition.get("to_node_id") is not None
            ):
                definition["to_rank_entity_id"] = definition.get(
                    "to_node_id"
                )
        if definition.get("prerequisites") is not None:
            definition["prerequisites"] = _normalize_power_prerequisites(
                definition["prerequisites"],
                "definition.prerequisites",
            )
        event["action"] = action
        event["spec_type"] = spec_type
        event["spec_entity_id"] = spec_entity_id
        event["definition"] = definition
        if action == "define":
            required_definition_fields = {
                "progression_track": ("track_kind",),
                "rank_node": ("track_entity_id",),
                "rank_edge": (
                    "track_entity_id",
                    "from_rank_entity_ids",
                    "to_rank_entity_id",
                ),
                "conversion_rule": (
                    "source_resource_entity_id",
                    "target_resource_entity_id",
                    "ratio",
                ),
            }.get(spec_type, ())
            missing = []
            for field in required_definition_fields:
                field_value = definition.get(field)
                if field_value is None or field_value == "":
                    missing.append(field)
                elif (
                    field == "from_rank_entity_ids"
                    and not list(field_value or [])
                ):
                    missing.append(field)
            if missing:
                raise ContinuityError(
                    "INVALID_POWER_SPEC",
                    "power specification definition is incomplete",
                    details={
                        "spec_type": spec_type,
                        "spec_entity_id": spec_entity_id,
                        "missing_fields": missing,
                    },
                )
            if spec_type == "progression_track":
                require_choice(
                    definition.get("track_kind"),
                    "definition.track_kind",
                    (
                        "ordered_rank",
                        "numeric_level",
                        "branch_tree",
                        "dag",
                        "state_machine",
                        "open_ended",
                        "none",
                    ),
                )
            if spec_type == "conversion_rule":
                validate_finite_number(
                    definition.get("ratio"),
                    "definition.ratio",
                    minimum=0,
                    allow_zero=False,
                )
        # Definition-plane changes are timeless only when accepted from an
        # authoritative setting/final artifact.  Outline/draft normalization
        # above continues to force them into planned scope.
        if artifact_stage in {"bootstrap", "final", "published"}:
            event["scope"] = "timeless"

    elif event_type == "progression":
        _require_entity(event, "actor_entity_id")
        _require_entity(event, "track_entity_id")
        action = require_choice(
            event.get("action") or "initialize",
            "action",
            (
                "initialize",
                "advance",
                "regress",
                "branch",
                "prestige",
                "reset",
            ),
        )
        event["action"] = action
        if event.get("from_rank_entity_id") is not None:
            event["from_rank_entity_id"] = str(
                event["from_rank_entity_id"]
            ).strip()
        event["to_rank_entity_id"] = _require_entity(
            event, "to_rank_entity_id"
        )
        if event.get("rank_edge_entity_id") is not None:
            event["rank_edge_entity_id"] = str(
                event["rank_edge_entity_id"]
            ).strip()
        event["state"] = dict(event.get("state") or {})

    elif event_type == "resource":
        _require_entity(event, "actor_entity_id")
        _require_entity(event, "resource_entity_id")
        action = require_choice(
            event.get("action") or "set",
            "action",
            (
                "initialize",
                "gain",
                "spend",
                "reserve",
                "release",
                "recover",
                "convert",
                "set",
            ),
        )
        amount = validate_finite_number(
            event.get("amount", 0),
            "amount",
            minimum=0,
            allow_zero=action in {"initialize", "set"},
        )
        event["action"] = action
        event["amount"] = amount
        if action == "convert":
            _require_entity(event, "target_resource_entity_id")
            _require_entity(event, "conversion_rule_entity_id")
            if event.get("target_amount") is not None:
                event["target_amount"] = validate_finite_number(
                    event["target_amount"],
                    "target_amount",
                    minimum=0,
                    allow_zero=False,
                )
        event["state"] = dict(event.get("state") or {})

    elif event_type == "status_effect":
        _require_entity(event, "actor_entity_id")
        _require_entity(event, "status_entity_id")
        action = require_choice(
            event.get("action") or "apply",
            "action",
            ("apply", "stack", "refresh", "remove", "expire"),
        )
        stacks = validate_positive_int(
            event.get("stacks", 1),
            "stacks",
            allow_none=False,
            minimum=1,
        )
        event["action"] = action
        event["stacks"] = stacks
        if event.get("source_entity_id") is not None:
            event["source_entity_id"] = str(
                event["source_entity_id"]
            ).strip()
        event["expires_coordinate"] = normalize_story_coordinate(
            event.get("expires_coordinate"), "expires_coordinate"
        )
        event["state"] = dict(event.get("state") or {})

    elif event_type == "power_binding":
        _require_entity(event, "actor_entity_id")
        _require_entity(event, "source_entity_id")
        binding_id = str(event.get("binding_id") or "").strip()
        if not binding_id:
            raise ContinuityError(
                "EVENT_FIELD_REQUIRED",
                "power_binding requires binding_id",
            )
        action = require_choice(
            event.get("action") or "bind",
            "action",
            (
                "bind",
                "unbind",
                "equip",
                "unequip",
                "contract",
                "summon",
                "dismiss",
                "suppress",
            ),
        )
        ability_ids = event.get("ability_entity_ids") or []
        if not isinstance(ability_ids, list) or not all(
            isinstance(item, str) and item.strip() for item in ability_ids
        ):
            raise ContinuityError(
                "INVALID_FIELD",
                "ability_entity_ids must be a list of entity ids",
            )
        event["binding_id"] = binding_id
        event["action"] = action
        event["ability_entity_ids"] = list(dict.fromkeys(ability_ids))
        event["unique"] = bool(event.get("unique", False))
        if event.get("slot_key") is not None:
            event["slot_key"] = str(event["slot_key"]).strip()
        event["state"] = dict(event.get("state") or {})

    elif event_type == "qualification":
        _require_entity(event, "actor_entity_id")
        _require_entity(event, "qualification_entity_id")
        action = require_choice(
            event.get("action") or "grant",
            "action",
            ("grant", "revoke", "consume", "expire"),
        )
        event["action"] = action
        event["quantity"] = validate_finite_number(
            event.get("quantity", 1),
            "quantity",
            minimum=0,
            allow_zero=False,
        )
        if event.get("source_entity_id") is not None:
            event["source_entity_id"] = str(
                event["source_entity_id"]
            ).strip()
        event["expires_coordinate"] = normalize_story_coordinate(
            event.get("expires_coordinate"), "expires_coordinate"
        )
        event["state"] = dict(event.get("state") or {})

    elif event_type == "power_observation":
        _require_entity(event, "observer_entity_id")
        if not event.get("subject_entity_id") and not event.get(
            "ability_entity_id"
        ):
            raise ContinuityError(
                "EVENT_ENTITY_REQUIRED",
                "power_observation requires subject_entity_id or ability_entity_id",
            )
        action = require_choice(
            event.get("action") or "observe",
            "action",
            ("observe", "infer", "confirm", "disprove"),
        )
        plane = require_choice(
            event.get("knowledge_plane") or "actor_belief",
            "knowledge_plane",
            KNOWLEDGE_PLANES,
        )
        confidence = validate_finite_number(
            event.get("confidence", 1.0),
            "confidence",
            minimum=0,
        )
        if confidence > 1:
            raise ContinuityError(
                "INVALID_FIELD", "confidence must be <= 1"
            )
        observed_fields = event.get("observed_fields") or {}
        if not isinstance(observed_fields, Mapping):
            raise ContinuityError(
                "INVALID_FIELD", "observed_fields must be an object"
            )
        event["action"] = action
        event["knowledge_plane"] = plane
        event["confidence"] = confidence
        event["observed_fields"] = dict(observed_fields)

    elif event_type in ADVANTAGE_EVENT_TYPES:
        _normalize_advantage_event(
            event,
            artifact_stage=artifact_stage,
        )

    elif event_type == "item_spec":
        _normalize_item_spec_event(event)
        if artifact_stage in {"bootstrap", "final", "published"}:
            event["scope"] = "timeless"

    elif event_type == "item_instance":
        _normalize_item_instance_event(event)

    elif event_type == "item_custody":
        _normalize_item_custody_event(event)

    elif event_type == "item_runtime":
        _normalize_item_runtime_event(event)

    elif event_type == "item_function_runtime":
        _normalize_item_function_runtime_event(event)

    elif event_type == "item_use":
        _normalize_item_use_event(event)

    elif event_type == "item_observation":
        _normalize_item_observation_event(event)

    elif event_type == "item_correction":
        action = require_choice(
            event.get("action") or "correct",
            "action",
            ("correct", "supersede", "retract"),
        )
        target_candidates: list[str] = []
        explicit_target = str(event.get("target_event_id") or "").strip()
        if explicit_target:
            target_candidates.append(explicit_target)
        for link_field in ("supersedes", "retracts"):
            raw_links = event.get(link_field)
            if isinstance(raw_links, str):
                if raw_links.strip():
                    target_candidates.append(raw_links.strip())
            elif isinstance(raw_links, list):
                target_candidates.extend(
                    value.strip()
                    for value in raw_links
                    if isinstance(value, str) and value.strip()
                )
        target_candidates = list(dict.fromkeys(target_candidates))
        if len(target_candidates) > 1:
            raise ContinuityError(
                "ITEM_CORRECTION_TARGET_AMBIGUOUS",
                "an item correction may target exactly one event",
                details={"target_event_ids": target_candidates},
            )
        target_event_id = target_candidates[0] if target_candidates else ""
        if not target_event_id:
            raise ContinuityError(
                "ITEM_CORRECTION_TARGET_REQUIRED",
                "item_correction requires target_event_id",
            )
        event["action"] = action
        event["target_event_id"] = target_event_id
        if action == "retract":
            event["retracts"] = [target_event_id]
            if event.get("replacement") is not None:
                raise ContinuityError(
                    "INVALID_FIELD",
                    "retract item_correction cannot contain replacement",
                )
        else:
            replacement = event.get("replacement")
            if not isinstance(replacement, Mapping):
                raise ContinuityError(
                    "CORRECTION_REPLACEMENT_REQUIRED",
                    "item_correction requires a replacement item event",
                )
            normalized_replacement = normalize_event(
                replacement,
                artifact_stage=artifact_stage,
                branch_id=event["branch_id"],
                chapter_no=event["chapter_no"],
                scene_index=event["scene_index"],
            )
            if normalized_replacement["event_type"] not in ITEM_EVENT_TYPES[:-1]:
                raise ContinuityError(
                    "ITEM_CORRECTION_REPLACEMENT_INVALID",
                    "item_correction replacement must be an item event",
                )
            event["replacement"] = normalized_replacement
            event["supersedes"] = [target_event_id]

    elif event_type == "belief":
        _require_entity(event, "believer_entity_id")
        proposition_key = str(event.get("proposition_key") or "").strip()
        if not proposition_key:
            raise ContinuityError(
                "BELIEF_PROPOSITION_REQUIRED",
                "belief requires proposition_key",
            )
        event["proposition_key"] = proposition_key
        if "value" not in event:
            raise ContinuityError(
                "EVENT_VALUE_REQUIRED", "belief requires value"
            )

    elif event_type == "open_loop":
        loop_id = str(event.get("loop_id") or "").strip()
        if not loop_id:
            loop_id = stable_hash(event, prefix="loop_")[:37]
        event["loop_id"] = loop_id
        status = normalize_text(str(event.get("status") or "open"))
        if status not in {
            "open",
            "escalated",
            "due",
            "fulfilled",
            "closed",
            "expired",
        }:
            raise ContinuityError(
                "INVALID_OPEN_LOOP_STATUS",
                f"unsupported open-loop status: {status}",
            )
        event["status"] = status
        event["loop_type"] = str(event.get("loop_type") or "promise")
        event["due_chapter"] = validate_positive_int(
            event.get("due_chapter"),
            "due_chapter",
            allow_none=True,
            minimum=1,
        )
        event["due_scene"] = validate_positive_int(
            event.get("due_scene"),
            "due_scene",
            allow_none=True,
            minimum=0,
        )

    elif event_type == "correction":
        replacement = event.get("replacement")
        if not isinstance(replacement, Mapping):
            raise ContinuityError(
                "CORRECTION_REPLACEMENT_REQUIRED",
                "correction requires a replacement event",
            )
        supersedes = event.get("supersedes")
        if not supersedes:
            raise ContinuityError(
                "CORRECTION_TARGET_REQUIRED",
                "correction requires supersedes event id(s)",
            )
        event["replacement"] = normalize_event(
            replacement,
            artifact_stage=artifact_stage,
            branch_id=event["branch_id"],
            chapter_no=event["chapter_no"],
            scene_index=event["scene_index"],
        )

    elif event_type == "retraction":
        retracts = event.get("retracts")
        if not retracts:
            raise ContinuityError(
                "RETRACTION_TARGET_REQUIRED",
                "retraction requires retracts event id(s)",
            )

    for link_field in ("supersedes", "retracts", "caused_by"):
        if link_field not in event:
            continue
        value = event[link_field]
        if isinstance(value, str):
            event[link_field] = [value]
        elif isinstance(value, list) and all(
            isinstance(item, str) and item for item in value
        ):
            event[link_field] = list(value)
        else:
            raise ContinuityError(
                "INVALID_EVENT_LINK",
                f"{link_field} must be an event id or list of event ids",
            )

    return event


def validate_proposal_metadata(
    *,
    artifact_stage: str,
    canon_status: str,
    branch_id: str,
    artifact_revision: int,
    chapter_no: int | None,
    scene_index: int | None,
    source_role: str,
) -> None:
    require_choice(artifact_stage, "artifact_stage", ARTIFACT_STAGES)
    require_choice(canon_status, "canon_status", CANON_STATUSES)
    require_choice(source_role, "source_role", SOURCE_ROLES)
    if not branch_id.strip():
        raise ContinuityError("INVALID_BRANCH", "branch_id cannot be empty")
    validate_positive_int(
        artifact_revision,
        "artifact_revision",
        allow_none=False,
        minimum=1,
    )
    validate_positive_int(
        chapter_no, "chapter_no", allow_none=True, minimum=1
    )
    validate_positive_int(
        scene_index, "scene_index", allow_none=True, minimum=0
    )


def changes_authority(artifact_stage: str, branch_id: str) -> bool:
    return branch_id == "main" and artifact_stage in {
        "bootstrap",
        "outline",
        "final",
        "published",
    }


def projection_is_provisional(artifact_stage: str, branch_id: str) -> bool:
    return branch_id != "main" or artifact_stage in {"brainstorm", "draft"}
