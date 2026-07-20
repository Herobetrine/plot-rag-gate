from __future__ import annotations

import unittest

from scripts.continuity.items import (
    ITEM_FUNCTION_RUNTIME_ACTIONS,
    ItemProjectionState,
)
from scripts.continuity.validators import ContinuityError


class ItemRuntimeM0Tests(unittest.TestCase):
    @staticmethod
    def coordinate(ordinal: int) -> dict[str, object]:
        return {"calendar_id": "m0-calendar", "ordinal": ordinal}

    def apply(
        self,
        state: ItemProjectionState,
        event: dict[str, object],
        *,
        order: int,
    ) -> None:
        state.apply(
            event,
            source_event_id=f"m0-event-{order}",
            updated_order=order,
        )

    def seed_instance(
        self,
        *,
        definition_id: str = "definition-relic",
        function_id: str = "function-flare",
        binding_id: str = "binding-flare",
        instance_id: str = "instance-relic",
        actor: str = "actor-owner",
        function: dict[str, object] | None = None,
        binding: dict[str, object] | None = None,
        uniqueness_policy: str = "ordinary",
    ) -> ItemProjectionState:
        state = ItemProjectionState()
        state.entity_types[actor] = "character"
        events: list[dict[str, object]] = [
            {
                "event_type": "item_spec",
                "action": "define",
                "spec_type": "item_definition",
                "spec_id": definition_id,
                "definition": {
                    "item_kind": "relic",
                    "stack_policy": "non_stackable",
                    "uniqueness_policy": uniqueness_policy,
                    "max_durability": 12,
                    "max_energy": 6,
                },
            },
            {
                "event_type": "item_spec",
                "action": "define",
                "spec_type": "function_definition",
                "spec_id": function_id,
                "definition": {
                    "item_definition_id": definition_id,
                    "effect_owner": "inline",
                    "inline_effects": [{"kind": "flare"}],
                    "activation_kind": "active",
                    "charges": 3,
                    **dict(function or {}),
                },
            },
            {
                "event_type": "item_instance",
                "action": "instantiate",
                "subject_type": "item_instance",
                "subject_id": instance_id,
                "item_instance_id": instance_id,
                "item_definition_id": definition_id,
                "story_coordinate": self.coordinate(1),
                "instance_name": "测试遗物",
                "serial_or_mark": f"SERIAL-{instance_id}",
                "unique": (
                    True
                    if uniqueness_policy
                    in {"unique_instance", "unique_definition"}
                    else "unknown"
                ),
                "attributes": {},
            },
            {
                "event_type": "item_spec",
                "action": "define",
                "spec_type": "function_binding",
                "spec_id": binding_id,
                "definition": {
                    "item_instance_id": instance_id,
                    "function_id": function_id,
                    **dict(binding or {}),
                },
            },
            {
                "event_type": "item_custody",
                "action": "acquire",
                "subject_type": "item_instance",
                "subject_id": instance_id,
                "item_instance_id": instance_id,
                "to_legal_owner_entity_id": actor,
                "to_custodian_entity_id": actor,
                "to_carrier_entity_id": actor,
                "custody_status": "possessed",
                "story_coordinate": self.coordinate(1),
            },
        ]
        for order, event in enumerate(events, start=1):
            self.apply(state, event, order=order)
        return state

    def seed_stack(self) -> ItemProjectionState:
        state = ItemProjectionState()
        state.entity_types["actor-stack-owner"] = "character"
        events: list[dict[str, object]] = [
            {
                "event_type": "item_spec",
                "action": "define",
                "spec_type": "item_definition",
                "spec_id": "definition-token",
                "definition": {
                    "item_kind": "consumable",
                    "stack_policy": "homogeneous",
                    "uniqueness_policy": "ordinary",
                },
            },
            {
                "event_type": "item_spec",
                "action": "define",
                "spec_type": "function_definition",
                "spec_id": "function-token-flare",
                "definition": {
                    "item_definition_id": "definition-token",
                    "effect_owner": "inline",
                    "inline_effects": [{"kind": "flare"}],
                    "charges": 10,
                    "cooldown": 2,
                },
            },
            {
                "event_type": "item_spec",
                "action": "define",
                "spec_type": "function_binding",
                "spec_id": "binding-token-flare",
                "definition": {
                    "item_definition_id": "definition-token",
                    "function_id": "function-token-flare",
                },
            },
            {
                "event_type": "item_instance",
                "action": "instantiate",
                "subject_type": "item_stack",
                "subject_id": "stack-token-a",
                "stack_id": "stack-token-a",
                "item_definition_id": "definition-token",
                "quantity": 10,
                "batch": {"lot": "same-lot"},
                "story_coordinate": self.coordinate(1),
            },
            {
                "event_type": "item_custody",
                "action": "acquire",
                "subject_type": "item_stack",
                "subject_id": "stack-token-a",
                "stack_id": "stack-token-a",
                "to_legal_owner_entity_id": "actor-stack-owner",
                "to_custodian_entity_id": "actor-stack-owner",
                "to_carrier_entity_id": "actor-stack-owner",
                "story_coordinate": self.coordinate(1),
            },
        ]
        for order, event in enumerate(events, start=1):
            self.apply(state, event, order=order)
        return state

    def test_function_runtime_bootstrap_is_exact_and_does_not_consume_item(
        self,
    ) -> None:
        state = self.seed_instance()
        self.assertEqual(
            {
                "bootstrap",
                "enable",
                "disable",
                "unlock",
                "lock",
                "suppress",
                "set_charges",
                "set_cooldown",
                "clear_cooldown",
            },
            set(ITEM_FUNCTION_RUNTIME_ACTIONS),
        )
        self.apply(
            state,
            {
                "event_type": "item_runtime",
                "action": "bootstrap",
                "subject_type": "item_instance",
                "subject_id": "instance-relic",
                "item_instance_id": "instance-relic",
                "durability": 7,
                "max_durability": 12,
                "energy": 2,
                "max_energy": 6,
                "sealed": False,
                "damaged": True,
                "destroyed": False,
                "active": True,
                "equipped_by_entity_id": "actor-owner",
                "slot_key": "waist",
                "bound_actor_entity_id": "actor-owner",
                "state": {"mode": "seek"},
                "story_coordinate": self.coordinate(2),
            },
            order=6,
        )
        self.apply(
            state,
            {
                "event_type": "item_function_runtime",
                "action": "bootstrap",
                "subject_type": "item_instance",
                "subject_id": "instance-relic",
                "item_instance_id": "instance-relic",
                "function_id": "function-flare",
                "enabled": False,
                "unlock_state": "suppressed",
                "remaining_charges": 1,
                "cooldown_until": self.coordinate(8),
                "state": {"suppressed_by": "seal"},
                "story_coordinate": self.coordinate(2),
            },
            order=7,
        )
        instance = state.instances["instance-relic"]
        runtime = state.runtime["instance-relic"]
        function_runtime = state.function_runtime[
            ("item_instance", "instance-relic", "function-flare")
        ]
        self.assertEqual("active", instance["instance_status"])
        self.assertFalse(runtime["destroyed"])
        self.assertEqual(7.0, runtime["durability"])
        self.assertEqual(2.0, runtime["energy"])
        self.assertTrue(runtime["active"])
        self.assertEqual("actor-owner", runtime["equipped_by_entity_id"])
        self.assertFalse(function_runtime["enabled"])
        self.assertEqual("suppressed", function_runtime["unlock_state"])
        self.assertEqual(1.0, function_runtime["remaining_charges"])
        self.assertEqual(
            self.coordinate(8),
            function_runtime["cooldown_until_json"],
        )

    def test_stack_runtime_use_split_and_merge_conserve_charges(self) -> None:
        state = self.seed_stack()
        self.apply(
            state,
            {
                "event_type": "item_function_runtime",
                "action": "bootstrap",
                "subject_type": "item_stack",
                "subject_id": "stack-token-a",
                "stack_id": "stack-token-a",
                "function_id": "function-token-flare",
                "enabled": True,
                "unlock_state": "unlocked",
                "remaining_charges": 8,
                "story_coordinate": self.coordinate(1),
            },
            order=6,
        )
        self.apply(
            state,
            {
                "event_type": "item_use",
                "action": "use",
                "subject_type": "item_stack",
                "subject_id": "stack-token-a",
                "stack_id": "stack-token-a",
                "actor_entity_id": "actor-stack-owner",
                "function_id": "function-token-flare",
                "delta": {},
                "story_coordinate": self.coordinate(2),
            },
            order=7,
        )
        self.apply(
            state,
            {
                "event_type": "item_instance",
                "action": "split",
                "source_stack_id": "stack-token-a",
                "target_stack_id": "stack-token-b",
                "quantity": 4,
                "target_batch": {"lot": "same-lot"},
                "story_coordinate": self.coordinate(3),
            },
            order=8,
        )
        source_runtime = state.function_runtime[
            ("item_stack", "stack-token-a", "function-token-flare")
        ]
        split_runtime = state.function_runtime[
            ("item_stack", "stack-token-b", "function-token-flare")
        ]
        self.assertAlmostEqual(4.2, source_runtime["remaining_charges"])
        self.assertAlmostEqual(2.8, split_runtime["remaining_charges"])
        self.assertEqual(
            self.coordinate(4),
            split_runtime["cooldown_until_json"],
        )
        self.apply(
            state,
            {
                "event_type": "item_instance",
                "action": "merge",
                "source_stack_id": "stack-token-b",
                "target_stack_id": "stack-token-a",
                "quantity": 4,
                "story_coordinate": self.coordinate(5),
            },
            order=9,
        )
        merged_runtime = state.function_runtime[
            ("item_stack", "stack-token-a", "function-token-flare")
        ]
        retired_runtime = state.function_runtime[
            ("item_stack", "stack-token-b", "function-token-flare")
        ]
        self.assertAlmostEqual(7.0, merged_runtime["remaining_charges"])
        self.assertEqual(0.0, retired_runtime["remaining_charges"])
        self.assertFalse(retired_runtime["enabled"])
        self.assertEqual("suppressed", retired_runtime["unlock_state"])
        self.assertEqual("merged", state.stacks["stack-token-b"]["stack_status"])

    def test_activation_binding_conditions_target_and_range_are_enforced(
        self,
    ) -> None:
        state = self.seed_instance(
            function={
                "activation_kind": "toggle",
                "targets": ["character"],
                "range": "same_location",
                "prerequisites": {
                    "qualification_entity_ids": ["qualification-a"]
                },
                "conditions": {
                    "qualification_entity_ids": ["qualification-b"],
                    "requires_active": True,
                    "requires_equipped": True,
                },
            },
            binding={
                "enabled": True,
                "conditions": {
                    "qualification_entity_ids": ["qualification-c"],
                    "location_entity_ids": ["location-shared"],
                },
            },
        )
        state.entity_types["actor-target"] = "character"
        state.locations["actor-owner"] = "location-shared"
        state.locations["actor-target"] = "location-shared"
        state.active_qualifications.update(
            {
                ("actor-owner", "qualification-a"),
                ("actor-owner", "qualification-b"),
                ("actor-owner", "qualification-c"),
            }
        )
        self.apply(
            state,
            {
                "event_type": "item_runtime",
                "action": "equip",
                "subject_type": "item_instance",
                "subject_id": "instance-relic",
                "item_instance_id": "instance-relic",
                "actor_entity_id": "actor-owner",
                "slot_key": "main_hand",
                "delta": {},
                "story_coordinate": self.coordinate(2),
            },
            order=6,
        )
        self.apply(
            state,
            {
                "event_type": "item_runtime",
                "action": "activate",
                "subject_type": "item_instance",
                "subject_id": "instance-relic",
                "item_instance_id": "instance-relic",
                "delta": {},
                "story_coordinate": self.coordinate(2),
            },
            order=7,
        )
        use = {
            "event_type": "item_use",
            "action": "use",
            "subject_type": "item_instance",
            "subject_id": "instance-relic",
            "item_instance_id": "instance-relic",
            "actor_entity_id": "actor-owner",
            "target_entity_id": "actor-target",
            "function_id": "function-flare",
            "delta": {"charges": 1},
            "story_coordinate": self.coordinate(3),
        }
        self.apply(state, use, order=8)
        state.locations["actor-target"] = "location-away"
        with self.assertRaises(ContinuityError) as range_error:
            self.apply(state, use, order=9)
        self.assertEqual("ITEM_RANGE_UNMET", range_error.exception.code)
        state.locations["actor-target"] = "location-shared"
        state.active_qualifications.remove(
            ("actor-owner", "qualification-c")
        )
        with self.assertRaises(ContinuityError) as qualification_error:
            self.apply(state, use, order=10)
        self.assertEqual(
            "ITEM_QUALIFICATION_UNMET",
            qualification_error.exception.code,
        )

    def test_retire_and_destroy_cleanup_runtime_custody_and_binding(self) -> None:
        retired = self.seed_instance()
        self.apply(
            retired,
            {
                "event_type": "item_runtime",
                "action": "equip",
                "subject_type": "item_instance",
                "subject_id": "instance-relic",
                "item_instance_id": "instance-relic",
                "actor_entity_id": "actor-owner",
                "slot_key": "main_hand",
                "story_coordinate": self.coordinate(2),
            },
            order=6,
        )
        self.apply(
            retired,
            {
                "event_type": "item_instance",
                "action": "retire",
                "subject_type": "item_instance",
                "subject_id": "instance-relic",
                "item_instance_id": "instance-relic",
                "story_coordinate": self.coordinate(3),
            },
            order=7,
        )
        self.assertEqual(
            "retired",
            retired.instances["instance-relic"]["instance_status"],
        )
        self.assertNotIn(
            ("item_instance", "instance-relic"), retired.custody
        )
        self.assertIsNone(
            retired.runtime["instance-relic"]["equipped_by_entity_id"]
        )
        runtime = retired.function_runtime[
            ("item_instance", "instance-relic", "function-flare")
        ]
        self.assertFalse(runtime["enabled"])
        self.assertEqual("suppressed", runtime["unlock_state"])
        self.assertEqual(
            "deprecated",
            retired.bindings["binding-flare"]["binding_status"],
        )

        destroyed = self.seed_instance(instance_id="instance-destroyed")
        self.apply(
            destroyed,
            {
                "event_type": "item_runtime",
                "action": "destroy",
                "subject_type": "item_instance",
                "subject_id": "instance-destroyed",
                "item_instance_id": "instance-destroyed",
                "story_coordinate": self.coordinate(3),
            },
            order=6,
        )
        self.assertEqual(
            "destroyed",
            destroyed.instances["instance-destroyed"]["instance_status"],
        )
        self.assertTrue(
            destroyed.runtime["instance-destroyed"]["destroyed"]
        )
        self.assertNotIn(
            ("item_instance", "instance-destroyed"), destroyed.custody
        )

    def test_preflight_rejects_bad_numbers_enums_and_duplicate_identity(
        self,
    ) -> None:
        invalid_number = ItemProjectionState()
        with self.assertRaises(ContinuityError) as number_error:
            self.apply(
                invalid_number,
                {
                    "event_type": "item_spec",
                    "action": "define",
                    "spec_type": "item_definition",
                    "spec_id": "definition-invalid-number",
                    "definition": {
                        "item_kind": "relic",
                        "stack_policy": "non_stackable",
                        "uniqueness_policy": "ordinary",
                        "max_energy": True,
                    },
                },
                order=1,
            )
        self.assertEqual("ITEM_INVALID_NUMBER", number_error.exception.code)

        invalid_enum = ItemProjectionState()
        with self.assertRaises(ContinuityError) as enum_error:
            self.apply(
                invalid_enum,
                {
                    "event_type": "item_spec",
                    "action": "define",
                    "spec_type": "item_definition",
                    "spec_id": "definition-invalid-enum",
                    "definition": {
                        "item_kind": "relic",
                        "stack_policy": "telepathic",
                        "uniqueness_policy": "ordinary",
                    },
                },
                order=1,
            )
        self.assertEqual("ITEM_INVALID_ENUM", enum_error.exception.code)

        state = self.seed_instance()
        with self.assertRaises(ContinuityError) as binding_error:
            self.apply(
                state,
                {
                    "event_type": "item_spec",
                    "action": "define",
                    "spec_type": "function_binding",
                    "spec_id": "binding-duplicate",
                    "definition": {
                        "item_instance_id": "instance-relic",
                        "function_id": "function-flare",
                    },
                },
                order=6,
            )
        self.assertEqual(
            "ITEM_BINDING_UNIQUENESS_CONFLICT",
            binding_error.exception.code,
        )
        with self.assertRaises(ContinuityError) as serial_error:
            self.apply(
                state,
                {
                    "event_type": "item_instance",
                    "action": "instantiate",
                    "subject_type": "item_instance",
                    "subject_id": "instance-relic-copy",
                    "item_instance_id": "instance-relic-copy",
                    "item_definition_id": "definition-relic",
                    "serial_or_mark": "SERIAL-instance-relic",
                    "unique": "unknown",
                    "attributes": {},
                    "story_coordinate": self.coordinate(2),
                },
                order=7,
            )
        self.assertEqual(
            "ITEM_SERIAL_UNIQUENESS_CONFLICT",
            serial_error.exception.code,
        )

    def test_definition_and_anonymous_observation_rules(self) -> None:
        state = self.seed_instance()
        self.apply(
            state,
            {
                "event_type": "item_observation",
                "action": "reveal",
                "subject_type": "item_definition",
                "subject_id": "definition-relic",
                "item_definition_id": "definition-relic",
                "observer_entity_id": None,
                "knowledge_plane": "reader_disclosed",
                "observation": {"origin": "old-city"},
                "story_coordinate": self.coordinate(2),
            },
            order=6,
        )
        row = next(iter(state.observations.values()))
        self.assertEqual("item_definition", row["subject_type"])
        self.assertIsNone(row["observer_entity_id"])
        with self.assertRaises(ContinuityError) as observer_error:
            self.apply(
                state,
                {
                    "event_type": "item_observation",
                    "action": "observe",
                    "subject_type": "item_instance",
                    "subject_id": "instance-relic",
                    "item_instance_id": "instance-relic",
                    "observer_entity_id": None,
                    "knowledge_plane": "actor_belief",
                    "observation": {"state": "warm"},
                    "story_coordinate": self.coordinate(2),
                },
                order=7,
            )
        self.assertEqual(
            "ITEM_OBSERVER_REQUIRED",
            observer_error.exception.code,
        )


if __name__ == "__main__":
    unittest.main()
