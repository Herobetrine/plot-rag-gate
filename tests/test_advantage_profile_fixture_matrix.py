from __future__ import annotations

import copy
import json
import sqlite3
import unittest
from pathlib import Path
from typing import Any, Mapping

from benchmarks.advantage_profile_fixtures import (
    DEFAULT_FIXTURE_PATH,
    build_profile_event_chain,
    event_phase_index,
    load_profile_fixture_manifest,
    normalize_profile_events,
    profile_fixture_cases,
)
from scripts.advantage_profiles import (
    ADVANTAGE_PROFILES,
    advantage_profile_registry,
)
from scripts.continuity.advantages import (
    ADVANTAGE_SCHEMA_VERSION,
    AdvantageProjectionState,
    compute_advantage_projection_hash,
    ensure_advantage_schema,
    query_advantage_anchors,
    query_advantage_context,
    query_advantage_contracts,
    query_advantage_exposure,
    query_advantage_knowledge,
    query_advantage_ledger,
    query_advantage_modules,
    query_advantage_progression,
    query_advantage_runtime,
    rebuild_advantage_projection,
    refresh_advantage_projection_metadata,
    validate_advantage_event_sequence,
)
from scripts.continuity.validators import ContinuityError


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    ensure_advantage_schema(connection)
    return connection


def _event_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "event_id": str(event["event_id"]),
            "event_type": str(event["event_type"]),
            "payload_json": json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
            ),
            "changes_authority": 1,
            "updated_order": index,
        }
        for index, event in enumerate(events, start=1)
    ]


def _ids(case: Mapping[str, Any]) -> dict[str, str]:
    slug = str(case["profile"]).replace("_", "-")
    return {
        "advantage_id": f"fixture-advantage-{slug}",
        "owner_id": f"fixture-owner-{slug}",
        "other_owner_id": f"fixture-other-owner-{slug}",
    }


class AdvantageProfileFixtureMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_profile_fixture_manifest(DEFAULT_FIXTURE_PATH)
        cls.cases = profile_fixture_cases(DEFAULT_FIXTURE_PATH)
        cls.registry = advantage_profile_registry()

    def test_manifest_is_readable_and_covers_registry_dimensions(self) -> None:
        self.assertEqual(
            set(ADVANTAGE_PROFILES),
            {case["profile"] for case in self.cases},
        )
        self.assertEqual(
            [
                "acquire",
                "bind",
                "activate",
                "first_use",
                "failure_cost",
                "upgrade",
                "exposure",
                "reveal",
                "replay",
            ],
            self.manifest["required_lifecycle"],
        )
        self.assertTrue(Path(DEFAULT_FIXTURE_PATH).is_file())
        for case in self.cases:
            spec = self.registry.get(case["profile"])
            with self.subTest(profile=case["profile"]):
                for field in (
                    "anchor_types",
                    "module_kinds",
                    "runtime_dimensions",
                    "ledger_entry_kinds",
                    "knowledge_requirements",
                    "contract_kinds",
                ):
                    self.assertEqual(
                        list(getattr(spec, field)),
                        case[field],
                    )
                self.assertEqual(
                    spec.narrative_contract,
                    case["narrative_contract"],
                )
                self.assertEqual(spec.compatibility, case["compatibility"])
                self.assertIn(
                    case["primary_anchor_type"],
                    case["anchor_types"],
                )
                self.assertIn(
                    case["primary_module_kind"],
                    case["module_kinds"],
                )
                self.assertIn(
                    case["upgrade_module_kind"],
                    case["module_kinds"],
                )

    def test_each_profile_executes_full_lifecycle_and_replays_stably(self) -> None:
        for case in self.cases:
            with self.subTest(profile=case["profile"]):
                raw_events = build_profile_event_chain(case)
                events = normalize_profile_events(raw_events)
                self.assertTrue(events)
                self.assertTrue(
                    all(
                        event["schema_version"] == ADVANTAGE_SCHEMA_VERSION
                        for event in events
                    )
                )
                phases = event_phase_index(events)
                self.assertEqual(
                    set(self.manifest["required_lifecycle"]),
                    set(phases),
                )

                connection = _connection()
                try:
                    validation = validate_advantage_event_sequence(
                        connection,
                        events,
                    )
                    self.assertEqual("passed", validation["status"])
                    self.assertEqual(len(events), validation["event_count"])

                    state = AdvantageProjectionState()
                    state.apply_sequence(events)
                    state.persist(connection)
                    first_hash = refresh_advantage_projection_metadata(connection)
                    self.assertRegex(
                        first_hash,
                        r"^advantage_projection_[0-9a-f]{64}$",
                    )
                    self.assertEqual(
                        first_hash,
                        compute_advantage_projection_hash(connection),
                    )

                    ids = _ids(case)
                    context = query_advantage_context(
                        connection,
                        ids["advantage_id"],
                        branch_id="main",
                        observer_entity_id=ids["owner_id"],
                        visibility="inspection",
                        ledger_limit=200,
                    )
                    self.assertEqual(
                        ids["advantage_id"],
                        context["definition"]["advantage_id"],
                    )
                    self.assertEqual(
                        [case["profile"]],
                        context["definition"]["profiles_json"],
                    )
                    self.assertEqual(
                        set(case["anchor_types"]),
                        {
                            row["anchor_type"]
                            for row in query_advantage_anchors(
                                connection,
                                ids["advantage_id"],
                            )
                        },
                    )

                    modules = query_advantage_modules(
                        connection,
                        ids["advantage_id"],
                        include_noncanon=True,
                    )
                    self.assertEqual(
                        set(case["module_kinds"]),
                        {row["module_kind"] for row in modules},
                    )
                    self.assertIn(
                        "planned",
                        {row["authority_status"] for row in modules},
                    )
                    primary_module_id = next(
                        row["module_id"]
                        for row in modules
                        if row["module_kind"] == case["primary_module_kind"]
                    )
                    upgrade_module_id = next(
                        row["module_id"]
                        for row in modules
                        if row["module_kind"] == case["upgrade_module_kind"]
                    )
                    self.assertIn(
                        primary_module_id,
                        context["progression"]["unlocked_modules"],
                    )
                    self.assertIn(
                        upgrade_module_id,
                        context["progression"]["unlocked_modules"],
                    )

                    runtime = query_advantage_runtime(
                        connection,
                        ids["advantage_id"],
                        branch_id="main",
                    )
                    self.assertTrue(runtime["enabled"])
                    self.assertEqual(case["upgrade_stage"], runtime["stage"])
                    self.assertGreaterEqual(runtime["exposure"], 0.35)
                    self.assertGreaterEqual(runtime["pollution"], 0.1)
                    self.assertGreaterEqual(runtime["debt"], 0.2)
                    metadata = runtime["runtime_json"]["dimensions"]
                    self.assertEqual(
                        set(case["runtime_dimensions"]),
                        set(metadata),
                    )

                    ledger = query_advantage_ledger(
                        connection,
                        ids["advantage_id"],
                        branch_id="main",
                        limit=200,
                        visibility="inspection",
                    )
                    ledger_kinds = {row["entry_kind"] for row in ledger}
                    self.assertTrue(
                        set(case["ledger_entry_kinds"]).issubset(ledger_kinds)
                    )
                    self.assertIn("bind", ledger_kinds)
                    self.assertIn("activate", ledger_kinds)
                    self.assertIn("use", ledger_kinds)
                    self.assertIn("cost", ledger_kinds)
                    self.assertIn("upgrade", ledger_kinds)
                    self.assertIn("acquire", ledger_kinds)
                    first_use = next(
                        row
                        for row in ledger
                        if row["entry_id"].endswith("ledger-first-use")
                    )
                    self.assertTrue(
                        any(
                            value.get("kind") == "first_use"
                            for value in first_use["output_json"]["effects"]
                        )
                    )
                    failure_cost = next(
                        row
                        for row in ledger
                        if row["entry_id"].endswith("ledger-failure-cost")
                    )
                    self.assertTrue(failure_cost["loss_json"]["side_effects"])

                    contracts = query_advantage_contracts(
                        connection,
                        ids["advantage_id"],
                        generation_visible_only=False,
                    )
                    self.assertEqual(1, len(contracts))
                    self.assertEqual(
                        case["contract_kinds"][0],
                        contracts[0]["terms_json"][0]["kind"],
                    )
                    self.assertGreaterEqual(contracts[0]["debt"], 0.1)
                    exposure = query_advantage_exposure(
                        connection,
                        ids["advantage_id"],
                        branch_id="main",
                        generation_visible_only=False,
                    )
                    self.assertGreaterEqual(exposure["exposure"], 0.35)

                    knowledge = query_advantage_knowledge(
                        connection,
                        ids["advantage_id"],
                        visibility="inspection",
                        include_noncanon=True,
                    )
                    self.assertEqual(
                        {
                            "objective",
                            "actor_belief",
                            "public_narrative",
                            "reader_disclosed",
                            "author_plan",
                        },
                        {row["knowledge_plane"] for row in knowledge},
                    )
                    self.assertIn(
                        "planned",
                        {row["knowledge_status"] for row in knowledge},
                    )
                    self.assertIn(
                        "misread",
                        {row["knowledge_status"] for row in knowledge},
                    )
                    self.assertIn(
                        "rumor",
                        {row["knowledge_status"] for row in knowledge},
                    )

                    # Rebuild through the production projection path twice.
                    rows = _event_rows(events)
                    first_replay = rebuild_advantage_projection(
                        connection,
                        rows,
                        set(),
                        record_run=False,
                    )
                    second_replay = rebuild_advantage_projection(
                        connection,
                        rows,
                        set(),
                        record_run=False,
                    )
                    self.assertEqual(
                        first_replay["advantage_projection_hash"],
                        second_replay["advantage_projection_hash"],
                    )
                    self.assertEqual(
                        second_replay["advantage_projection_hash"],
                        compute_advantage_projection_hash(connection),
                    )
                finally:
                    connection.close()

    def test_generation_privacy_keeps_owner_belief_and_hides_plans(self) -> None:
        for case in self.cases:
            with self.subTest(profile=case["profile"]):
                events = normalize_profile_events(
                    build_profile_event_chain(case)
                )
                connection = _connection()
                try:
                    state = AdvantageProjectionState()
                    state.apply_sequence(events)
                    state.persist(connection)
                    ids = _ids(case)
                    owner_visible = query_advantage_knowledge(
                        connection,
                        ids["advantage_id"],
                        visibility="generation",
                        observer_entity_id=ids["owner_id"],
                        visible_reveal_stages=[
                            "initial",
                            "current",
                            "first_use",
                            "public",
                            "reader_disclosed",
                            "revealed",
                        ],
                    )
                    owner_planes = {
                        row["knowledge_plane"] for row in owner_visible
                    }
                    self.assertIn("actor_belief", owner_planes)
                    self.assertNotIn("objective", owner_planes)
                    self.assertNotIn("author_plan", owner_planes)
                    self.assertNotIn(
                        "misread",
                        {row["knowledge_status"] for row in owner_visible},
                    )
                    anonymous = query_advantage_knowledge(
                        connection,
                        ids["advantage_id"],
                        visibility="generation",
                        visible_reveal_stages=[
                            "initial",
                            "current",
                            "first_use",
                            "public",
                            "reader_disclosed",
                            "revealed",
                        ],
                    )
                    self.assertNotIn(
                        "actor_belief",
                        {row["knowledge_plane"] for row in anonymous},
                    )
                    self.assertNotIn(
                        "author_plan",
                        {row["knowledge_plane"] for row in anonymous},
                    )
                    inspection = query_advantage_knowledge(
                        connection,
                        ids["advantage_id"],
                        visibility="inspection",
                        include_noncanon=True,
                    )
                    self.assertIn(
                        "author_plan",
                        {row["knowledge_plane"] for row in inspection},
                    )
                finally:
                    connection.close()

    def test_branch_capable_profiles_keep_branch_runtime_isolated(self) -> None:
        for case in self.cases:
            if not case["compatibility"].get("branch_isolation"):
                continue
            with self.subTest(profile=case["profile"]):
                events = normalize_profile_events(
                    build_profile_event_chain(case)
                )
                connection = _connection()
                try:
                    state = AdvantageProjectionState()
                    state.apply_sequence(events)
                    state.persist(connection)
                    ids = _ids(case)
                    main = query_advantage_runtime(
                        connection,
                        ids["advantage_id"],
                        branch_id="main",
                    )
                    branch = query_advantage_runtime(
                        connection,
                        ids["advantage_id"],
                        branch_id="fixture-branch",
                    )
                    self.assertIsNotNone(main)
                    self.assertIsNotNone(branch)
                    self.assertNotEqual(main["stage"], branch["stage"])
                    self.assertEqual("branch_probe", branch["stage"])
                    main_ledger = query_advantage_ledger(
                        connection,
                        ids["advantage_id"],
                        branch_id="main",
                        limit=200,
                        visibility="inspection",
                    )
                    branch_ledger = query_advantage_ledger(
                        connection,
                        ids["advantage_id"],
                        branch_id="fixture-branch",
                        limit=200,
                        visibility="inspection",
                    )
                    self.assertNotIn(
                        "fixture-branch",
                        json.dumps(main_ledger, ensure_ascii=False),
                    )
                    self.assertIn(
                        "fixture-branch",
                        json.dumps(branch_ledger, ensure_ascii=False),
                    )
                finally:
                    connection.close()

    def test_mutated_first_use_is_rejected_by_advantage_validator(self) -> None:
        case = next(
            case
            for case in self.cases
            if case["profile"] == "pocket_domain"
        )
        events = normalize_profile_events(build_profile_event_chain(case))
        mutated = copy.deepcopy(events)
        first_use = next(
            event
            for event in mutated
            if str(event.get("entry_id") or "").endswith("ledger-first-use")
        )
        first_use["costs"] = [{"kind": "charges", "amount": 99}]
        connection = _connection()
        try:
            with self.assertRaises(ContinuityError) as raised:
                validate_advantage_event_sequence(connection, mutated)
            self.assertIn(
                raised.exception.code,
                {
                    "ADVANTAGE_CHARGES_INSUFFICIENT",
                    "ADVANTAGE_RESOURCE_INSUFFICIENT",
                },
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
