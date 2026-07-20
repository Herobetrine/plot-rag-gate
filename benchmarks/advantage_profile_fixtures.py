"""Executable fixture matrix for the sixteen Advantage profiles.

The JSON manifest in ``benchmarks/fixtures/advantage_profile_matrix.v1.json``
is intentionally declarative and readable.  This module turns each manifest
row into a deterministic ``plot-rag-advantage/v1`` event chain.  The chain is
kept outside the continuity service so the fixture can exercise the same
validator, pure reducer, projection, and replay code used by production
without making the service aware of test-only profile semantics.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from scripts.advantage_profiles import (
    ADVANTAGE_PROFILES,
    advantage_profile_registry,
)
from scripts.continuity.advantages import ADVANTAGE_SCHEMA_VERSION


FIXTURE_SCHEMA_VERSION = "plot-rag-advantage-profile-fixture-matrix/v1"
DEFAULT_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "advantage_profile_matrix.v1.json"
)
_ID_RE = re.compile(r"[^a-z0-9]+")


class AdvantageProfileFixtureError(ValueError):
    """Raised when the auditable profile fixture manifest is malformed."""


def _stable_id(value: str) -> str:
    folded = _ID_RE.sub("-", str(value).casefold()).strip("-")
    return folded or "fixture"


def _required_list(
    value: Any,
    *,
    field: str,
    profile: str,
) -> list[str]:
    if not isinstance(value, list) or not value:
        raise AdvantageProfileFixtureError(
            f"{profile}.{field} must be a non-empty string array"
        )
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise AdvantageProfileFixtureError(
            f"{profile}.{field} contains an empty/non-string value"
        )
    return [str(item).strip() for item in value]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AdvantageProfileFixtureError(
            f"cannot read Advantage fixture manifest: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise AdvantageProfileFixtureError("fixture manifest root must be an object")
    return payload


def validate_profile_fixture_manifest(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the static manifest against the independent profile registry."""

    if str(payload.get("schema_version") or "") != FIXTURE_SCHEMA_VERSION:
        raise AdvantageProfileFixtureError(
            "unsupported profile fixture manifest schema"
        )
    profiles = payload.get("profiles")
    if not isinstance(profiles, list):
        raise AdvantageProfileFixtureError("fixture manifest profiles must be an array")
    registry = advantage_profile_registry()
    by_name: dict[str, dict[str, Any]] = {}
    for raw in profiles:
        if not isinstance(raw, Mapping):
            raise AdvantageProfileFixtureError("fixture profile rows must be objects")
        profile = str(raw.get("profile") or "").strip()
        if profile in by_name:
            raise AdvantageProfileFixtureError(f"duplicate fixture profile: {profile}")
        if profile not in ADVANTAGE_PROFILES:
            raise AdvantageProfileFixtureError(f"unknown fixture profile: {profile}")
        spec = registry.get(profile)
        for field in (
            "anchor_types",
            "module_kinds",
            "runtime_dimensions",
            "ledger_entry_kinds",
            "knowledge_requirements",
            "contract_kinds",
        ):
            expected = list(getattr(spec, field))
            actual = _required_list(raw.get(field), field=field, profile=profile)
            if actual != expected:
                raise AdvantageProfileFixtureError(
                    f"{profile}.{field} does not match the registry snapshot"
                )
        narrative = raw.get("narrative_contract")
        if not isinstance(narrative, Mapping):
            raise AdvantageProfileFixtureError(
                f"{profile}.narrative_contract must be an object"
            )
        if dict(narrative) != spec.narrative_contract:
            raise AdvantageProfileFixtureError(
                f"{profile}.narrative_contract does not match the registry snapshot"
            )
        compatibility = raw.get("compatibility")
        if not isinstance(compatibility, Mapping):
            raise AdvantageProfileFixtureError(
                f"{profile}.compatibility must be an object"
            )
        if dict(compatibility) != spec.compatibility:
            raise AdvantageProfileFixtureError(
                f"{profile}.compatibility does not match the registry snapshot"
            )
        primary_anchor = str(raw.get("primary_anchor_type") or "").strip()
        if primary_anchor not in spec.anchor_types:
            raise AdvantageProfileFixtureError(
                f"{profile}.primary_anchor_type is not registered"
            )
        for field in ("primary_module_kind", "upgrade_module_kind"):
            module_kind = str(raw.get(field) or "").strip()
            if module_kind not in spec.module_kinds:
                raise AdvantageProfileFixtureError(
                    f"{profile}.{field} is not registered"
                )
        if raw.get("primary_module_kind") == raw.get("upgrade_module_kind"):
            raise AdvantageProfileFixtureError(
                f"{profile} primary and upgrade module kinds must differ"
            )
        for field in (
            "title",
            "failure_mode",
            "reader_experience",
            "upgrade_stage",
            "planned_capability",
            "counterplay",
        ):
            value = raw.get(field)
            if isinstance(value, str):
                if not value.strip():
                    raise AdvantageProfileFixtureError(
                        f"{profile}.{field} must be non-empty"
                    )
            elif not isinstance(value, list) or not value:
                raise AdvantageProfileFixtureError(
                    f"{profile}.{field} must be non-empty text/list"
                )
        by_name[profile] = copy.deepcopy(dict(raw))
    expected_profiles = set(ADVANTAGE_PROFILES)
    if set(by_name) != expected_profiles:
        raise AdvantageProfileFixtureError(
            "fixture manifest must contain exactly all sixteen profiles"
        )
    return {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "required_lifecycle": list(payload.get("required_lifecycle") or []),
        "profiles": [by_name[name] for name in sorted(by_name)],
    }


def load_profile_fixture_manifest(
    path: str | Path = DEFAULT_FIXTURE_PATH,
) -> dict[str, Any]:
    """Load and validate the human-readable fixture manifest."""

    return validate_profile_fixture_manifest(_load_json(Path(path)))


def profile_fixture_cases(
    path: str | Path = DEFAULT_FIXTURE_PATH,
) -> tuple[dict[str, Any], ...]:
    """Return cases in stable profile order."""

    payload = load_profile_fixture_manifest(path)
    return tuple(copy.deepcopy(row) for row in payload["profiles"])


def _case_ids(case: Mapping[str, Any]) -> dict[str, str]:
    slug = _stable_id(str(case["profile"]))
    return {
        "slug": slug,
        "advantage_id": f"fixture-advantage-{slug}",
        "owner_id": f"fixture-owner-{slug}",
        "other_owner_id": f"fixture-other-owner-{slug}",
        "counterparty_id": f"fixture-counterparty-{slug}",
        "calendar_id": f"fixture-calendar-{slug}",
        "target_id": f"fixture-target-{slug}",
        "narrative_id": f"fixture-narrative-{slug}",
        "contract_id": f"fixture-contract-{slug}",
    }


def _experience_contract(case: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "entry_emotion": str(case["reader_experience"]),
        "pressure": "收益、限制与失败代价同时可见",
        "peak": f"{case['title']}首次使用暴露能力边界",
        "aftertaste": "读者记住收益，但保留下一次使用的不安",
        "benefit_cost_ratio": "收益与代价并置",
    }


def _dimension_value(
    dimension: str,
    *,
    case: Mapping[str, Any],
    ids: Mapping[str, str],
) -> Any:
    """Return a typed, deterministic value for a registry runtime dimension."""

    slug = ids["slug"]
    lower = dimension.casefold()
    if lower in {"enabled"}:
        return True
    if lower in {
        "access_list",
        "observer_set",
        "retained_fields",
        "validated_fragments",
        "unlocked_modules",
        "visible_fields",
    }:
        return [f"{slug}:{dimension}:fixture"]
    if lower in {
        "exposure",
        "pollution",
        "debt",
        "backlash",
        "conversion_rate",
        "confidence",
        "purity",
        "leakage",
        "integrity",
        "quality",
    }:
        return 0.25
    if lower in {
        "stage",
        "awakening_stage",
        "inheritance_stage",
        "awake_state",
        "active_recipe",
        "task_state",
        "claim_state",
        "error_state",
        "contract_status",
        "pool_version",
        "catalog_version",
        "last_claim_time",
        "checkpoint_id",
        "branch_id",
    } or lower.endswith("_stage"):
        return f"{slug}:{dimension}:initial"
    if lower in {
        "time_ratio",
        "currency_balance",
        "balance",
        "capacity",
        "occupancy",
        "copy_slots",
        "copy_uses",
        "active_branches",
        "simulation_budget",
        "branch_depth",
        "uses",
        "pity_count",
        "streak",
        "progress",
        "failure_count",
        "permission_level",
        "distance",
        "loyalty",
        "shared_cost",
        "agency",
        "memory_access",
        "stock",
    } or lower.endswith("_count") or lower.endswith("_uses"):
        return 1
    return f"{slug}:{dimension}:fixture"


def _base_event(
    case: Mapping[str, Any],
    *,
    event_no: int,
    event_type: str,
    branch_id: str,
    calendar_id: str,
    experience: Mapping[str, Any],
    **fields: Any,
) -> dict[str, Any]:
    ids = _case_ids(case)
    coordinate = {
        "calendar_id": calendar_id,
        "ordinal": int(event_no),
    }
    event: dict[str, Any] = {
        "schema_version": ADVANTAGE_SCHEMA_VERSION,
        "event_type": event_type,
        "event_id": f"{ids['advantage_id']}-event-{event_no:03d}",
        "scope": "current",
        "branch_id": branch_id,
        "chapter_no": 1,
        "scene_index": int(event_no) - 1,
        "story_time": f"fixture-day-1-{event_no:03d}",
        "story_coordinate": coordinate,
        "evidence": {
            "quote": f"{case['title']} fixture event {event_no}: {event_type}"
        },
        "knowledge_plane": "objective",
        "confidence": 1.0,
        "source_claim_ids": [f"fixture.{case['profile']}"],
        "advantage_id": ids["advantage_id"],
        "experience_contract_id": f"experience-{ids['slug']}",
        "experience_contract": copy.deepcopy(dict(experience)),
        "causal_provenance": {
            "fixture_profile": case["profile"],
            "fixture_event_no": int(event_no),
        },
    }
    event.update(fields)
    return event


def _module_id(case: Mapping[str, Any], module_kind: str) -> str:
    return (
        f"{_case_ids(case)['advantage_id']}-module-{_stable_id(module_kind)}"
    )


def _anchor_id(case: Mapping[str, Any], anchor_type: str) -> str:
    return (
        f"{_case_ids(case)['advantage_id']}-anchor-{_stable_id(anchor_type)}"
    )


def _runtime_metadata(
    case: Mapping[str, Any],
    *,
    ids: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "fixture_profile": case["profile"],
        "fixture_phase": "activated",
        "dimensions": {
            str(dimension): _dimension_value(
                str(dimension),
                case=case,
                ids=ids,
            )
            for dimension in case["runtime_dimensions"]
        },
    }


def build_profile_event_chain(
    case: Mapping[str, Any],
    *,
    include_dimension_ledger: bool = True,
    include_branch_probe: bool = True,
) -> list[dict[str, Any]]:
    """Build one complete, independent lifecycle for a profile case.

    The raw events intentionally contain no reducer-computed before/after
    snapshots.  Call :func:`normalize_profile_events` before validation.
    """

    ids = _case_ids(case)
    profile = str(case["profile"])
    experience = _experience_contract(case)
    calendar_id = ids["calendar_id"]
    events: list[dict[str, Any]] = []
    event_no = 0

    def add(event_type: str, **fields: Any) -> dict[str, Any]:
        nonlocal event_no
        event_no += 1
        event = _base_event(
            case,
            event_no=event_no,
            event_type=event_type,
            branch_id="main",
            calendar_id=calendar_id,
            experience=experience,
            **fields,
        )
        events.append(event)
        return event

    add(
        "advantage_spec",
        action="define",
        spec_type="advantage_definition",
        title=case["title"],
        profiles=[profile],
        anchor_type=case["primary_anchor_type"],
        acquisition_mode="fixture_acquisition_then_binding",
        uniqueness="fixture_unique",
        status="canon",
        promise={
            "profile": profile,
            "reader_experience": case["reader_experience"],
        },
        counterplay=[case["counterplay"]],
        definition={
            "initial_stage": "dormant",
            "initial_charges": 4,
            "max_charges": 4,
            "initial_resources": {
                f"{ids['slug']}_resource": 3,
                "upgrade_material": 1,
            },
        },
    )

    for anchor_type in case["anchor_types"]:
        anchor_fields: dict[str, Any] = {
            "action": "define",
            "anchor_id": _anchor_id(case, anchor_type),
            "anchor_type": anchor_type,
            "anchor_ref_id": f"{ids['slug']}-anchor-ref-{_stable_id(anchor_type)}",
            "binding_state": (
                "unbound"
                if anchor_type == case["primary_anchor_type"]
                else "dormant"
            ),
            "authority_status": "canon",
            "transfer_rule": {
                "mode": "fixture-controlled",
                "profile": profile,
            },
            "attributes": {
                "fixture_anchor_type": anchor_type,
                "primary": anchor_type == case["primary_anchor_type"],
            },
        }
        if anchor_type == case["primary_anchor_type"]:
            anchor_fields["owner_entity_id"] = ids["owner_id"]
        add("advantage_anchor", **anchor_fields)

    planned_kind = next(
        kind
        for kind in reversed(case["module_kinds"])
        if kind
        not in {
            case["primary_module_kind"],
            case["upgrade_module_kind"],
        }
    )
    for module_kind in case["module_kinds"]:
        authority_status = "planned" if module_kind == planned_kind else "canon"
        if module_kind == case["primary_module_kind"]:
            module_status = "available"
        elif module_kind == planned_kind:
            module_status = "locked"
        else:
            module_status = "locked"
        add(
            "advantage_module",
            action="define",
            module_id=_module_id(case, module_kind),
            title=f"{case['title']}·{module_kind}",
            kind=module_kind,
            authority_status=authority_status,
            module_status=module_status,
            stage="initial",
            profile=profile,
            trigger={"cooldown": 0},
            preconditions=[
                "bound_owner",
                f"runtime_dimension:{case['runtime_dimensions'][0]}",
            ],
            targets=[{"kind": "fixture_target", "id": ids["target_id"]}],
            costs=[
                {"kind": "charges", "amount": 1},
                {"resource": f"{ids['slug']}_resource", "amount": 1},
            ],
            effects=[
                {
                    "kind": "profile_effect",
                    "profile": profile,
                    "module_kind": module_kind,
                }
            ],
            side_effects=[{"kind": "exposure", "amount": 0.1}],
            failure_modes=[case["failure_mode"]],
            counters=[case["counterplay"]],
            anchor_ids=[
                _anchor_id(case, str(case["primary_anchor_type"]))
            ],
        )

    add(
        "advantage_spec",
        action="define",
        spec_type="runtime_slot",
        slot_id=f"{ids['advantage_id']}-slot-primary",
        module_id=_module_id(case, str(case["primary_module_kind"])),
        stage="initial",
        capacity=2,
        slot_status="available",
        unlock_graph={"upgrade_stage": case["upgrade_stage"]},
        set_membership=[profile],
    )
    narrative = copy.deepcopy(dict(case["narrative_contract"]))
    add(
        "advantage_spec",
        action="define",
        spec_type="narrative_contract",
        narrative_contract_id=ids["narrative_id"],
        contract_status="active",
        reading_promise=narrative["reading_promise"],
        reward_loop=narrative["reward_loop"],
        risk_loop=narrative["risk_loop"],
        reveal_ladder=narrative["reveal_ladder"],
        experience_binding=experience,
    )

    # "Acquire" is represented as an auditable reward ledger entry because
    # Advantage v1 has no separate acquisition event family.
    add(
        "advantage_reward",
        action="record",
        record_only=True,
        ledger_entry_kind="acquire",
        entry_id=f"{ids['advantage_id']}-ledger-acquire",
        input={"source": "fixture_acquisition"},
        output={"anchor_type": case["primary_anchor_type"]},
        loss={},
        module_id=_module_id(case, str(case["primary_module_kind"])),
    )
    add(
        "advantage_bind",
        action="bind",
        anchor_id=_anchor_id(case, str(case["primary_anchor_type"])),
        owner_entity_id=ids["owner_id"],
    )
    add(
        "advantage_activate",
        action="activate",
        owner_entity_id=ids["owner_id"],
        stage="activated",
        charges=4,
        max_charges=4,
        resources={
            f"{ids['slug']}_resource": 3,
            "upgrade_material": 1,
        },
        pollution=0,
        exposure=0,
        debt=0,
        runtime_metadata=_runtime_metadata(case, ids=ids),
    )
    add(
        "advantage_module",
        action="enable",
        module_id=_module_id(case, str(case["primary_module_kind"])),
    )
    add(
        "advantage_contract",
        action="define",
        contract_id=ids["contract_id"],
        contract_kind=str(case["contract_kinds"][0]),
        actor_entity_id=ids["owner_id"],
        counterparty_entity_id=ids["counterparty_id"],
        contract_status="active",
        status="canon",
        terms=[
            {
                "kind": str(kind),
                "rule": f"fixture rule for {kind}",
            }
            for kind in case["contract_kinds"]
        ],
        agency={
            str(kind): "constrained"
            for kind in case["contract_kinds"]
        },
        trust_delta=0.2,
        debt_delta=0.1,
        breach_effect={
            "kind": "fixture_breach",
            "counterplay": case["counterplay"],
        },
    )
    first_use_event = add(
        "advantage_use",
        action="use",
        module_id=_module_id(case, str(case["primary_module_kind"])),
        entry_id=f"{ids['advantage_id']}-ledger-first-use",
        actor=ids["owner_id"],
        target=ids["target_id"],
        target_kind="fixture_target",
        target_filter={"profile": profile},
        preconditions_satisfied=True,
        costs=[
            {"kind": "charges", "amount": 1},
            {"resource": f"{ids['slug']}_resource", "amount": 1},
        ],
        rewards=[{"resource": "fixture_insight", "amount": 1}],
        effects=[
            {
                "kind": "first_use",
                "profile": profile,
                "module_kind": case["primary_module_kind"],
            }
        ],
        side_effects=[{"kind": "exposure_marker", "amount": 0.25}],
        exposure_delta=0.25,
    )
    add(
        "advantage_cost",
        action="record_failure_cost",
        module_id=_module_id(case, str(case["primary_module_kind"])),
        entry_id=f"{ids['advantage_id']}-ledger-failure-cost",
        actor=ids["owner_id"],
        target=ids["target_id"],
        target_kind="fixture_target",
        preconditions_satisfied=False,
        costs=[
            {"kind": "charges", "amount": 1},
            {"resource": f"{ids['slug']}_resource", "amount": 1},
        ],
        effects=[
            {
                "kind": "failed_attempt",
                "failure_mode": case["failure_mode"],
            }
        ],
        side_effects=[
            {"kind": "failure_cost", "description": case["failure_mode"]}
        ],
        pollution_delta=0.1,
        exposure_delta=0.1,
        debt_delta=0.1,
        causal_event_id=str(first_use_event["event_id"]),
    )
    add(
        "advantage_upgrade",
        to_stage=case["upgrade_stage"],
        stage=case["upgrade_stage"],
        unlock_modules=[
            _module_id(case, str(case["upgrade_module_kind"]))
        ],
        max_charges=5,
        entry_id=f"{ids['advantage_id']}-ledger-upgrade",
    )

    if include_branch_probe and bool(
        case["compatibility"].get("branch_isolation")
    ):
        # Branch-capable profiles get a second runtime and one isolated use.
        # The main branch remains the generation authority.
        branch_id = "fixture-branch"
        event_no += 1
        branch_activate = _base_event(
            case,
            event_no=event_no,
            event_type="advantage_activate",
            branch_id=branch_id,
            calendar_id=calendar_id,
            experience=experience,
            action="activate",
            owner_entity_id=ids["owner_id"],
            stage="branch_probe",
            charges=1,
            max_charges=2,
            resources={f"{ids['slug']}_resource": 1},
            runtime_metadata={
                "fixture_profile": profile,
                "branch_probe": True,
            },
        )
        events.append(branch_activate)
        event_no += 1
        events.append(
            _base_event(
                case,
                event_no=event_no,
                event_type="advantage_use",
                branch_id=branch_id,
                calendar_id=calendar_id,
                experience=experience,
                action="use",
                module_id=_module_id(
                    case, str(case["primary_module_kind"])
                ),
                entry_id=f"{ids['advantage_id']}-ledger-branch-use",
                actor=ids["owner_id"],
                target=ids["target_id"],
                target_kind="fixture_branch_target",
                preconditions_satisfied=True,
                effects=[{"kind": "branch_probe", "branch_id": branch_id}],
                exposure_delta=0.05,
            )
        )

    objective_id = f"{ids['advantage_id']}-knowledge-objective"
    requirements = {
        str(requirement): f"verified:{requirement}"
        for requirement in case["knowledge_requirements"]
    }
    event_no += 1
    events.append(
        _base_event(
            case,
            event_no=event_no,
            event_type="advantage_reveal",
            branch_id="main",
            calendar_id=calendar_id,
            experience=experience,
            knowledge_id=objective_id,
            knowledge_plane="objective",
            status="canon",
            claim={
                "text": f"{case['title']}的客观机制已被记录",
                "requirements": requirements,
            },
            reveal_stage="author_known",
            record_ledger=True,
        )
    )
    event_no += 1
    events.append(
        _base_event(
            case,
            event_no=event_no,
            event_type="advantage_reveal",
            branch_id="main",
            calendar_id=calendar_id,
            experience=experience,
            knowledge_id=f"{ids['advantage_id']}-knowledge-owner",
            knowledge_plane="actor_belief",
            observer_entity_id=ids["owner_id"],
            status="canon",
            confidence=0.8,
            claim={
                "text": f"{case['title']}对持有者的可用解释",
                "known_requirements": list(case["knowledge_requirements"]),
            },
            reveal_stage="first_use",
            record_ledger=True,
        )
    )
    event_no += 1
    events.append(
        _base_event(
            case,
            event_no=event_no,
            event_type="advantage_reveal",
            branch_id="main",
            calendar_id=calendar_id,
            experience=experience,
            knowledge_id=f"{ids['advantage_id']}-knowledge-other",
            knowledge_plane="actor_belief",
            observer_entity_id=ids["other_owner_id"],
            status="canon",
            confidence=0.4,
            claim={"text": "旁观者只能得到不完整解释"},
            reveal_stage="first_use",
            record_ledger=True,
        )
    )
    event_no += 1
    events.append(
        _base_event(
            case,
            event_no=event_no,
            event_type="advantage_reveal",
            branch_id="main",
            calendar_id=calendar_id,
            experience=experience,
            knowledge_id=f"{ids['advantage_id']}-knowledge-misread",
            knowledge_plane="actor_belief",
            observer_entity_id=ids["owner_id"],
            status="misread",
            confidence=0.3,
            claim={"text": "持有者暂时误判了失败代价"},
            reveal_stage="first_use",
            misread_of=objective_id,
            record_ledger=False,
        )
    )
    event_no += 1
    events.append(
        _base_event(
            case,
            event_no=event_no,
            event_type="advantage_reveal",
            branch_id="main",
            calendar_id=calendar_id,
            experience=experience,
            knowledge_id=f"{ids['advantage_id']}-knowledge-public",
            knowledge_plane="public_narrative",
            status="rumor",
            confidence=0.5,
            claim={"text": f"坊间流传{case['title']}能带来捷径"},
            reveal_stage="public",
            record_ledger=False,
        )
    )
    event_no += 1
    events.append(
        _base_event(
            case,
            event_no=event_no,
            event_type="advantage_reveal",
            branch_id="main",
            calendar_id=calendar_id,
            experience=experience,
            knowledge_id=f"{ids['advantage_id']}-knowledge-reader",
            knowledge_plane="reader_disclosed",
            status="canon",
            confidence=1.0,
            claim={"text": f"读者已看到{case['title']}的收益与代价"},
            reveal_stage="reader_disclosed",
            record_ledger=True,
        )
    )
    event_no += 1
    events.append(
        _base_event(
            case,
            event_no=event_no,
            event_type="advantage_reveal",
            branch_id="main",
            calendar_id=calendar_id,
            experience=experience,
            knowledge_id=f"{ids['advantage_id']}-knowledge-plan",
            knowledge_plane="author_plan",
            status="planned",
            confidence=1.0,
            claim={"text": str(case["planned_capability"])},
            reveal_stage="future",
            record_ledger=False,
        )
    )

    if include_dimension_ledger:
        for index, entry_kind in enumerate(case["ledger_entry_kinds"], start=1):
            add(
                "advantage_reward",
                action="record_dimension",
                record_only=True,
                ledger_entry_kind=str(entry_kind),
                entry_id=(
                    f"{ids['advantage_id']}-ledger-dimension-{index:02d}"
                ),
                input={"fixture_phase": "dimension_coverage"},
                output={
                    "profile": profile,
                    "entry_kind": str(entry_kind),
                },
                loss={"failure_mode": case["failure_mode"]},
                module_id=_module_id(
                    case, str(case["primary_module_kind"])
                ),
            )
    return events


def normalize_profile_events(
    events: Iterable[Mapping[str, Any]],
    *,
    artifact_stage: str = "final",
    branch_id: str = "main",
) -> list[dict[str, Any]]:
    """Normalize raw fixture events through the production validator."""

    from scripts.continuity.validators import normalize_event

    normalized: list[dict[str, Any]] = []
    for index, event in enumerate(events, start=1):
        raw = dict(event)
        normalized.append(
            normalize_event(
                raw,
                artifact_stage=artifact_stage,
                branch_id=str(raw.get("branch_id") or branch_id),
                chapter_no=int(raw.get("chapter_no") or 1),
                scene_index=int(raw.get("scene_index") or index - 1),
            )
        )
    return normalized


def event_phase_index(events: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Return auditable lifecycle markers for assertions and reports."""

    result: dict[str, str] = {}
    for event in events:
        event_type = str(event.get("event_type") or "")
        event_id = str(event.get("event_id") or "")
        action = str(event.get("action") or "")
        if event_type == "advantage_reward" and event.get("ledger_entry_kind") == "acquire":
            result["acquire"] = event_id
        elif event_type == "advantage_bind" and action == "bind":
            result["bind"] = event_id
        elif event_type == "advantage_activate" and action == "activate":
            result.setdefault("activate", event_id)
        elif event_type == "advantage_use" and event.get("entry_id", "").endswith(
            "first-use"
        ):
            result["first_use"] = event_id
        elif event_type == "advantage_cost" and event.get("entry_id", "").endswith(
            "failure-cost"
        ):
            result["failure_cost"] = event_id
        elif event_type == "advantage_upgrade":
            result["upgrade"] = event_id
        if float(event.get("exposure_delta") or 0) > 0:
            result.setdefault("exposure", event_id)
        if event_type == "advantage_reveal":
            result.setdefault("reveal", event_id)
    result["replay"] = "projection_hash"
    return result


__all__ = [
    "ADVANTAGE_SCHEMA_VERSION",
    "DEFAULT_FIXTURE_PATH",
    "FIXTURE_SCHEMA_VERSION",
    "AdvantageProfileFixtureError",
    "build_profile_event_chain",
    "event_phase_index",
    "load_profile_fixture_manifest",
    "normalize_profile_events",
    "profile_fixture_cases",
    "validate_profile_fixture_manifest",
]
