from __future__ import annotations

import copy
import unittest
from typing import Any

from scripts import state_rag
from scripts import v1_runtime
from scripts.continuity.validators import (
    ContinuityError,
    validate_advantage_experience_contract_bindings,
)


def contract(
    order: int,
    *,
    suffix: str,
) -> dict[str, Any]:
    return {
        "dependency_order": order,
        "contract_id": f"experience-{suffix}",
        "contract_hash": f"{order}" * 64,
        "event_seed_id": f"event-seed-{suffix}",
        "event_seed_revision": order,
    }


def trigger_candidate(
    *,
    ordinal: int,
    evidence: str,
) -> dict[str, Any]:
    return {
        "event_type": "advantage_trigger",
        "action": "trigger",
        "subject": {"kind": "advantage", "mention": "示例核心"},
        "objects": [{"role": "module", "mention": "状态解析"}],
        "changes": {"effects": ["辨明异常能量"]},
        "scope": "current",
        "story_coordinate": {
            "calendar_id": "story-main",
            "ordinal": ordinal,
        },
        "knowledge_plane": "objective",
        "confidence": 1.0,
        "evidence": evidence,
    }


def resolver(
    mention: str,
    reference_type: str,
    _role: str,
) -> dict[str, Any]:
    identities = {
        ("示例核心", "advantage"): {
            "advantage_id": "advantage-sample-core"
        },
        ("状态解析", "advantage_module"): {"module_id": "module-discern"},
        ("旧触发事件", "advantage_event"): {
            "event_id": "event-old-trigger",
            "advantage_id": "advantage-sample-core",
        },
    }
    return identities.get(
        (mention, reference_type),
        {
            "status": "UNRESOLVED",
            "mention": mention,
            "reference_type": reference_type,
        },
    )


class AdvantageEventExperienceBindingTests(unittest.TestCase):
    def test_batch_adapter_injects_tuple_into_events_and_correction_leaf(
        self,
    ) -> None:
        first_quote = "示例核心的状态解析模块在第一刻触发并辨明异常能量。"
        correction_quote = "第二刻校正旧触发事件。"
        replacement_quote = "示例核心的状态解析模块在第二刻重新触发并辨明异常能量。"
        replacement = trigger_candidate(
            ordinal=2,
            evidence=replacement_quote,
        )
        correction = {
            "event_type": "advantage_correction",
            "action": "correct",
            "subject": {
                "kind": "advantage_event",
                "mention": "旧触发事件",
            },
            "objects": [
                {"role": "target_event", "mention": "旧触发事件"},
            ],
            "changes": {"replacement": replacement},
            "scope": "current",
            "story_coordinate": {
                "calendar_id": "story-main",
                "ordinal": 2,
            },
            "knowledge_plane": "objective",
            "confidence": 1.0,
            "evidence": correction_quote,
        }
        candidates = [
            trigger_candidate(ordinal=1, evidence=first_quote),
            correction,
        ]
        manifest = {
            "contracts": [
                contract(2, suffix="second"),
                contract(1, suffix="first"),
            ]
        }
        bindings = v1_runtime._advantage_experience_bindings(
            candidates,
            manifest,
        )

        result = state_rag.adapt_advantage_extraction_candidates(
            candidates,
            first_quote + correction_quote + replacement_quote,
            {
                "artifact_stage": "final",
                "branch_id": "main",
                "chapter_no": 1,
                "scene_index": 0,
                "advantage_experience_required": True,
                "advantage_experience_bindings": bindings,
            },
            resolver,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(2, result["adapted_count"])
        first_event, correction_event = result["events"]
        replacement_event = correction_event["replacement"]
        self.assertEqual("experience-first", first_event["experience_contract_id"])
        self.assertEqual("1" * 64, first_event["experience_contract_hash"])
        self.assertEqual(
            {
                "event_seed_id": "event-seed-first",
                "event_seed_revision": 1,
            },
            first_event["causal_provenance"],
        )
        for event in (correction_event, replacement_event):
            self.assertEqual("experience-second", event["experience_contract_id"])
            self.assertEqual("2" * 64, event["experience_contract_hash"])
            self.assertEqual(
                {
                    "event_seed_id": "event-seed-second",
                    "event_seed_revision": 2,
                },
                event["causal_provenance"],
            )
        validate_advantage_experience_contract_bindings(
            result["events"],
            required=True,
            allowed_contract_bindings=manifest["contracts"],
        )

        tampered = copy.deepcopy(correction_event)
        tampered["replacement"]["causal_provenance"][
            "event_seed_id"
        ] = "event-seed-foreign"
        with self.assertRaises(ContinuityError) as caught:
            validate_advantage_experience_contract_bindings(
                [tampered],
                required=True,
                allowed_contract_bindings=manifest["contracts"],
            )
        self.assertEqual(
            "ADVANTAGE_EXPERIENCE_CONTRACT_MISMATCH",
            caught.exception.code,
        )

    def test_multi_contract_manifest_identity_errors_fail_closed(self) -> None:
        candidates = [
            trigger_candidate(ordinal=1, evidence="示例核心的状态解析模块触发。"),
            trigger_candidate(ordinal=2, evidence="示例核心的状态解析模块再次触发。"),
        ]
        cases = (
            (
                "duplicate-order",
                {
                    "contracts": [
                        contract(1, suffix="first"),
                        {
                            **contract(2, suffix="second"),
                            "dependency_order": 1,
                        },
                    ]
                },
                "ADVANTAGE_EXPERIENCE_BINDING_AMBIGUOUS",
            ),
            (
                "missing-seed",
                {
                    "contracts": [
                        contract(1, suffix="first"),
                        {
                            **contract(2, suffix="second"),
                            "event_seed_id": "",
                        },
                    ]
                },
                "ADVANTAGE_EXPERIENCE_BINDING_UNRESOLVED",
            ),
        )
        for label, manifest, expected_code in cases:
            with self.subTest(label=label):
                with self.assertRaises(ContinuityError) as caught:
                    v1_runtime._advantage_experience_bindings(
                        candidates,
                        manifest,
                    )
                self.assertEqual(expected_code, caught.exception.code)

    def test_batch_adapter_marks_missing_candidate_binding_as_error(
        self,
    ) -> None:
        first_quote = "示例核心的状态解析模块触发。"
        second_quote = "示例核心的状态解析模块再次触发。"
        result = state_rag.adapt_advantage_extraction_candidates(
            [
                trigger_candidate(ordinal=1, evidence=first_quote),
                trigger_candidate(ordinal=2, evidence=second_quote),
            ],
            first_quote + second_quote,
            {
                "artifact_stage": "final",
                "branch_id": "main",
                "chapter_no": 1,
                "scene_index": 0,
                "advantage_experience_required": True,
                "advantage_experience_bindings": {
                    0: {
                        "experience_contract_id": "experience-first",
                        "experience_contract_hash": "1" * 64,
                        "event_seed_id": "event-seed-first",
                        "event_seed_revision": 1,
                    }
                },
            },
            resolver,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(1, result["adapted_count"])
        issue = next(
            item
            for item in result["issues"]
            if item["code"] == "ADVANTAGE_EXPERIENCE_CONTRACT_REQUIRED"
        )
        self.assertEqual(1, issue["details"]["candidate_index"])
        self.assertEqual(
            "experience_binding",
            issue["details"]["adapter_stage"],
        )


if __name__ == "__main__":
    unittest.main()
