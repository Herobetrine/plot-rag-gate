from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.continuity import ContinuityService, HostApprovalAuthority
from scripts.continuity.validators import normalize_event
from scripts.plot_init import PlotInitError, PlotInitService
from scripts.plot_init.lifecycle_adapter import proposal_to_lifecycle_package
from scripts.plot_init.items import build_item_package
from tests.test_item_initialization import typed_seed


ITEM_EVENT_TYPES = {
    "item_spec",
    "item_instance",
    "item_custody",
    "item_runtime",
    "item_function_runtime",
    "item_observation",
}


def m0_seed() -> dict:
    seed = copy.deepcopy(typed_seed())
    seed["item_instances"][0].update(
        {
            "item_entity_id": "entity-key-instance",
            "instance_name": "测试角色甲的青铜钥匙",
            "serial_or_mark": "KEY-001",
            # A unique-definition item must carry an explicit uniqueness
            # assertion through the initialization -> lifecycle adapter.
            "unique": True,
            "provenance": {
                "acquired_from": "旧站遗物箱",
                "source_revision": 3,
            },
        }
    )
    seed["item_custody_bootstrap"][0]["custody_status"] = "possessed"
    seed["item_runtime_bootstrap"][0].update(
        {
            "max_durability": 12,
            "durability": 7,
            "max_energy": 6,
            "energy": 2,
            "sealed": False,
            "damaged": True,
            "destroyed": False,
            "active": True,
            "bound_actor": "测试角色甲",
            "state": {"mode": "寻门", "trace": 2},
            "story_coordinate": {
                "calendar_id": "calendar-key",
                "ordinal": 4,
                "label": "钥匙运行态初始化",
            },
        }
    )
    seed["item_function_runtime_bootstrap"][0].update(
        {
            "enabled": False,
            "unlock_state": "suppressed",
            "remaining_charges": 1,
            "cooldown_until": {
                "calendar_id": "calendar-key",
                "ordinal": 8,
                "label": "冷却结束",
            },
            "state": {"suppressed_by": "旧站封锁"},
            "story_coordinate": {
                "calendar_id": "calendar-key",
                "ordinal": 4,
                "label": "功能运行态初始化",
            },
        }
    )
    seed["item_observations"].append(
        {
            "observation_id": "itemobs-definition-key",
            "item_definition_id": "itemdef-key",
            "observation_action": "reveal",
            "knowledge_plane": "reader_disclosed",
            "confidence": 0.9,
            "observation": {
                "material_origin": "旧城铸造",
                "can_open": "封锁门",
            },
        }
    )
    seed["item_definitions"].append(
        {
            "item_definition_id": "itemdef-signal-dust",
            "name": "信标尘",
            "item_kind": "consumable",
            "stack_policy": "homogeneous",
            "uniqueness_policy": "ordinary",
        }
    )
    seed.setdefault("item_stacks", []).append(
        {
            "stack_id": "itemstack-signal-dust",
            "item_definition_id": "itemdef-signal-dust",
            "stack_name": "同批信标尘",
            "quantity": 6,
            "batch_properties": {"lot": "station-a"},
        }
    )
    seed["item_functions"].append(
        {
            "function_id": "itemfn-signal-flare",
            "item_definition_id": "itemdef-signal-dust",
            "name": "点燃信标",
            "function_kind": "signal",
            "activation_kind": "active",
            "effect_owner": "inline",
            "inline_effects": [{"effect": "emit_signal_flare"}],
            "charges": 5,
        }
    )
    seed["item_function_bindings"].append(
        {
            "binding_id": "itembind-stack-signal-flare",
            "stack_id": "itemstack-signal-dust",
            "function_id": "itemfn-signal-flare",
        }
    )
    seed["item_function_runtime_bootstrap"].append(
        {
            "stack_id": "itemstack-signal-dust",
            "function_id": "itemfn-signal-flare",
            "enabled": True,
            "unlock_state": "unlocked",
            "remaining_charges": 4,
            "cooldown_until": {
                "calendar_id": "calendar-key",
                "ordinal": 6,
                "label": "信标冷却结束",
            },
            "state": {"lot": "station-a", "primed": True},
            "story_coordinate": {
                "calendar_id": "calendar-key",
                "ordinal": 4,
                "label": "堆叠功能运行态初始化",
            },
        }
    )
    return seed


class ItemLifecycleM0Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.project = self.workspace / "novel"
        self.project.mkdir()
        runtime_root = self.project / ".plot-rag"
        runtime_root.mkdir()
        (runtime_root / "config.json").write_text(
            json.dumps(
                {
                    "items": {
                        "strict_runtime_validation": True,
                        "power_binding_bridge": True,
                    }
                }
            ),
            encoding="utf-8",
        )
        initializer = PlotInitService(self.workspace)
        started = initializer.start(
            project_root=self.project,
            mode="new",
            interaction_profile="deep",
            seed=m0_seed(),
            idempotency_key="item-m0-start",
        )
        self.frozen = initializer.propose(
            started["session_id"],
            expected_session_revision=started["session_revision"],
            idempotency_key="item-m0-propose",
        )["proposal"]
        self.lifecycle = proposal_to_lifecycle_package(self.frozen)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def item_events(self) -> list[dict]:
        return [
            event
            for event in self.lifecycle["events"]
            if event["event_type"] in ITEM_EVENT_TYPES
        ]

    def test_adapter_preserves_source_order_and_maps_the_complete_bootstrap(
        self,
    ) -> None:
        events = self.item_events()
        source_ordinals = [
            event["evidence"]["source_ordinal"] for event in events
        ]
        self.assertEqual(sorted(source_ordinals), source_ordinals)
        self.assertEqual(len(source_ordinals), len(set(source_ordinals)))

        instance = next(
            event
            for event in events
            if event["event_type"] == "item_instance"
            and event["subject_id"] == "iteminst-key"
        )
        self.assertEqual("entity-key-instance", instance["item_entity_id"])
        self.assertEqual("测试角色甲的青铜钥匙", instance["instance_name"])
        self.assertEqual("KEY-001", instance["serial_or_mark"])
        self.assertTrue(instance["unique"])
        self.assertEqual(
            "旧站遗物箱",
            instance["provenance"]["acquired_from"],
        )
        self.assertEqual(
            {"锈蚀": False, "旧编号": "A-17"},
            instance["attributes"],
        )

        custody = next(
            event
            for event in events
            if event["event_type"] == "item_custody"
        )
        self.assertEqual("possessed", custody["custody_status"])

        runtime = next(
            event
            for event in events
            if event["event_type"] == "item_runtime"
        )
        self.assertEqual("bootstrap", runtime["action"])
        self.assertEqual(7, runtime["durability"])
        self.assertEqual(12, runtime["max_durability"])
        self.assertEqual(2, runtime["energy"])
        self.assertEqual(6, runtime["max_energy"])
        self.assertTrue(runtime["damaged"])
        self.assertTrue(runtime["active"])
        self.assertEqual("腰间", runtime["slot_key"])
        self.assertEqual({"mode": "寻门", "trace": 2}, runtime["state"])

        function_runtime = next(
            event
            for event in events
            if event["event_type"] == "item_function_runtime"
            and event["subject_type"] == "item_instance"
        )
        self.assertEqual("bootstrap", function_runtime["action"])
        self.assertFalse(function_runtime["enabled"])
        self.assertEqual("suppressed", function_runtime["unlock_state"])
        self.assertEqual(1, function_runtime["remaining_charges"])
        self.assertEqual(
            8,
            function_runtime["cooldown_until"]["ordinal"],
        )
        self.assertEqual(
            {"suppressed_by": "旧站封锁"},
            function_runtime["state"],
        )
        self.assertFalse(
            any(
                event["event_type"] == "item_runtime"
                and event.get("action") == "consume"
                for event in events
            )
        )
        stack_function_runtime = next(
            event
            for event in events
            if event["event_type"] == "item_function_runtime"
            and event["subject_type"] == "item_stack"
        )
        self.assertEqual(
            "itemstack-signal-dust",
            stack_function_runtime["subject_id"],
        )
        self.assertEqual(
            "itemstack-signal-dust",
            stack_function_runtime["stack_id"],
        )
        self.assertNotIn("item_instance_id", stack_function_runtime)
        self.assertEqual(
            "itemfn-signal-flare",
            stack_function_runtime["function_id"],
        )
        self.assertTrue(stack_function_runtime["enabled"])
        self.assertEqual(
            4,
            stack_function_runtime["remaining_charges"],
        )

        definition_observation = next(
            event
            for event in events
            if event["event_type"] == "item_observation"
            and event["subject_type"] == "item_definition"
        )
        self.assertEqual("itemdef-key", definition_observation["subject_id"])
        self.assertEqual(
            "itemdef-key",
            definition_observation["item_definition_id"],
        )
        self.assertIsNone(definition_observation["observer_entity_id"])
        for event in events:
            normalized = normalize_event(
                event,
                artifact_stage="bootstrap",
                branch_id="main",
                chapter_no=None,
                scene_index=None,
            )
            self.assertEqual(event["event_type"], normalized["event_type"])

    def test_accepted_initialization_replays_bootstrap_without_destroying_instance(
        self,
    ) -> None:
        service = ContinuityService(self.project)
        host = HostApprovalAuthority(
            service,
            issuer="item-m0-initialization-test",
            channel="interactive_test",
        )
        saved = service.save_initialization_bundle(
            self.frozen,
            artifact_id=self.frozen["proposal_id"],
            idempotency_key="item-m0-save",
        )
        grant = host.issue(
            saved["proposal_id"],
            expected_canon_revision=0,
            operations=("accept_initialization",),
        )
        service.accept_proposal(
            saved["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=0,
        )

        projection = service.query_item_instance("iteminst-key")
        self.assertEqual("active", projection["instance"]["instance_status"])
        metadata = projection["instance"]["instance"]
        self.assertEqual("测试角色甲的青铜钥匙", metadata["instance_name"])
        self.assertEqual("KEY-001", metadata["serial_or_mark"])
        self.assertTrue(metadata["unique"])
        self.assertEqual(
            "旧站遗物箱",
            metadata["provenance"]["acquired_from"],
        )
        runtime = projection["runtime"]
        self.assertEqual(7.0, runtime["durability"])
        self.assertEqual(12.0, runtime["max_durability"])
        self.assertEqual(2.0, runtime["energy"])
        self.assertEqual(6.0, runtime["max_energy"])
        self.assertTrue(runtime["damaged"])
        self.assertTrue(runtime["active"])
        self.assertEqual("腰间", runtime["slot_key"])
        self.assertEqual("寻门", runtime["state"]["mode"])
        self.assertEqual("possessed", projection["custody"]["custody_status"])
        function_runtime = projection["function_runtime"][0]
        self.assertFalse(function_runtime["enabled"])
        self.assertEqual("suppressed", function_runtime["unlock_state"])
        self.assertEqual(1.0, function_runtime["remaining_charges"])
        self.assertEqual(
            8,
            function_runtime["cooldown_until"]["ordinal"],
        )
        with service.store.read_connection() as connection:
            stack_runtime = connection.execute(
                """
                SELECT * FROM item_stack_function_runtime_state
                WHERE stack_id=? AND function_id=?
                """,
                ("itemstack-signal-dust", "itemfn-signal-flare"),
            ).fetchone()
        self.assertIsNotNone(stack_runtime)
        self.assertEqual(1, stack_runtime["enabled"])
        self.assertEqual("unlocked", stack_runtime["unlock_state"])
        self.assertEqual(4.0, stack_runtime["remaining_charges"])
        self.assertEqual(
            6,
            json.loads(stack_runtime["cooldown_until_json"])["ordinal"],
        )
        self.assertEqual(
            "station-a",
            json.loads(stack_runtime["state_json"])["lot"],
        )

    def test_invalid_bootstrap_values_are_diagnosed_before_lifecycle(self) -> None:
        invalid_runtime = m0_seed()
        invalid_runtime["item_runtime_bootstrap"][0]["durability"] = "seven"
        with self.assertRaises(PlotInitError) as runtime_error:
            build_item_package(
                invalid_runtime,
                [],
                work_id="work-item-m0-invalid-runtime",
                source_initialization_schema_version="plot-rag-init/v1",
                source_snapshot_hash="a" * 64,
            )
        self.assertEqual(
            "ITEM_RUNTIME_VALUE_INVALID",
            runtime_error.exception.code,
        )
        self.assertEqual(
            "durability",
            runtime_error.exception.details["field"],
        )

        invalid_function = m0_seed()
        invalid_function["item_function_runtime_bootstrap"][0][
            "unlock_state"
        ] = "half-open"
        with self.assertRaises(PlotInitError) as function_error:
            build_item_package(
                invalid_function,
                [],
                work_id="work-item-m0-invalid-function-runtime",
                source_initialization_schema_version="plot-rag-init/v1",
                source_snapshot_hash="b" * 64,
            )
        self.assertEqual(
            "ITEM_FUNCTION_RUNTIME_VALUE_INVALID",
            function_error.exception.code,
        )


if __name__ == "__main__":
    unittest.main()
