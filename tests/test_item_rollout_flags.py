from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.continuity import (
    ContinuityError,
    ContinuityService,
    HostApprovalAuthority,
)
from scripts.continuity.items import (
    ItemRolloutPolicy,
    assert_item_rollout_acceptance,
    detect_item_ability_bridge_attempts,
    inspect_item_event_sequence,
    load_item_rollout_policy,
    validate_item_event_sequence,
)
from scripts.continuity.validators import normalize_event


class ItemRolloutFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.write_policy(strict=True, bridge=True)
        self.service = ContinuityService(self.root)
        self.host = HostApprovalAuthority(
            self.service,
            issuer="item-rollout-test-host",
            channel="interactive_test",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_policy(self, *, strict: bool, bridge: bool) -> None:
        runtime = self.root / ".plot-rag"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "config.json").write_text(
            json.dumps(
                {
                    "items": {
                        "strict_runtime_validation": strict,
                        "power_binding_bridge": bridge,
                    }
                }
            ),
            encoding="utf-8",
        )

    def entity(self, entity_type: str, name: str) -> str:
        return self.service.register_entity(entity_type, name)["entity_id"]

    def accept_events(
        self,
        events: list[dict[str, object]],
        *,
        artifact_id: str,
        stage: str = "final",
        proposal_kind: str = "story_delta",
        operation: str = "accept",
    ) -> dict[str, object]:
        proposal = self.service.save_proposal(
            events=events,
            artifact_id=artifact_id,
            artifact_stage=stage,
            proposal_kind=proposal_kind,
            branch_id="main",
            chapter_no=None if stage == "bootstrap" else 1,
            scene_index=None if stage == "bootstrap" else 0,
        )
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            str(proposal["proposal_id"]),
            expected_canon_revision=revision,
            operations=(operation,),
        )
        return self.service.accept_proposal(
            str(proposal["proposal_id"]),
            approval_id=str(grant["approval_id"]),
            expected_canon_revision=revision,
        )

    @staticmethod
    def grant_row(
        service: ContinuityService,
        proposal_id: str,
    ) -> dict[str, object]:
        with service.store.read_connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM approval_grants
                WHERE proposal_id=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (proposal_id,),
            ).fetchone()
        return dict(row) if row is not None else {}

    def normalized_item(
        self,
        event_type: str,
        *,
        quote: str,
        ordinal: int = 1,
        **fields: object,
    ) -> dict[str, object]:
        return normalize_event(
            {
                "schema_version": "plot-rag-delta/v4",
                "event_type": event_type,
                "story_coordinate": {
                    "calendar_id": "item-rollout-calendar",
                    "ordinal": ordinal,
                },
                "knowledge_plane": "objective",
                "evidence": {"quote": quote},
                **fields,
            },
            artifact_stage="final",
            branch_id="main",
            chapter_no=1,
            scene_index=0,
        )

    def test_policy_loader_uses_release_defaults_and_rejects_bad_types(
        self,
    ) -> None:
        (self.root / ".plot-rag" / "config.json").unlink()
        self.assertEqual(
            {
                "strict_runtime_validation": False,
                "power_binding_bridge": True,
            },
            load_item_rollout_policy(self.root).as_dict(),
        )
        (self.root / ".plot-rag" / "config.json").write_text(
            '{"items":{"strict_runtime_validation":"true"}}',
            encoding="utf-8",
        )
        with self.assertRaises(ContinuityError) as caught:
            load_item_rollout_policy(self.root)
        self.assertEqual("ITEM_ROLLOUT_CONFIG_INVALID", caught.exception.code)

    def test_shadow_dry_run_reports_runtime_difference_without_writes(
        self,
    ) -> None:
        actor = self.entity("character", "影子校验使用者")
        event = self.normalized_item(
            "item_use",
            quote="他试图使用一个尚未建立的物品功能。",
            subject_type="item_instance",
            subject_id="missing-instance",
            item_instance_id="missing-instance",
            actor_entity_id=actor,
            function_id="missing-function",
            delta={},
        )
        with self.service.store.read_connection() as connection:
            before = connection.execute(
                "SELECT COUNT(*) FROM item_use_history"
            ).fetchone()[0]
            report = inspect_item_event_sequence(
                connection,
                [event],
                rollout_policy=ItemRolloutPolicy(
                    strict_runtime_validation=False,
                    power_binding_bridge=True,
                ),
            )
            after = connection.execute(
                "SELECT COUNT(*) FROM item_use_history"
            ).fetchone()[0]
        self.assertEqual("differences", report["status"])
        self.assertEqual(1, report["diagnostic_count"])
        self.assertEqual(
            "ITEM_INSTANCE_NOT_FOUND",
            report["diagnostics"][0]["code"],
        )
        self.assertEqual(before, after)

    def test_shadow_authority_gate_blocks_v4_but_not_legacy_inventory(
        self,
    ) -> None:
        item_event = self.normalized_item(
            "item_spec",
            quote="定义一件只用于验证 rollout 的普通物品。",
            action="define",
            spec_type="item_definition",
            spec_id="rollout-definition",
            definition={
                "item_kind": "miscellaneous",
                "stack_policy": "non_stackable",
                "uniqueness_policy": "ordinary",
            },
        )
        shadow = ItemRolloutPolicy(
            strict_runtime_validation=False,
            power_binding_bridge=True,
        )
        with self.service.store.read_connection() as connection:
            with self.assertRaises(ContinuityError) as caught:
                assert_item_rollout_acceptance(
                    connection,
                    [item_event],
                    rollout_policy=shadow,
                    changes_authority=True,
                )
            legacy_report = assert_item_rollout_acceptance(
                connection,
                [
                    {
                        "schema_version": "plot-rag-delta/v3",
                        "event_type": "inventory",
                        "action": "add",
                    }
                ],
                rollout_policy=shadow,
                changes_authority=True,
            )
        self.assertEqual("ITEM_STRICT_RUNTIME_DISABLED", caught.exception.code)
        self.assertEqual(0, legacy_report["event_count"])

    def test_service_shadow_proposal_records_diagnostics_and_keeps_grant(
        self,
    ) -> None:
        self.write_policy(strict=False, bridge=True)
        service = ContinuityService(self.root)
        host = HostApprovalAuthority(
            service,
            issuer="item-rollout-shadow-host",
            channel="interactive_test",
        )
        actor = service.register_entity("character", "影子差异使用者")[
            "entity_id"
        ]
        event = self.normalized_item(
            "item_spec",
            quote="定义一件仍处于影子校验阶段的物品。",
            action="define",
            spec_type="item_definition",
            spec_id="shadow-only-definition",
            definition={
                "item_kind": "miscellaneous",
                "stack_policy": "non_stackable",
                "uniqueness_policy": "ordinary",
            },
        )
        invalid_use = self.normalized_item(
            "item_use",
            quote="他试图使用一个尚未建立的候选物品实例。",
            ordinal=2,
            subject_type="item_instance",
            subject_id="missing-shadow-instance",
            item_instance_id="missing-shadow-instance",
            actor_entity_id=actor,
            function_id="missing-shadow-function",
            delta={},
        )
        proposal = service.save_proposal(
            events=[event, invalid_use],
            artifact_id="shadow-only-item",
            artifact_stage="final",
            branch_id="main",
            chapter_no=1,
            scene_index=0,
        )
        self.assertEqual("valid", proposal["validation_status"])
        self.assertIn(
            "ITEM_STRICT_RUNTIME_SHADOW_ONLY",
            {issue["code"] for issue in proposal["issues"]},
        )
        shadow_diagnostics = [
            issue
            for issue in proposal["issues"]
            if issue["code"] == "ITEM_SHADOW_DIAGNOSTIC"
        ]
        self.assertEqual(1, len(shadow_diagnostics))
        self.assertEqual(
            "ITEM_INSTANCE_NOT_FOUND",
            shadow_diagnostics[0]["details"]["source_code"],
        )
        revision = service.get_canon_revisions()["active"]
        grant = host.issue(
            str(proposal["proposal_id"]),
            expected_canon_revision=revision,
        )
        with self.assertRaises(ContinuityError) as caught:
            service.accept_proposal(
                str(proposal["proposal_id"]),
                approval_id=str(grant["approval_id"]),
                expected_canon_revision=revision,
            )
        self.assertEqual("ITEM_STRICT_RUNTIME_DISABLED", caught.exception.code)
        self.assertEqual(revision, service.get_canon_revisions()["active"])
        self.assertEqual(
            "proposed",
            service.inspect_proposal(str(proposal["proposal_id"]))[
                "canon_status"
            ],
        )
        grant_state = self.grant_row(service, str(proposal["proposal_id"]))
        self.assertIsNone(grant_state["consumed_request_hash"])
        self.assertIsNone(grant_state["accepted_commit_id"])

    def test_service_strict_true_accepts_v4_item_authority(self) -> None:
        event = self.normalized_item(
            "item_spec",
            quote="严格校验开启后定义一件普通物品。",
            action="define",
            spec_type="item_definition",
            spec_id="strict-definition",
            definition={
                "item_kind": "miscellaneous",
                "stack_policy": "non_stackable",
                "uniqueness_policy": "ordinary",
            },
        )
        proposal = self.service.save_proposal(
            events=[event],
            artifact_id="strict-item",
            artifact_stage="final",
            branch_id="main",
            chapter_no=1,
            scene_index=0,
        )
        self.assertNotIn(
            "ITEM_STRICT_RUNTIME_SHADOW_ONLY",
            {issue["code"] for issue in proposal["issues"]},
        )
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            str(proposal["proposal_id"]),
            expected_canon_revision=revision,
        )
        commit = self.service.accept_proposal(
            str(proposal["proposal_id"]),
            approval_id=str(grant["approval_id"]),
            expected_canon_revision=revision,
        )
        self.assertEqual(revision + 1, commit["active_canon_revision"])
        self.assertEqual(
            "strict-definition",
            self.service.query_item_definition("strict-definition")[
                "definition"
            ]["item_definition_id"],
        )

    def test_service_shadow_default_leaves_legacy_v3_inventory_unchanged(
        self,
    ) -> None:
        self.write_policy(strict=False, bridge=True)
        service = ContinuityService(self.root)
        host = HostApprovalAuthority(
            service,
            issuer="item-rollout-legacy-host",
            channel="interactive_test",
        )
        actor = service.register_entity("character", "旧库存持有人")[
            "entity_id"
        ]
        item = service.register_entity("item", "旧库存物品")["entity_id"]
        proposal = service.save_proposal(
            events=[
                {
                    "schema_version": "plot-rag-delta/v3",
                    "event_type": "inventory",
                    "action": "acquire",
                    "item_entity_id": item,
                    "to_owner_entity_id": actor,
                    "quantity": 1,
                    "unique": True,
                }
            ],
            artifact_id="legacy-v3-inventory",
            artifact_stage="final",
            branch_id="main",
            chapter_no=1,
            scene_index=0,
        )
        self.assertNotIn(
            "ITEM_STRICT_RUNTIME_SHADOW_ONLY",
            {issue["code"] for issue in proposal["issues"]},
        )
        revision = service.get_canon_revisions()["active"]
        grant = host.issue(
            str(proposal["proposal_id"]),
            expected_canon_revision=revision,
        )
        commit = service.accept_proposal(
            str(proposal["proposal_id"]),
            approval_id=str(grant["approval_id"]),
            expected_canon_revision=revision,
        )
        self.assertEqual(revision + 1, commit["active_canon_revision"])
        with service.store.read_connection() as connection:
            inventory = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM inventory_state
                    WHERE owner_entity_id=? AND item_entity_id=?
                    """,
                    (actor, item),
                )
            ]
        self.assertEqual(1, len(inventory))
        self.assertEqual(1.0, inventory[0]["quantity"])

    def test_shadow_non_authority_item_remains_diagnostic_only(self) -> None:
        item_event = self.normalized_item(
            "item_spec",
            quote="分支草稿定义一件候选物品。",
            action="define",
            spec_type="item_definition",
            spec_id="branch-only-definition",
            definition={
                "item_kind": "miscellaneous",
                "stack_policy": "non_stackable",
                "uniqueness_policy": "ordinary",
            },
        )
        with self.service.store.read_connection() as connection:
            report = assert_item_rollout_acceptance(
                connection,
                [item_event],
                rollout_policy=ItemRolloutPolicy(
                    strict_runtime_validation=False,
                    power_binding_bridge=True,
                ),
                changes_authority=False,
            )
        self.assertEqual("shadow", report["mode"])
        self.assertEqual("passed", report["status"])

    def test_bridge_disabled_blocks_definition_and_item_power_binding(
        self,
    ) -> None:
        actor = self.entity("character", "桥接使用者")
        item = self.entity("item", "桥接媒介")
        ability = self.entity("ability", "桥接能力")
        bridge_definition = self.normalized_item(
            "item_spec",
            quote="物品功能声明桥接既有能力。",
            action="define",
            spec_type="function_definition",
            spec_id="disabled-bridge-function",
            definition={
                "item_definition_id": "some-item-definition",
                "effect_owner": "ability_bridge",
                "granted_ability_ids": [ability],
                "inline_effects": [],
            },
        )
        power_binding = {
            "event_type": "power_binding",
            "actor_entity_id": actor,
            "binding_id": "disabled-item-binding",
            "source_entity_id": item,
            "action": "equip",
            "ability_entity_ids": [ability],
        }
        disabled = ItemRolloutPolicy(
            strict_runtime_validation=True,
            power_binding_bridge=False,
        )
        with self.service.store.read_connection() as connection:
            attempts = detect_item_ability_bridge_attempts(
                connection,
                [bridge_definition, power_binding],
            )
            with self.assertRaises(ContinuityError) as caught:
                assert_item_rollout_acceptance(
                    connection,
                    [bridge_definition, power_binding],
                    rollout_policy=disabled,
                    changes_authority=True,
                )
        self.assertEqual(
            {"function_definition", "item_power_binding"},
            {item["reason"] for item in attempts},
        )
        self.assertEqual(
            "ITEM_POWER_BINDING_BRIDGE_DISABLED",
            caught.exception.code,
        )

    def test_service_bridge_false_blocks_item_power_binding_before_consumption(
        self,
    ) -> None:
        self.write_policy(strict=True, bridge=False)
        service = ContinuityService(self.root)
        host = HostApprovalAuthority(
            service,
            issuer="item-rollout-bridge-off-host",
            channel="interactive_test",
        )
        actor = service.register_entity("character", "禁用桥接使用者")[
            "entity_id"
        ]
        item = service.register_entity("item", "禁用桥接媒介")["entity_id"]
        ability = service.register_entity("ability", "禁用桥接能力")[
            "entity_id"
        ]
        proposal = service.save_proposal(
            events=[
                {
                    "event_type": "power_binding",
                    "actor_entity_id": actor,
                    "binding_id": "bridge-disabled-binding",
                    "source_entity_id": item,
                    "action": "equip",
                    "ability_entity_ids": [ability],
                }
            ],
            artifact_id="bridge-disabled-power-binding",
            artifact_stage="final",
            branch_id="main",
            chapter_no=1,
            scene_index=0,
        )
        self.assertIn(
            "ITEM_POWER_BINDING_BRIDGE_DISABLED",
            {issue["code"] for issue in proposal["issues"]},
        )
        revision = service.get_canon_revisions()["active"]
        grant = host.issue(
            str(proposal["proposal_id"]),
            expected_canon_revision=revision,
        )
        with self.assertRaises(ContinuityError) as caught:
            service.accept_proposal(
                str(proposal["proposal_id"]),
                approval_id=str(grant["approval_id"]),
                expected_canon_revision=revision,
            )
        self.assertEqual(
            "ITEM_POWER_BINDING_BRIDGE_DISABLED",
            caught.exception.code,
        )
        grant_state = self.grant_row(service, str(proposal["proposal_id"]))
        self.assertIsNone(grant_state["consumed_request_hash"])
        with service.store.read_connection() as connection:
            binding_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM power_bindings
                WHERE binding_id='bridge-disabled-binding'
                """
            ).fetchone()[0]
        self.assertEqual(0, binding_count)

    def test_bridge_semantics_are_reducer_owned_in_shadow_and_strict_modes(
        self,
    ) -> None:
        item = self.entity("item", "重复效果媒介")
        ability = self.entity("ability", "既有能力定义")
        power_system = self.entity("power_system", "桥接测试体系")
        self.accept_events(
            [
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "power_system",
                    "spec_entity_id": power_system,
                    "definition": {"profile": "magic"},
                },
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "ability_definition",
                    "spec_entity_id": ability,
                    "definition": {
                        "system_entity_id": power_system,
                        "requirements": [],
                    },
                },
            ],
            artifact_id="item-rollout-power-spec",
            stage="bootstrap",
            proposal_kind="power_spec_change",
            operation="accept_power_spec",
        )
        definition = self.normalized_item(
            "item_spec",
            quote="定义承载能力的媒介。",
            action="define",
            spec_type="item_definition",
            spec_id="bridge-item-definition",
            definition={
                "item_entity_id": item,
                "item_kind": "weapon",
                "stack_policy": "non_stackable",
                "uniqueness_policy": "ordinary",
            },
        )
        duplicate_bridge = self.normalized_item(
            "item_spec",
            quote="桥接功能同时重复书写了能力效果。",
            action="define",
            spec_type="function_definition",
            spec_id="duplicate-bridge-function",
            definition={
                "item_definition_id": "bridge-item-definition",
                "effect_owner": "ability_bridge",
                "granted_ability_ids": [ability],
                "inline_effects": [{"kind": "duplicate"}],
            },
        )
        with self.service.store.read_connection() as connection:
            shadow = inspect_item_event_sequence(
                connection,
                [definition, duplicate_bridge],
                rollout_policy=ItemRolloutPolicy(
                    strict_runtime_validation=False,
                    power_binding_bridge=True,
                ),
            )
            with self.assertRaises(ContinuityError) as strict:
                validate_item_event_sequence(
                    connection,
                    [definition, duplicate_bridge],
                    rollout_policy=ItemRolloutPolicy(
                        strict_runtime_validation=True,
                        power_binding_bridge=True,
                    ),
                )
        self.assertEqual("differences", shadow["status"])
        self.assertEqual(
            "ITEM_ABILITY_BRIDGE_DUPLICATE",
            shadow["diagnostics"][0]["code"],
        )
        self.assertEqual(
            "ITEM_ABILITY_BRIDGE_DUPLICATE",
            strict.exception.code,
        )


if __name__ == "__main__":
    unittest.main()
