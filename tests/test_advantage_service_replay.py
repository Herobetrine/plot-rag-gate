from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.continuity import (
    ADVANTAGE_SCHEMA_VERSION,
    ContinuityError,
    ContinuityService,
    ContinuityStore,
    HostApprovalAuthority,
)
from scripts.continuity.store import _V6_ITEM_PROJECTION_TABLES


class AdvantageServiceReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        state_dir = self.root / ".plot-rag"
        state_dir.mkdir(parents=True)
        (state_dir / "config.json").write_text(
            json.dumps(
                {
                    "advantage": {"readable_projection": False},
                    "items": {"strict_runtime_validation": True},
                }
            ),
            encoding="utf-8",
        )
        self.service = ContinuityService(self.root)
        self.host = HostApprovalAuthority(
            self.service,
            issuer="advantage-service-test",
            channel="interactive_test",
        )
        self.actor_id = self.service.register_entity(
            "character",
            "测试角色甲",
        )["entity_id"]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def event(
        event_type: str,
        quote: str,
        **fields: object,
    ) -> dict[str, object]:
        return {
            "schema_version": ADVANTAGE_SCHEMA_VERSION,
            "event_type": event_type,
            "evidence": {"quote": quote},
            **fields,
        }

    def sample_core_events(self) -> list[dict[str, object]]:
        actor = self.actor_id
        return [
            self.event(
                "advantage_spec",
                "样例优势核心寄宿于测试载体。",
                advantage_id="adv_sample_core",
                action="define",
                spec_type="advantage_definition",
                title="样例优势核心",
                profiles=["inheritance", "resource_transformer"],
                anchor_type="body_or_vessel",
                acquisition_mode="inheritance",
                uniqueness="unique",
                definition={
                    "initial_stage": "dormant",
                    "max_charges": 3,
                    "initial_charges": 2,
                    "initial_resources": {"sample_resource": 1},
                },
            ),
            self.event(
                "advantage_anchor",
                "测试载体成为样例装置锚点。",
                advantage_id="adv_sample_core",
                action="define",
                anchor_id="anchor_body",
                anchor_ref_id="sample_vessel",
                anchor_type="body_or_vessel",
                owner_entity_id=actor,
                binding_state="unbound",
            ),
            self.event(
                "advantage_module",
                "状态解析可观察能力核心。",
                advantage_id="adv_sample_core",
                action="define",
                module_id="module_discern",
                title="状态解析",
                kind="appraisal",
                module_status="enabled",
                stage="kindling",
                costs=[{"kind": "charges", "amount": 1}],
                effects=[{"kind": "observe"}],
            ),
            self.event(
                "advantage_bind",
                "测试角色甲完成认主。",
                advantage_id="adv_sample_core",
                action="bind",
                anchor_id="anchor_body",
                owner_entity_id=actor,
                story_coordinate={"calendar_id": "main", "ordinal": 2},
            ),
            self.event(
                "advantage_activate",
                "样例优势核心激活。",
                advantage_id="adv_sample_core",
                action="activate",
                owner_entity_id=actor,
                stage="kindling",
                story_coordinate={"calendar_id": "main", "ordinal": 3},
            ),
            self.event(
                "advantage_use",
                "测试角色甲以状态解析观察异常样本。",
                advantage_id="adv_sample_core",
                module_id="module_discern",
                actor_entity_id=actor,
                story_coordinate={"calendar_id": "main", "ordinal": 4},
                costs=[{"kind": "charges", "amount": 1}],
                effects=[{"kind": "observe"}],
                exposure_delta=0.5,
                causal_provenance={"source": "chapter-1"},
            ),
        ]

    def accept(self, events: list[dict[str, object]]) -> dict[str, object]:
        return self.accept_events(
            events,
            artifact_id="advantage-sample-core-e2e",
        )

    def accept_events(
        self,
        events: list[dict[str, object]],
        *,
        artifact_id: str,
        artifact_stage: str = "final",
        branch_id: str = "main",
        chapter_no: int = 1,
        scene_index: int = 0,
    ) -> dict[str, object]:
        proposal = self.service.save_proposal(
            events=events,
            artifact_id=artifact_id,
            artifact_stage=artifact_stage,
            proposal_kind="story_delta",
            branch_id=branch_id,
            chapter_no=chapter_no,
            scene_index=scene_index,
        )
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            str(proposal["proposal_id"]),
            expected_canon_revision=revision,
        )
        return self.service.accept_proposal(
            str(proposal["proposal_id"]),
            approval_id=str(grant["approval_id"]),
            expected_canon_revision=revision,
        )

    def test_strict_dry_run_accept_query_and_replay_hash(self) -> None:
        invalid = self.sample_core_events()
        invalid[-1]["costs"] = [{"kind": "charges", "amount": 3}]
        with self.assertRaises(ContinuityError) as raised:
            self.service.save_proposal(
                events=invalid,
                artifact_id="invalid-advantage",
                artifact_stage="final",
                branch_id="main",
                chapter_no=1,
                scene_index=0,
            )
        self.assertIn(
            raised.exception.code,
            {"ADVANTAGE_CHARGES_INSUFFICIENT", "ADVANTAGE_RESOURCE_INSUFFICIENT"},
        )

        commit = self.accept(self.sample_core_events())
        first_hash = str(commit["advantage_projection_hash"])
        self.assertRegex(first_hash, r"^advantage_projection_[0-9a-f]{64}$")
        self.assertEqual(1, commit["advantage_projection_schema_version"])
        self.assertEqual(
            "disabled",
            commit["readable_advantage_projection"]["status"],
        )

        runtime = self.service.query_advantage_runtime("adv_sample_core")
        self.assertEqual(1.0, runtime["runtime"]["charges"])
        self.assertEqual(0.5, runtime["runtime"]["exposure"])
        self.assertEqual(first_hash, runtime["advantage_projection_hash"])
        definition = self.service.query_advantage_definition(
            "adv_sample_core"
        )
        self.assertEqual("样例优势核心", definition["definition"]["title"])
        definitions = self.service.query_advantage_definitions(
            owner_entity_id=self.actor_id
        )
        self.assertEqual(1, definitions["count"])
        modules = self.service.query_advantage_modules(
            "adv_sample_core",
            enabled_only=True,
        )
        self.assertEqual(["module_discern"], [
            item["module_id"] for item in modules["modules"]
        ])
        ledger = self.service.query_advantage_ledger("adv_sample_core")
        self.assertEqual(3, ledger["count"])
        knowledge = self.service.query_advantage_knowledge(
            "adv_sample_core"
        )
        self.assertEqual(0, knowledge["count"])
        progression = self.service.query_advantage_progression(
            "adv_sample_core"
        )
        self.assertEqual("kindling", progression["stage"])
        exposure = self.service.query_advantage_exposure("adv_sample_core")
        self.assertEqual(0.5, exposure["exposure"])
        contracts = self.service.query_advantage_contracts(
            "adv_sample_core"
        )
        self.assertEqual(0, contracts["count"])
        context = self.service.query_special_item_context("adv_sample_core")
        self.assertEqual("样例优势核心", context["contexts"][0]["definition"]["title"])
        self.assertEqual(3, len(context["contexts"][0]["ledger"]))

        replayed = self.service.replay()
        self.assertEqual(first_hash, replayed["advantage_projection_hash"])
        replayed_again = self.service.replay()
        self.assertEqual(
            replayed["advantage_projection_hash"],
            replayed_again["advantage_projection_hash"],
        )

    def test_v6_to_v7_migration_is_additive_and_initializes_hash(self) -> None:
        self.service.schema_status()
        db_path = self.root / ".plot-rag" / "state.sqlite3"
        connection = sqlite3.connect(db_path)
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN")
            for table in (
                "advantage_projection_meta",
                "advantage_ledger",
                "advantage_knowledge",
                "advantage_contracts",
                "advantage_narrative_contracts",
                "advantage_runtime_state",
                "advantage_runtime_slots",
                "advantage_module_definitions",
                "advantage_anchors",
                "advantage_definitions",
                "item_stack_function_runtime_state",
                "item_knowledge_observations",
            ):
                connection.execute(f"DROP TABLE {table}")
            legacy_hash = ContinuityStore._item_projection_hash_for_tables(
                connection,
                _V6_ITEM_PROJECTION_TABLES,
            )
            connection.execute(
                """
                UPDATE item_projection_meta
                SET value_json=?
                WHERE meta_key='projection_hash'
                """,
                (json.dumps(legacy_hash),),
            )
            connection.execute(
                """
                UPDATE state_meta
                SET value='6'
                WHERE key='continuity_schema_version'
                """
            )
            connection.commit()
        finally:
            connection.close()

        migrated_store = ContinuityStore(self.root)
        backup = migrated_store.ensure_schema()
        self.assertIsNotNone(backup)
        with migrated_store.read_connection() as connection:
            self.assertEqual(
                "7",
                connection.execute(
                    """
                    SELECT value FROM state_meta
                    WHERE key='continuity_schema_version'
                    """
                ).fetchone()[0],
            )
            self.assertEqual(
                4,
                connection.execute(
                    "SELECT COUNT(*) FROM advantage_projection_meta"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                connection.execute(
                    """
                    SELECT COUNT(*) FROM sqlite_master
                    WHERE type='table'
                      AND name='item_stack_function_runtime_state'
                    """
                ).fetchone()[0],
            )
            metadata = dict(
                connection.execute(
                    """
                    SELECT meta_key, value_json
                    FROM advantage_projection_meta
                    """
                ).fetchall()
            )
            self.assertIn("projection_hash", metadata)

    def test_save_proposal_idempotency_precedes_state_dry_run(self) -> None:
        """A retry remains stable after the Advantage runtime has changed."""

        initial = self.sample_core_events()
        # Leave one charge available after the bootstrap/use sequence.
        initial[-1]["costs"] = [{"kind": "charges", "amount": 1}]
        self.accept(initial)

        retry_events = [
            self.event(
                "advantage_use",
                "测试角色甲再次以状态解析观察异常样本。",
                advantage_id="adv_sample_core",
                module_id="module_discern",
                actor_entity_id=self.actor_id,
                story_coordinate={"calendar_id": "main", "ordinal": 5},
                costs=[{"kind": "charges", "amount": 1}],
                effects=[{"kind": "observe"}],
                exposure_delta=0.1,
            )
        ]
        idempotency_key = "save-proposal-retry-after-state-change"
        first = self.service.save_proposal(
            events=retry_events,
            artifact_id="retry-after-state-change",
            artifact_stage="final",
            proposal_kind="story_delta",
            branch_id="main",
            chapter_no=1,
            scene_index=1,
            idempotency_key=idempotency_key,
        )

        # Advance the accepted state so the same event is no longer valid.
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            str(first["proposal_id"]),
            expected_canon_revision=revision,
        )
        self.service.accept_proposal(
            str(first["proposal_id"]),
            approval_id=str(grant["approval_id"]),
            expected_canon_revision=revision,
        )

        second = self.service.save_proposal(
            events=retry_events,
            artifact_id="retry-after-state-change",
            artifact_stage="final",
            proposal_kind="story_delta",
            branch_id="main",
            chapter_no=1,
            scene_index=1,
            idempotency_key=idempotency_key,
        )
        self.assertEqual(first["proposal_id"], second["proposal_id"])

    def test_planned_and_historical_events_do_not_mutate_current_runtime(
        self,
    ) -> None:
        initial = self.accept(self.sample_core_events())
        initial_hash = str(initial["advantage_projection_hash"])
        initial_runtime = self.service.query_advantage_runtime(
            "adv_sample_core"
        )["runtime"]

        self.accept_events(
            [
                self.event(
                    "advantage_use",
                    "计划里测试角色甲还会再用一次状态解析。",
                    advantage_id="adv_sample_core",
                    module_id="module_discern",
                    actor_entity_id=self.actor_id,
                    story_coordinate={
                        "calendar_id": "main",
                        "ordinal": 6,
                    },
                    costs={"charges": 1},
                    exposure_delta=9,
                )
            ],
            artifact_id="advantage-outline",
            artifact_stage="outline",
            chapter_no=2,
        )
        after_outline = self.service.query_advantage_runtime(
            "adv_sample_core"
        )
        self.assertEqual(initial_runtime, after_outline["runtime"])
        self.assertEqual(
            initial_hash,
            after_outline["advantage_projection_hash"],
        )
        planned = self.service.query_facts(scope="planned")["facts"]
        self.assertTrue(
            any(
                row["fact_type"] == "advantage_use"
                and row["scope"] == "planned"
                for row in planned
            )
        )

        self.accept_events(
            [
                self.event(
                    "advantage_use",
                    "旧日里测试角色甲曾以状态解析观察异常样本。",
                    advantage_id="adv_sample_core",
                    module_id="module_discern",
                    actor_entity_id=self.actor_id,
                    scope="historical",
                    story_coordinate={
                        "calendar_id": "main",
                        "ordinal": 1,
                    },
                    costs={"charges": 1},
                    exposure_delta=7,
                )
            ],
            artifact_id="advantage-history",
            chapter_no=3,
        )
        after_history = self.service.query_advantage_runtime(
            "adv_sample_core"
        )
        self.assertEqual(initial_runtime, after_history["runtime"])
        self.assertEqual(
            initial_hash,
            after_history["advantage_projection_hash"],
        )
        historical = self.service.query_facts(
            chapter_no=3,
            scene_index=0,
            include_historical=True,
        )["facts"]
        self.assertTrue(
            any(
                row["fact_type"] == "advantage_use"
                and row["scope"] == "historical"
                for row in historical
            )
        )

    def test_branch_runtime_survives_accept_and_replay_without_main_hash_change(
        self,
    ) -> None:
        initial = self.accept(self.sample_core_events())
        initial_hash = str(initial["advantage_projection_hash"])
        main_before = self.service.query_advantage_runtime(
            "adv_sample_core",
            branch_id="main",
        )["runtime"]
        self.assertEqual(1.0, main_before["charges"])

        branch_commit = self.accept_events(
            [
                self.event(
                    "advantage_use",
                    "在假设分支中，测试角色甲再次消耗一枚充能。",
                    advantage_id="adv_sample_core",
                    module_id="module_discern",
                    actor_entity_id=self.actor_id,
                    branch_id="what-if",
                    story_coordinate={
                        "calendar_id": "main",
                        "ordinal": 5,
                    },
                    costs={"charges": 1},
                    exposure_delta=0.2,
                )
            ],
            artifact_id="advantage-branch-use",
            branch_id="what-if",
            chapter_no=2,
        )
        self.assertFalse(branch_commit["changes_authority"])
        self.assertEqual(
            initial_hash,
            branch_commit["advantage_projection_hash"],
        )
        self.assertEqual(
            main_before,
            self.service.query_advantage_runtime(
                "adv_sample_core",
                branch_id="main",
            )["runtime"],
        )
        branch_runtime = self.service.query_advantage_runtime(
            "adv_sample_core",
            branch_id="what-if",
        )["runtime"]
        self.assertEqual(0.0, branch_runtime["charges"])
        self.assertEqual(0.7, branch_runtime["exposure"])
        branch_facts = self.service.query_facts(
            branch_id="what-if",
            include_provisional=True,
        )["facts"]
        self.assertTrue(
            any(row["fact_type"] == "advantage_use" for row in branch_facts)
        )

        replayed = self.service.replay()
        self.assertEqual(initial_hash, replayed["advantage_projection_hash"])
        replayed_branch = self.service.query_advantage_runtime(
            "adv_sample_core",
            branch_id="what-if",
        )["runtime"]
        self.assertEqual(branch_runtime, replayed_branch)

    def test_advantage_correction_replays_replacement_at_target_position(
        self,
    ) -> None:
        initial = self.accept(self.sample_core_events())
        module_event_id = next(
            str(row["event_id"])
            for row in initial["events"]
            if row["event_type"] == "advantage_module"
            and row["payload"]["action"] == "define"
        )
        correction = self.event(
            "advantage_correction",
            "状态解析模块的定义被校正，但后续使用事实保持不变。",
            advantage_id="adv_sample_core",
            action="correct",
            target_event_id=module_event_id,
            supersedes=[module_event_id],
            replacement=self.event(
                "advantage_module",
                "状态解析模块的定义被校正。",
                advantage_id="adv_sample_core",
                action="define",
                module_id="module_discern",
                title="状态解析·校正版",
                kind="appraisal",
                module_status="enabled",
                stage="kindling",
                costs=[{"kind": "charges", "amount": 1}],
                effects=[{"kind": "observe"}],
            ),
        )
        self.accept_events(
            [correction],
            artifact_id="advantage-module-correction",
            chapter_no=2,
        )
        modules = self.service.query_advantage_modules("adv_sample_core")
        self.assertEqual("状态解析·校正版", modules["modules"][0]["title"])
        self.assertEqual(
            1.0,
            self.service.query_advantage_runtime(
                "adv_sample_core"
            )["runtime"]["charges"],
        )
        replayed = self.service.replay()
        self.assertRegex(
            replayed["advantage_projection_hash"],
            r"^advantage_projection_[0-9a-f]{64}$",
        )


if __name__ == "__main__":
    unittest.main()
