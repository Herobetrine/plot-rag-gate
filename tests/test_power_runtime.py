from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
BENCHMARKS = PLUGIN_ROOT / "benchmarks"
if str(BENCHMARKS) not in sys.path:
    sys.path.insert(0, str(BENCHMARKS))

import run_longform_benchmark as benchmark_entry  # noqa: E402
import generate_power_fixtures as fixture_generator  # noqa: E402
import plot_rag_mcp as mcp  # noqa: E402
import plot_state as cli  # noqa: E402
import state_rag  # noqa: E402
import v1_runtime as v1  # noqa: E402
from continuity import ContinuityService, HostApprovalAuthority  # noqa: E402
from longform import benchmarking as power_benchmarking  # noqa: E402
from longform import (  # noqa: E402
    AuthorityIndex,
    AuthoritySource,
    ContextContractBuilder,
    LayeredMemoryStore,
    WebnovelMethodPack,
    validate_power_annotation_manifest,
)


POWER_FIXTURE = (
    PLUGIN_ROOT
    / "benchmarks"
    / "fixtures"
    / "power_system_annotations.v1.jsonl"
)


class PowerRuntimeTests(unittest.TestCase):
    def make_project(self, base: Path) -> Path:
        root = base / "novel"
        (root / ".plot-rag").mkdir(parents=True)
        for name in ("正文", "设定集", "剧情"):
            (root / name).mkdir()
        config = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v3.json").read_text(
                encoding="utf-8"
            )
        )
        # Power-runtime tests isolate typed delta and projection behavior.
        # Event-experience gating has its own suite and would otherwise stop
        # these fixtures before a Prepare receipt is created.
        config["event_experience"]["enabled"] = False
        (root / ".plot-rag" / "config.json").write_text(
            json.dumps(config, ensure_ascii=False),
            encoding="utf-8",
        )
        return root

    @staticmethod
    def accept(
        service: ContinuityService,
        host: HostApprovalAuthority,
        proposal: dict[str, object],
        *,
        operation: str = "accept",
    ) -> dict[str, object]:
        revision = service.get_canon_revisions()["active"]
        grant = host.issue(
            str(proposal["proposal_id"]),
            expected_canon_revision=revision,
            operations=(operation,),
        )
        return service.accept_proposal(
            str(proposal["proposal_id"]),
            approval_id=str(grant["approval_id"]),
            expected_canon_revision=revision,
        )

    def power_world(
        self,
        root: Path,
    ) -> tuple[ContinuityService, dict[str, str]]:
        service = ContinuityService(root)
        host = HostApprovalAuthority(
            service,
            issuer="power-runtime-test",
            channel="interactive_test",
        )
        ids = {
            "left": service.register_entity("character", "甲")["entity_id"],
            "right": service.register_entity("character", "乙")["entity_id"],
            "system": service.register_entity("power_system", "法术")[
                "entity_id"
            ],
            "track": service.register_entity("progression_track", "法师等级")[
                "entity_id"
            ],
            "rank": service.register_entity("rank_node", "一环")["entity_id"],
            "ability": service.register_entity("ability", "火球术")[
                "entity_id"
            ],
        }
        specification = service.save_proposal(
            events=[
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "power_system",
                    "spec_entity_id": ids["system"],
                    "definition": {
                        "profile": "magic",
                        "namespace": "runtime-test",
                    },
                },
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "progression_track",
                    "spec_entity_id": ids["track"],
                    "definition": {
                        "system_entity_id": ids["system"],
                        "track_kind": "ordered_rank",
                    },
                },
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "rank_node",
                    "spec_entity_id": ids["rank"],
                    "definition": {"track_entity_id": ids["track"]},
                },
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "ability_definition",
                    "spec_entity_id": ids["ability"],
                    "definition": {
                        "system_entity_id": ids["system"],
                        "requirements": [],
                    },
                },
            ],
            artifact_id="runtime-power-spec",
            artifact_stage="bootstrap",
            proposal_kind="power_spec_change",
        )
        self.accept(
            service,
            host,
            specification,
            operation="accept_power_spec",
        )
        runtime = service.save_proposal(
            events=[
                {
                    "event_type": "ability",
                    "owner_entity_id": ids["left"],
                    "ability_entity_id": ids["ability"],
                    "action": "gain",
                    "state": {"level": 1},
                },
                {
                    "event_type": "ability",
                    "owner_entity_id": ids["right"],
                    "ability_entity_id": ids["ability"],
                    "action": "gain",
                    "state": {"level": 1},
                },
                {
                    "event_type": "progression",
                    "actor_entity_id": ids["left"],
                    "track_entity_id": ids["track"],
                    "action": "initialize",
                    "to_rank_entity_id": ids["rank"],
                    "state": {"rank_entity_id": ids["rank"]},
                },
            ],
            artifact_id="runtime-power-state",
            artifact_stage="final",
            chapter_no=1,
            scene_index=0,
        )
        self.accept(service, host, runtime)
        return service, ids

    def test_typed_stop_v3_validation_and_mock_chat_extract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            config = state_rag._load_runtime_config(root)
            assistant = "甲获得了火球术。"
            envelope = {
                "schema_version": "plot-rag-delta/v3",
                "deltas": [
                    {
                        "event_type": "ability",
                        "action": "gain",
                        "subject": "甲",
                        "object": "火球术",
                        "field": "ability",
                        "value": {"level": 1},
                        "scope": "current",
                        "knowledge_plane": "objective",
                        "confidence": 0.99,
                        "evidence": assistant,
                    }
                ],
            }
            validated, skipped = state_rag._validate_deltas(
                envelope,
                assistant,
                config,
            )
            self.assertEqual([], skipped)
            self.assertEqual("ability", validated[0]["event_type"])
            response = {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                envelope,
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }
            with patch(
                "state_rag._remote_json",
                return_value=(response, {"status": "ok"}),
            ) as remote:
                extracted, skipped, status = state_rag._chat_extract(
                    config,
                    assistant,
                    "记录获得能力",
                    [],
                )
            self.assertEqual(validated, extracted)
            self.assertEqual([], skipped)
            self.assertEqual("ok", status["status"])
            extract_service = remote.call_args.args[0]
            system_prompt = remote.call_args.args[1]["messages"][0]["content"]
            self.assertEqual(
                "https://api.siliconflow.cn/v1",
                extract_service.base_url,
            )
            self.assertEqual(
                "SILICONFLOW_API_KEY",
                extract_service.api_key_env,
            )
            self.assertIn("plot-rag-delta/v3", system_prompt)
            self.assertIn(
                "movement=arrive|depart|enter|leave|move|teleport",
                system_prompt,
            )
            self.assertIn(
                "inventory=acquire|consume|lose|set|transfer",
                system_prompt,
            )
            self.assertIn(
                "never echo the event_type as the action",
                system_prompt,
            )
            self.assertIn(
                "active generated scene are current",
                system_prompt,
            )
            self.assertIn(
                "world_rule is only for durable world mechanics",
                system_prompt,
            )

    def test_v3_story_coordinate_ordinal_requires_exact_json_integer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            config = state_rag._load_runtime_config(root)
            assistant = "甲获得了火球术。"
            for invalid_ordinal in (True, 1.0, "1"):
                with self.subTest(value=repr(invalid_ordinal)):
                    envelope = {
                        "schema_version": "plot-rag-delta/v3",
                        "deltas": [
                            {
                                "event_type": "ability",
                                "action": "gain",
                                "subject": "甲",
                                "object": "火球术",
                                "field": "ability",
                                "value": {"level": 1},
                                "scope": "current",
                                "knowledge_plane": "objective",
                                "confidence": 0.99,
                                "evidence": assistant,
                                "story_coordinate": {
                                    "calendar_id": "chapter_scene",
                                    "ordinal": invalid_ordinal,
                                },
                            }
                        ],
                    }
                    with self.assertRaisesRegex(
                        state_rag.StateRagError,
                        r"story_coordinate\.ordinal is invalid",
                    ):
                        state_rag._validate_deltas(
                            envelope,
                            assistant,
                            config,
                        )

    def test_v3_movement_set_current_location_normalizes_to_arrive(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            config = state_rag._load_runtime_config(root)
            assistant = "测试角色甲当前位于测试城。"
            envelope = {
                "schema_version": "plot-rag-delta/v3",
                "deltas": [
                    {
                        "event_type": "movement",
                        "action": "set",
                        "subject": "测试角色甲",
                        "object": "测试城",
                        "field": "current",
                        "value": {},
                        "scope": "current",
                        "knowledge_plane": "objective",
                        "confidence": 0.99,
                        "evidence": assistant,
                    }
                ],
            }

            validated, skipped = state_rag._validate_deltas(
                envelope,
                assistant,
                config,
            )

        self.assertEqual([], skipped)
        self.assertEqual(1, len(validated))
        self.assertEqual("movement", validated[0]["event_type"])
        self.assertEqual("arrive", validated[0]["action"])
        self.assertEqual("测试角色甲", validated[0]["subject"])
        self.assertEqual("测试城", validated[0]["object"])

    def test_v3_movement_set_explicit_origin_normalizes_to_move(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            config = state_rag._load_runtime_config(root)
            assistant = "测试角色甲从旧港抵达测试城。"
            envelope = {
                "schema_version": "plot-rag-delta/v3",
                "deltas": [
                    {
                        "event_type": "movement",
                        "action": "set",
                        "subject": "测试角色甲",
                        "object": "测试城",
                        "field": "current",
                        "value": {"from_location": "旧港"},
                        "scope": "current",
                        "knowledge_plane": "objective",
                        "confidence": 0.99,
                        "evidence": assistant,
                    }
                ],
            }

            validated, skipped = state_rag._validate_deltas(
                envelope,
                assistant,
                config,
            )

        self.assertEqual([], skipped)
        self.assertEqual(1, len(validated))
        self.assertEqual("move", validated[0]["action"])
        self.assertEqual(
            {"from_location": "旧港"},
            validated[0]["value"],
        )

    def test_v3_movement_set_unsafe_contexts_remain_invalid(
        self,
    ) -> None:
        cases = {
            "ambiguous": {
                "assistant": "测试角色甲望向测试城。",
            },
            "other_actor_observation": {
                "assistant": "测试角色甲看见测试角色丙抵达测试城。",
            },
            "actor_belief": {
                "assistant": "测试角色甲已经抵达测试城。",
                "knowledge_plane": "actor_belief",
            },
            "planned": {
                "assistant": "测试角色甲已经抵达测试城。",
                "scope": "planned",
            },
            "historical": {
                "assistant": "测试角色甲已经抵达测试城。",
                "scope": "historical",
            },
            "not_arrived": {
                "assistant": "测试角色甲尚未抵达测试城。",
            },
            "future_day": {
                "assistant": "测试角色甲明日抵达测试城。",
            },
            "historical_day": {
                "assistant": "测试角色甲昨日抵达测试城。",
            },
            "no_longer_there": {
                "assistant": "测试角色甲不再停留在测试城。",
            },
        }
        for label, case in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temporary:
                root = self.make_project(Path(temporary))
                config = state_rag._load_runtime_config(root)
                assistant = str(case["assistant"])
                envelope = {
                    "schema_version": "plot-rag-delta/v3",
                    "deltas": [
                        {
                            "event_type": "movement",
                            "action": "set",
                            "subject": "测试角色甲",
                            "object": "测试城",
                            "field": "current",
                            "value": {},
                            "scope": case.get("scope", "current"),
                            "knowledge_plane": case.get(
                                "knowledge_plane",
                                "objective",
                            ),
                            "confidence": 0.99,
                            "evidence": assistant,
                        }
                    ],
                }

                with self.assertRaisesRegex(
                    state_rag.StateRagError,
                    r"action is unsupported for movement: set",
                ):
                    state_rag._validate_deltas(
                        envelope,
                        assistant,
                        config,
                    )

    def test_v3_move_requires_explicit_origin_in_same_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            config = state_rag._load_runtime_config(root)
            assistant = "测试角色甲抵达测试城。"
            envelope = {
                "schema_version": "plot-rag-delta/v3",
                "deltas": [
                    {
                        "event_type": "movement",
                        "action": "move",
                        "subject": "测试角色甲",
                        "object": "测试城",
                        "field": "current",
                        "value": {},
                        "scope": "current",
                        "knowledge_plane": "objective",
                        "confidence": 0.99,
                        "evidence": assistant,
                    }
                ],
            }

            with self.assertRaisesRegex(
                state_rag.StateRagError,
                "move requires an explicit same-evidence origin",
            ):
                state_rag._validate_deltas(
                    envelope,
                    assistant,
                    config,
                )

    def test_v3_time_label_only_coordinate_populates_value_and_effective_at(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            config = state_rag._load_runtime_config(root)
            assistant = "此刻是景历十二年三月初七夜。"
            label = "景历十二年三月初七夜"
            envelope = {
                "schema_version": "plot-rag-delta/v3",
                "deltas": [
                    {
                        "event_type": "time",
                        "action": "set",
                        "subject": "故事",
                        "object": None,
                        "field": "current",
                        "value": {},
                        "scope": "current",
                        "story_coordinate": {"label": label},
                        "knowledge_plane": "objective",
                        "confidence": 0.99,
                        "evidence": assistant,
                    }
                ],
            }

            validated, skipped = state_rag._validate_deltas(
                envelope,
                assistant,
                config,
            )

        self.assertEqual([], skipped)
        self.assertEqual(1, len(validated))
        self.assertEqual(label, validated[0]["value"])
        self.assertEqual(label, validated[0]["effective_at"])
        self.assertIsNone(validated[0]["story_coordinate"])

    def test_v3_empty_story_coordinate_normalizes_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            config = state_rag._load_runtime_config(root)
            assistant = "此刻是景历十二年三月初七夜。"
            envelope = {
                "schema_version": "plot-rag-delta/v3",
                "deltas": [
                    {
                        "event_type": "time",
                        "action": "set",
                        "subject": "故事",
                        "object": None,
                        "field": "current",
                        "value": "景历十二年三月初七夜",
                        "scope": "current",
                        "story_coordinate": {},
                        "knowledge_plane": "objective",
                        "confidence": 0.99,
                        "evidence": assistant,
                    }
                ],
            }

            validated, skipped = state_rag._validate_deltas(
                envelope,
                assistant,
                config,
            )

        self.assertEqual([], skipped)
        self.assertEqual(1, len(validated))
        self.assertIsNone(validated[0]["story_coordinate"])
        self.assertIsNone(validated[0]["effective_at"])

    def test_v3_complete_story_coordinate_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            config = state_rag._load_runtime_config(root)
            assistant = "此刻是景历十二年三月初七夜。"
            coordinate = {
                "calendar_id": "景历",
                "ordinal": 120307,
                "label": "景历十二年三月初七夜",
                "precision": "night",
                "source_event_id": "event:clock",
            }
            envelope = {
                "schema_version": "plot-rag-delta/v3",
                "deltas": [
                    {
                        "event_type": "time",
                        "action": "set",
                        "subject": "故事",
                        "object": None,
                        "field": "current",
                        "value": "景历十二年三月初七夜",
                        "scope": "current",
                        "story_coordinate": coordinate,
                        "knowledge_plane": "objective",
                        "confidence": 0.99,
                        "evidence": assistant,
                    }
                ],
            }

            validated, skipped = state_rag._validate_deltas(
                envelope,
                assistant,
                config,
            )

        self.assertEqual([], skipped)
        self.assertEqual(1, len(validated))
        self.assertEqual(coordinate, validated[0]["story_coordinate"])

    def test_v3_incomplete_structured_story_coordinates_fail_closed(
        self,
    ) -> None:
        cases = {
            "ordinal_only": {"ordinal": 12},
            "calendar_only": {"calendar_id": "景历"},
            "precision_only": {"precision": "night"},
            "source_event_only": {"source_event_id": "event:clock"},
        }
        for label, coordinate in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temporary:
                root = self.make_project(Path(temporary))
                config = state_rag._load_runtime_config(root)
                assistant = "此刻是景历十二年三月初七夜。"
                envelope = {
                    "schema_version": "plot-rag-delta/v3",
                    "deltas": [
                        {
                            "event_type": "time",
                            "action": "set",
                            "subject": "故事",
                            "object": None,
                            "field": "current",
                            "value": "景历十二年三月初七夜",
                            "scope": "current",
                            "story_coordinate": coordinate,
                            "knowledge_plane": "objective",
                            "confidence": 0.99,
                            "evidence": assistant,
                        }
                    ],
                }

                with self.assertRaisesRegex(
                    state_rag.StateRagError,
                    r"story_coordinate requires calendar_id and integer ordinal",
                ):
                    state_rag._validate_deltas(
                        envelope,
                        assistant,
                        config,
                    )

    def test_repaired_arrive_converts_to_v1_event_without_origin(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            config = state_rag._load_runtime_config(root)
            service = ContinuityService(root)
            host = HostApprovalAuthority(
                service,
                issuer="movement-runtime-test",
                channel="interactive_test",
            )
            actor = service.register_entity("character", "测试角色甲")["entity_id"]
            old_location = service.register_entity("location", "旧港")[
                "entity_id"
            ]
            seed = service.save_proposal(
                events=[
                    {
                        "event_type": "movement",
                        "actor_entity_id": actor,
                        "from_location_entity_id": None,
                        "to_location_entity_id": old_location,
                        "action": "arrive",
                    }
                ],
                artifact_id="movement-origin-seed",
                artifact_stage="final",
                chapter_no=1,
                scene_index=0,
            )
            self.accept(service, host, seed)
            self.assertEqual(
                old_location,
                v1._current_location(service, actor),
            )

            assistant = "测试角色甲当前位于测试城。"
            envelope = {
                "schema_version": "plot-rag-delta/v3",
                "deltas": [
                    {
                        "event_type": "movement",
                        "action": "set",
                        "subject": "测试角色甲",
                        "object": "测试城",
                        "field": "current",
                        "value": {},
                        "scope": "current",
                        "knowledge_plane": "objective",
                        "confidence": 0.99,
                        "evidence": assistant,
                    }
                ],
            }
            validated, skipped = state_rag._validate_deltas(
                envelope,
                assistant,
                config,
            )
            self.assertEqual([], skipped)
            events, issues = v1.legacy_deltas_to_events(
                service,
                validated,
                artifact_context={
                    "artifact_id": "movement-arrive-runtime",
                    "chapter_no": 2,
                    "scene_index": 0,
                },
                receipt_id="receipt-movement-arrive",
                assistant_hash="hash-movement-arrive",
            )

        self.assertEqual([], issues)
        self.assertEqual(1, len(events))
        self.assertEqual("arrive", events[0]["action"])
        self.assertIsNone(events[0]["from_location_entity_id"])

    def test_v3_stop_rejects_legacy_or_unknown_envelopes_without_story_writes(
        self,
    ) -> None:
        assistant = "测试角色甲突破至金丹境。"
        cases = {
            "legacy": {
                "deltas": [
                    {
                        "category": "character_state",
                        "subject": "测试角色甲",
                        "field": "realm",
                        "operation": "set",
                        "value": "金丹",
                        "confidence": 0.99,
                        "evidence": assistant,
                    }
                ]
            },
            "unknown_schema": {
                "schema_version": "plot-rag-delta/v999",
                "deltas": [],
            },
        }
        for label, envelope in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = self.make_project(Path(temporary))
                prepared = v1.prepare_plot_turn(
                    root,
                    "剧情推演：让测试角色甲突破境界",
                    request_id=f"strict-envelope-{label}",
                    session_id="strict-envelope-session",
                    turn_id=f"strict-envelope-{label}",
                )
                response = {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    envelope,
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
                with patch(
                    "state_rag._remote_json",
                    return_value=(response, {"status": "ok"}),
                ):
                    result = v1.propose_plot_turn(
                        root,
                        assistant,
                        request_id=prepared["receipt_id"],
                )

                self.assertEqual("failed", result["status"])
                self.assertIn("plot-rag-delta/v4", result["reason"])
                self.assertEqual([], result["proposal_events"])
                service = ContinuityService(root)
                self.assertEqual([], service.list_proposals())
                self.assertEqual(
                    {"head": 0, "active": 0},
                    service.get_canon_revisions(),
                )
                self.assertEqual([], service.query_facts()["facts"])
                with service.store.read_connection() as connection:
                    for table in (
                        "artifacts",
                        "proposals",
                        "canon_commits",
                        "continuity_events",
                        "state_events",
                        "current_facts",
                    ):
                        self.assertEqual(
                            0,
                            connection.execute(
                                f"SELECT COUNT(*) FROM {table}"
                            ).fetchone()[0],
                            table,
                        )

    def test_pre_v3_stop_keeps_legacy_envelope_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            config_path = root / ".plot-rag" / "config.json"
            config_payload = json.loads(config_path.read_text(encoding="utf-8"))
            config_payload["config_version"] = 2
            config_path.write_text(
                json.dumps(config_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            config = state_rag._load_runtime_config(root)
            assistant = "测试角色甲抵达测试城。"
            legacy = {
                "deltas": [
                    {
                        "category": "location",
                        "subject": "测试角色甲",
                        "field": "current",
                        "operation": "set",
                        "value": "测试城",
                        "confidence": 0.99,
                        "evidence": assistant,
                    }
                ]
            }
            validated, skipped = state_rag._validate_deltas(
                legacy,
                assistant,
                config,
            )
            self.assertEqual(2, config.version)
            self.assertEqual([], skipped)
            self.assertEqual("location", validated[0]["category"])
            self.assertNotIn("schema_version", validated[0])

    def test_all_seven_power_event_adapters_create_valid_proposal(self) -> None:
        records = [
            json.loads(line)
            for line in POWER_FIXTURE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        selected: dict[str, dict[str, object]] = {}
        for record in records:
            if record["case_kind"] != "zero_delta":
                selected.setdefault(str(record["expected_event_type"]), record)
        expected = {
            "ability",
            "progression",
            "resource",
            "status_effect",
            "power_binding",
            "qualification",
            "power_observation",
        }
        self.assertEqual(expected, set(selected))
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            config = state_rag._load_runtime_config(root)
            service = ContinuityService(root)
            deltas: list[dict[str, object]] = []
            for event_type in sorted(expected):
                record = selected[event_type]
                current, skipped = state_rag._validate_deltas(
                    record["stop_envelope"],
                    str(record["assistant_text"]),
                    config,
                )
                self.assertEqual([], skipped)
                deltas.extend(current)
            events, issues = v1.legacy_deltas_to_events(
                service,
                deltas,
                artifact_context={
                    "artifact_id": "seven-power-events",
                    "chapter_no": 1,
                    "scene_index": 0,
                },
                receipt_id="receipt-seven",
                assistant_hash="hash-seven",
            )
            self.assertEqual(expected, {event["event_type"] for event in events})
            proposal = service.save_proposal(
                events=events,
                issues=issues,
                artifact_id="seven-power-events",
                artifact_stage="final",
                chapter_no=1,
                scene_index=0,
            )
            self.assertEqual("valid", proposal["validation_status"])

    def test_runtime_queries_reverse_owner_planes_and_no_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            _, ids = self.power_world(root)
            systems = v1.list_power_systems(root)
            state = v1.query_power_state(root, ability_id=ids["ability"])
            path = v1.query_progression_path(
                root,
                entity_id=ids["left"],
                track_id=ids["track"],
            )
            explained = v1.explain_power_action(
                root,
                action_id="use",
                entity_id=ids["left"],
                ability_id=ids["ability"],
            )
            compared = v1.compare_power_conditions(
                root,
                left_entity_id=ids["left"],
                right_entity_id=ids["right"],
                conditions={"terrain": "open"},
            )
            self.assertEqual(1, len(systems["systems"]))
            self.assertEqual(
                {ids["left"], ids["right"]},
                {
                    item["owner_entity_id"]
                    for item in state["abilities"]
                },
            )
            self.assertIn("legal_edges", path)
            self.assertIn("decision", explained)
            self.assertIsNone(compared.get("winner"))
            self.assertNotIn("author_plan", state["knowledge_planes"])
            for section in (
                "abilities",
                "progression",
                "resources",
                "statuses",
                "bindings",
                "qualifications",
                "observations",
            ):
                self.assertTrue(
                    all(
                        item.get("knowledge_plane") != "author_plan"
                        for item in state.get(section, [])
                    )
                )

    def test_power_query_story_position_requires_exact_json_integers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            _, ids = self.power_world(root)
            calls = {
                "list_power_systems": (
                    lambda **position: v1.list_power_systems(
                        root,
                        **position,
                    )
                ),
                "query_power_state": lambda **position: v1.query_power_state(
                    root,
                    entity_id=ids["left"],
                    **position,
                ),
                "query_progression_path": (
                    lambda **position: v1.query_progression_path(
                        root,
                        entity_id=ids["left"],
                        track_id=ids["track"],
                        **position,
                    )
                ),
                "explain_power_action": (
                    lambda **position: v1.explain_power_action(
                        root,
                        action_id="use",
                        entity_id=ids["left"],
                        ability_id=ids["ability"],
                        **position,
                    )
                ),
                "compare_power_conditions": (
                    lambda **position: v1.compare_power_conditions(
                        root,
                        left_entity_id=ids["left"],
                        right_entity_id=ids["right"],
                        **position,
                    )
                ),
            }
            for name, call in calls.items():
                for field, invalid_values in (
                    ("chapter_no", (False, 1.0, "1")),
                    ("scene_index", (False, 0.0, "0")),
                ):
                    for invalid_value in invalid_values:
                        with self.subTest(
                            tool=name,
                            field=field,
                            value=repr(invalid_value),
                        ):
                            with self.assertRaises(
                                v1.ContinuityError
                            ) as invalid:
                                call(**{field: invalid_value})
                            self.assertEqual(
                                "INVALID_FIELD",
                                invalid.exception.code,
                            )

    def test_five_cli_power_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            conditions = root / "conditions.json"
            conditions.write_text('{"terrain":"open"}', encoding="utf-8")
            cases = [
                ("list_power_systems", ["power", "systems"]),
                (
                    "query_power_state",
                    ["power", "state", "--mention", "甲"],
                ),
                (
                    "query_progression_path",
                    ["power", "path", "--mention", "甲"],
                ),
                (
                    "explain_power_action",
                    [
                        "power",
                        "explain",
                        "--mention",
                        "甲",
                        "--action-id",
                        "use",
                    ],
                ),
                (
                    "compare_power_conditions",
                    [
                        "power",
                        "compare",
                        "--left-mention",
                        "甲",
                        "--right-mention",
                        "乙",
                        "--conditions-json",
                        str(conditions),
                    ],
                ),
            ]
            for name, arguments in cases:
                with self.subTest(name=name), patch.object(
                    cli.v1,
                    name,
                    return_value={"tool": name},
                ) as called:
                    parsed = cli._parser().parse_args(
                        [*arguments, "--project-root", str(root)]
                    )
                    result = cli._dispatch(parsed)
                    self.assertEqual(name, result["tool"])
                    called.assert_called_once()
            with (
                self.assertRaises(SystemExit),
                patch("plot_state.sys.stderr", io.StringIO()),
            ):
                cli._parser().parse_args(
                    [
                        "power",
                        "systems",
                        "--project-root",
                        str(root),
                        "--system-id",
                        "query-only",
                    ]
                )

    def test_compare_cli_accepts_inline_default_file_and_stdin_json(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            conditions_file = root / "conditions.json"
            conditions_file.write_text(
                '{"terrain":"file"}',
                encoding="utf-8",
            )
            cases = [
                (
                    "inline",
                    ["--conditions-json", '{"terrain":"inline"}'],
                    {"terrain": "inline"},
                    None,
                ),
                ("default", [], {}, None),
                (
                    "file",
                    ["--conditions-json", str(conditions_file)],
                    {"terrain": "file"},
                    None,
                ),
                (
                    "stdin",
                    ["--conditions-json", "-"],
                    {"terrain": "stdin"},
                    io.StringIO('{"terrain":"stdin"}'),
                ),
            ]
            for name, extra, expected, stream in cases:
                with self.subTest(name=name), patch.object(
                    cli.v1,
                    "compare_power_conditions",
                    return_value={"tool": "compare_power_conditions"},
                ) as called:
                    parsed = cli._parser().parse_args(
                        [
                            "power",
                            "compare",
                            "--project-root",
                            str(root),
                            "--left-mention",
                            "甲",
                            "--right-mention",
                            "乙",
                            *extra,
                        ]
                    )
                    if stream is None:
                        result = cli._dispatch(parsed)
                    else:
                        with patch("plot_state.sys.stdin", stream):
                            result = cli._dispatch(parsed)
                    self.assertEqual(
                        "compare_power_conditions",
                        result["tool"],
                    )
                    self.assertEqual(
                        expected,
                        called.call_args.kwargs["conditions"],
                    )

    def test_five_mcp_power_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            cases = [
                ("list_power_systems", {}),
                ("query_power_state", {"mention": "甲"}),
                ("query_progression_path", {"mention": "甲"}),
                (
                    "explain_power_action",
                    {"mention": "甲", "action_id": "use"},
                ),
                (
                    "compare_power_conditions",
                    {
                        "left_mention": "甲",
                        "right_mention": "乙",
                        "conditions": {"terrain": "open"},
                    },
                ),
            ]
            for name, arguments in cases:
                with self.subTest(name=name), patch.object(
                    v1,
                    name,
                    return_value={"tool": name},
                ) as called:
                    result = mcp._dispatch_tool(
                        name,
                        {"project_root": str(root), **arguments},
                    )
                    self.assertEqual(name, result["tool"])
                    called.assert_called_once()

    def test_power_memory_keeps_metadata_and_deduplicates_views(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            service = ContinuityService(root)
            host = HostApprovalAuthority(
                service,
                issuer="power-memory-test",
                channel="interactive_test",
            )
            actor = service.register_entity("character", "甲")["entity_id"]
            ability = service.register_entity("ability", "火球术")["entity_id"]
            proposal = service.save_proposal(
                events=[
                    {
                        "event_type": "ability",
                        "owner_entity_id": actor,
                        "ability_entity_id": ability,
                        "action": "gain",
                        "state": {
                            "level": 1,
                            "cooldown_until": {
                                "calendar_id": "main",
                                "ordinal": 2,
                            },
                        },
                        "knowledge_plane": "actor_belief",
                    }
                ],
                artifact_id="power-memory",
                artifact_stage="final",
                chapter_no=1,
                scene_index=0,
                payload={"assistant_text": "甲认为自己掌握了火球术。"},
            )
            commit = self.accept(service, host, proposal)
            frozen = service.inspect_proposal(str(proposal["proposal_id"]))
            payload = v1._commit_payload(service, commit, frozen)
            memory = LayeredMemoryStore(root / ".plot-rag" / "memory-test.sqlite3")
            projected = memory.project_accepted_commit(payload)
            rows = memory.query(
                "火球术",
                layers=("working",),
                branch_id="main",
                limit=50,
            )
            power_rows = [
                row
                for row in rows
                if row["metadata"].get("fact_type")
            ]
            semantic_keys = [
                row["metadata"]["semantic_key"] for row in power_rows
            ]
            self.assertEqual(len(semantic_keys), len(set(semantic_keys)))
            self.assertEqual(len(power_rows), projected["working"])
            self.assertTrue(power_rows)
            for row in power_rows:
                self.assertEqual(
                    "actor_belief",
                    row["metadata"]["knowledge_plane"],
                )
                self.assertEqual("current", row["metadata"]["scope"])
                self.assertTrue(row["metadata"]["source_event_id"])

    def test_mandatory_power_quota_preserves_accepted_canon(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            (root / "正文" / "第一章.md").write_text(
                "CANON_POWER_RULE：焚天火球在雨夜只能维持一次呼吸。",
                encoding="utf-8",
            )
            index = AuthorityIndex(root / ".plot-rag" / "quota-index.sqlite3")
            index.refresh(
                root,
                [
                    AuthoritySource(
                        glob="正文/**/*.md",
                        role="canon",
                        priority=100,
                        scope_policy="infer_and_review",
                        ingest_policy="include",
                    )
                ],
            )
            memory = LayeredMemoryStore(
                root / ".plot-rag" / "quota-memory.sqlite3"
            )
            for current in range(12):
                memory.add(
                    layer="working",
                    category="power_state",
                    content=(
                        f"焚天火球战斗状态{current}："
                        + "力量资源冷却限制" * 20
                    ),
                    source_commit_id="quota-commit",
                    canon_status="accepted",
                    scope="current",
                )
            contract = ContextContractBuilder(
                index,
                memory_store=memory,
            ).build(
                "推演焚天火球战斗，核对力量、资源、能力和冷却。",
                max_context_chars=420,
                category_quotas={
                    "accepted_authority": 1,
                    "current_state": 0,
                    "open_loop": 0,
                    "power_state": 6,
                },
            )
            self.assertTrue(contract["within_budget"])
            self.assertEqual(1, contract["accepted_authority_selected"])
            self.assertIn("CANON_POWER_RULE", contract["context_text"])
            self.assertLessEqual(contract["context_chars"], 420)

    def test_six_power_method_cards_are_reachable(self) -> None:
        pack = WebnovelMethodPack()
        expected = {
            "power_combat_conditions",
            "progression_transition_gate",
            "skill_tree_unlock_chain",
            "cooldown_recovery_debt",
            "hybrid_power_bridge",
            "power_social_consequence",
        }
        cards = {card["id"]: card for card in pack.cards}
        self.assertTrue(expected.issubset(cards))
        for card_id in expected:
            retrieved = pack.retrieve(
                cards[card_id]["title"],
                genre="all",
                artifact_stage="outline",
                task="outline",
                limit=len(pack.cards),
            )
            self.assertIn(card_id, {item["id"] for item in retrieved})

    def test_power_benchmark_validate_and_public_route(self) -> None:
        validation = validate_power_annotation_manifest(POWER_FIXTURE)
        self.assertEqual(360, validation["case_count"])
        self.assertEqual(60, validation["cross_system_dangerous_count"])
        self.assertEqual(
            hashlib.sha256(POWER_FIXTURE.read_bytes()).hexdigest(),
            validation["manifest_file_sha256"],
        )
        self.assertEqual(
            "plot-rag-power",
            benchmark_entry.validate_manifest(POWER_FIXTURE)["suite"],
        )
        with patch.object(
            benchmark_entry,
            "run_power_annotation_benchmark",
            return_value={"suite": "plot-rag-power", "status": "passed"},
        ) as standalone:
            routed = benchmark_entry.run_manifest(POWER_FIXTURE)
        self.assertEqual("plot-rag-power", routed["suite"])
        standalone.assert_called_once_with(POWER_FIXTURE)

        with patch.object(
            cli.v1,
            "run_longform_benchmark",
            return_value={"suite": "plot-rag-power", "status": "passed"},
        ) as cli_route:
            parsed = cli._parser().parse_args(
                [
                    "longform",
                    "benchmark",
                    "--manifest",
                    str(POWER_FIXTURE),
                ]
            )
            cli_result = cli._dispatch(parsed)
        self.assertEqual("plot-rag-power", cli_result["suite"])
        cli_route.assert_called_once_with(POWER_FIXTURE.resolve())

        with patch.object(
            v1,
            "run_longform_benchmark",
            return_value={"suite": "plot-rag-power", "status": "passed"},
        ) as mcp_route:
            mcp_result = mcp._dispatch_tool(
                "run_longform_benchmark",
                {"manifest_path": str(POWER_FIXTURE)},
            )
        self.assertEqual("plot-rag-power", mcp_result["suite"])
        mcp_route.assert_called_once_with(POWER_FIXTURE.resolve())

        result = v1.run_longform_benchmark(POWER_FIXTURE)
        self.assertEqual("plot-rag-power", result["suite"])
        self.assertEqual("passed", result["status"])
        self.assertTrue(result["quality_gate"]["passed"])
        metrics = result["result"]
        self.assertEqual(1.0, metrics["accepted_delta_precision"])
        self.assertEqual(1.0, metrics["accepted_delta_recall"])
        self.assertEqual(1.0, metrics["quarantine_recall"])
        self.assertEqual(1.0, metrics["zero_delta_accuracy"])
        self.assertEqual(1.0, metrics["typed_stop_coverage"])
        self.assertEqual(1.0, metrics["profile_mapping_accuracy"])
        self.assertEqual(360, metrics["profile_mapping_total"])
        self.assertEqual(360, metrics["profile_mapping_correct"])
        self.assertEqual([], metrics["profile_mapping_failures"])
        self.assertGreaterEqual(
            metrics["mandatory_context_recall"],
            0.98,
        )
        self.assertEqual(
            metrics["mandatory_context"]["required_fact_count"],
            metrics["mandatory_context"]["retrieved_fact_count"],
        )
        self.assertGreaterEqual(
            metrics["ability_availability_precision"],
            0.99,
        )
        self.assertEqual(
            1.0,
            metrics["ability_availability"][
                "ability_availability_accuracy"
            ],
        )
        self.assertEqual(0, metrics["hidden_knowledge_leaks"])
        self.assertEqual(
            metrics["hidden_knowledge"]["hidden_fact_count"],
            metrics["hidden_knowledge"][
                "explicit_author_plan_retrieval_count"
            ],
        )
        self.assertEqual(
            1.0,
            metrics["knowledge_plane_preservation"]["accuracy"],
        )
        self.assertIn(
            "ContinuityService",
            metrics["metric_sources"]["adapter_runtime_contract"],
        )
        self.assertTrue(metrics["replay"]["hash_stable"])
        self.assertTrue(metrics["replay"]["normalized_hash_stable"])
        self.assertEqual(
            metrics["replay"]["normalized_projection_hash_before"],
            metrics["replay"]["normalized_projection_hash_after_first"],
        )
        self.assertEqual(
            metrics["replay"]["normalized_projection_hash_before"],
            metrics["replay"]["normalized_projection_hash_after_second"],
        )
        self.assertIn(
            "CONTINUITY_POWER_TABLES",
            metrics["metric_sources"]["normalized_projection_hash"],
        )
        self.assertEqual(
            validation["cross_system_dangerous_count"],
            metrics["cross_system_interaction_block_count"],
        )
        setup = metrics["cross_system_setup"]
        self.assertEqual(60, setup["setup_cases"])
        self.assertEqual(12, setup["system_count"])
        self.assertEqual(120, setup["resource_count"])
        self.assertEqual(24, setup["qualification_definition_count"])
        self.assertEqual(0, setup["bridge_rule_count"])
        self.assertEqual(0, setup["conversion_rule_count"])
        self.assertTrue(setup["proposal_id"])
        self.assertTrue(setup["commit_id"])
        self.assertEqual(0, metrics["cross_system_unbridged_accepts"])
        self.assertGreaterEqual(
            metrics["dangerous_block_reason_counts"].get(
                "POWER_INTERACTION_UNKNOWN",
                0,
            ),
            60,
        )

    def test_power_manifest_version_requires_exact_json_integer(self) -> None:
        records = [
            json.loads(line)
            for line in POWER_FIXTURE.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        with tempfile.TemporaryDirectory() as temporary:
            tampered = Path(temporary) / "invalid-manifest-version.jsonl"
            for invalid_version in (True, 1.0, "1"):
                with self.subTest(value=repr(invalid_version)):
                    mutated_records = json.loads(
                        json.dumps(records, ensure_ascii=False)
                    )
                    mutated_records[0]["manifest_version"] = invalid_version
                    tampered.write_text(
                        "".join(
                            json.dumps(
                                record,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            + "\n"
                            for record in mutated_records
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        ValueError,
                        "power case 1 has unsupported manifest version",
                    ):
                        validate_power_annotation_manifest(tampered)

    def test_power_fixture_generator_is_byte_deterministic(self) -> None:
        generated = "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in fixture_generator.build_records()
        )
        checked_in = POWER_FIXTURE.read_text(encoding="utf-8")
        self.assertEqual(checked_in, generated)
        records = [
            json.loads(line)
            for line in generated.splitlines()
            if line.strip()
        ]
        self.assertEqual(360, len(records))
        self.assertTrue(all(record["profile_probe"] for record in records))
        self.assertEqual(
            60,
            sum(
                "cross_system_dangerous"
                in set(record.get("coverage_tags") or [])
                for record in records
            ),
        )

    def test_normalized_projection_hash_excludes_run_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            service = ContinuityService(root)
            service.schema_status()
            with service.store.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO legacy_power_imports(
                        import_key, owner_entity_id, ability_entity_id,
                        state_json, imported_event_id, provenance_json,
                        created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "import:test",
                        "entity:owner",
                        "entity:ability",
                        json.dumps(
                            {
                                "available": True,
                                "source_event_id": "event:first",
                            },
                            sort_keys=True,
                        ),
                        "event:first",
                        json.dumps(
                            {
                                "commit_id": "commit:first",
                                "run_id": "run:first",
                            },
                            sort_keys=True,
                        ),
                        "2026-01-01T00:00:00Z",
                    ),
                )
            first = power_benchmarking._normalized_power_projection_hash(
                service,
                state_rag.CONTINUITY_POWER_TABLES,
            )
            with service.store.transaction() as connection:
                connection.execute(
                    """
                    UPDATE legacy_power_imports
                    SET state_json=?, imported_event_id=?,
                        provenance_json=?, created_at=?
                    WHERE import_key='import:test'
                    """,
                    (
                        json.dumps(
                            {
                                "available": True,
                                "source_event_id": "event:second",
                            },
                            sort_keys=True,
                        ),
                        "event:second",
                        json.dumps(
                            {
                                "commit_id": "commit:second",
                                "run_id": "run:second",
                            },
                            sort_keys=True,
                        ),
                        "2026-07-16T00:00:00Z",
                    ),
                )
            second = power_benchmarking._normalized_power_projection_hash(
                service,
                state_rag.CONTINUITY_POWER_TABLES,
            )
            self.assertEqual(first, second)
            with service.store.transaction() as connection:
                connection.execute(
                    """
                    UPDATE legacy_power_imports
                    SET state_json=?
                    WHERE import_key='import:test'
                    """,
                    (
                        json.dumps(
                            {
                                "available": False,
                                "source_event_id": "event:third",
                            },
                            sort_keys=True,
                        ),
                    ),
                )
            third = power_benchmarking._normalized_power_projection_hash(
                service,
                state_rag.CONTINUITY_POWER_TABLES,
            )
            self.assertNotEqual(first, third)

    def test_cross_system_power_fixture_requires_empty_rule_setup(self) -> None:
        records = [
            json.loads(line)
            for line in POWER_FIXTURE.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        target = next(
            record
            for record in records
            if "cross_system_dangerous"
            in set(record.get("coverage_tags") or [])
        )
        target.pop("power_setup")
        with tempfile.TemporaryDirectory() as temporary:
            tampered = Path(temporary) / "tampered.jsonl"
            tampered.write_text(
                "".join(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                    for record in records
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError,
                "provide an executable power_setup",
            ):
                validate_power_annotation_manifest(tampered)

    def test_doctor_fails_when_any_power_table_is_missing(self) -> None:
        for table in sorted(state_rag.CONTINUITY_POWER_TABLES):
            with self.subTest(table=table), tempfile.TemporaryDirectory() as temporary:
                root = self.make_project(Path(temporary))
                service = ContinuityService(root)
                service.schema_status()
                with closing(sqlite3.connect(service.store.db_path)) as connection:
                    connection.execute(f'DROP TABLE "{table}"')
                    connection.commit()
                health = v1.doctor_v1(root)
                continuity = health["components"]["continuity"]
                self.assertEqual("failed", continuity["status"])
                self.assertIn(table, continuity["missing_tables"])
                legacy = state_rag.doctor(root)
                lifecycle = next(
                    item
                    for item in legacy["checks"]
                    if item["name"] == "continuity_lifecycle"
                )
                self.assertEqual("failed", lifecycle["status"])
                self.assertIn(table, lifecycle["missing_tables"])

    def test_redirect_and_shared_secret_policy_regression(self) -> None:
        service = state_rag.ServiceConfig(
            name="rerank",
            enabled=True,
            base_url="https://api.jina.ai/v1",
            model="fixture-rerank",
            api_key_env="SILICONFLOW_API_KEY",
            api_key_required=True,
            endpoint="rerank",
            timeout_seconds=1.0,
            max_tokens=100,
        )
        with patch.dict(
            os.environ,
            {"SILICONFLOW_API_KEY": "TEST_SHARED_PROVIDER_KEY"},
            clear=False,
        ):
            readiness = state_rag._service_readiness(service)
            self.assertFalse(readiness["url_policy_ok"])
            with self.assertRaisesRegex(
                state_rag.StateRagError,
                "restricted to api.siliconflow.cn",
            ):
                state_rag._remote_json(service, {"model": service.model})
        response = MagicMock()
        with self.assertRaisesRegex(
            state_rag.StateRagError,
            "redirects are blocked",
        ):
            state_rag._NoRedirectHandler().redirect_request(
                MagicMock(),
                response,
                302,
                "Found",
                {},
                "https://example.invalid/credential-sink",
            )
        response.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
