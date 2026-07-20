from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import state_rag
from scripts.continuity.validators import ContinuityError, normalize_event


def neutral_candidate(
    event_type: str,
    action: str,
    *,
    evidence: str,
    subject_kind: str,
    subject_mention: str,
    objects: list[dict[str, str]] | None = None,
    changes: dict[str, object] | None = None,
    knowledge_plane: str = "objective",
) -> dict[str, object]:
    return {
        "event_type": event_type,
        "action": action,
        "subject": {
            "kind": subject_kind,
            "mention": subject_mention,
        },
        "objects": list(objects or []),
        "changes": dict(changes or {}),
        "scope": "current",
        "story_coordinate": {
            "calendar_id": "production-chain",
            "ordinal": 7,
        },
        "knowledge_plane": knowledge_plane,
        "confidence": 1.0,
        "evidence": evidence,
    }


def item_event(event_type: str, **fields: object) -> dict[str, object]:
    return {
        "schema_version": "plot-rag-delta/v4",
        "event_type": event_type,
        "scope": "current",
        "branch_id": "main",
        "chapter_no": 1,
        "scene_index": 0,
        "story_coordinate": {
            "calendar_id": "production-chain",
            "ordinal": 7,
        },
        "knowledge_plane": "objective",
        "evidence": {"quote": "生产链测试证据。"},
        **fields,
    }


def normalized(event: dict[str, object]) -> dict[str, object]:
    return normalize_event(
        event,
        artifact_stage="final",
        branch_id="main",
        chapter_no=1,
        scene_index=0,
    )


class ItemV4ProductionChainTests(unittest.TestCase):
    def test_batch_adapter_keeps_valid_events_and_indexes_failures(self) -> None:
        valid_quote = "钥匙被定义为一次性通行凭证。"
        invalid_quote = "不存在物品受损，耐久下降1点。"
        candidates = [
            neutral_candidate(
                "item_spec",
                "define",
                evidence=valid_quote,
                subject_kind="item_definition",
                subject_mention="钥匙",
                changes={
                    "definition": {
                        "item_kind": "key",
                        "stack_policy": "non_stackable",
                        "uniqueness_policy": "ordinary",
                    }
                },
            ),
            neutral_candidate(
                "item_runtime",
                "damage",
                evidence=invalid_quote,
                subject_kind="item_instance",
                subject_mention="不存在物品",
                changes={"delta": {"durability": 1}},
            ),
        ]

        def resolver(
            mention: str,
            reference_type: str,
            role: str,
        ) -> dict[str, object]:
            if mention == "钥匙":
                return {
                    "status": "RESOLVED",
                    "reference_id": "itemdef-key",
                }
            return {
                "status": "UNRESOLVED",
                "candidates": [],
                "reference_type": reference_type,
                "role": role,
            }

        result = state_rag.adapt_item_extraction_candidates(
            candidates,
            valid_quote + invalid_quote,
            {
                "artifact_id": "artifact-production-chain",
                "branch_id": "main",
                "chapter_no": 1,
                "scene_index": 0,
            },
            resolver,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(2, result["candidate_count"])
        self.assertEqual(1, result["adapted_count"])
        self.assertEqual(["item_spec"], [
            event["event_type"] for event in result["events"]
        ])
        self.assertEqual(
            "ITEM_REFERENCE_UNRESOLVED",
            result["issues"][0]["code"],
        )
        self.assertEqual(
            1,
            result["issues"][0]["details"]["candidate_index"],
        )
        with self.assertRaises(state_rag.StateRagError):
            state_rag.adapt_item_extraction_candidates(
                "bad",  # type: ignore[arg-type]
                valid_quote,
                {},
                resolver,
            )

    def test_strict_delegate_honors_switches_and_uses_provisional_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = SimpleNamespace(
                root=root,
                version=3,
                enabled=True,
                auto_record=True,
                embedding=None,
                rerank=None,
                extract=None,
            )
            sentinel = {"status": "proposal_saved"}
            with (
                patch.object(
                    state_rag,
                    "_load_runtime_config",
                    return_value=config,
                ),
                patch.object(
                    state_rag,
                    "load_config",
                    return_value={"lifecycle": {"strict": True}},
                ),
                patch(
                    "scripts.v1_runtime.propose_plot_turn",
                    return_value=sentinel,
                ) as delegate,
            ):
                result = state_rag.commit_turn(
                    root,
                    "测试角色甲收起钥匙。",
                    session_id="session-production",
                    turn_id="turn-production",
                    prompt="推进剧情",
                )
            expected_request_id, _ = state_rag._request_identity(
                "",
                "session-production",
                "turn-production",
                "推进剧情",
                "测试角色甲收起钥匙。",
            )
            self.assertIs(sentinel, result)
            delegate.assert_called_once_with(
                root,
                "测试角色甲收起钥匙。",
                request_id=expected_request_id,
                session_id="session-production",
                turn_id="turn-production",
                prompt="推进剧情",
            )

            disabled = SimpleNamespace(
                **{**config.__dict__, "enabled": False}
            )
            with patch.object(
                state_rag,
                "_load_runtime_config",
                return_value=disabled,
            ):
                self.assertEqual(
                    "disabled",
                    state_rag.commit_turn(root, "文本")["status"],
                )

            no_record = SimpleNamespace(
                **{**config.__dict__, "auto_record": False}
            )
            with (
                patch.object(
                    state_rag,
                    "_load_runtime_config",
                    return_value=no_record,
                ),
                patch.object(
                    state_rag,
                    "load_config",
                    return_value={"lifecycle": {"strict": True}},
                ),
                patch("scripts.v1_runtime.propose_plot_turn") as delegate,
            ):
                skipped = state_rag.commit_turn(root, "文本")
            self.assertEqual("auto_record_disabled", skipped["reason"])
            delegate.assert_not_called()

    def test_action_contract_is_idempotent_and_belief_cannot_mutate_runtime(
        self,
    ) -> None:
        set_zero = item_event(
            "item_function_runtime",
            action="set_charges",
            subject_type="item_instance",
            subject_id="iteminst-key",
            item_instance_id="iteminst-key",
            function_id="itemfn-open",
            remaining_charges=0,
        )
        first = normalized(set_zero)
        second = normalized(first)
        self.assertEqual(first, second)
        self.assertEqual(0, second["remaining_charges"])

        invalid_action = {
            **set_zero,
            "action": "enable",
        }
        with self.assertRaises(ContinuityError) as caught:
            normalized(invalid_action)
        self.assertEqual(
            "ITEM_ACTION_FIELD_UNSUPPORTED",
            caught.exception.code,
        )

        belief_runtime = item_event(
            "item_runtime",
            action="damage",
            subject_type="item_instance",
            subject_id="iteminst-key",
            item_instance_id="iteminst-key",
            delta={"durability": 1},
            knowledge_plane="actor_belief",
        )
        with self.assertRaises(ContinuityError) as caught:
            normalized(belief_runtime)
        self.assertEqual(
            "ITEM_KNOWLEDGE_PLANE_REQUIRES_OBSERVATION",
            caught.exception.code,
        )

    def test_stack_binding_and_context_references_survive_adapter(self) -> None:
        ids = {
            "绑定-堆": "itembind-stack",
            "通行功能": "itemfn-pass",
            "信标堆": "itemstack-beacon",
            "钥匙": "iteminst-key",
            "测试角色甲": "actor-testactora",
            "闸门": "location-gate",
            "灵力": "resource-spirit",
            "旧城记录": "source-old-city",
        }
        calls: list[tuple[str, str, str]] = []

        def resolver(
            mention: str,
            reference_type: str,
            role: str,
        ) -> dict[str, object]:
            calls.append((mention, reference_type, role))
            return {
                "status": "RESOLVED",
                "reference_id": ids[mention],
            }

        binding_quote = "绑定-堆把通行功能绑定到信标堆。"
        binding = state_rag.adapt_item_extraction_candidate(
            neutral_candidate(
                "item_spec",
                "define",
                evidence=binding_quote,
                subject_kind="function_binding",
                subject_mention="绑定-堆",
                objects=[
                    {"role": "function", "mention": "通行功能"},
                    {"role": "item_stack", "mention": "信标堆"},
                ],
                changes={"definition": {"enabled": True}},
            ),
            binding_quote,
            {"branch_id": "main"},
            resolver,
        )
        self.assertTrue(binding["ok"])
        self.assertEqual(
            "itemstack-beacon",
            binding["event"]["definition"]["stack_id"],
        )
        self.assertIn(
            ("信标堆", "item_stack", "item_stack"),
            calls,
        )

        use_quote = "测试角色甲在闸门使用钥匙的通行功能消耗灵力。"
        use = state_rag.adapt_item_extraction_candidate(
            neutral_candidate(
                "item_use",
                "use",
                evidence=use_quote,
                subject_kind="item_instance",
                subject_mention="钥匙",
                objects=[
                    {"role": "actor", "mention": "测试角色甲"},
                    {"role": "function", "mention": "通行功能"},
                    {"role": "location", "mention": "闸门"},
                    {"role": "resource", "mention": "灵力"},
                ],
                changes={"delta": {}},
            ),
            use_quote,
            {"branch_id": "main"},
            resolver,
        )
        self.assertEqual(
            "location-gate",
            use["event"]["location_entity_id"],
        )
        self.assertEqual(
            "resource-spirit",
            use["event"]["resource_entity_id"],
        )

        observation_quote = "测试角色甲观察钥匙，来源是旧城记录。"
        observation = state_rag.adapt_item_extraction_candidate(
            neutral_candidate(
                "item_observation",
                "observe",
                evidence=observation_quote,
                subject_kind="item_instance",
                subject_mention="钥匙",
                objects=[
                    {"role": "observer", "mention": "测试角色甲"},
                    {"role": "source", "mention": "旧城记录"},
                ],
                changes={"observation": {"origin": "旧城"}},
                knowledge_plane="actor_belief",
            ),
            observation_quote,
            {"branch_id": "main"},
            resolver,
        )
        self.assertEqual(
            "source-old-city",
            observation["event"]["source_entity_id"],
        )

    def test_generic_item_correction_promotion_and_nested_replay_shape(
        self,
    ) -> None:
        replacement = item_event(
            "item_spec",
            action="define",
            spec_type="item_definition",
            spec_id="itemdef-key",
            item_definition_id="itemdef-key",
            definition={
                "item_kind": "key",
                "stack_policy": "non_stackable",
                "uniqueness_policy": "ordinary",
            },
        )
        promoted = normalized(
            {
                "event_type": "correction",
                "supersedes": ["event-old"],
                "replacement": replacement,
            }
        )
        self.assertEqual("item_correction", promoted["event_type"])
        self.assertEqual("event-old", promoted["target_event_id"])
        self.assertEqual("item_spec", promoted["replacement"]["event_type"])

        nested = normalized(
            {
                "event_type": "correction",
                "supersedes": ["event-correction-old"],
                "replacement": promoted,
            }
        )
        self.assertEqual("correction", nested["event_type"])
        self.assertEqual(
            "item_correction",
            nested["replacement"]["event_type"],
        )


if __name__ == "__main__":
    unittest.main()
