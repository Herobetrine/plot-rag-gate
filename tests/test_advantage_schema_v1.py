from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from scripts.continuity.advantages import (
    ADVANTAGE_PROJECTION_TABLES,
    ADVANTAGE_SCHEMA_VERSION,
    AdvantageProjectionState,
    advantage_projection_payload,
    bootstrap_advantage_projection,
    compute_advantage_projection_hash,
    ensure_advantage_schema,
    query_advantage_context,
    query_advantage_knowledge,
    rebuild_advantage_projection,
    refresh_advantage_projection_metadata,
)
from scripts.continuity.schema import (
    CONTINUITY_V7_SCHEMA_SQL,
    SCHEMA_VERSION,
    STATE_DATABASE_TABLES,
)
from scripts.continuity.validators import ContinuityError


class AdvantageSchemaV1Tests(unittest.TestCase):
    @staticmethod
    def definition_event(
        *,
        advantage_id: str = "advantage_sample_core",
        status: str = "canon",
    ) -> dict[str, object]:
        return {
            "schema_version": ADVANTAGE_SCHEMA_VERSION,
            "event_type": "advantage_spec",
            "action": "define",
            "spec_type": "advantage_definition",
            "advantage_id": advantage_id,
            "title": "样例优势核心",
            "profiles": [
                "legacy_artifact",
                "knowledge_analysis",
                "growth_relic",
            ],
            "anchor_type": "body_or_vessel",
            "acquisition_mode": "inheritance",
            "uniqueness": "unique",
            "status": status,
            "promise": {"loop": "状态解析、模块调用、结果验证"},
            "counterplay": ["误差", "暴露", "容量"],
            "definition": {
                "initial_stage": "dormant",
                "initial_charges": 2,
                "max_charges": 3,
                "initial_resources": {"sample_resource": 1},
            },
            "source_claim_ids": ["claim.example_core"],
        }

    @staticmethod
    def lifecycle_events() -> list[dict[str, object]]:
        coordinate = {"calendar_id": "generic", "ordinal": 1}
        return [
            AdvantageSchemaV1Tests.definition_event(),
            {
                "event_type": "advantage_anchor",
                "action": "define",
                "advantage_id": "advantage_sample_core",
                "anchor_id": "anchor_sample_vessel",
                "anchor_type": "body_or_vessel",
                "anchor_ref_id": "sample_vessel",
                "owner_entity_id": "actor_test_actor_a",
                "binding_state": "unbound",
                "status": "canon",
            },
            {
                "event_type": "advantage_module",
                "action": "define",
                "advantage_id": "advantage_sample_core",
                "module_id": "module_inspect_sample",
                "title": "状态解析",
                "kind": "appraisal",
                "status": "canon",
                "module_status": "available",
                "stage": "active",
                "trigger": {"cooldown": 2},
                "preconditions": [],
                "targets": ["sample_resource"],
                "costs": [{"kind": "charges", "amount": 1}],
                "effects": [{"kind": "observe"}],
                "side_effects": [],
                "failure_modes": ["insufficient_charge"],
                "counters": ["concealment"],
            },
            {
                "event_type": "advantage_bind",
                "action": "bind",
                "advantage_id": "advantage_sample_core",
                "anchor_id": "anchor_sample_vessel",
                "owner_entity_id": "actor_test_actor_a",
                "branch_id": "main",
                "story_coordinate": coordinate,
            },
            {
                "event_type": "advantage_activate",
                "action": "activate",
                "advantage_id": "advantage_sample_core",
                "owner_entity_id": "actor_test_actor_a",
                "stage": "active",
                "branch_id": "main",
                "story_coordinate": {
                    "calendar_id": "generic",
                    "ordinal": 2,
                },
            },
            {
                "event_type": "advantage_module",
                "action": "unlock",
                "advantage_id": "advantage_sample_core",
                "module_id": "module_inspect_sample",
                "branch_id": "main",
            },
            {
                "event_type": "advantage_use",
                "advantage_id": "advantage_sample_core",
                "module_id": "module_inspect_sample",
                "actor_entity_id": "actor_test_actor_a",
                "entry_id": "ledger_first_discern",
                "branch_id": "main",
                "story_coordinate": {
                    "calendar_id": "generic",
                    "ordinal": 3,
                },
                "exposure_delta": 0.25,
            },
            {
                "event_type": "advantage_reveal",
                "advantage_id": "advantage_sample_core",
                "module_id": "module_inspect_sample",
                "knowledge_id": "knowledge_objective",
                "knowledge_plane": "objective",
                "status": "canon",
                "claim": {"text": "状态解析可观察能力核心"},
                "confidence": 1,
                "evidence": {"quote": "他看见了异常样本。"},
                "reveal_stage": "author_known",
            },
            {
                "event_type": "advantage_reveal",
                "advantage_id": "advantage_sample_core",
                "module_id": "module_inspect_sample",
                "knowledge_id": "knowledge_actor_a",
                "observer_entity_id": "actor_test_actor_a",
                "knowledge_plane": "actor_belief",
                "status": "canon",
                "claim": {"text": "这东西能辨认异常样本"},
                "confidence": 0.8,
                "evidence": {"quote": "测试角色甲记住了这次观察。"},
                "reveal_stage": "first_use",
            },
            {
                "event_type": "advantage_reveal",
                "advantage_id": "advantage_sample_core",
                "module_id": "module_inspect_sample",
                "knowledge_id": "knowledge_future",
                "knowledge_plane": "author_plan",
                "status": "planned",
                "claim": {"text": "未来可以深层解析"},
                "confidence": 1,
                "evidence": {"quote": "仅为后续计划。"},
                "reveal_stage": "future",
            },
            {
                "event_type": "advantage_contract",
                "action": "define",
                "advantage_id": "advantage_sample_core",
                "contract_id": "contract_inheritance",
                "actor_entity_id": "actor_test_actor_a",
                "counterparty_entity_id": "actor_test_actor_b",
                "status": "canon",
                "terms": ["继承样例优势核心"],
                "agency": {"actor_test_actor_b": "limited"},
                "trust_delta": 0.25,
                "debt_delta": 1,
                "breach_effect": {"kind": "generic_penalty"},
            },
            {
                "event_type": "advantage_spec",
                "action": "define",
                "spec_type": "narrative_contract",
                "spec_id": "narrative_sample_core",
                "advantage_id": "advantage_sample_core",
                "status": "canon",
                "reading_promise": {"experience": "能力验证与限制反馈"},
                "reward_loop": ["状态解析", "模块调用", "结果验证"],
                "risk_loop": ["误差", "身份追查"],
                "reveal_ladder": ["激活", "首次校验", "完整说明"],
                "experience_binding": {"contract_id": "experience_first_use"},
            },
        ]

    def connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        return connection

    def test_canonical_entry_and_continuity_v7_own_all_tables(self) -> None:
        root = Path(__file__).resolve().parents[1]
        entry = json.loads(
            (
                root
                / "schemas"
                / "plot-rag-advantage"
                / "v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            "../plot-rag-advantage.v1.json",
            entry["$ref"],
        )
        self.assertEqual(7, SCHEMA_VERSION)
        self.assertTrue(
            set(ADVANTAGE_PROJECTION_TABLES).issubset(
                STATE_DATABASE_TABLES
            )
        )
        connection = self.connection()
        try:
            connection.executescript(CONTINUITY_V7_SCHEMA_SQL)
            table_names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(
                set(ADVANTAGE_PROJECTION_TABLES).issubset(table_names)
            )
            self.assertIn("item_stack_function_runtime_state", table_names)
            self.assertIn("item_knowledge_observations", table_names)
        finally:
            connection.close()

    def test_schema_creation_is_transactional_and_read_only_safe(self) -> None:
        connection = self.connection()
        try:
            connection.execute("BEGIN")
            ensure_advantage_schema(connection)
            connection.rollback()
            self.assertNotIn(
                "advantage_definitions",
                {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                },
            )
            ensure_advantage_schema(connection)
            connection.commit()
            connection.execute("PRAGMA query_only=ON")
            self.assertEqual(
                "advantage_projection_",
                compute_advantage_projection_hash(connection)[:21],
            )
        finally:
            connection.close()

    def test_reducer_projects_runtime_ledger_contract_and_knowledge(self) -> None:
        connection = self.connection()
        try:
            ensure_advantage_schema(connection)
            state = AdvantageProjectionState.from_connection(connection)
            events = self.lifecycle_events()
            state.apply_sequence(events)
            state.persist(connection)
            first_hash = refresh_advantage_projection_metadata(connection)
            context = query_advantage_context(
                connection,
                "advantage_sample_core",
                branch_id="main",
                knowledge_plane="actor_belief",
                observer_entity_id="actor_test_actor_a",
                visibility="inspection",
            )
            self.assertEqual("样例优势核心", context["definition"]["title"])
            self.assertEqual(1.0, context["runtime"]["charges"])
            self.assertEqual(0.25, context["runtime"]["exposure"])
            self.assertEqual(1.0, context["runtime"]["debt"])
            self.assertEqual(
                {"calendar_id": "generic", "ordinal": 5},
                context["runtime"]["cooldown_until_json"],
            )
            self.assertEqual(
                ["module_inspect_sample"],
                context["runtime"]["unlocked_modules_json"],
            )
            self.assertEqual(
                ["knowledge_actor_a"],
                [row["knowledge_id"] for row in context["knowledge"]],
            )
            self.assertEqual(
                "active",
                context["narrative_contract"]["contract_status"],
            )
            self.assertTrue(first_hash.startswith("advantage_projection_"))
            self.assertEqual(
                first_hash,
                compute_advantage_projection_hash(connection),
            )
            payload = advantage_projection_payload(connection)
            self.assertEqual(
                1,
                len(payload["tables"]["advantage_runtime_state"]),
            )
        finally:
            connection.close()

    def test_resource_mapping_and_list_have_identical_scalar_semantics(
        self,
    ) -> None:
        def prepared_state() -> AdvantageProjectionState:
            state = AdvantageProjectionState()
            state.apply(
                {
                    **self.definition_event(
                        advantage_id="advantage-scalars",
                    ),
                    "definition": {
                        "initial_stage": "active",
                        "initial_charges": 4,
                        "max_charges": 10,
                        "initial_resources": {"mana": 10},
                    },
                },
                source_event_id="event-definition",
                updated_order=1,
            )
            state.apply(
                {
                    "event_type": "advantage_module",
                    "action": "define",
                    "advantage_id": "advantage-scalars",
                    "module_id": "module-scalars",
                    "title": "标量测试",
                    "kind": "ability",
                    "module_status": "enabled",
                    "costs": [{"resource": "charges", "amount": 1}],
                },
                source_event_id="event-module",
                updated_order=2,
            )
            state.apply(
                {
                    "event_type": "advantage_activate",
                    "action": "activate",
                    "advantage_id": "advantage-scalars",
                    "owner_entity_id": "actor-scalar",
                    "branch_id": "main",
                    "charges": 4,
                    "max_charges": 10,
                    "resources": {"mana": 10},
                    "pollution": 5,
                    "exposure": 5,
                    "debt": 5,
                },
                source_event_id="event-activate",
                updated_order=3,
            )
            return state

        list_state = prepared_state()
        mapping_state = prepared_state()
        list_state.apply(
            {
                "event_type": "advantage_use",
                "advantage_id": "advantage-scalars",
                "module_id": "module-scalars",
                "branch_id": "main",
                "costs": [
                    {"resource": "charges", "amount": 1},
                    {"resource": "pollution", "amount": 2},
                    {"resource": "exposure", "amount": 3},
                    {"resource": "debt", "amount": 4},
                    {"resource": "mana", "amount": 2},
                ],
                "rewards": [
                    {"resource": "charges", "amount": 2},
                    {"resource": "pollution", "amount": 1},
                    {"resource": "exposure", "amount": 1},
                    {"resource": "debt", "amount": 1},
                    {"resource": "mana", "amount": 1},
                ],
            },
            source_event_id="event-list",
            updated_order=4,
        )
        mapping_state.apply(
            {
                "event_type": "advantage_use",
                "advantage_id": "advantage-scalars",
                "module_id": "module-scalars",
                "branch_id": "main",
                "costs": {
                    "charges": 1,
                    "pollution": 2,
                    "exposure": 3,
                    "debt": 4,
                    "mana": 2,
                },
                "rewards": {
                    "charges": 2,
                    "pollution": 1,
                    "exposure": 1,
                    "debt": 1,
                    "mana": 1,
                },
            },
            source_event_id="event-mapping",
            updated_order=4,
        )
        for field in (
            "charges",
            "resources_json",
            "pollution",
            "exposure",
            "debt",
        ):
            self.assertEqual(
                list_state.runtime[("advantage-scalars", "main")][field],
                mapping_state.runtime[("advantage-scalars", "main")][field],
                field,
            )
        self.assertEqual(
            {
                "charges": 5.0,
                "resources_json": {"mana": 9.0},
                "pollution": 6.0,
                "exposure": 7.0,
                "debt": 8.0,
            },
            {
                key: list_state.runtime[
                    ("advantage-scalars", "main")
                ][key]
                for key in (
                    "charges",
                    "resources_json",
                    "pollution",
                    "exposure",
                    "debt",
                )
            },
        )

        for output in (
            {"charges": 9, "damage": 10},
            [
                {"resource": "charges", "amount": 9},
                {"kind": "damage", "amount": 10},
            ],
        ):
            with self.subTest(output_type=type(output).__name__):
                state = prepared_state()
                state.apply(
                    {
                        "event_type": "advantage_use",
                        "advantage_id": "advantage-scalars",
                        "module_id": "module-scalars",
                        "branch_id": "main",
                        "costs": [],
                        "rewards": [],
                        "output": output,
                    },
                    source_event_id=f"event-output-{type(output).__name__}",
                    updated_order=4,
                )
                runtime = state.runtime[("advantage-scalars", "main")]
                self.assertEqual(4.0, runtime["charges"])
                self.assertEqual({"mana": 10.0}, runtime["resources_json"])
                entry = next(
                    row
                    for row in state.ledger.values()
                    if row["entry_kind"] == "use"
                )
                self.assertEqual(output, entry["output_json"]["output"])
                self.assertEqual([], entry["output_json"]["rewards"])

    def test_planned_module_and_knowledge_do_not_enter_canon_context(self) -> None:
        connection = self.connection()
        try:
            ensure_advantage_schema(connection)
            state = AdvantageProjectionState()
            state.apply(
                self.definition_event(
                    advantage_id="advantage_planned",
                    status="planned",
                ),
                source_event_id="event_definition",
                updated_order=1,
            )
            state.apply(
                {
                    "event_type": "advantage_module",
                    "action": "define",
                    "advantage_id": "advantage_planned",
                    "module_id": "module_future",
                    "title": "未来能力",
                    "kind": "prediction",
                    "status": "planned",
                    "stage": "future",
                },
                source_event_id="event_module",
                updated_order=2,
            )
            with self.assertRaises(ContinuityError) as activate:
                state.apply(
                    {
                        "event_type": "advantage_activate",
                        "action": "activate",
                        "advantage_id": "advantage_planned",
                        "owner_entity_id": "actor_test_actor_a",
                    },
                    source_event_id="event_activate",
                    updated_order=3,
                )
            self.assertEqual(
                "ADVANTAGE_NONCANON_AUTHORITY",
                activate.exception.code,
            )
            state.persist(connection)
            context = query_advantage_context(
                connection,
                "advantage_planned",
            )
            self.assertEqual([], context["modules"])
            self.assertIsNone(context["runtime"])
        finally:
            connection.close()

    def test_nonmain_global_events_are_rejected_by_public_reducer(self) -> None:
        state = AdvantageProjectionState()
        state.apply(
            self.definition_event(advantage_id="advantage-branch-guard"),
            source_event_id="event-definition",
            updated_order=1,
        )
        cases = (
            {
                "event_type": "advantage_spec",
                "action": "define",
                "spec_type": "advantage_definition",
                "advantage_id": "advantage-new-branch",
                "title": "分支新金手指",
                "anchor_type": "virtual_system",
            },
            {
                "event_type": "advantage_anchor",
                "action": "define",
                "advantage_id": "advantage-branch-guard",
                "anchor_id": "anchor-branch",
                "anchor_type": "virtual_system",
                "anchor_ref_id": "system-branch",
            },
            {
                "event_type": "advantage_module",
                "action": "define",
                "advantage_id": "advantage-branch-guard",
                "module_id": "module-branch",
                "title": "分支模块",
                "kind": "ability",
            },
            {
                "event_type": "advantage_contract",
                "action": "define",
                "advantage_id": "advantage-branch-guard",
                "contract_id": "contract-branch",
            },
            {
                "event_type": "advantage_reveal",
                "advantage_id": "advantage-branch-guard",
                "knowledge_plane": "public_narrative",
                "claim": {"text": "分支秘密"},
            },
        )
        for index, event in enumerate(cases, start=2):
            with self.subTest(event_type=event["event_type"]):
                with self.assertRaises(ContinuityError) as raised:
                    state.apply(
                        {**event, "branch_id": "what-if"},
                        source_event_id=f"event-{index}",
                        updated_order=index,
                    )
                self.assertEqual(
                    "ADVANTAGE_BRANCH_EVENT_UNSUPPORTED",
                    raised.exception.code,
                )

    def test_rebuild_is_idempotent_and_filters_inactive_events(self) -> None:
        connection = self.connection()
        try:
            ensure_advantage_schema(connection)
            events: list[dict[str, object]] = []
            for index, event in enumerate(self.lifecycle_events(), start=1):
                events.append(
                    {
                        "event_id": f"accepted_{index}",
                        "event_type": event["event_type"],
                        "payload_json": json.dumps(
                            event,
                            ensure_ascii=False,
                        ),
                        "changes_authority": 1,
                        "updated_order": index,
                    }
                )
            first = rebuild_advantage_projection(
                connection,
                events,
                frozenset(),
                record_run=False,
            )
            second = rebuild_advantage_projection(
                connection,
                events,
                frozenset(),
                record_run=False,
            )
            self.assertEqual(
                first["advantage_projection_hash"],
                second["advantage_projection_hash"],
            )
            filtered = rebuild_advantage_projection(
                connection,
                events,
                {"accepted_7"},
                record_run=False,
            )
            self.assertNotEqual(
                first["advantage_projection_hash"],
                filtered["advantage_projection_hash"],
            )
            runtime = query_advantage_context(
                connection,
                "advantage_sample_core",
            )["runtime"]
            self.assertEqual(2.0, runtime["charges"])
            self.assertEqual(0.0, runtime["exposure"])
        finally:
            connection.close()

    def test_sidecar_bootstrap_maps_authority_and_domain_ledger_kinds(self) -> None:
        sidecar = {
            "schema_version": ADVANTAGE_SCHEMA_VERSION,
            "definitions": [
                {
                    **self.definition_event(),
                    "event_type": None,
                }
            ],
            "anchors": [],
            "modules": [
                {
                    "module_id": "module_planned",
                    "advantage_id": "advantage_sample_core",
                    "title": "计划模块",
                    "kind": "growth",
                    "status": "planned",
                    "stage": "future",
                }
            ],
            "runtime_slots": [],
            "runtime_bootstrap": [
                {
                    "runtime_id": "runtime_sample_core",
                    "advantage_id": "advantage_sample_core",
                    "branch_id": "main",
                    "stage": "active",
                    "enabled": True,
                    "charges": 1,
                    "max_charges": 2,
                    "resources": {"sample_resource": 1},
                    "pollution": 0,
                    "exposure": 0,
                    "debt": 0,
                    "unlocked_modules": [],
                    "runtime_metadata": {"note": "canonical bootstrap"},
                }
            ],
            "ledger_bootstrap": [
                {
                    "entry_id": "ledger_sample_resource",
                    "advantage_id": "advantage_sample_core",
                    "entry_kind": "sample_resource_acquired",
                    "input": {},
                    "output": {"sample_resource": 1},
                    "loss": {},
                    "provenance": {"chapter": 2},
                }
            ],
            "knowledge": [],
            "contracts": [],
            "narrative_contracts": [
                {
                    "narrative_contract_id": "narrative_sample_core",
                    "advantage_id": "advantage_sample_core",
                    "status": "canon",
                    "reading_promise": "收益伴随代价",
                    "reward_loop": [],
                    "risk_loop": [],
                    "reveal_ladder": [],
                    "experience_binding": {},
                }
            ],
        }
        connection = self.connection()
        try:
            result = bootstrap_advantage_projection(
                connection,
                sidecar,
                replace=True,
            )
            self.assertTrue(
                result["advantage_projection_hash"].startswith(
                    "advantage_projection_"
                )
            )
            context = query_advantage_context(
                connection,
                "advantage_sample_core",
            )
            self.assertEqual([], context["modules"])
            self.assertEqual(
                {"note": "canonical bootstrap"},
                context["runtime"]["runtime_json"],
            )
            self.assertEqual(
                "active",
                context["narrative_contract"]["contract_status"],
            )
            self.assertEqual(
                "sample_resource_acquired",
                context["ledger"][0]["entry_kind"],
            )
            self.assertEqual(
                [],
                query_advantage_knowledge(
                    connection,
                    "advantage_sample_core",
                ),
            )
        finally:
            connection.close()

    def test_misread_knowledge_is_persisted_after_its_reference(self) -> None:
        connection = self.connection()
        try:
            ensure_advantage_schema(connection)
            state = AdvantageProjectionState()
            state.apply(
                self.definition_event(),
                source_event_id="definition",
                updated_order=1,
            )
            state.apply(
                {
                    "event_type": "advantage_reveal",
                    "advantage_id": "advantage_sample_core",
                    "knowledge_id": "knowledge_misread",
                    "knowledge_plane": "actor_belief",
                    "observer_entity_id": "actor_test_actor_a",
                    "status": "misread",
                    "claim": {"text": "样例装置没有代价"},
                    "confidence": 0.5,
                    "evidence": {"quote": "测试角色甲暂时如此判断。"},
                    "reveal_stage": "first_use",
                    "misread_of": "knowledge_objective_cost",
                    "record_ledger": False,
                },
                source_event_id="misread",
                updated_order=2,
            )
            state.apply(
                {
                    "event_type": "advantage_reveal",
                    "advantage_id": "advantage_sample_core",
                    "knowledge_id": "knowledge_objective_cost",
                    "knowledge_plane": "objective",
                    "status": "canon",
                    "claim": {"text": "样例装置会积累误差"},
                    "confidence": 1,
                    "evidence": {"quote": "污染客观存在。"},
                    "reveal_stage": "author_known",
                    "record_ledger": False,
                },
                source_event_id="objective",
                updated_order=3,
            )
            state.persist(connection)
            self.assertEqual(
                [
                    "knowledge_objective_cost",
                    "knowledge_misread",
                ],
                [
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT knowledge_id
                        FROM advantage_knowledge
                        ORDER BY rowid
                        """
                    )
                ],
            )

            broken = AdvantageProjectionState()
            broken.apply(
                self.definition_event(),
                source_event_id="definition",
                updated_order=1,
            )
            broken.apply(
                {
                    "event_type": "advantage_reveal",
                    "advantage_id": "advantage_sample_core",
                    "knowledge_id": "knowledge_broken",
                    "knowledge_plane": "actor_belief",
                    "observer_entity_id": "actor_test_actor_a",
                    "status": "misread",
                    "claim": {"text": "错误判断"},
                    "confidence": 0.5,
                    "evidence": {"quote": "错误判断出现。"},
                    "reveal_stage": "first_use",
                    "misread_of": "knowledge_missing",
                    "record_ledger": False,
                },
                source_event_id="broken",
                updated_order=2,
            )
            with self.assertRaises(ContinuityError) as error:
                broken.persist(connection)
            self.assertEqual(
                "ADVANTAGE_KNOWLEDGE_REFERENCE_INVALID",
                error.exception.code,
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
