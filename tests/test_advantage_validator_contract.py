from __future__ import annotations

import copy
import unittest

from scripts.continuity.validators import (
    ContinuityError,
    normalize_event,
    validate_advantage_experience_contract_bindings,
)


def advantage_event(event_type: str, **fields: object) -> dict[str, object]:
    return {
        "schema_version": "plot-rag-advantage/v1",
        "event_type": event_type,
        "scope": "current",
        "branch_id": "main",
        "chapter_no": 1,
        "scene_index": 0,
        "story_coordinate": {
            "calendar_id": "validator-contract",
            "ordinal": 1,
        },
        "evidence": {"quote": "样例优势核心完成初始化。"},
        "knowledge_plane": "objective",
        "confidence": 1.0,
        "source_claim_ids": [],
        "advantage_id": "advantage-validator-contract",
        "experience_contract_id": "experience-validator-contract",
        "causal_provenance": {"source": "unit-test"},
        **fields,
    }


def normalized(event: dict[str, object]) -> dict[str, object]:
    return normalize_event(
        event,
        artifact_stage="bootstrap",
        branch_id="main",
        chapter_no=1,
        scene_index=0,
    )


class AdvantageValidatorLifecycleContractTests(unittest.TestCase):
    def test_activate_preserves_complete_bootstrap_runtime(self) -> None:
        cooldown = {
            "calendar_id": "validator-contract",
            "ordinal": 3,
        }
        runtime_metadata = {
            "source": "advantage-sidecar",
            "nested": {"mode": "bootstrap"},
        }
        resources = {
            "sample_resource": 1,
            "sample_resource_capacity": 3,
        }
        event = advantage_event(
            "advantage_activate",
            action="activate",
            owner_entity_id="actor-owner",
            stage="激活",
            charges=1,
            max_charges=3,
            resources=resources,
            pollution=0.1,
            exposure=0.2,
            debt=0.3,
            cooldown_until=cooldown,
            runtime_metadata=runtime_metadata,
        )

        result = normalized(event)

        self.assertEqual(3, result["max_charges"])
        self.assertEqual(resources, result["resources"])
        self.assertEqual(0.1, result["pollution"])
        self.assertEqual(0.2, result["exposure"])
        self.assertEqual(0.3, result["debt"])
        self.assertEqual(cooldown, result["cooldown_until"])
        self.assertEqual(runtime_metadata, result["runtime_metadata"])
        self.assertIsNot(resources, result["resources"])
        self.assertIsNot(runtime_metadata, result["runtime_metadata"])

    def test_record_only_reward_and_cost_preserve_ledger_payload(self) -> None:
        payload = {
            "record_only": True,
            "ledger_entry_kind": "bootstrap",
            "entry_id": "ledger-bootstrap-1",
            "input": {"sample_input": 1},
            "output": {"sample_resource": 1},
            "loss": {"heat": 0.1},
        }
        for event_type in ("advantage_reward", "advantage_cost"):
            with self.subTest(event_type=event_type):
                event = advantage_event(
                    event_type,
                    **copy.deepcopy(payload),
                )
                result = normalized(event)
                for field, value in payload.items():
                    self.assertEqual(value, result[field])

    def test_record_only_requires_boolean(self) -> None:
        for value in (1, 0, "true", [], {}):
            with self.subTest(value=value):
                event = advantage_event(
                    "advantage_reward",
                    record_only=value,
                    ledger_entry_kind="bootstrap",
                    entry_id="ledger-bootstrap-invalid",
                    input={},
                    output={},
                    loss={},
                )
                with self.assertRaises(ContinuityError) as caught:
                    normalized(event)
                self.assertEqual("INVALID_FIELD", caught.exception.code)

    def test_lifecycle_binding_checks_correction_replacement_leaf(self) -> None:
        replacement = advantage_event(
            "advantage_reveal",
            experience_contract_id=None,
            knowledge_id="knowledge-correction-leaf",
            knowledge_plane="objective",
            status="canon",
            claim={"text": "校正后的揭示。"},
            reveal_stage="first_reveal",
        )
        replacement.pop("experience_contract_id")
        wrapper = advantage_event(
            "advantage_correction",
            action="correct",
            target_event_id="event-original",
            replacement=replacement,
        )
        result = normalized(wrapper)

        with self.assertRaises(ContinuityError) as caught:
            validate_advantage_experience_contract_bindings(
                [result],
                required=True,
            )
        self.assertEqual(
            "ADVANTAGE_EXPERIENCE_CONTRACT_REQUIRED",
            caught.exception.code,
        )

    def test_lifecycle_binding_rejects_correction_contract_conflict(
        self,
    ) -> None:
        replacement = advantage_event(
            "advantage_reveal",
            experience_contract_id="experience-replacement",
            knowledge_id="knowledge-correction-conflict",
            knowledge_plane="objective",
            status="canon",
            claim={"text": "校正后的揭示。"},
            reveal_stage="first_reveal",
        )
        wrapper = advantage_event(
            "advantage_correction",
            experience_contract_id="experience-wrapper",
            action="correct",
            target_event_id="event-original",
            replacement=replacement,
        )
        result = normalized(wrapper)

        with self.assertRaises(ContinuityError) as caught:
            validate_advantage_experience_contract_bindings(
                [result],
                required=True,
                allowed_contract_ids=[
                    "experience-wrapper",
                    "experience-replacement",
                ],
            )
        self.assertEqual(
            "ADVANTAGE_EXPERIENCE_CONTRACT_MISMATCH",
            caught.exception.code,
        )

    def test_lifecycle_binding_validates_full_locked_contract_tuple(
        self,
    ) -> None:
        contract_id = "experience-validator-contract"
        contract_hash = "a" * 64
        binding = {
            "contract_id": contract_id,
            "contract_hash": contract_hash,
            "event_seed_id": "event-seed-validator",
            "event_seed_revision": 3,
        }
        event = advantage_event(
            "advantage_trigger",
            experience_contract_id=contract_id,
            experience_contract_hash=contract_hash,
            causal_provenance={
                "event_seed_id": "event-seed-validator",
                "event_seed_revision": 3,
            },
        )

        validated = validate_advantage_experience_contract_bindings(
            [event],
            required=True,
            allowed_contract_bindings=[binding],
        )

        self.assertEqual(1, validated["checked_advantage_event_count"])
        self.assertEqual(
            [contract_id],
            validated["bound_contract_ids"],
        )

        mismatches = (
            (
                "contract_hash",
                {
                    "experience_contract_hash": "b" * 64,
                },
            ),
            (
                "event_seed_id",
                {
                    "causal_provenance": {
                        "event_seed_id": "event-seed-foreign",
                        "event_seed_revision": 3,
                    },
                },
            ),
            (
                "event_seed_revision",
                {
                    "causal_provenance": {
                        "event_seed_id": "event-seed-validator",
                        "event_seed_revision": 4,
                    },
                },
            ),
            (
                "event_seed_revision_type",
                {
                    "causal_provenance": {
                        "event_seed_id": "event-seed-validator",
                        "event_seed_revision": 3.0,
                    },
                },
            ),
            (
                "frozen_snapshot",
                {
                    "experience_contract": {
                        "contract_id": contract_id,
                        "contract_hash": contract_hash,
                        "event_seed_id": "event-seed-foreign",
                        "event_seed_revision": 3,
                    },
                },
            ),
        )
        for label, patch in mismatches:
            with self.subTest(label=label):
                invalid = copy.deepcopy(event)
                invalid.update(copy.deepcopy(patch))
                with self.assertRaises(ContinuityError) as caught:
                    validate_advantage_experience_contract_bindings(
                        [invalid],
                        required=True,
                        allowed_contract_bindings=[binding],
                    )
                self.assertEqual(
                    "ADVANTAGE_EXPERIENCE_CONTRACT_MISMATCH",
                    caught.exception.code,
                )


if __name__ == "__main__":
    unittest.main()
