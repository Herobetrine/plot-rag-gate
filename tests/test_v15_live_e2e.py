from __future__ import annotations

import contextlib
import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from benchmarks.v15_live_e2e import (
    CHAT_EXTRACTION_SMOKE_SCHEMA,
    PROVENANCE_SCHEMA,
    REPORT_SCHEMA,
    RemoteCallRecorder,
    StrictChainFailure,
    collect_benchmark_provenance,
    compare_tree_snapshots,
    load_jsonl_annotations,
    load_prompt_fixture,
    run_live_chat_extraction_smoke,
    run_v15_live_e2e,
    scan_text_for_credentials,
    tree_snapshot,
    write_redacted_report,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "benchmarks" / "fixtures"


class V15LiveE2ETests(unittest.TestCase):
    def make_project(self, root: Path) -> Path:
        project = root / "novel"
        (project / ".plot-rag").mkdir(parents=True)
        (project / "正文").mkdir()
        (project / "设定集").mkdir()
        (project / "剧情").mkdir()
        (project / ".plot-rag" / "config.json").write_text(
            (ROOT / "templates" / "config.v3.json").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )
        (project / "正文" / "第一章.md").write_text(
            "测试角色甲位于测试城南站，正被监管机构追查。\n",
            encoding="utf-8",
        )
        (project / "设定集" / "世界.md").write_text(
            "跨层通行只能乘坐专用列车，车站会校验身份与货物。\n",
            encoding="utf-8",
        )
        (project / "剧情" / "伏笔.md").write_text(
            "临时通行证会留下可追踪记录。\n",
            encoding="utf-8",
        )
        return project

    def make_chat_smoke_delta(self) -> dict[str, object]:
        return {
            "schema_version": "plot-rag-delta/v3",
            "event_type": "movement",
            "category": "location",
            "action": "arrive",
            "subject": "基准角色甲",
            "object": "基准南站",
            "field": "current",
            "value": {},
            "scope": "current",
            "effective_at": None,
            "story_coordinate": None,
            "knowledge_plane": "objective",
            "ambiguity": None,
            "confidence": 0.99,
            "evidence": "基准角色甲已经抵达基准南站。",
        }

    def run_chat_smoke_stub(
        self,
        deltas: list[object],
        *,
        skipped: list[object] | None = None,
    ) -> dict[str, object]:
        runtime = types.SimpleNamespace(
            extract=types.SimpleNamespace(
                model="fixture-chat",
                endpoint="chat/completions",
            )
        )
        with (
            mock.patch(
                "benchmarks.v15_live_e2e.state_rag._load_runtime_config",
                return_value=runtime,
            ),
            mock.patch(
                "benchmarks.v15_live_e2e.state_rag._chat_extract",
                return_value=(
                    deltas,
                    list(skipped or []),
                    {
                        "status": "ok",
                        "http_status": 200,
                        "model": "fixture-chat",
                        "attempts": 1,
                    },
                ),
            ),
        ):
            return run_live_chat_extraction_smoke(
                ROOT,
                RemoteCallRecorder(),
            )

    def test_prompt_fixture_has_25_unique_structured_prompts(self) -> None:
        prompts = load_prompt_fixture(
            FIXTURES / "v15_generic_live_prompts.v1.json"
        )
        self.assertEqual(25, len(prompts))
        self.assertEqual(25, len({item["prompt_id"] for item in prompts}))
        self.assertEqual(
            {"outline", "scene", "revision"},
            {item["task"] for item in prompts},
        )
        self.assertTrue(
            all(
                any(
                    marker in item["prompt"]
                    for marker in ("状态", "关系", "位置", "物品", "力量", "时间", "伏笔")
                )
                for item in prompts
            )
        )

    def test_annotation_sets_cover_required_labels(self) -> None:
        experiences = load_jsonl_annotations(
            FIXTURES / "event_experience_annotations.v1.jsonl"
        )
        items = load_jsonl_annotations(
            FIXTURES / "item_function_annotations.v1.jsonl"
        )
        self.assertGreaterEqual(len(experiences), 12)
        self.assertGreaterEqual(len(items), 12)
        self.assertTrue(
            all(
                value["schema_version"]
                == "plot-rag-event-experience-annotation/v1"
                and value["expected"]["primary_emotion"]
                and value["expected"]["payoff"]
                for value in experiences
            )
        )
        surfaces = {
            surface
            for value in items
            for surface in value["expected"].get("surfaces", [])
        }
        self.assertTrue(
            {
                "definition",
                "instance",
                "custody",
                "function",
                "runtime",
                "history",
                "observation",
            }.issubset(surfaces)
        )

    def test_tree_snapshot_detects_content_and_mtime_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            first = tree_snapshot(project)
            second = tree_snapshot(project)
            self.assertTrue(compare_tree_snapshots(first, second)["unchanged"])
            path = project / "正文" / "第一章.md"
            path.write_text(
                path.read_text(encoding="utf-8") + "新增变化。\n",
                encoding="utf-8",
            )
            third = tree_snapshot(project)
            comparison = compare_tree_snapshots(first, third)
            self.assertFalse(comparison["unchanged"])
            self.assertIn("content_hash", comparison["mismatch_fields"])
            self.assertIn("metadata_hash", comparison["mismatch_fields"])

    def test_credential_scan_covers_environment_and_generic_tokens(self) -> None:
        text = (
            "Authorization: Bearer TOKENVALUE123 "
            "api_key=sf-1234567890 password=hunter22 ak-abcdefghij"
        )
        scan = scan_text_for_credentials(text)
        self.assertGreaterEqual(scan["finding_count"], 4)
        self.assertIn("bearer", scan["type_counts"])
        self.assertIn("sf_token", scan["type_counts"])
        self.assertIn("ak_token", scan["type_counts"])
        self.assertIn("credential_field", scan["type_counts"])

    def test_provenance_fingerprints_plugin_runner_python_and_os(self) -> None:
        provenance = collect_benchmark_provenance()
        self.assertEqual(PROVENANCE_SCHEMA, provenance["schema_version"])
        self.assertEqual(
            hashlib.sha256(
                (ROOT / "benchmarks" / "v15_live_e2e.py").read_bytes()
            ).hexdigest(),
            provenance["runner"]["source_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(
                (ROOT / "benchmarks" / "run_v15_live_e2e.py").read_bytes()
            ).hexdigest(),
            provenance["runner"]["cli_source_sha256"],
        )
        self.assertEqual(
            64,
            len(provenance["plugin"]["git_dirty_fingerprint_sha256"]),
        )
        self.assertEqual(
            64,
            len(provenance["plugin"]["worktree_source_sha256"]),
        )
        self.assertTrue(provenance["python"]["implementation"])
        self.assertTrue(provenance["python"]["version"])
        self.assertTrue(provenance["os"]["system"])

    def test_live_chat_extraction_smoke_reports_separate_remote_latency(
        self,
    ) -> None:
        recorder = RemoteCallRecorder()
        runtime = types.SimpleNamespace(
            extract=types.SimpleNamespace(
                model="fixture-chat",
                endpoint="chat/completions",
            )
        )
        delta = self.make_chat_smoke_delta()

        def extract_stub(
            runtime_value: object,
            assistant_text: str,
            prompt: str,
            current_facts: object,
        ) -> tuple[list[dict[str, object]], list[object], dict[str, object]]:
            self.assertIs(runtime, runtime_value)
            self.assertEqual([], current_facts)
            self.assertIn("非物品 movement", prompt)
            self.assertIn("field=current", prompt)
            self.assertIn("value={}", prompt)
            self.assertIn("不要输出 story_coordinate", prompt)
            recorder.record(
                service="extract",
                model="fixture-chat",
                latency_ms=1.0,
                status="ok",
                call_kind="extract",
            )
            return [delta], [], {
                "status": "ok",
                "http_status": 200,
                "model": "fixture-chat",
                "attempts": 1,
            }

        with (
            mock.patch(
                "benchmarks.v15_live_e2e.state_rag._load_runtime_config",
                return_value=runtime,
            ),
            mock.patch(
                "benchmarks.v15_live_e2e.state_rag._chat_extract",
                side_effect=extract_stub,
            ),
        ):
            result = run_live_chat_extraction_smoke(
                ROOT,
                recorder,
            )
        self.assertEqual(
            CHAT_EXTRACTION_SMOKE_SCHEMA,
            result["schema_version"],
        )
        self.assertTrue(result["passed"])
        self.assertEqual("siliconflow_chat", result["transport"])
        self.assertFalse(result["mutates_continuity"])
        self.assertEqual(1, result["attempt_count"])
        self.assertEqual(1, result["delta_count"])
        self.assertEqual(1, result["remote_calls"]["call_count"])
        self.assertEqual(1.0, result["remote_latency_sum_ms"])
        self.assertEqual(
            {
                "passed": True,
                "status": "valid",
                "target_movement_count": 1,
                "invalid_delta_count": 0,
                "invalid_delta_indices": [],
                "failure_codes": [],
            },
            result["semantic_validation"],
        )
        self.assertNotIn(
            "基准角色甲已经抵达基准南站",
            json.dumps(result, ensure_ascii=False),
        )

    def test_live_chat_extraction_smoke_rejects_empty_deltas(self) -> None:
        result = self.run_chat_smoke_stub([])
        self.assertFalse(result["passed"])
        self.assertEqual("semantic_validation_failed", result["status"])
        self.assertEqual(0, result["delta_count"])
        self.assertIn(
            "CHAT_SMOKE_NO_DELTAS",
            result["semantic_validation"]["failure_codes"],
        )
        self.assertIn(
            "CHAT_SMOKE_TARGET_MOVEMENT_COUNT_MISMATCH",
            result["semantic_validation"]["failure_codes"],
        )

    def test_live_chat_extraction_smoke_rejects_wrong_actor_or_place(
        self,
    ) -> None:
        cases = (
            (
                "actor",
                {"subject": "错误角色"},
                "CHAT_SMOKE_SUBJECT_MISMATCH",
            ),
            (
                "place",
                {"object": "错误地点"},
                "CHAT_SMOKE_OBJECT_MISMATCH",
            ),
        )
        for name, changes, expected_code in cases:
            with self.subTest(name=name):
                delta = self.make_chat_smoke_delta()
                delta.update(changes)
                result = self.run_chat_smoke_stub([delta])
                self.assertFalse(result["passed"])
                self.assertEqual(
                    "semantic_validation_failed",
                    result["status"],
                )
                self.assertIn(
                    expected_code,
                    result["semantic_validation"]["failure_codes"],
                )
                self.assertEqual(
                    0,
                    result["semantic_validation"]["target_movement_count"],
                )

    def test_live_chat_extraction_smoke_rejects_nonverbatim_evidence(
        self,
    ) -> None:
        delta = self.make_chat_smoke_delta()
        secret_evidence = "Authorization: Bearer TOKENVALUE123"
        delta["evidence"] = secret_evidence
        result = self.run_chat_smoke_stub([delta])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertFalse(result["passed"])
        self.assertEqual("semantic_validation_failed", result["status"])
        self.assertIn(
            "CHAT_SMOKE_EVIDENCE_NOT_EXACT",
            result["semantic_validation"]["failure_codes"],
        )
        self.assertNotIn(secret_evidence, serialized)
        self.assertNotIn("基准角色甲已经抵达基准南站", serialized)
        self.assertEqual(
            0,
            scan_text_for_credentials(serialized)["finding_count"],
        )

        unanchored = self.make_chat_smoke_delta()
        unanchored["evidence"] = "基准南站"
        unanchored_result = self.run_chat_smoke_stub([unanchored])
        self.assertFalse(unanchored_result["passed"])
        self.assertIn(
            "CHAT_SMOKE_EVIDENCE_DOES_NOT_SUPPORT_LOCATION",
            unanchored_result["semantic_validation"]["failure_codes"],
        )

    def test_live_chat_extraction_smoke_rejects_forbidden_coordinate(
        self,
    ) -> None:
        delta = self.make_chat_smoke_delta()
        delta["story_coordinate"] = {
            "calendar_id": "story-main",
            "ordinal": 1,
        }
        result = self.run_chat_smoke_stub([delta])
        self.assertFalse(result["passed"])
        self.assertIn(
            "CHAT_SMOKE_STORY_COORDINATE_FORBIDDEN",
            result["semantic_validation"]["failure_codes"],
        )

    def test_live_chat_extraction_smoke_rejects_skips_and_extra_delta(
        self,
    ) -> None:
        valid = self.make_chat_smoke_delta()
        skipped_result = self.run_chat_smoke_stub(
            [valid],
            skipped=[{"reason": "fixture_skip", "evidence": "敏感正文"}],
        )
        self.assertFalse(skipped_result["passed"])
        self.assertIn(
            "CHAT_SMOKE_SKIPPED_DELTAS",
            skipped_result["semantic_validation"]["failure_codes"],
        )
        self.assertNotIn(
            "敏感正文",
            json.dumps(skipped_result, ensure_ascii=False),
        )

        extra = self.make_chat_smoke_delta()
        extra["object"] = "错误地点"
        extra_result = self.run_chat_smoke_stub([valid, extra])
        self.assertFalse(extra_result["passed"])
        self.assertEqual(
            "semantic_validation_failed",
            extra_result["status"],
        )
        self.assertIn(
            "CHAT_SMOKE_UNEXPECTED_DELTA_COUNT",
            extra_result["semantic_validation"]["failure_codes"],
        )
        self.assertIn(
            "CHAT_SMOKE_OBJECT_MISMATCH",
            extra_result["semantic_validation"]["failure_codes"],
        )

    def test_live_report_separates_prepare_and_chat_extraction_smoke(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)

            @contextlib.contextmanager
            def transport_stub(
                mode: str,
                recorder: RemoteCallRecorder,
            ) -> object:
                self.assertEqual("live", mode)
                yield

            def context_stub(
                state_root: Path,
                prompt: str,
                *,
                artifact_context: dict[str, object],
            ) -> dict[str, object]:
                state = state_root.name.rsplit("-", 1)[-1]
                chosen = {
                    "FF": "v1",
                    "FT": "v1",
                    "TF": "v2",
                    "TT": "v1",
                }[state]
                comparison = (
                    {"equivalent": True}
                    if state in {"FT", "TT"}
                    else {}
                )
                return {
                    "status": "ready",
                    "context": "stable context",
                    "contract": {
                        "task": artifact_context["task"],
                        "needs": [],
                        "context_text": "stable context",
                        "retrieval_telemetry": {"queries": []},
                    },
                    "telemetry": {
                        "prepare_v2": {
                            "chosen_path": chosen,
                            "executed_paths": [chosen],
                            "comparison": comparison,
                            chosen: {
                                "semantic_hash": "stable-semantic",
                                "context_hash": "stable-context",
                            },
                        }
                    },
                    "index": {
                        "query_policy": {
                            "embedding_model": "fixture-embed",
                            "rerank_model": "fixture-rerank",
                        }
                    },
                }

            smoke = {
                "schema_version": CHAT_EXTRACTION_SMOKE_SCHEMA,
                "requested": True,
                "executed": True,
                "passed": True,
                "status": "passed",
                "transport": "siliconflow_chat",
                "mutates_continuity": False,
                "wall_ms": 125.0,
                "remote_latency_sum_ms": 120.0,
                "remote_calls": {
                    "call_count": 1,
                    "by_service": {
                        "extract": {
                            "call_count": 1,
                            "latency_sum_ms": 120.0,
                        }
                    },
                },
            }
            with (
                mock.patch(
                    "benchmarks.v15_live_e2e.transport_context",
                    side_effect=transport_stub,
                ),
                mock.patch(
                    "benchmarks.v15_live_e2e.v1_runtime.build_longform_context",
                    side_effect=context_stub,
                ),
                mock.patch(
                    "benchmarks.v15_live_e2e.run_live_chat_extraction_smoke",
                    return_value=smoke,
                ),
                mock.patch(
                    "benchmarks.v15_live_e2e.collect_benchmark_provenance",
                    return_value={"schema_version": PROVENANCE_SCHEMA},
                ),
            ):
                report = run_v15_live_e2e(
                    project_root=project,
                    prompts_path=(
                        FIXTURES / "v15_generic_live_prompts.v1.json"
                    ),
                    transport="live",
                    workspace_parent=root / "workspaces",
                    prompt_limit=1,
                    include_strict=False,
                    include_chat_extraction_smoke=True,
                )
        self.assertTrue(report["passed"])
        self.assertFalse(
            report["transport_contract"]["prepare_matrix_uses_remote_chat"]
        )
        self.assertTrue(
            report["transport_contract"][
                "chat_extraction_smoke_uses_remote_chat"
            ]
        )
        self.assertFalse(
            report["latency_contract"]["prepare_matrix"][
                "includes_remote_chat_extraction"
            ]
        )
        self.assertEqual(
            125.0,
            report["chat_extraction_smoke"]["wall_ms"],
        )
        self.assertEqual(
            1,
            report["remote_call_phases"]["chat_extraction_smoke"][
                "call_count"
            ],
        )
        self.assertEqual(
            0,
            report["quality_gate"]["degraded_turn_count"],
        )
        self.assertEqual(
            0,
            report["quality_gate"]["prepare_remote_error_call_count"],
        )
        self.assertEqual(
            0,
            report["quality_gate"]["remote_error_call_count"],
        )

    def test_live_quality_gate_rejects_remote_error_in_shadow_path(
        self,
    ) -> None:
        """A healthy chosen path must not hide a failed shadow request."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            recorder_ref: dict[str, RemoteCallRecorder] = {}
            recorded = {"value": False}

            @contextlib.contextmanager
            def transport_stub(
                mode: str,
                recorder: RemoteCallRecorder,
            ) -> object:
                self.assertEqual("live", mode)
                recorder_ref["value"] = recorder
                yield

            def context_stub(
                state_root: Path,
                prompt: str,
                *,
                artifact_context: dict[str, object],
            ) -> dict[str, object]:
                if not recorded["value"]:
                    recorder_ref["value"].record(
                        service="rerank",
                        model="fixture-rerank",
                        latency_ms=1.0,
                        status="error",
                        call_kind="rerank",
                    )
                    recorded["value"] = True
                state = state_root.name.rsplit("-", 1)[-1]
                chosen = {
                    "FF": "v1",
                    "FT": "v1",
                    "TF": "v2",
                    "TT": "v1",
                }[state]
                comparison = (
                    {"equivalent": True}
                    if state in {"FT", "TT"}
                    else {}
                )
                return {
                    "status": "ready",
                    "context": "stable context",
                    "contract": {
                        "task": artifact_context["task"],
                        "needs": [],
                        "context_text": "stable context",
                        "retrieval_telemetry": {"queries": []},
                    },
                    "telemetry": {
                        "prepare_v2": {
                            "chosen_path": chosen,
                            "executed_paths": [chosen],
                            "comparison": comparison,
                            chosen: {
                                "semantic_hash": "stable-semantic",
                                "context_hash": "stable-context",
                            },
                        }
                    },
                    "index": {
                        "query_policy": {
                            "embedding_model": "fixture-embed",
                            "rerank_model": "fixture-rerank",
                        }
                    },
                }

            with (
                mock.patch(
                    "benchmarks.v15_live_e2e.transport_context",
                    side_effect=transport_stub,
                ),
                mock.patch(
                    "benchmarks.v15_live_e2e.v1_runtime.build_longform_context",
                    side_effect=context_stub,
                ),
            ):
                report = run_v15_live_e2e(
                    project_root=project,
                    prompts_path=(
                        FIXTURES / "v15_generic_live_prompts.v1.json"
                    ),
                    transport="live",
                    workspace_parent=root / "workspaces",
                    prompt_limit=1,
                    include_strict=False,
                )

        self.assertEqual(0, report["quality_gate"]["mismatch_count"])
        self.assertEqual(0, report["quality_gate"]["degraded_turn_count"])
        self.assertEqual(
            1,
            report["quality_gate"]["prepare_remote_error_call_count"],
        )
        self.assertEqual(
            1,
            report["quality_gate"]["remote_error_call_count"],
        )
        self.assertFalse(report["passed"])

    def test_live_quality_gate_rejects_degraded_turns_without_remote_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)

            @contextlib.contextmanager
            def transport_stub(
                mode: str,
                recorder: RemoteCallRecorder,
            ) -> object:
                self.assertEqual("live", mode)
                yield

            def context_stub(
                state_root: Path,
                prompt: str,
                *,
                artifact_context: dict[str, object],
            ) -> dict[str, object]:
                state = state_root.name.rsplit("-", 1)[-1]
                chosen = {
                    "FF": "v1",
                    "FT": "v1",
                    "TF": "v2",
                    "TT": "v1",
                }[state]
                comparison = (
                    {"equivalent": True}
                    if state in {"FT", "TT"}
                    else {}
                )
                return {
                    "status": "ready",
                    "context": "stable context",
                    "contract": {
                        "task": artifact_context["task"],
                        "needs": [],
                        "context_text": "stable context",
                        "retrieval_telemetry": {
                            "queries": [
                                {
                                    "status": "error",
                                    "embedding_status": "ok",
                                    "rerank_statuses": [],
                                }
                            ]
                        },
                    },
                    "telemetry": {
                        "prepare_v2": {
                            "chosen_path": chosen,
                            "executed_paths": [chosen],
                            "comparison": comparison,
                            chosen: {
                                "semantic_hash": "stable-semantic",
                                "context_hash": "stable-context",
                            },
                        }
                    },
                    "index": {
                        "query_policy": {
                            "embedding_model": "fixture-embed",
                            "rerank_model": "fixture-rerank",
                        }
                    },
                }

            with (
                mock.patch(
                    "benchmarks.v15_live_e2e.transport_context",
                    side_effect=transport_stub,
                ),
                mock.patch(
                    "benchmarks.v15_live_e2e.v1_runtime.build_longform_context",
                    side_effect=context_stub,
                ),
            ):
                report = run_v15_live_e2e(
                    project_root=project,
                    prompts_path=(
                        FIXTURES / "v15_generic_live_prompts.v1.json"
                    ),
                    transport="live",
                    workspace_parent=root / "workspaces",
                    prompt_limit=1,
                    include_strict=False,
                )

        self.assertEqual(4, report["quality_gate"]["degraded_turn_count"])
        self.assertEqual(
            0,
            report["quality_gate"]["remote_error_call_count"],
        )
        self.assertFalse(report["passed"])

    def test_chat_extraction_smoke_requires_live_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            with self.assertRaisesRegex(
                ValueError,
                "requires live transport",
            ):
                run_v15_live_e2e(
                    project_root=project,
                    prompts_path=(
                        FIXTURES / "v15_generic_live_prompts.v1.json"
                    ),
                    transport="offline",
                    prompt_limit=1,
                    include_strict=False,
                    include_chat_extraction_smoke=True,
                )

    def test_offline_four_state_and_strict_chain_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            before = tree_snapshot(project)
            report = run_v15_live_e2e(
                project_root=project,
                prompts_path=FIXTURES / "v15_generic_live_prompts.v1.json",
                transport="offline",
                workspace_parent=root / "workspaces",
                prompt_limit=1,
                include_strict=True,
            )
            after = tree_snapshot(project)
            self.assertEqual(REPORT_SCHEMA, report["schema_version"])
            self.assertEqual(4, report["measured_round_count"])
            self.assertTrue(report["formal_project_tree"]["unchanged"])
            self.assertTrue(compare_tree_snapshots(before, after)["unchanged"])
            self.assertTrue(report["strict_chain"]["passed"])
            self.assertEqual(
                "deterministic_typed_events",
                report["strict_chain"]["extraction_transport"],
            )
            self.assertEqual(
                {
                    "retrieval_transport": "offline",
                    "retrieval_is_deterministic": True,
                    "prepare_matrix_uses_remote_embedding_rerank": False,
                    "prepare_matrix_uses_remote_chat": False,
                    "strict_extraction_transport": (
                        "deterministic_typed_events"
                    ),
                    "strict_extraction_uses_remote_chat": False,
                    "chat_extraction_smoke_transport": "not_requested",
                    "chat_extraction_smoke_uses_remote_chat": False,
                },
                report["transport_contract"],
            )
            self.assertEqual(
                PROVENANCE_SCHEMA,
                report["provenance"]["schema_version"],
            )
            self.assertEqual(
                "not_requested",
                report["chat_extraction_smoke"]["status"],
            )
            self.assertFalse(
                report["latency_contract"]["prepare_matrix"][
                    "includes_remote_chat_extraction"
                ]
            )
            self.assertEqual(
                7,
                report["strict_chain"]["item_surfaces"]["surface_count"],
            )
            self.assertEqual(
                0,
                report["quality_gate"]["critical_fact_mismatch_count"],
            )
            self.assertTrue(report["passed"])
            serialized = json.dumps(report, ensure_ascii=False)
            self.assertNotIn(str(project.resolve()), serialized)
            self.assertNotIn(
                load_prompt_fixture(
                    FIXTURES / "v15_generic_live_prompts.v1.json"
                )[0]["prompt"],
                serialized,
            )

    def test_strict_failure_reports_stable_redacted_code_and_stage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            failure = StrictChainFailure(
                stage="accept",
                error_code="CANON_REVISION_STALE",
            )
            with mock.patch(
                "benchmarks.v15_live_e2e.run_strict_chain",
                side_effect=failure,
            ):
                report = run_v15_live_e2e(
                    project_root=project,
                    prompts_path=(
                        FIXTURES / "v15_generic_live_prompts.v1.json"
                    ),
                    transport="offline",
                    workspace_parent=root / "workspaces",
                    prompt_limit=1,
                    include_strict=True,
                )
            strict = report["strict_chain"]
            self.assertFalse(strict["passed"])
            self.assertEqual("error", strict["status"])
            self.assertEqual("accept", strict["error_stage"])
            self.assertEqual(
                "CANON_REVISION_STALE",
                strict["error_code"],
            )
            self.assertEqual(64, len(strict["error_sha256"]))
            self.assertNotIn("message", strict)
            self.assertFalse(report["passed"])

    def test_redacted_writer_rejects_existing_file_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.redacted.json"
            report = {
                "schema_version": REPORT_SCHEMA,
                "passed": True,
                "credential_scan": {"finding_count": 0},
            }
            first = write_redacted_report(report, output, pretty=True)
            self.assertTrue(output.is_file())
            self.assertEqual(64, len(first["sha256"]))
            with self.assertRaises(FileExistsError):
                write_redacted_report(report, output)


if __name__ == "__main__":
    unittest.main()
