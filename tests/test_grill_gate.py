from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from grill_gate import GrillGateService


class GrillGateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "novel"
        self.root.mkdir()
        self.database = self.root / ".plot-rag" / "grill.sqlite3"
        self.service = GrillGateService(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def process(
        self,
        prompt: str,
        turn_id: str,
        *,
        task_family: str = "plot",
        continuation: bool = False,
        max_questions: int = 6,
    ) -> dict:
        return self.service.process(
            project_root=self.root,
            prompt=prompt,
            task_family=task_family,
            host_session_id="host-session",
            turn_id=turn_id,
            continuation=continuation,
            max_questions=max_questions,
        )

    def test_ambiguous_task_asks_exactly_one_question_with_recommendation(self) -> None:
        result = self.process("剧情推演", "turn-1")

        self.assertEqual("ask", result["action"])
        self.assertEqual("problem_to_solve", result["question"]["field"])
        self.assertEqual(1, result["question"]["index"])
        self.assertTrue(result["question"]["recommended_answer"])
        self.assertTrue(result["question"]["recommendation_rationale"])
        self.assertTrue(result["suppress_plot_receipt"])
        self.assertTrue(self.database.is_file())

    def test_generic_continue_repeats_current_question_without_revision_change(self) -> None:
        first = self.process("剧情推演", "turn-1")
        before = self.service.active(
            project_root=self.root,
            host_session_id="host-session",
        )

        for index, phrase in enumerate(
            (
                "继续",
                "开始吧",
                "下一步",
                "一口气推进到底，最后再审查",
                "按计划推进",
            ),
            start=2,
        ):
            with self.subTest(phrase=phrase):
                repeated = self.process(phrase, f"turn-{index}")
                after = self.service.active(
                    project_root=self.root,
                    host_session_id="host-session",
                )

                self.assertEqual("repeat_current_question", repeated["reason"])
                self.assertEqual(first["question"], repeated["question"])
                self.assertEqual(before["revision"], after["revision"])
                self.assertEqual(
                    before["question_index"],
                    after["question_index"],
                )

    def test_recommended_answer_requires_explicit_delegation_and_records_source(
        self,
    ) -> None:
        first = self.process("剧情推演", "turn-1")
        before = self.service.active(
            project_root=self.root,
            host_session_id="host-session",
        )
        empty = self.process("", "turn-empty")
        after_empty = self.service.active(
            project_root=self.root,
            host_session_id="host-session",
        )
        delegated = self.process("按推荐答案", "turn-2")

        self.assertEqual("empty_answer_repeats_question", empty["reason"])
        self.assertEqual(before["revision"], after_empty["revision"])
        self.assertEqual("ask", delegated["action"])
        self.assertEqual(
            first["question"]["recommended_answer"],
            delegated["contract"]["fields"]["problem_to_solve"]["value"],
        )
        self.assertEqual(
            "recommended_delegation",
            delegated["contract"]["fields"]["problem_to_solve"]["source"],
        )

    def test_answer_advances_one_dependency_and_explicit_skip_hands_off(self) -> None:
        self.process("剧情推演", "turn-1")
        second = self.process(
            "测试角色甲必须拿到临时通行证，守卫阻止，失败就会暴露身份。",
            "turn-2",
        )
        handed_off = self.process("跳过目的确认", "turn-3")

        self.assertEqual("ask", second["action"])
        self.assertEqual("reader_experience", second["question"]["field"])
        self.assertEqual("proceed", handed_off["action"])
        self.assertEqual("explicit_skip", handed_off["reason"])
        self.assertIn("[LOCKED_INTENT_CONTRACT]", handed_off["execution_prompt"])
        for field in handed_off["contract"]["fields"].values():
            self.assertTrue(field["value"])

    def test_question_limit_fills_remaining_fields_and_hands_off(self) -> None:
        self.process("剧情推演", "turn-1", max_questions=2)
        second = self.process("解决主角如何进入封锁城的问题", "turn-2", max_questions=2)
        final = self.process("制造压迫感与阶段兑现", "turn-3", max_questions=2)

        self.assertEqual(2, second["question"]["index"])
        self.assertEqual("proceed", final["action"])
        self.assertEqual("question_limit_reached", final["reason"])

    def test_specific_task_uses_zero_question_fast_path(self) -> None:
        result = self.process(
            "推演下一章：测试角色甲想拿到通行证，守卫阻止；"
            "以测试角色甲获得临时证但身份暴露为终点，制造压迫与阶段爽点，"
            "保持主角限知，不新增核心法器。",
            "turn-specific",
        )

        self.assertEqual("proceed", result["action"])
        self.assertEqual("intent_already_clear", result["reason"])

    def test_completed_contract_is_reused_for_continuation(self) -> None:
        handed_off = self.process(
            "跳过目的确认，推演下一章测试角色甲遭遇盘查。",
            "turn-1",
        )
        self.assertEqual("proceed", handed_off["action"])
        self.service.complete_execution(
            project_root=self.root,
            host_session_id="host-session",
        )

        continued = self.process(
            "继续",
            "turn-2",
            continuation=True,
        )

        self.assertEqual("proceed", continued["action"])
        self.assertEqual("inherited_locked_contract", continued["reason"])
        self.assertEqual(
            handed_off["grill_session_id"],
            self.service.complete_execution(
                project_root=self.root,
                host_session_id="host-session",
            )["parent_grill_session_id"],
        )

    def test_unprepared_execution_is_not_reused_for_continuation(self) -> None:
        first = self.process(
            "跳过目的确认，推演下一章测试角色甲遭遇盘查。",
            "turn-1",
        )
        continued = self.process(
            "继续",
            "turn-2",
            continuation=True,
        )

        self.assertEqual("proceed", first["action"])
        self.assertEqual("ask", continued["action"])
        self.assertNotEqual("inherited_locked_contract", continued["reason"])

    def test_prepare_and_complete_bind_to_exact_grill_session(self) -> None:
        first = self.process(
            "跳过目的确认，推演下一章测试角色甲遭遇盘查。",
            "turn-a",
        )
        second = self.process(
            "跳过目的确认，推演下一章林清遭遇追杀。",
            "turn-b",
        )
        prepared = {
            "status": "prepared",
            "receipt_id": "receipt-a",
            "context": "fixture",
        }

        first_state = self.service.mark_prepared(
            project_root=self.root,
            host_session_id="host-session",
            grill_session_id=first["grill_session_id"],
            expected_session_revision=first["session_revision"],
            receipt_id="receipt-a",
            prepare_status="prepared",
            turn_id="turn-a",
            prepare_result=prepared,
        )
        replayed = self.service.mark_prepared(
            project_root=self.root,
            host_session_id="host-session",
            grill_session_id=first["grill_session_id"],
            expected_session_revision=first["session_revision"],
            receipt_id="receipt-a",
            prepare_status="prepared",
            turn_id="turn-a",
            prepare_result=prepared,
        )
        for invalid_revision in (True, 1.0, "1", -1):
            with self.subTest(value=repr(invalid_revision)):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "non-negative integer",
                ):
                    self.service.mark_prepared(
                        project_root=self.root,
                        host_session_id="host-session",
                        grill_session_id=first["grill_session_id"],
                        expected_session_revision=invalid_revision,
                        receipt_id="receipt-a",
                        prepare_status="prepared",
                        turn_id="turn-a",
                        prepare_result=prepared,
                    )
        second_state = self.service.session_state(
            project_root=self.root,
            host_session_id="host-session",
            grill_session_id=second["grill_session_id"],
        )
        completed = self.service.complete_execution(
            project_root=self.root,
            host_session_id="host-session",
            handoff_turn_id="turn-a",
        )

        self.assertEqual("receipt-a", first_state["prepared_receipt_id"])
        self.assertEqual(first_state["revision"], replayed["revision"])
        self.assertNotIn("prepared_receipt_id", second_state)
        self.assertEqual(first["grill_session_id"], completed["grill_session_id"])
        self.assertEqual("COMPLETED", completed["status"])
        self.assertEqual(
            "EXECUTING",
            self.service.session_state(
                project_root=self.root,
                host_session_id="host-session",
                grill_session_id=second["grill_session_id"],
            )["status"],
        )

    def test_mark_prepared_rejects_malformed_persisted_revision(self) -> None:
        for index, malformed in enumerate((True, 1.0, "1"), start=1):
            with self.subTest(json_revision=repr(malformed)):
                handed_off = self.process(
                    (
                        "跳过目的确认，推演下一章："
                        f"测试角色甲处理第{index}次城门盘查。"
                    ),
                    f"turn-malformed-revision-{index}",
                )
                with closing(sqlite3.connect(self.database)) as connection:
                    row = connection.execute(
                        """
                        SELECT state_json
                        FROM grill_sessions
                        WHERE grill_session_id=?
                        """,
                        (handed_off["grill_session_id"],),
                    ).fetchone()
                    state = json.loads(str(row[0]))
                    state["revision"] = malformed
                    connection.execute(
                        """
                        UPDATE grill_sessions
                        SET state_json=?
                        WHERE grill_session_id=?
                        """,
                        (
                            json.dumps(state, ensure_ascii=False),
                            handed_off["grill_session_id"],
                        ),
                    )
                    connection.commit()
                with self.assertRaisesRegex(
                    RuntimeError,
                    "state revision must be a non-negative integer",
                ):
                    self.service.mark_prepared(
                        project_root=self.root,
                        host_session_id="host-session",
                        grill_session_id=handed_off["grill_session_id"],
                        expected_session_revision=handed_off[
                            "session_revision"
                        ],
                        receipt_id=f"receipt-malformed-{index}",
                        prepare_status="prepared",
                        turn_id=f"turn-malformed-revision-{index}",
                        prepare_result={"status": "prepared"},
                    )

        handed_off = self.process(
            "跳过目的确认，推演下一章：测试角色甲处理数据库 revision 不一致。",
            "turn-revision-mismatch",
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                UPDATE grill_sessions
                SET revision=revision+1
                WHERE grill_session_id=?
                """,
                (handed_off["grill_session_id"],),
            )
            connection.commit()
        with self.assertRaisesRegex(
            RuntimeError,
            "revision does not match database revision",
        ):
            self.service.mark_prepared(
                project_root=self.root,
                host_session_id="host-session",
                grill_session_id=handed_off["grill_session_id"],
                expected_session_revision=handed_off["session_revision"],
                receipt_id="receipt-revision-mismatch",
                prepare_status="prepared",
                turn_id="turn-revision-mismatch",
                prepare_result={"status": "prepared"},
            )

    def test_fail_handoff_rewrites_cached_response_idempotently(self) -> None:
        handed_off = self.process(
            "跳过目的确认，推演下一章测试角色甲遭遇盘查。",
            "turn-failed-handoff",
        )

        failed = self.service.fail_handoff(
            project_root=self.root,
            host_session_id="host-session",
            grill_session_id=handed_off["grill_session_id"],
            turn_id="turn-failed-handoff",
            reason="fixture persistence failure",
        )
        replayed = self.service.fail_handoff(
            project_root=self.root,
            host_session_id="host-session",
            grill_session_id=handed_off["grill_session_id"],
            turn_id="turn-failed-handoff",
            reason="fixture persistence failure",
        )
        cached = self.service.turn_response(
            project_root=self.root,
            host_session_id="host-session",
            turn_id="turn-failed-handoff",
        )
        state = self.service.session_state(
            project_root=self.root,
            host_session_id="host-session",
            grill_session_id=handed_off["grill_session_id"],
        )

        self.assertEqual("conflict", failed["action"])
        self.assertEqual(failed, replayed)
        self.assertEqual(failed, cached)
        self.assertTrue(cached["suppress_plot_stop_extract"])
        self.assertEqual("HANDOFF_FAILED", state["status"])
        self.assertEqual(failed["session_revision"], state["revision"])

    def test_same_turn_different_request_is_conflict_and_projects_are_isolated(self) -> None:
        first = self.process("剧情推演", "same-turn")
        conflict = self.process("写下一章", "same-turn")

        other_root = Path(self.temporary.name) / "other"
        other_root.mkdir()
        other = self.service.process(
            project_root=other_root,
            prompt="写下一章",
            task_family="plot",
            host_session_id="host-session",
            turn_id="same-turn",
        )

        self.assertEqual("ask", first["action"])
        self.assertEqual("conflict", conflict["action"])
        self.assertEqual("turn_id_request_conflict", conflict["reason"])
        self.assertEqual("ask", other["action"])

    def test_read_only_turn_replay_validates_request_hash_when_prompt_is_supplied(
        self,
    ) -> None:
        first = self.process(
            "跳过目的确认，初始化一部作品。",
            "same-init-turn",
            task_family="initialization",
        )

        replayed = self.service.turn_response(
            project_root=self.root,
            host_session_id="host-session",
            turn_id="same-init-turn",
            prompt="跳过目的确认，初始化一部作品。",
            task_family="initialization",
        )
        conflict = self.service.turn_response(
            project_root=self.root,
            host_session_id="host-session",
            turn_id="same-init-turn",
            prompt="把题材改成科幻。",
            task_family="initialization",
        )

        self.assertEqual(first, replayed)
        self.assertEqual("conflict", conflict["action"])
        self.assertEqual("turn_id_request_conflict", conflict["reason"])

    def test_read_only_active_lookup_does_not_create_database(self) -> None:
        self.assertIsNone(
            self.service.active(
                project_root=self.root,
                host_session_id="host-session",
            )
        )
        self.assertFalse(self.database.exists())

    def test_unknown_database_schema_is_not_rewritten(self) -> None:
        self.database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "CREATE TABLE grill_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO grill_meta(key, value) VALUES('schema_version', '999')"
            )
            connection.commit()
        before = self.database.read_bytes()

        with self.assertRaisesRegex(RuntimeError, "unsupported Grill database"):
            self.process("剧情推演", "turn-future")

        self.assertEqual(before, self.database.read_bytes())
        with closing(sqlite3.connect(self.database)) as connection:
            stored = connection.execute(
                "SELECT value FROM grill_meta WHERE key='schema_version'"
            ).fetchone()[0]
        self.assertEqual("999", stored)

    def test_intent_schema_closes_root_and_field_objects(self) -> None:
        schema = json.loads(
            (
                PLUGIN_ROOT
                / "schemas"
                / "plot-rag-intent"
                / "v1"
                / "intent-contract.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["properties"]["fields"]["additionalProperties"])
        self.assertFalse(schema["$defs"]["field"]["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
