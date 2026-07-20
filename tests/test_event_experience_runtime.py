from __future__ import annotations

import copy
import hashlib
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from event_experience import EventExperienceError, EventExperienceService
from event_experience_runtime import (
    ensure_locked_manifest,
    verify_locked_manifest,
)
from continuity.service import ContinuityService, HostApprovalAuthority


class EventExperienceRuntimeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "novel"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_namespace_package_import_reuses_package_event_module(self) -> None:
        code = "\n".join(
            [
                "import scripts.event_experience as event_module",
                "import scripts.event_experience_runtime as runtime_module",
                "assert runtime_module.EventExperienceError "
                "is event_module.EventExperienceError",
                "assert runtime_module.EventExperienceService "
                "is event_module.EventExperienceService",
            ]
        )
        completed = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", "-c", code],
            cwd=PLUGIN_ROOT,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def intent(
        self,
        *,
        experience: str = "紧张、希望、余悸",
        experience_source: str = "user_answer",
        autonomy: str = "模型可决定场景实现与次级冲突",
        autonomy_source: str = "user_answer",
        status: str = "EXECUTING",
    ) -> dict:
        values = {
            "problem_to_solve": "让主角在封锁中找到一条现实退路",
            "expected_deliverable": "一个可执行、可核验的事件链",
            "reader_experience": experience,
            "protagonist_drive_conflict": "主角优先保命，对手持续压缩退路",
            "scope_endpoint": "推进到主角换取一次局部主动",
            "success_criteria": "完成一次不可逆状态变化并留下后续压力",
            "hard_constraints": "不改写 accepted 事实，不让主角舍己",
            "model_autonomy": autonomy,
        }
        fields = {
            field: {
                "value": value,
                "source": (
                    experience_source
                    if field == "reader_experience"
                    else (
                        autonomy_source
                        if field == "model_autonomy"
                        else "user_answer"
                    )
                ),
            }
            for field, value in values.items()
        }
        return {
            "status": status,
            "grill_session_id": "grill-session-1",
            "revision": 3,
            "contract": {
                "schema_version": "plot-rag-intent/v1",
                "task_family": "plot",
                "fields": fields,
            },
        }

    def artifact(self, **overrides) -> dict:
        value = {
            "artifact_id": "chapter-001",
            "artifact_revision": 2,
            "branch_id": "main",
            "chapter_no": 1,
            "scene_index": 0,
            "reader_knowledge_position": "读者比视角人物多知道一条追踪线索",
            "open_loop_links": ["loop-exposure"],
        }
        value.update(overrides)
        return value

    def ensure(self, **overrides) -> dict:
        arguments = {
            "prompt": "剧情推演：测试角色甲如何在封锁中保住退路并拿回局部主动",
            "artifact_context": self.artifact(),
            "intent_contract": self.intent(),
            "session_identity": "host-session-1",
            "turn_identity": "turn-1",
        }
        arguments.update(overrides)
        return ensure_locked_manifest(self.root, **arguments)

    def assert_error(self, code: str):
        class Context:
            def __init__(self, outer: unittest.TestCase) -> None:
                self.outer = outer
                self.caught = None

            def __enter__(self):
                self.caught = self.outer.assertRaises(EventExperienceError)
                return self.caught.__enter__()

            def __exit__(self, exc_type, exc, traceback):
                handled = self.caught.__exit__(exc_type, exc, traceback)
                if handled:
                    self.outer.assertEqual(code, self.caught.exception.code)
                return handled

        return Context(self)

    def accept_outline(
        self,
        *,
        payload: dict,
        artifact_id: str = "outline-001",
        artifact_revision: int = 1,
    ) -> tuple[dict, dict]:
        continuity = ContinuityService(self.root)
        with continuity.store.read_connection() as connection:
            active_revision = continuity.store.get_meta_int(
                connection,
                "active_canon_revision",
            )
        proposal = continuity.save_proposal(
            events=[],
            payload=payload,
            artifact_id=artifact_id,
            artifact_stage="outline",
            branch_id="main",
            chapter_no=1,
            scene_index=0,
            artifact_revision=artifact_revision,
            prepared_canon_revision=active_revision,
        )
        host = HostApprovalAuthority(
            continuity,
            issuer="event-experience-runtime-tests",
        )
        grant = host.issue(
            proposal["proposal_id"],
            expected_canon_revision=active_revision,
        )
        commit = continuity.accept_proposal(
            proposal["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=active_revision,
        )
        return proposal, commit

    def test_high_confidence_intent_auto_locks_and_returns_lifecycle_binding(
        self,
    ) -> None:
        result = self.ensure()
        self.assertEqual("locked", result["action"])
        self.assertTrue(result["ready"])
        self.assertTrue(result["zero_remote"])
        self.assertEqual("locked", result["arc"]["status"])
        self.assertEqual(
            result["manifest"]["event_seed_manifest_hash"],
            result["binding"]["event_seed_manifest_hash"],
        )
        self.assertEqual(
            result["manifest"]["source_intent_contract_hash"],
            result["binding"]["source_intent_contract_hash"],
        )
        self.assertEqual(
            result["control_revision"],
            result["binding"]["control_revision"],
        )
        self.assertEqual(1, len(result["binding"]["contracts"]))

    def test_runtime_is_stable_idempotent_and_performs_zero_network_calls(
        self,
    ) -> None:
        with patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network call attempted"),
        ):
            first = self.ensure(idempotency_key="runtime-idempotent")
            second = self.ensure(
                idempotency_key="runtime-idempotent",
                expected_control_revision=first["control_revision"],
            )
        self.assertEqual(
            first["manifest"]["event_seed_manifest_hash"],
            second["manifest"]["event_seed_manifest_hash"],
        )
        self.assertEqual(first["binding"], second["binding"])
        service = EventExperienceService.for_project(self.root)
        counts = service.storage_boundary_report()["control_row_counts"]
        self.assertEqual(1, counts["event_seeds"])
        self.assertEqual(1, counts["event_experience_arcs"])
        self.assertEqual(1, counts["event_experience_contracts"])
        self.assertEqual(
            first["control_revision"], service.get_control_revision()
        )

    def test_default_experience_without_delegation_opens_one_question(self) -> None:
        intent = self.intent(
            experience="形成明确期待、有效阻力、阶段兑现和新的后续问题",
            experience_source="workflow_default",
            autonomy="核心情绪方向必须由用户明确裁决",
            autonomy_source="workflow_default",
        )
        first = self.ensure(intent_contract=intent)
        second = self.ensure(intent_contract=intent)
        self.assertEqual("ask", first["action"])
        self.assertFalse(first["ready"])
        self.assertTrue(first["suppress_plot_receipt"])
        self.assertTrue(first["suppress_remote_retrieval"])
        self.assertTrue(first["suppress_stop_proposal"])
        self.assertEqual(
            first["question"]["question_hash"],
            second["question"]["question_hash"],
        )
        service = EventExperienceService.for_project(self.root)
        self.assertEqual(
            1,
            service.storage_boundary_report()["control_row_counts"][
                "event_experience_questions"
            ],
        )

    def test_explicit_model_delegation_auto_locks_default_experience(self) -> None:
        result = self.ensure(
            intent_contract=self.intent(
                experience="形成明确期待、有效阻力、阶段兑现和新的后续问题",
                experience_source="workflow_default",
                autonomy="情绪方向和场景实现都交给模型决定",
                autonomy_source="user_answer",
            )
        )
        self.assertEqual("locked", result["action"])
        self.assertEqual("delegated_auto_lock", result["reason"])

    def test_question_answer_revises_arc_then_locks_contracts(self) -> None:
        intent = self.intent(
            experience="形成明确期待、有效阻力、阶段兑现和新的后续问题",
            experience_source="workflow_default",
            autonomy="核心情绪方向必须由用户明确裁决",
            autonomy_source="workflow_default",
        )
        asked = self.ensure(intent_contract=intent)
        service = EventExperienceService.for_project(self.root)
        selected = service.answer_question(
            asked["question"]["event_seed_manifest_hash"],
            "按推荐答案",
            expected_control_revision=asked["control_revision"],
            idempotency_key="runtime-question-answer",
        )
        self.assertEqual("C", selected["selected_option"]["option_id"])
        locked = self.ensure(intent_contract=intent)
        self.assertEqual("locked", locked["action"])
        self.assertEqual("question_answer_locked", locked["reason"])
        self.assertEqual(2, locked["arc"]["arc_revision"])
        contract = service.get_contract(
            locked["binding"]["contracts"][0]["contract_id"]
        )
        self.assertEqual("余悸", contract["primary_emotion"])
        self.assertIn("暴露风险", contract["target_reader_state"])

    def test_invalid_answers_do_not_create_contract_or_remote_handoff(self) -> None:
        intent = self.intent(
            experience="形成明确期待、有效阻力、阶段兑现和新的后续问题",
            experience_source="workflow_default",
            autonomy="核心情绪方向必须由用户明确裁决",
            autonomy_source="workflow_default",
        )
        asked = self.ensure(intent_contract=intent)
        service = EventExperienceService.for_project(self.root)
        revision = asked["control_revision"]
        service.answer_question(
            asked["question"]["event_seed_manifest_hash"],
            "继续",
            expected_control_revision=revision,
            idempotency_key="invalid-1",
        )
        service.answer_question(
            asked["question"]["event_seed_manifest_hash"],
            "下一步",
            expected_control_revision=revision,
            idempotency_key="invalid-2",
        )
        waiting = self.ensure(intent_contract=intent)
        self.assertEqual("ask", waiting["action"])
        self.assertEqual("awaiting_explicit_choice", waiting["reason"])
        self.assertTrue(waiting["suppress_plot_receipt"])
        self.assertIsNone(
            service.active_contract_for_seed(
                waiting["seed_references"][0]["event_seed_id"], 1
            )
        )

    def test_verify_locked_manifest_accepts_binding_and_detects_stale_control(
        self,
    ) -> None:
        locked = self.ensure()
        verified = verify_locked_manifest(
            self.root,
            seed_references=locked["seed_references"],
            binding=locked["binding"],
        )
        self.assertEqual("verified", verified["action"])
        service = EventExperienceService.for_project(self.root)
        service.create_seed(
            {
                "event_seed_id": "unrelated-seed",
                "event_seed_revision": 1,
                "parent_chain_id": "unrelated-chain",
                "dependency_order": 1,
                "dramatic_function": "unrelated",
                "causal_role": "unrelated",
                "intended_state_change": "unrelated",
                "event_boundary": "unrelated",
                "artifact_id": "unrelated-artifact",
                "artifact_revision": 1,
                "branch_id": "main",
            },
            expected_control_revision=locked["control_revision"],
            idempotency_key="unrelated-control-change",
        )
        with self.assert_error("EVENT_EXPERIENCE_STALE_CONTROL"):
            verify_locked_manifest(
                self.root,
                seed_references=locked["seed_references"],
                binding=locked["binding"],
            )

    def test_verify_rejects_tampered_contract_arc_and_binding_hash(self) -> None:
        locked = self.ensure()
        contract_tamper = copy.deepcopy(locked["binding"])
        contract_tamper["contracts"][0]["contract_hash"] = "0" * 64
        with self.assert_error(
            "EVENT_EXPERIENCE_RUNTIME_BINDING_MISMATCH"
        ):
            verify_locked_manifest(
                self.root,
                seed_references=locked["seed_references"],
                binding=contract_tamper,
            )
        arc_tamper = copy.deepcopy(locked["binding"])
        arc_tamper["arc_hash"] = "0" * 64
        with self.assert_error(
            "EVENT_EXPERIENCE_RUNTIME_BINDING_MISMATCH"
        ):
            verify_locked_manifest(
                self.root,
                seed_references=locked["seed_references"],
                binding=arc_tamper,
            )
        hash_tamper = copy.deepcopy(locked["binding"])
        hash_tamper["binding_hash"] = "0" * 64
        with self.assert_error(
            "EVENT_EXPERIENCE_RUNTIME_BINDING_HASH_MISMATCH"
        ):
            verify_locked_manifest(
                self.root,
                seed_references=locked["seed_references"],
                binding=hash_tamper,
            )

    def test_multiple_explicit_event_seeds_produce_sorted_contract_identity(self) -> None:
        result = self.ensure(
            artifact_context=self.artifact(
                event_seeds=[
                    {
                        "dependency_order": 2,
                        "dramatic_function": "主角换取局部主动",
                        "causal_role": "兑现反击",
                        "intended_state_change": "获得短时通行窗口",
                        "event_boundary": "从交涉破裂到夺取窗口",
                    },
                    {
                        "dependency_order": 1,
                        "dramatic_function": "对手封死旧退路",
                        "causal_role": "升级压力",
                        "intended_state_change": "旧路线失效",
                        "event_boundary": "从察觉跟踪到旧路线失效",
                    },
                ]
            )
        )
        self.assertEqual(2, len(result["binding"]["contracts"]))
        orders = [
            item["dependency_order"]
            for item in result["manifest"]["contracts"]
        ]
        self.assertEqual([1, 2], orders)
        self.assertEqual(
            [
                item["event_seed_id"]
                for item in result["manifest"]["contracts"]
            ],
            [
                item["event_seed_id"]
                for item in result["binding"]["contracts"]
            ],
        )

    def test_accepted_outline_derives_bound_event_seeds_without_remote_calls(
        self,
    ) -> None:
        _, commit = self.accept_outline(
            payload={
                "event_seeds": [
                    {
                        "dependency_order": 1,
                        "dramatic_function": "封死旧退路",
                        "causal_role": "升级压力",
                        "intended_state_change": "旧路线失效",
                        "event_boundary": "从察觉跟踪到旧路线失效",
                    },
                    {
                        "dependency_order": 2,
                        "dramatic_function": "发现可行缝隙",
                        "causal_role": "建立有限希望",
                        "intended_state_change": "获得临时通行窗口",
                        "event_boundary": "从无路可退到拿到一次通行机会",
                    },
                ]
            }
        )
        with patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network call attempted"),
        ):
            result = self.ensure()
        binding = result["manifest"]["accepted_outline_binding"]
        self.assertEqual(commit["commit_id"], binding["source_outline_commit_id"])
        self.assertEqual(
            "outline-001",
            binding["source_outline_artifact_id"],
        )
        self.assertEqual(1, binding["source_outline_artifact_revision"])
        self.assertEqual(binding, result["binding"]["accepted_outline_binding"])
        self.assertEqual(2, len(result["seed_references"]))
        service = EventExperienceService.for_project(self.root)
        seeds = [
            service.get_seed(
                reference["event_seed_id"],
                reference["event_seed_revision"],
            )
            for reference in result["seed_references"]
        ]
        self.assertEqual(
            ["封死旧退路", "发现可行缝隙"],
            [seed["dramatic_function"] for seed in seeds],
        )

    def test_reused_outline_seed_id_supersedes_seed_arc_and_contract(
        self,
    ) -> None:
        self.accept_outline(
            payload={
                "event_seeds": [
                    {
                        "event_seed_id": "evt-stable",
                        "dependency_order": 1,
                        "dramatic_function": "封死旧退路",
                        "causal_role": "升级压力",
                        "intended_state_change": "旧路线失效",
                        "event_boundary": "从察觉跟踪到旧路线失效",
                    }
                ]
            },
            artifact_revision=1,
        )
        first = self.ensure(idempotency_key="outline-supersede-r1")
        first_contract_id = first["binding"]["contracts"][0]["contract_id"]

        self.accept_outline(
            payload={
                "event_seeds": [
                    {
                        "event_seed_id": "evt-stable",
                        "dependency_order": 1,
                        "dramatic_function": "从封锁中夺取一次通行窗口",
                        "causal_role": "完成局部反击",
                        "intended_state_change": "获得临时通行权",
                        "event_boundary": "从旧路线失效到夺取通行窗口",
                    }
                ]
            },
            artifact_revision=2,
        )
        second = self.ensure(idempotency_key="outline-supersede-r2")

        self.assertEqual(
            [{"event_seed_id": "evt-stable", "event_seed_revision": 2}],
            second["seed_references"],
        )
        self.assertEqual(first["arc"]["arc_id"], second["arc"]["arc_id"])
        self.assertEqual(2, second["arc"]["arc_revision"])
        service = EventExperienceService.for_project(self.root)
        old_seed = service.get_seed("evt-stable", 1)
        new_seed = service.get_seed("evt-stable", 2)
        self.assertEqual("retired", old_seed["status"])
        self.assertEqual("experience_locked", new_seed["status"])
        self.assertEqual(1, new_seed["supersedes_seed_revision"])
        self.assertEqual(
            "retired",
            service.get_contract(first_contract_id)["status"],
        )
        self.assertEqual(
            "locked",
            service.get_contract(
                second["binding"]["contracts"][0]["contract_id"]
            )["status"],
        )
        self.assertEqual(
            "retired",
            service.get_arc(first["arc"]["arc_id"], 1)["status"],
        )
        self.assertEqual(
            "locked",
            service.get_arc(first["arc"]["arc_id"], 2)["status"],
        )

    def test_outline_supersession_retry_does_not_create_revision_three(
        self,
    ) -> None:
        self.accept_outline(
            payload={
                "event_seeds": [
                    {
                        "event_seed_id": "evt-stable",
                        "dependency_order": 1,
                        "dramatic_function": "旧事件职责",
                    }
                ]
            },
            artifact_revision=1,
        )
        self.ensure(idempotency_key="outline-retry-r1")
        self.accept_outline(
            payload={
                "event_seeds": [
                    {
                        "event_seed_id": "evt-stable",
                        "dependency_order": 1,
                        "dramatic_function": "新事件职责",
                    }
                ]
            },
            artifact_revision=2,
        )
        first = self.ensure(idempotency_key="outline-retry-r2")
        service = EventExperienceService.for_project(self.root)
        counts_before = service.storage_boundary_report()[
            "control_row_counts"
        ]
        revision_before = service.get_control_revision()

        second = self.ensure(
            idempotency_key="outline-retry-r2",
            expected_control_revision=revision_before,
        )

        self.assertEqual(first["binding"], second["binding"])
        self.assertEqual(2, service.get_seed("evt-stable")["event_seed_revision"])
        self.assertEqual(2, service.get_arc(first["arc"]["arc_id"])["arc_revision"])
        self.assertEqual(
            counts_before,
            service.storage_boundary_report()["control_row_counts"],
        )
        self.assertEqual(revision_before, service.get_control_revision())

    def test_runtime_key_cannot_cross_outline_revision(self) -> None:
        self.accept_outline(
            payload={
                "event_seeds": [
                    {
                        "event_seed_id": "evt-stable",
                        "dependency_order": 1,
                        "dramatic_function": "旧事件职责",
                    }
                ]
            },
            artifact_revision=1,
        )
        self.ensure(idempotency_key="one-runtime-key")
        self.accept_outline(
            payload={
                "event_seeds": [
                    {
                        "event_seed_id": "evt-stable",
                        "dependency_order": 1,
                        "dramatic_function": "新事件职责",
                    }
                ]
            },
            artifact_revision=2,
        )
        service = EventExperienceService.for_project(self.root)
        counts_before = service.storage_boundary_report()[
            "control_row_counts"
        ]
        revision_before = service.get_control_revision()

        with self.assert_error("EVENT_EXPERIENCE_IDEMPOTENCY_CONFLICT"):
            self.ensure(
                idempotency_key="one-runtime-key",
                expected_control_revision=revision_before,
            )

        self.assertEqual(
            counts_before,
            service.storage_boundary_report()["control_row_counts"],
        )
        self.assertEqual(revision_before, service.get_control_revision())
        self.assertEqual(1, service.get_seed("evt-stable")["event_seed_revision"])
        self.assertEqual(
            "experience_locked",
            service.get_seed("evt-stable", 1)["status"],
        )

    def test_explicit_seed_id_collision_across_outline_scopes_is_zero_write(
        self,
    ) -> None:
        payload = {
            "event_seeds": [
                {
                    "event_seed_id": "evt-stable",
                    "dependency_order": 1,
                    "dramatic_function": "同名事件",
                }
            ]
        }
        self.accept_outline(payload=payload, artifact_id="outline-A")
        first = self.ensure(
            idempotency_key="outline-A-runtime",
            artifact_context=self.artifact(
                accepted_outline_artifact_id="outline-A"
            ),
        )
        self.accept_outline(payload=payload, artifact_id="outline-B")
        service = EventExperienceService.for_project(self.root)
        counts_before = service.storage_boundary_report()[
            "control_row_counts"
        ]
        revision_before = service.get_control_revision()

        with self.assert_error("EVENT_EXPERIENCE_SEED_SCOPE_COLLISION"):
            self.ensure(
                idempotency_key="outline-B-runtime",
                artifact_context=self.artifact(
                    accepted_outline_artifact_id="outline-B"
                ),
            )

        self.assertEqual(
            counts_before,
            service.storage_boundary_report()["control_row_counts"],
        )
        self.assertEqual(revision_before, service.get_control_revision())
        self.assertEqual(
            "experience_locked",
            service.get_seed("evt-stable", 1)["status"],
        )
        self.assertEqual(1, first["arc"]["arc_revision"])

    def test_same_outline_revision_payload_drift_is_zero_write(self) -> None:
        self.accept_outline(
            payload={
                "event_seeds": [
                    {
                        "event_seed_id": "evt-stable",
                        "dependency_order": 1,
                        "dramatic_function": "冻结后的事件职责",
                    }
                ]
            }
        )
        locked = self.ensure(idempotency_key="same-revision-r1")
        service = EventExperienceService.for_project(self.root)
        counts_before = service.storage_boundary_report()[
            "control_row_counts"
        ]
        revision_before = service.get_control_revision()
        database = self.root / ".plot-rag" / "state.sqlite3"
        artifact_version_id = locked["binding"][
            "accepted_outline_binding"
        ]["source_outline_artifact_version_id"]
        tampered = json.dumps(
            {
                "event_seeds": [
                    {
                        "event_seed_id": "evt-stable",
                        "dependency_order": 1,
                        "dramatic_function": "同 revision 被原位改写",
                    }
                ]
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                """
                UPDATE artifacts
                SET content_json=?
                WHERE artifact_version_id=?
                """,
                (tampered, artifact_version_id),
            )
            connection.commit()

        with self.assert_error("EVENT_EXPERIENCE_SEED_REVISION_CONFLICT"):
            self.ensure(idempotency_key="same-revision-r1-drift")

        self.assertEqual(
            counts_before,
            service.storage_boundary_report()["control_row_counts"],
        )
        self.assertEqual(revision_before, service.get_control_revision())
        self.assertEqual(1, service.get_seed("evt-stable")["event_seed_revision"])

    def test_multi_seed_scope_collision_preflight_prevents_partial_supersession(
        self,
    ) -> None:
        self.accept_outline(
            artifact_id="outline-A",
            payload={
                "event_seeds": [
                    {
                        "event_seed_id": "evt-A",
                        "dependency_order": 1,
                        "dramatic_function": "A 的旧职责",
                    }
                ]
            },
        )
        self.ensure(
            idempotency_key="multi-preflight-A-r1",
            artifact_context=self.artifact(
                accepted_outline_artifact_id="outline-A"
            ),
        )
        self.accept_outline(
            artifact_id="outline-B",
            payload={
                "event_seeds": [
                    {
                        "event_seed_id": "evt-foreign",
                        "dependency_order": 2,
                        "dramatic_function": "B 的事件",
                    }
                ]
            },
        )
        self.ensure(
            idempotency_key="multi-preflight-B-r1",
            artifact_context=self.artifact(
                accepted_outline_artifact_id="outline-B"
            ),
        )
        self.accept_outline(
            artifact_id="outline-A",
            artifact_revision=2,
            payload={
                "event_seeds": [
                    {
                        "event_seed_id": "evt-A",
                        "dependency_order": 1,
                        "dramatic_function": "A 的新职责",
                    },
                    {
                        "event_seed_id": "evt-foreign",
                        "dependency_order": 2,
                        "dramatic_function": "错误复用 B 的 ID",
                    },
                ]
            },
        )
        service = EventExperienceService.for_project(self.root)
        counts_before = service.storage_boundary_report()[
            "control_row_counts"
        ]
        revision_before = service.get_control_revision()

        with self.assert_error("EVENT_EXPERIENCE_SEED_SCOPE_COLLISION"):
            self.ensure(
                idempotency_key="multi-preflight-A-r2",
                artifact_context=self.artifact(
                    accepted_outline_artifact_id="outline-A"
                ),
            )

        self.assertEqual(
            counts_before,
            service.storage_boundary_report()["control_row_counts"],
        )
        self.assertEqual(revision_before, service.get_control_revision())
        self.assertEqual(1, service.get_seed("evt-A")["event_seed_revision"])
        self.assertEqual(
            "experience_locked",
            service.get_seed("evt-A", 1)["status"],
        )

    def test_outline_revision_drift_blocks_event_manifest(self) -> None:
        self.accept_outline(
            payload={
                "event_seeds": [
                    {
                        "dependency_order": 1,
                        "dramatic_function": "旧事件职责",
                    }
                ]
            }
        )
        locked = self.ensure()
        self.accept_outline(
            payload={
                "event_seeds": [
                    {
                        "dependency_order": 1,
                        "dramatic_function": "新事件职责",
                    }
                ]
            },
            artifact_revision=2,
        )
        with self.assert_error(
            "EVENT_EXPERIENCE_OUTLINE_BINDING_DRIFT"
        ):
            verify_locked_manifest(
                self.root,
                seed_references=locked["seed_references"],
                binding=locked["binding"],
            )

    def test_outline_same_revision_content_and_lifecycle_drift_blocks_locally(
        self,
    ) -> None:
        _, commit = self.accept_outline(
            payload={
                "event_seeds": [
                    {
                        "dependency_order": 1,
                        "dramatic_function": "守住同一版本的内容绑定",
                    }
                ]
            }
        )
        locked = self.ensure()
        service = EventExperienceService.for_project(self.root)
        counts_before = service.storage_boundary_report()[
            "control_row_counts"
        ]
        database = self.root / ".plot-rag" / "state.sqlite3"
        artifact_version_id = locked["binding"][
            "accepted_outline_binding"
        ]["source_outline_artifact_version_id"]
        with closing(sqlite3.connect(database)) as connection:
            row = connection.execute(
                """
                SELECT content_json, active
                FROM artifacts
                WHERE artifact_version_id=?
                """,
                (artifact_version_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            original_content_json = str(row[0])
            original_active = int(row[1])

            connection.execute(
                """
                UPDATE artifacts
                SET content_json=?
                WHERE artifact_version_id=?
                """,
                ('{"tampered":true}', artifact_version_id),
            )
            connection.commit()
            with (
                patch.object(
                    socket,
                    "create_connection",
                    side_effect=AssertionError("network call attempted"),
                ),
                self.assert_error(
                    "EVENT_EXPERIENCE_OUTLINE_BINDING_DRIFT"
                ),
            ):
                verify_locked_manifest(
                    self.root,
                    seed_references=locked["seed_references"],
                    binding=locked["binding"],
                )

            connection.execute(
                """
                UPDATE artifacts
                SET content_json=?, active=0
                WHERE artifact_version_id=?
                """,
                (original_content_json, artifact_version_id),
            )
            connection.commit()
            with self.assert_error(
                "EVENT_EXPERIENCE_OUTLINE_BINDING_DRIFT"
            ):
                verify_locked_manifest(
                    self.root,
                    seed_references=locked["seed_references"],
                    binding=locked["binding"],
                )

            connection.execute(
                """
                UPDATE artifacts
                SET active=?
                WHERE artifact_version_id=?
                """,
                (original_active, artifact_version_id),
            )
            connection.execute(
                """
                UPDATE canon_commits
                SET operation='retract'
                WHERE commit_id=?
                """,
                (commit["commit_id"],),
            )
            connection.commit()
            with self.assert_error(
                "EVENT_EXPERIENCE_OUTLINE_BINDING_DRIFT"
            ):
                verify_locked_manifest(
                    self.root,
                    seed_references=locked["seed_references"],
                    binding=locked["binding"],
                )

        self.assertEqual(
            counts_before,
            service.storage_boundary_report()["control_row_counts"],
        )

    def test_legacy_accepted_artifact_is_not_backfilled_and_next_revision_gets_contract(
        self,
    ) -> None:
        _, commit = self.accept_outline(
            payload={"outline_text": "旧章纲只有自由文本，没有结构化事件种子。"},
            artifact_id="chapter-001",
        )
        service = EventExperienceService.for_project(self.root)
        self.assertEqual(
            0,
            service.storage_boundary_report()["control_row_counts"][
                "event_seeds"
            ],
        )
        with self.assert_error(
            "EVENT_EXPERIENCE_GRANDFATHERED_REVISION_REQUIRED"
        ):
            self.ensure(
                artifact_context=self.artifact(
                    artifact_id="chapter-001",
                    artifact_revision=1,
                )
            )
        self.assertEqual(0, service.get_control_revision())
        result = self.ensure(
            artifact_context=self.artifact(
                artifact_id="chapter-001",
                artifact_revision=2,
            )
        )
        self.assertEqual("locked", result["action"])
        self.assertNotIn(
            "accepted_outline_binding",
            result["manifest"],
        )

        source_text = "旧章纲只有自由文本，没有结构化事件种子。"
        content_json = json.dumps(
            {"outline_text": source_text},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        observed = service.record_observed_review(
            {
                "artifact_id": "chapter-001",
                "artifact_revision": 1,
                "branch_id": "main",
                "source_commit_id": commit["commit_id"],
                "source_content_hash": hashlib.sha256(
                    content_json.encode("utf-8")
                ).hexdigest(),
                "assistant_sha256": hashlib.sha256(
                    source_text.encode("utf-8")
                ).hexdigest(),
                "observed_entry": "旧文本直接进入事件说明",
                "observed_peak": "未冻结 intended contract",
                "observed_exit": "仅保留观察结论",
                "supporting_quotes": [source_text],
                "supporting_quote_offsets": [[0, len(source_text)]],
                "drift": "observed only",
                "severity": "info",
                "recommendation": "新 revision 再建立 intended contract",
            },
            expected_control_revision=service.get_control_revision(),
            idempotency_key="grandfather-observed-review",
            source_text=source_text,
        )
        self.assertEqual(
            "grandfathered_observed_only",
            observed["review"]["review_mode"],
        )
        self.assertNotIn("contract_id", observed["review"])

    def test_invalid_or_unlocked_intent_fails_before_control_writes(self) -> None:
        with self.assert_error("EVENT_EXPERIENCE_RUNTIME_INTENT_NOT_LOCKED"):
            self.ensure(intent_contract=self.intent(status="ACTIVE"))
        service = EventExperienceService.for_project(self.root)
        self.assertEqual(0, service.get_control_revision())

        bad_hash = self.intent()
        bad_hash["intent_contract_hash"] = "0" * 64
        with self.assert_error(
            "EVENT_EXPERIENCE_RUNTIME_INTENT_HASH_MISMATCH"
        ):
            self.ensure(intent_contract=bad_hash)
        self.assertEqual(0, service.get_control_revision())

    def test_stale_initial_control_revision_has_zero_writes(self) -> None:
        service = EventExperienceService.for_project(self.root)
        service.create_seed(
            {
                "event_seed_id": "prior",
                "event_seed_revision": 1,
                "parent_chain_id": "prior-chain",
                "dependency_order": 1,
                "dramatic_function": "prior",
                "causal_role": "prior",
                "intended_state_change": "prior",
                "event_boundary": "prior",
                "artifact_id": "prior-artifact",
                "artifact_revision": 1,
                "branch_id": "main",
            },
            expected_control_revision=0,
            idempotency_key="prior",
        )
        with self.assert_error("EVENT_EXPERIENCE_STALE_CONTROL"):
            self.ensure(expected_control_revision=0)
        report = service.storage_boundary_report()
        self.assertEqual(1, report["control_row_counts"]["event_seeds"])
        self.assertEqual(0, report["control_row_counts"]["event_experience_arcs"])
        self.assertEqual(
            0, report["control_row_counts"]["event_experience_contracts"]
        )


if __name__ == "__main__":
    unittest.main()
