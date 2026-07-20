from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HOOKS = PLUGIN_ROOT / "hooks"
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import plot_progression_gate as hook
from continuity import ContinuityService, HostApprovalAuthority
from event_experience import EventExperienceService, canonical_hash
from extraction_jobs import ExtractionJobQueue
from plot_init import PlotInitService


class HookTestCase(unittest.TestCase):
    def make_project(self, base: Path, config: str | None = None) -> Path:
        root = base / "novel"
        (root / ".plot-rag").mkdir(parents=True)
        if config is None:
            config = json.dumps(
                {
                    "version": 2,
                    "enabled": True,
                    "trigger_short_continue": True,
                    "grill": {"enabled": False},
                    "authority_globs": ["settings/*.md"],
                    "craft": {
                        "enabled": True,
                        "auto_retrieve": True,
                        "use_embedding": False,
                        "use_rerank": False,
                    },
                    "remote": {
                        "embedding": {"enabled": False},
                        "rerank": {"enabled": False},
                        "extract": {"enabled": False},
                    },
                }
            )
        (root / ".plot-rag" / "config.json").write_text(config, encoding="utf-8")
        return root

    def make_v3_project(self, base: Path) -> Path:
        root = base / "novel"
        (root / ".plot-rag").mkdir(parents=True)
        (root / "正文").mkdir()
        (root / "正文" / "第一章.md").write_text(
            "测试角色甲正在测试城南站。",
            encoding="utf-8",
        )
        config = {
            "config_version": 3,
            "enabled": True,
            "trigger_short_continue": True,
            "grill": {"enabled": False},
            "event_experience": {"enabled": False},
            "authority_sources": [
                {
                    "glob": "正文/**/*.md",
                    "role": "canon",
                    "scope_policy": "infer_and_review",
                    "ingest_policy": "include",
                    "priority": 100,
                }
            ],
            "remote": {
                "embedding": {"enabled": False},
                "rerank": {"enabled": False},
                "extract": {"enabled": False},
            },
        }
        (root / ".plot-rag" / "config.json").write_text(
            json.dumps(config, ensure_ascii=False),
            encoding="utf-8",
        )
        return root

    def make_grill_project(self, base: Path) -> Path:
        root = self.make_v3_project(base)
        config_path = root / ".plot-rag" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config.pop("grill", None)
        # Legacy Grill behavior remains covered independently from the v1.5
        # event-experience hard gate.  Dedicated tests below enable that gate.
        config["event_experience"] = {"enabled": False}
        config_path.write_text(
            json.dumps(config, ensure_ascii=False),
            encoding="utf-8",
        )
        return root

    def enable_v15_event_experience(
        self,
        project: Path,
        *,
        extraction_mode: str = "sync",
        async_shadow: bool = True,
    ) -> None:
        config_path = project / ".plot-rag" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["event_experience"] = {
            "enabled": True,
            "required_before_event_design": True,
        }
        config["performance"] = {
            "extraction": {
                "mode": extraction_mode,
                "async_shadow": async_shadow,
                "next_plot_turn_barrier": True,
                "barrier_requires_proposal_resolution": True,
            }
        }
        config_path.write_text(
            json.dumps(config, ensure_ascii=False),
            encoding="utf-8",
        )

    def create_locked_event_experience(
        self,
        project: Path,
        *,
        intent_id: str,
        intent_revision: int,
        intent_hash: str,
        seed_id: str = "seed-hook-1",
        order: int = 1,
    ) -> list[dict[str, object]]:
        service = EventExperienceService.for_project(project)
        seed = service.create_seed(
            {
                "event_seed_id": seed_id,
                "event_seed_revision": 1,
                "parent_chain_id": f"chain-{intent_id}",
                "dependency_order": order,
                "dramatic_function": "逼迫主角作出不可逆选择",
                "causal_role": "升级主动对手反应",
                "intended_state_change": "主角失去旧退路并获得新目标",
                "event_boundary": "从收到最后通牒到作出选择",
                "narrative_event_id": f"event-{seed_id}",
                "artifact_id": "chapter-hook",
                "artifact_revision": 1,
                "branch_id": "main",
                "chapter_no": 1,
                "scene_index": order,
            },
            expected_control_revision=service.get_control_revision(),
            idempotency_key=f"{seed_id}:seed",
        )["seed"]
        service.propose_and_lock_contract(
            {
                "contract_id": f"contract-{seed_id}",
                "contract_revision": 1,
                "event_seed_id": seed_id,
                "event_seed_revision": 1,
                "source_intent_contract_id": intent_id,
                "source_intent_contract_revision": intent_revision,
                "source_intent_contract_hash": intent_hash,
                "entry_reader_state": "担忧",
                "target_reader_state": "压迫中产生希望",
                "primary_emotion": "紧张",
                "ordered_secondary_emotions": ["不安", "期待"],
                "emotional_turn": "从被动压迫转向有限主动",
                "intensity": {"entry": 35, "peak": 82, "exit": 55},
                "emotion_curve": [
                    "期待",
                    "压迫",
                    "惊讶",
                    "短促释放",
                    "余悸",
                ],
                "mechanisms": ["信息差", "选择代价", "局部反击"],
                "reader_knowledge_position": "与视角人物同步",
                "viewpoint_character_state": "谨慎评估退路",
                "payoff_or_reveal": "兑现局部脱困并暴露更大代价",
                "aftertaste": "短暂希望后留下身份暴露余悸",
                "anti_experiences": ["滑稽化", "无代价开挂"],
                "success_signals": [
                    "选择改变后续前提",
                    "结尾保留持续压力",
                ],
                "derivation": {
                    "source": "locked_intent_contract",
                    "confidence": 0.94,
                    "user_confirmed": True,
                    "delegated_choice": False,
                },
                "field_provenance": {
                    field: {
                        "source": "test locked intent/artifact context",
                        "source_intent_contract_hash": intent_hash,
                    }
                    for field in (
                        "entry_reader_state",
                        "target_reader_state",
                        "primary_emotion",
                        "emotional_turn",
                        "intensity",
                        "emotion_curve",
                        "mechanisms",
                        "reader_knowledge_position",
                        "viewpoint_character_state",
                        "payoff_or_reveal",
                        "aftertaste",
                        "anti_experiences",
                        "success_signals",
                    )
                },
            },
            expected_control_revision=service.get_control_revision(),
            idempotency_key=f"{seed_id}:contract",
        )
        return [
            {
                "event_seed_id": seed["event_seed_id"],
                "event_seed_revision": seed["event_seed_revision"],
            }
        ]

    def run_hook(
        self,
        project: Path,
        prompt: str,
        *,
        transcript_path: Path | None = None,
        turn_id: str | None = "turn-test",
        session_id: str | None = "session-test",
        extra_payload: dict[str, object] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        payload = {
            "cwd": str(project),
            "prompt": prompt,
        }
        if extra_payload:
            payload.update(extra_payload)
        if session_id is not None:
            payload["session_id"] = session_id
        if turn_id is not None:
            payload["turn_id"] = turn_id
        if transcript_path is not None:
            payload["transcript_path"] = str(transcript_path)
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(HOOKS / "plot_progression_gate.py")],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            cwd=project,
        )

    def run_session_start(
        self,
        project: Path,
        *,
        disable_worker: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        if disable_worker:
            environment["PLOT_RAG_GATE_WORKER_DISABLED"] = "1"
        return subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                str(HOOKS / "plot_progression_gate.py"),
                "--session-start",
            ],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            cwd=project,
            env=environment,
        )

    def run_session_end(
        self,
        project: Path,
        *,
        session_id: str = "session-test",
        branch_id: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        payload: dict[str, object] = {
            "hook_event_name": "SessionEnd",
            "cwd": str(project),
            "session_id": session_id,
        }
        if branch_id is not None:
            payload["branch_id"] = branch_id
        return subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                str(HOOKS / "plot_progression_gate.py"),
                "--session-end",
            ],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            cwd=project,
        )

    def run_stop(
        self,
        project: Path,
        assistant_text: str,
        *,
        turn_id: str = "turn-test",
        disable_worker: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        payload = {
            "hook_event_name": "Stop",
            "cwd": str(project),
            "last_assistant_message": assistant_text,
            "session_id": "session-test",
            "turn_id": turn_id,
        }
        environment = os.environ.copy()
        if disable_worker:
            environment["PLOT_RAG_GATE_WORKER_DISABLED"] = "1"
        return subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                str(HOOKS / "plot_progression_gate.py"),
                "--stop",
            ],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            cwd=project,
            env=environment,
        )

    def run_stop_hook(
        self,
        project: Path,
        assistant_text: str,
        *,
        turn_id: str = "turn-test",
    ) -> subprocess.CompletedProcess[str]:
        payload = {
            "hook_event_name": "Stop",
            "cwd": str(project),
            "last_assistant_message": assistant_text,
            "session_id": "session-test",
            "turn_id": turn_id,
        }
        return subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                str(HOOKS / "plot_progression_gate.py"),
                "--stop",
            ],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            cwd=project,
        )

    def write_transcript(
        self,
        path: Path,
        messages: list[tuple[str, str]],
        *,
        trailing_invalid_json: bool = False,
    ) -> None:
        rows = []
        for text, turn_id in messages:
            rows.append(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": text}],
                            "internal_chat_message_metadata_passthrough": {
                                "turn_id": turn_id
                            },
                        },
                    },
                    ensure_ascii=False,
                )
            )
        if trailing_invalid_json:
            rows.append('{"type":"response_item"')
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def test_trigger_classifier_separates_plot_from_setting_work(self) -> None:
        positives = [
            "继续",
            "继续。",
            "继续推进剧情",
            "规划后续事件链",
            "写第一章正文",
            "写下一章",
            "剧情推演",
            "推演下一幕剧情",
            "请用这个插件推演下一章谈判场景",
            "直接使用剧情RAG门禁继续推进剧情",
            "接下来会发生什么？",
            "续一章",
            "再来一章",
            "把下一章写出来",
            "接着上一章写",
            "把这一幕写完",
            "给我来一段正文",
            "规划本卷后半段",
            "设计接下来的冲突",
            "把章纲扩成正文",
        ]
        negatives = [
            "全面审查现有世界观",
            "查询测试角色甲的境界设定",
            "告诉我界海如何设定",
            "继续推进世界观",
            "不要继续写正文",
            "暂时不要推进剧情",
            "分析如何写好剧情",
            "现在当我提到“剧情推演”的时候，这个插件将会进行什么流程",
            "“剧情推演”这个词会触发插件吗？",
            "剧情推演功能的执行流程是什么？",
            "解释一下剧情RAG门禁如何工作",
            "为什么我说剧情推演时插件会启动？",
            "如何使用插件推演下一章剧情？",
            "剧情推演应该如何进行？",
            "剧情推演有哪些步骤？",
            "给剧情推演增加测试",
            "剧情推演的关键词有哪些",
            "审查剧情推演流程",
            "升级剧情写作功能",
            "优化剧情推演正则",
            "当我说再来一章时会触发吗？",
            "“续一章”会触发吗？",
            "续一章的触发规则是什么",
            "再来一章会触发插件吗",
            "当主角说“开门”时会触发法阵吗",
        ]
        for prompt in positives:
            with self.subTest(prompt=prompt):
                self.assertTrue(hook.is_plot_progression(prompt))
        for prompt in negatives:
            with self.subTest(prompt=prompt):
                self.assertFalse(hook.is_plot_progression(prompt))

    def test_story_trigger_question_is_not_plugin_meta_work(self) -> None:
        prompt = "当主角说“开门”时会触发法阵吗"
        self.assertEqual("other_work", hook.classify_task_family(prompt))
        self.assertFalse(hook.is_meta_plot_discussion(prompt))

    def test_configured_skip_phrase_preserves_plot_and_real_negation(self) -> None:
        skip_phrases = ["直接执行，不要追问"]
        for prompt in (
            "直接执行，不要追问，写下一章",
            "直接执行不要追问，继续推进剧情",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(
                    "plot",
                    hook.classify_task_family(
                        prompt,
                        skip_phrases=skip_phrases,
                    ),
                )
        self.assertEqual(
            "plot",
            hook.classify_task_family(
                "直接写下一章，不要追问",
                skip_phrases=["直接写下一章，不要追问"],
            ),
        )
        for prompt in (
            "直接执行，不要追问，但不要写下一章",
            "直接执行，不要追问，但别写下一章正文",
            "直接执行，不要追问，但禁止写下一章正文",
            "不要继续写正文，直接执行，不要追问",
            "继续，直接执行，不要追问",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(
                    "other_work",
                    hook.classify_task_family(
                        prompt,
                        skip_phrases=skip_phrases,
                    ),
                )
        for prompt in (
            "为什么“直接执行，不要追问，写下一章”会启动？",
            "“直接执行，不要追问，写下一章”是什么意思？",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(
                    "meta_init",
                    hook.classify_task_family(
                        prompt,
                        skip_phrases=skip_phrases,
                    ),
                )
                self.assertFalse(
                    hook.is_plot_progression(
                        prompt,
                        skip_phrases=skip_phrases,
                    )
                )

    def test_short_continue_can_be_disabled(self) -> None:
        self.assertFalse(hook.is_plot_progression("继续", allow_short_continue=False))
        self.assertTrue(hook.is_plot_progression("继续推进剧情", allow_short_continue=False))

    def test_hook_emits_valid_additional_context_for_plot_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_project(Path(temporary))
            completed = self.run_hook(project, "规划后续事件链")
            self.assertEqual(0, completed.returncode, completed.stderr)
            output = json.loads(completed.stdout)
            specific = output["hookSpecificOutput"]
            self.assertEqual("UserPromptSubmit", specific["hookEventName"])
            context = specific["additionalContext"]
            self.assertIn("plot_rag.py", context)
            self.assertIn("--request-id", context)
            self.assertIn("MISS_CONFIRMED", context)
            project_lines = [
                line
                for line in context.splitlines()
                if line.startswith("project_root:")
            ]
            self.assertEqual(1, len(project_lines))
            reported_project = Path(
                project_lines[0].partition(":")[2].strip()
            )
            self.assertEqual(
                os.path.normcase(str(project.resolve(strict=False))),
                os.path.normcase(
                    str(reported_project.resolve(strict=False))
                ),
            )
            self.assertIn("[STATE_RAG_RECEIPT]", context)

    def test_default_grill_asks_one_question_and_stop_writes_no_plot_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            submitted = self.run_hook(
                project,
                "剧情推演",
                turn_id="grill-turn-1",
            )
            stopped = self.run_stop(
                project,
                "测试角色甲获得青铜钥匙，并进入测试城。",
                turn_id="grill-turn-1",
            )

            self.assertEqual(0, submitted.returncode, submitted.stderr)
            context = json.loads(submitted.stdout)["hookSpecificOutput"][
                "additionalContext"
            ]
            self.assertIn("[PLOT_RAG_GRILL]", context)
            self.assertIn("Q1/6:", context)
            self.assertEqual(1, context.count("Recommended answer:"))
            self.assertNotIn("[PLOT_RAG_GATE:剧情推进检索门禁]", context)
            self.assertTrue(
                (project / ".plot-rag" / "grill.sqlite3").is_file()
            )
            self.assertFalse(
                (project / ".plot-rag" / "state.sqlite3").exists()
            )
            self.assertIn("Grill owns this turn", stopped.stdout)
            self.assertFalse(
                (project / ".plot-rag" / "state.sqlite3").exists()
            )

    def test_stop_lookup_failure_keeps_grill_output_out_of_plot_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            submitted = self.run_hook(
                project,
                "剧情推演",
                turn_id="grill-corrupt-1",
            )
            self.assertEqual(0, submitted.returncode, submitted.stderr)
            (project / ".plot-rag" / "grill.sqlite3").write_bytes(
                b"corrupt-grill-store"
            )

            stopped = self.run_stop(
                project,
                "测试角色甲获得青铜钥匙，并进入测试城。",
                turn_id="grill-corrupt-1",
            )

            self.assertEqual(0, stopped.returncode, stopped.stderr)
            self.assertIn("Grill state lookup failed", stopped.stdout)
            self.assertIn("plot extraction suppressed", stopped.stdout)
            self.assertFalse(
                (project / ".plot-rag" / "state.sqlite3").exists()
            )

    def test_grill_answer_advances_one_question_then_skip_prepares_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            self.run_hook(project, "剧情推演", turn_id="grill-seq-1")
            answered = self.run_hook(
                project,
                "测试角色甲必须拿到临时通行证，守卫阻止，失败就会暴露身份。",
                turn_id="grill-seq-2",
            )
            handed_off = self.run_hook(
                project,
                "跳过目的确认",
                turn_id="grill-seq-3",
            )
            replayed = self.run_hook(
                project,
                "跳过目的确认",
                turn_id="grill-seq-3",
            )

            answer_context = json.loads(answered.stdout)["hookSpecificOutput"][
                "additionalContext"
            ]
            handoff_context = json.loads(handed_off.stdout)["hookSpecificOutput"][
                "additionalContext"
            ]
            replay_context = json.loads(replayed.stdout)["hookSpecificOutput"][
                "additionalContext"
            ]
            with closing(
                sqlite3.connect(project / ".plot-rag" / "state.sqlite3")
            ) as connection:
                prepared_turns = int(
                    connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
                )
            with closing(
                sqlite3.connect(project / ".plot-rag" / "grill.sqlite3")
            ) as connection:
                state_json = connection.execute(
                    """
                    SELECT state_json
                    FROM grill_sessions
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """
                ).fetchone()[0]
                revision = int(json.loads(state_json)["revision"])

        self.assertIn("Q2/6:", answer_context)
        self.assertEqual(1, answer_context.count("Recommended answer:"))
        self.assertIn("shared_understanding_reached: true", handoff_context)
        self.assertIn("[PLOT_RAG_GATE:剧情推进检索门禁]", handoff_context)
        self.assertIn("shared_understanding_reached: true", replay_context)
        handoff_receipt = next(
            line
            for line in handoff_context.splitlines()
            if line.startswith("state_receipt_id:")
        )
        replay_receipt = next(
            line
            for line in replay_context.splitlines()
            if line.startswith("state_receipt_id:")
        )
        self.assertEqual(handoff_receipt, replay_receipt)
        self.assertEqual(1, prepared_turns)
        self.assertGreaterEqual(revision, 3)

    def test_fresh_plot_skip_phrase_hands_off_but_real_negation_stays_silent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            handed_off = self.run_hook(
                project,
                "直接执行，不要追问，写下一章：测试角色甲进入测试城。",
                turn_id="grill-fresh-skip",
            )

            self.assertEqual(0, handed_off.returncode, handed_off.stderr)
            context = json.loads(handed_off.stdout)["hookSpecificOutput"][
                "additionalContext"
            ]
            self.assertIn("[PLOT_RAG_GRILL]", context)
            self.assertIn("reason: explicit_skip", context)
            self.assertIn("shared_understanding_reached: true", context)
            self.assertNotIn("Q1/", context)
            self.assertIn("[PLOT_RAG_GATE:剧情推进检索门禁]", context)
            self.assertTrue(
                (project / ".plot-rag" / "state.sqlite3").is_file()
            )

        for index, prompt in enumerate(
            (
                "直接执行，不要追问，但不要写下一章正文。",
                "直接执行，不要追问，但别写下一章正文。",
                "为什么“直接执行，不要追问，写下一章”会启动？",
            ),
            start=1,
        ):
            with self.subTest(prompt=prompt), tempfile.TemporaryDirectory() as temporary:
                project = self.make_grill_project(Path(temporary))
                negated = self.run_hook(
                    project,
                    prompt,
                    turn_id=f"grill-fresh-silent-{index}",
                )

                self.assertEqual(0, negated.returncode, negated.stderr)
                self.assertEqual("", negated.stdout)
                self.assertFalse(
                    (project / ".plot-rag" / "grill.sqlite3").exists()
                )
                self.assertFalse(
                    (project / ".plot-rag" / "state.sqlite3").exists()
                )

    def test_specific_intent_uses_fast_path_without_question(self) -> None:
        prompt = (
            "推演下一章：测试角色甲想拿到通行证，守卫阻止；"
            "以测试角色甲获得临时证但身份暴露为终点，制造压迫与阶段爽点，"
            "保持主角限知，不新增核心法器。"
        )
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            completed = self.run_hook(
                project,
                prompt,
                turn_id="grill-fast-path",
            )

        context = json.loads(completed.stdout)["hookSpecificOutput"][
            "additionalContext"
        ]
        self.assertIn("shared_understanding_reached: true", context)
        self.assertNotIn("Q1/", context)
        self.assertIn("[PLOT_RAG_GATE:剧情推进检索门禁]", context)

    def test_v15_explicit_reader_experience_auto_locks_and_prepares_atomically(
        self,
    ) -> None:
        prompt = (
            "推演下一章：测试角色甲想拿到通行证，守卫阻止；"
            "以测试角色甲获得临时证但身份暴露为终点，制造压迫与阶段爽点，"
            "保持主角限知，不新增核心法器。"
        )
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            self.enable_v15_event_experience(project)
            prepared = self.run_hook(
                project,
                prompt,
                turn_id="v15-experience-turn",
            )
            self.assertEqual(0, prepared.returncode, prepared.stderr)
            payload = json.loads(prepared.stdout)
            context = payload["hookSpecificOutput"][
                "additionalContext"
            ]
            self.assertNotIn("decision", payload)
            self.assertIn("status: locked", context)
            self.assertIn("event_seed_manifest_hash:", context)
            self.assertIn("remote_called: false", context)
            self.assertIn("[STATE_RAG_RECEIPT]", context)
            self.assertLess(
                context.index("[PLOT_RAG_GRILL]"),
                context.index("[PLOT_RAG_EVENT_EXPERIENCE]"),
            )
            self.assertLess(
                context.index("[PLOT_RAG_EVENT_EXPERIENCE]"),
                context.index("[PLOT_RAG_GATE:剧情推进检索门禁]"),
            )
            with closing(
                sqlite3.connect(project / ".plot-rag" / "state.sqlite3")
            ) as connection:
                row = connection.execute(
                    """
                    SELECT lifecycle_identity_json, prompt
                    FROM turns
                    """
                ).fetchone()
                self.assertIsNotNone(row)
                lifecycle_identity = json.loads(row[0])
                self.assertEqual(
                    64,
                    len(lifecycle_identity["intent_contract_hash"]),
                )
                self.assertEqual(
                    64,
                    len(
                        lifecycle_identity[
                            "event_seed_manifest_hash"
                        ]
                    ),
                )
                self.assertTrue(
                    lifecycle_identity["experience_contract_hashes"]
                )
                self.assertTrue(
                    lifecycle_identity["event_seed_references"]
                )
                self.assertIn(
                    "[LOCKED_EVENT_EXPERIENCE_MANIFEST]",
                    str(row[1]),
                )

    def test_v15_ambiguous_experience_asks_repeats_then_resumes_original_plot(
        self,
    ) -> None:
        prompt = (
            "推演下一章的事件链：测试角色甲想拿到通行证，守卫阻止；"
            "以测试角色甲失去旧退路为终点，完成后必须形成不可逆选择；"
            "保持主角限知，严格按我的终点与约束，情绪路径由我裁决。"
        )
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            self.enable_v15_event_experience(project)

            asked = self.run_hook(
                project,
                prompt,
                turn_id="v15-ambiguous-start",
            )
            asked_payload = json.loads(asked.stdout)
            asked_context = asked_payload["hookSpecificOutput"][
                "additionalContext"
            ]
            self.assertEqual("block", asked_payload["decision"])
            self.assertIn("status: ask", asked_context)
            self.assertIn("Question:", asked_context)
            self.assertIn("Recommended answer: C", asked_context)
            self.assertNotIn("[STATE_RAG_RECEIPT]", asked_context)
            with closing(
                sqlite3.connect(project / ".plot-rag" / "state.sqlite3")
            ) as connection:
                self.assertEqual(
                    0,
                    connection.execute("SELECT COUNT(*) FROM turns").fetchone()[
                        0
                    ],
                )
            with closing(
                sqlite3.connect(project / ".plot-rag" / "grill.sqlite3")
            ) as connection:
                state = json.loads(
                    connection.execute(
                        """
                        SELECT state_json
                        FROM grill_sessions
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """
                    ).fetchone()[0]
                )
                self.assertEqual(
                    "awaiting_event_experience",
                    state["prepare_status"],
                )
                self.assertTrue(
                    state["prepare_result"]["intent_contract_hash"]
                )

            repeated = self.run_hook(
                project,
                "继续",
                turn_id="v15-ambiguous-repeat",
            )
            repeated_payload = json.loads(repeated.stdout)
            repeated_context = repeated_payload["hookSpecificOutput"][
                "additionalContext"
            ]
            self.assertEqual("block", repeated_payload["decision"])
            self.assertIn("status: ask", repeated_context)
            self.assertIn("Question:", repeated_context)
            self.assertNotIn("[STATE_RAG_RECEIPT]", repeated_context)

            resumed = self.run_hook(
                project,
                "C",
                turn_id="v15-ambiguous-answer",
            )
            resumed_payload = json.loads(resumed.stdout)
            resumed_context = resumed_payload["hookSpecificOutput"][
                "additionalContext"
            ]
            self.assertNotIn("decision", resumed_payload)
            self.assertIn("status: locked", resumed_context)
            self.assertIn("reason: question_answer_locked", resumed_context)
            self.assertIn("[STATE_RAG_RECEIPT]", resumed_context)
            self.assertLess(
                resumed_context.index("[PLOT_RAG_GRILL]"),
                resumed_context.index("[PLOT_RAG_EVENT_EXPERIENCE]"),
            )
            self.assertLess(
                resumed_context.index("[PLOT_RAG_EVENT_EXPERIENCE]"),
                resumed_context.index(
                    "[PLOT_RAG_GATE:剧情推进检索门禁]"
                ),
            )
            with closing(
                sqlite3.connect(project / ".plot-rag" / "state.sqlite3")
            ) as connection:
                row = connection.execute(
                    """
                    SELECT turn_id, lifecycle_identity_json
                    FROM turns
                    """
                ).fetchone()
                self.assertEqual("v15-ambiguous-answer", row[0])
                self.assertTrue(
                    json.loads(row[1])["event_seed_references"]
                )

    def test_v15_event_manifest_must_match_exact_grill_identity(self) -> None:
        prompt = (
            "推演下一章：测试角色甲争取通行证并承担身份暴露代价，"
            "制造压迫感和阶段兑现。"
        )
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            self.enable_v15_event_experience(project)
            first = self.run_hook(
                project,
                prompt,
                turn_id="v15-intent-mismatch",
            )
            self.assertEqual(0, first.returncode, first.stderr)
            with closing(
                sqlite3.connect(project / ".plot-rag" / "grill.sqlite3")
            ) as connection:
                state = json.loads(
                    connection.execute(
                        """
                        SELECT state_json
                        FROM grill_sessions
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """
                    ).fetchone()[0]
                )
            references = self.create_locked_event_experience(
                project,
                intent_id="different-intent",
                intent_revision=99,
                intent_hash=canonical_hash(state["contract"]),
                seed_id="seed-mismatch",
            )
            mismatch = self.run_hook(
                project,
                prompt,
                turn_id="v15-intent-mismatch",
                extra_payload={"event_seed_references": references},
            )
            payload = json.loads(mismatch.stdout)
            context = payload["hookSpecificOutput"]["additionalContext"]
            self.assertEqual("block", payload["decision"])
            self.assertIn(
                "EVENT_EXPERIENCE_MANIFEST_INTENT_MISMATCH",
                context,
            )
            self.assertNotIn("[STATE_RAG_RECEIPT]", context)

    def test_v15_async_stop_is_durable_idempotent_and_blocks_next_plot(
        self,
    ) -> None:
        prompt = (
            "推演下一章：测试角色甲想拿到通行证，守卫阻止；"
            "以测试角色甲获得临时证但身份暴露为终点，制造压迫与阶段爽点，"
            "保持主角限知，不新增核心法器。"
        )
        assistant_text = "测试角色甲拿到临时证，但守卫记住了他的脸。"
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            self.enable_v15_event_experience(
                project,
                extraction_mode="async",
                async_shadow=True,
            )
            prepared = self.run_hook(
                project,
                prompt,
                turn_id="v15-async-turn",
            )
            self.assertIn("[STATE_RAG_RECEIPT]", prepared.stdout)

            first_stop = self.run_stop(
                project,
                assistant_text,
                turn_id="v15-async-turn",
                disable_worker=True,
            )
            first_payload = json.loads(first_stop.stdout)
            first_message = first_payload["systemMessage"]
            self.assertIn("status=queued", first_message)
            self.assertIn("job=extract-", first_message)

            second_stop = self.run_stop(
                project,
                assistant_text,
                turn_id="v15-async-turn",
                disable_worker=True,
            )
            second_payload = json.loads(second_stop.stdout)
            second_message = second_payload["systemMessage"]
            self.assertIn("status=queued", second_message)
            self.assertEqual(
                first_message.partition("job=")[2].partition(";")[0],
                second_message.partition("job=")[2].partition(";")[0],
            )
            self.assertIn("enqueue_ms=", first_message)
            self.assertIn("enqueue_ms=", second_message)

            database = project / ".plot-rag" / "state.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                row = connection.execute(
                    """
                    SELECT job_id, job_status, result_proposal_id
                    FROM extraction_jobs
                    """
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual("queued", row[1])
                self.assertIsNone(row[2])
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM extraction_jobs"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM proposals"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    1,
                    connection.execute("SELECT COUNT(*) FROM turns").fetchone()[
                        0
                    ],
                )
            control_revision_before_barrier = (
                EventExperienceService.for_project(
                    project
                ).get_control_revision()
            )
            with closing(
                sqlite3.connect(project / ".plot-rag" / "grill.sqlite3")
            ) as connection:
                grill_before_barrier = connection.execute(
                    """
                    SELECT grill_session_id, revision, status, state_json
                    FROM grill_sessions
                    ORDER BY grill_session_id
                    """
                ).fetchall()

            meta = self.run_hook(
                project,
                "审查剧情 rag 门禁插件的 Hook 实现，不做剧情推演。",
                turn_id="v15-async-meta",
            )
            self.assertEqual("", meta.stdout)

            blocked = self.run_hook(
                project,
                (
                    "推演下一章：测试角色甲必须躲开守卫追查，"
                    "以离开车站为终点，制造紧张与余悸。"
                ),
                turn_id="v15-async-next",
            )
            blocked_payload = json.loads(blocked.stdout)
            blocked_context = blocked_payload["hookSpecificOutput"][
                "additionalContext"
            ]
            self.assertEqual("block", blocked_payload["decision"])
            self.assertEqual(
                "extraction_barrier_blocking",
                blocked_payload["reason"],
            )
            self.assertIn("[PLOT_RAG_EXTRACTION_BARRIER]", blocked_context)
            self.assertIn("status: queued", blocked_context)
            self.assertNotIn("[STATE_RAG_RECEIPT]", blocked_context)
            self.assertNotIn(
                "[PLOT_RAG_EVENT_EXPERIENCE]",
                blocked_context,
            )
            self.assertEqual(
                control_revision_before_barrier,
                EventExperienceService.for_project(
                    project
                ).get_control_revision(),
            )
            with closing(
                sqlite3.connect(project / ".plot-rag" / "grill.sqlite3")
            ) as connection:
                self.assertEqual(
                    grill_before_barrier,
                    connection.execute(
                        """
                        SELECT grill_session_id, revision, status, state_json
                        FROM grill_sessions
                        ORDER BY grill_session_id
                        """
                    ).fetchall(),
                )
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    1,
                    connection.execute("SELECT COUNT(*) FROM turns").fetchone()[
                        0
                    ],
                )

            queue = ExtractionJobQueue(project)
            queued_job = queue.list_jobs(limit=1)[0]
            cancelled = queue.cancel(
                str(queued_job["job_id"]),
                expected_attempt_count=int(
                    queued_job["attempt_count"]
                ),
                reason="test explicitly cancels extraction",
            )
            self.assertEqual("cancelled", cancelled["status"])
            cancelled_block = self.run_hook(
                project,
                (
                    "推演下一章：测试角色甲必须躲开守卫追查，"
                    "以离开车站为终点，制造紧张与余悸。"
                ),
                turn_id="v15-async-cancelled",
            )
            cancelled_context = json.loads(cancelled_block.stdout)[
                "hookSpecificOutput"
            ]["additionalContext"]
            self.assertIn("status: cancelled", cancelled_context)

            resolution = queue.resolve_barrier(
                str(queued_job["job_id"]),
                expected_attempt_count=int(
                    queued_job["attempt_count"]
                ),
                action="discard",
                reason="test explicitly discards the cancelled turn",
            )
            self.assertEqual("discard", resolution["action"])
            resumed = self.run_hook(
                project,
                (
                    "推演下一章：测试角色甲必须躲开守卫追查，"
                    "以离开车站为终点，制造紧张与余悸。"
                ),
                turn_id="v15-async-resolved",
            )
            resumed_payload = json.loads(resumed.stdout)
            self.assertNotIn("decision", resumed_payload)
            self.assertIn("[STATE_RAG_RECEIPT]", resumed.stdout)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    2,
                    connection.execute("SELECT COUNT(*) FROM turns").fetchone()[
                        0
                    ],
                )
            with closing(
                sqlite3.connect(project / ".plot-rag" / "grill.sqlite3")
            ) as connection:
                grill_states = [
                    json.loads(row[0])
                    for row in connection.execute(
                        "SELECT state_json FROM grill_sessions"
                    ).fetchall()
                ]
                original = next(
                    state
                    for state in grill_states
                    if state.get("handoff_turn_id")
                    == "v15-async-turn"
                )
                self.assertEqual("COMPLETED", original["status"])

    def test_v15_session_start_recovers_stale_extraction_worker_lease(
        self,
    ) -> None:
        prompt = (
            "推演下一章：测试角色甲想拿到通行证，守卫阻止；"
            "以测试角色甲获得临时证但身份暴露为终点，制造压迫与阶段爽点，"
            "保持主角限知，不新增核心法器。"
        )
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            self.enable_v15_event_experience(
                project,
                extraction_mode="async",
                async_shadow=False,
            )
            prepared = self.run_hook(
                project,
                prompt,
                turn_id="v15-stale-worker",
            )
            self.assertIn("[STATE_RAG_RECEIPT]", prepared.stdout)
            stopped = self.run_stop(
                project,
                "测试角色甲拿到临时证，但守卫记住了他的脸。",
                turn_id="v15-stale-worker",
                disable_worker=True,
            )
            self.assertIn("status=queued", stopped.stdout)

            queue = ExtractionJobQueue(project)
            claimed = queue.claim(
                worker_id="dead-worker",
                lease_seconds=1,
                now="2000-01-01T00:00:00Z",
            )
            self.assertIsNotNone(claimed)
            self.assertEqual("running", claimed["status"])

            session_start = self.run_session_start(
                project,
                disable_worker=True,
            )
            self.assertEqual(
                0,
                session_start.returncode,
                session_start.stderr,
            )
            self.assertIn(
                "extraction_recovered=1",
                session_start.stdout,
            )
            self.assertIn("extraction_queued=1", session_start.stdout)
            self.assertIn("extraction_running=0", session_start.stdout)
            self.assertIn(
                "extraction_worker_started=false",
                session_start.stdout,
            )
            recovered = queue.inspect(str(claimed["job_id"]))
            self.assertEqual("queued", recovered["status"])

    def test_v15_async_worker_no_delta_clears_barrier(self) -> None:
        prompt = (
            "推演下一章：测试角色甲想拿到通行证，守卫阻止；"
            "以测试角色甲获得临时证但身份暴露为终点，制造压迫与阶段爽点，"
            "保持主角限知，不新增核心法器。"
        )
        assistant_text = "测试角色甲观察了守卫片刻，没有形成新的持续状态。"
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            self.enable_v15_event_experience(
                project,
                extraction_mode="async",
                async_shadow=False,
            )
            prepared = self.run_hook(
                project,
                prompt,
                turn_id="v15-worker-no-delta",
            )
            self.assertIn("[STATE_RAG_RECEIPT]", prepared.stdout)
            stopped = self.run_stop(
                project,
                assistant_text,
                turn_id="v15-worker-no-delta",
                disable_worker=True,
            )
            self.assertIn("status=queued", stopped.stdout)
            session_end = self.run_session_end(project)
            self.assertEqual(0, session_end.returncode, session_end.stderr)
            self.assertIn("close_pending=1", session_end.stdout)
            close_path = (
                project / ".plot-rag" / "session-close-pending.json"
            )
            close_payload = json.loads(
                close_path.read_text(encoding="utf-8")
            )
            self.assertEqual(1, len(close_payload["entries"]))
            self.assertEqual(
                "session-test",
                close_payload["entries"][0]["session_id"],
            )
            self.assertEqual(
                "main",
                close_payload["entries"][0]["branch_id"],
            )

            queue = ExtractionJobQueue(project)
            queued = queue.list_jobs(status="queued", limit=1)[0]
            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    [],
                    [],
                    {
                        "status": "ok",
                        "configured": True,
                        "model": "fixture-extractor",
                    },
                ),
            ):
                self.assertEqual(
                    0,
                    hook._run_extraction_worker(
                        project,
                        worker_id="test-worker-no-delta",
                        max_jobs=1,
                    ),
                )

            completed = queue.inspect(str(queued["job_id"]))
            self.assertEqual("succeeded", completed["status"])
            self.assertEqual("no_delta", completed["result_kind"])
            self.assertIsNone(completed["result_proposal_id"])
            barrier = queue.barrier_status(
                branch_id=str(completed["branch_id"]),
                sequence_no=int(completed["sequence_no"]),
                include_prior=True,
            )
            self.assertEqual("clear", barrier["code"])
            self.assertFalse(barrier["blocking"])
            reviews = EventExperienceService.for_project(
                project
            ).list_reviews()
            self.assertEqual(1, len(reviews))
            self.assertTrue(
                str(reviews[0]["proposal_id"]).startswith("no-delta:")
            )
            for quote, offset in zip(
                reviews[0]["supporting_quotes"],
                reviews[0]["supporting_quote_offsets"],
                strict=True,
            ):
                start = int(offset["start"])
                end = int(offset["end"])
                self.assertEqual(quote, assistant_text[start:end])
            session_start = self.run_session_start(
                project,
                disable_worker=True,
            )
            self.assertEqual(
                0,
                session_start.returncode,
                session_start.stderr,
            )
            self.assertIn(
                "session_close_pending=0",
                session_start.stdout,
            )
            self.assertFalse(close_path.exists())
            with closing(
                sqlite3.connect(project / ".plot-rag" / "state.sqlite3")
            ) as connection:
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM proposals"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    "no_delta",
                    connection.execute(
                        "SELECT status FROM turns WHERE turn_id=?",
                        ("v15-worker-no-delta",),
                    ).fetchone()[0],
                )

    def test_v15_async_worker_proposal_binding_reject_and_resolution(
        self,
    ) -> None:
        prompt = (
            "推演下一章：测试角色甲想拿到通行证，守卫阻止；"
            "以测试角色甲获得临时证但身份暴露为终点，制造压迫与阶段爽点，"
            "保持主角限知，不新增核心法器。"
        )
        assistant_text = (
            "压迫感攀升后，测试角色甲抵达测试城南站，"
            "心中仍有希望与余悸。"
        )
        extracted = [
            {
                "category": "location",
                "subject": "测试角色甲",
                "field": "current",
                "operation": "set",
                "scope": "current",
                "effective_at": None,
                "value": "测试城南站",
                "confidence": 0.99,
                "evidence": assistant_text,
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            self.enable_v15_event_experience(
                project,
                extraction_mode="async",
                async_shadow=False,
            )
            prepared = self.run_hook(
                project,
                prompt,
                turn_id="v15-worker-proposal",
            )
            self.assertIn("[STATE_RAG_RECEIPT]", prepared.stdout)
            stopped = self.run_stop(
                project,
                assistant_text,
                turn_id="v15-worker-proposal",
                disable_worker=True,
            )
            self.assertIn("status=queued", stopped.stdout)

            queue = ExtractionJobQueue(project)
            queued = queue.list_jobs(status="queued", limit=1)[0]
            expected_binding = queue.proposal_binding(queued)
            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    extracted,
                    [],
                    {
                        "status": "ok",
                        "configured": True,
                        "model": "fixture-extractor",
                    },
                ),
            ):
                self.assertEqual(
                    0,
                    hook._run_extraction_worker(
                        project,
                        worker_id="test-worker-proposal",
                        max_jobs=1,
                    ),
                )

            completed = queue.inspect(str(queued["job_id"]))
            self.assertEqual("succeeded", completed["status"])
            self.assertEqual("proposal", completed["result_kind"])
            proposal_id = str(completed["result_proposal_id"])
            self.assertTrue(proposal_id)
            proposal = ContinuityService(project).inspect_proposal(proposal_id)
            self.assertEqual("proposed", proposal["canon_status"])
            reviews = EventExperienceService.for_project(
                project
            ).list_reviews()
            self.assertEqual(1, len(reviews))
            self.assertEqual(proposal_id, reviews[0]["proposal_id"])
            self.assertEqual(
                assistant_text,
                reviews[0]["supporting_quotes"][0],
            )
            self.assertEqual(
                {"start": 0, "end": len(assistant_text)},
                reviews[0]["supporting_quote_offsets"][0],
            )
            self.assertEqual(
                expected_binding,
                {
                    key: proposal["payload"][key]
                    for key in expected_binding
                },
            )
            self.assertEqual(
                proposal["payload"]["lifecycle_identity"],
                {
                    "intent_contract_hash": expected_binding[
                        "intent_contract_hash"
                    ],
                    "event_seed_manifest_hash": expected_binding[
                        "event_seed_manifest_hash"
                    ],
                    "experience_contract_hashes": expected_binding[
                        "experience_contract_hashes"
                    ],
                    "event_experience_control_revision": expected_binding[
                        "event_experience_control_revision"
                    ],
                    "event_seed_references": expected_binding[
                        "event_seed_references"
                    ],
                },
            )
            pending = queue.barrier_status(
                branch_id=str(completed["branch_id"]),
                sequence_no=int(completed["sequence_no"]),
                include_prior=True,
            )
            self.assertEqual("pending_review", pending["code"])
            self.assertTrue(pending["blocking"])

            rejected = ContinuityService(project).reject_proposal(
                proposal_id,
                reason="worker E2E rejection",
                idempotency_key="worker-e2e-reject",
            )
            self.assertEqual("rejected", rejected["canon_status"])
            rejected_barrier = queue.barrier_status(
                branch_id=str(completed["branch_id"]),
                sequence_no=int(completed["sequence_no"]),
                include_prior=True,
            )
            self.assertEqual("rejected", rejected_barrier["code"])
            self.assertTrue(rejected_barrier["blocking"])
            resolution = queue.resolve_barrier(
                str(completed["job_id"]),
                expected_attempt_count=int(completed["attempt_count"]),
                action="discard",
                reason="worker E2E explicitly discards rejected proposal",
            )
            self.assertEqual("discard", resolution["action"])
            clear = queue.barrier_status(
                branch_id=str(completed["branch_id"]),
                sequence_no=int(completed["sequence_no"]),
                include_prior=True,
            )
            self.assertEqual("clear", clear["code"])
            self.assertFalse(clear["blocking"])

            resumed = self.run_hook(
                project,
                (
                    "推演下一章：测试角色甲离开车站，"
                    "以摆脱第一轮追查为终点，制造紧张后的短暂松弛。"
                ),
                turn_id="v15-worker-after-resolution",
            )
            self.assertIn("[STATE_RAG_RECEIPT]", resumed.stdout)
            with closing(
                sqlite3.connect(project / ".plot-rag" / "grill.sqlite3")
            ) as connection:
                states = [
                    json.loads(row[0])
                    for row in connection.execute(
                        "SELECT state_json FROM grill_sessions"
                    ).fetchall()
                ]
            original = next(
                state
                for state in states
                if state.get("handoff_turn_id") == "v15-worker-proposal"
            )
            self.assertEqual("COMPLETED", original["status"])

    def test_v15_async_worker_accept_then_retract_updates_hook_barrier(
        self,
    ) -> None:
        prompt = (
            "推演下一章：测试角色甲想拿到通行证，守卫阻止；"
            "以测试角色甲获得临时证但身份暴露为终点，制造压迫与阶段爽点，"
            "保持主角限知，不新增核心法器。"
        )
        assistant_text = "测试角色甲抵达测试城南站。"
        extracted = [
            {
                "category": "location",
                "subject": "测试角色甲",
                "field": "current",
                "operation": "set",
                "scope": "current",
                "effective_at": None,
                "value": "测试城南站",
                "confidence": 0.99,
                "evidence": assistant_text,
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            self.enable_v15_event_experience(
                project,
                extraction_mode="async",
                async_shadow=False,
            )
            prepared = self.run_hook(
                project,
                prompt,
                turn_id="v15-worker-accept-retract",
            )
            self.assertIn("[STATE_RAG_RECEIPT]", prepared.stdout)
            stopped = self.run_stop(
                project,
                assistant_text,
                turn_id="v15-worker-accept-retract",
                disable_worker=True,
            )
            self.assertIn("status=queued", stopped.stdout)

            queue = ExtractionJobQueue(project)
            queued = queue.list_jobs(status="queued", limit=1)[0]
            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    extracted,
                    [],
                    {
                        "status": "ok",
                        "configured": True,
                        "model": "fixture-extractor",
                    },
                ),
            ):
                self.assertEqual(
                    0,
                    hook._run_extraction_worker(
                        project,
                        worker_id="test-worker-accept-retract",
                        max_jobs=1,
                    ),
                )

            completed = queue.inspect(str(queued["job_id"]))
            proposal_id = str(completed["result_proposal_id"])
            pending = queue.barrier_status(
                branch_id=str(completed["branch_id"]),
                sequence_no=int(completed["sequence_no"]),
                include_prior=True,
            )
            self.assertEqual("pending_review", pending["code"])
            self.assertTrue(pending["blocking"])
            session_end_pending = self.run_session_end(project)
            self.assertEqual(
                0,
                session_end_pending.returncode,
                session_end_pending.stderr,
            )
            self.assertIn(
                "close_pending=1",
                session_end_pending.stdout,
            )
            close_path = (
                project / ".plot-rag" / "session-close-pending.json"
            )
            self.assertTrue(close_path.is_file())

            service = ContinuityService(project)
            host = HostApprovalAuthority(
                service,
                issuer="hook-e2e-host",
                channel="interactive_test",
            )
            active_revision = service.get_canon_revisions()["active"]
            accept_grant = host.issue(
                proposal_id,
                expected_canon_revision=active_revision,
                operations=("accept",),
            )
            accepted = service.accept_proposal(
                proposal_id,
                approval_id=str(accept_grant["approval_id"]),
                expected_canon_revision=active_revision,
            )
            self.assertEqual("accept", accepted["operation"])
            accepted_barrier = queue.barrier_status(
                branch_id=str(completed["branch_id"]),
                sequence_no=int(completed["sequence_no"]),
                include_prior=True,
            )
            self.assertEqual("accepted", accepted_barrier["code"])
            self.assertFalse(accepted_barrier["blocking"])
            session_end_clear = self.run_session_end(project)
            self.assertEqual(
                0,
                session_end_clear.returncode,
                session_end_clear.stderr,
            )
            self.assertIn("close_clear=true", session_end_clear.stdout)
            self.assertFalse(close_path.exists())

            resumed = self.run_hook(
                project,
                (
                    "推演下一章：测试角色甲离开车站，"
                    "以摆脱第一轮追查为终点，制造紧张后的短暂松弛。"
                ),
                turn_id="v15-worker-after-accept",
            )
            self.assertIn("[STATE_RAG_RECEIPT]", resumed.stdout)

            retract_revision = service.get_canon_revisions()["active"]
            retract_grant = host.issue(
                proposal_id,
                expected_canon_revision=retract_revision,
                operations=("retract",),
            )
            retracted = service.retract_proposal(
                proposal_id,
                approval_id=str(retract_grant["approval_id"]),
                expected_canon_revision=retract_revision,
                reason="Hook E2E withdraws the accepted worker proposal",
            )
            self.assertEqual("retract", retracted["operation"])
            retracted_barrier = queue.barrier_status(
                branch_id=str(completed["branch_id"]),
                sequence_no=int(completed["sequence_no"]),
                include_prior=True,
            )
            self.assertEqual("retracted", retracted_barrier["code"])
            self.assertTrue(retracted_barrier["blocking"])

            blocked = self.run_hook(
                project,
                (
                    "推演下一章：测试角色甲回看南站的追查后果，"
                    "以确认新退路为终点，制造持续不安。"
                ),
                turn_id="v15-worker-after-retract",
            )
            blocked_payload = json.loads(blocked.stdout)
            self.assertEqual("block", blocked_payload["decision"])
            self.assertEqual(
                "extraction_barrier_blocking",
                blocked_payload["reason"],
            )
            self.assertIn(
                "status: retracted",
                blocked_payload["hookSpecificOutput"]["additionalContext"],
            )

    def test_v15_sync_shadow_is_non_accepting_and_non_blocking(self) -> None:
        prompt = (
            "推演下一章：测试角色甲想拿到通行证，守卫阻止；"
            "以测试角色甲获得临时证但身份暴露为终点，制造压迫与阶段爽点，"
            "保持主角限知，不新增核心法器。"
        )
        assistant_text = "测试角色甲抵达测试城南站。"
        extracted = [
            {
                "category": "location",
                "subject": "测试角色甲",
                "field": "current",
                "operation": "set",
                "scope": "current",
                "effective_at": None,
                "value": "测试城南站",
                "confidence": 0.99,
                "evidence": assistant_text,
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            self.enable_v15_event_experience(
                project,
                extraction_mode="sync",
                async_shadow=True,
            )
            prepared = self.run_hook(
                project,
                prompt,
                turn_id="v15-sync-shadow",
            )
            self.assertIn("[STATE_RAG_RECEIPT]", prepared.stdout)
            model_binding = hook._extraction_model_binding(project)
            generation_params = model_binding["generation_params"]
            self.assertEqual(
                "json_object",
                generation_params["authoritative_protocol"],
            )
            self.assertFalse(
                generation_params["tool_shadow"]["enabled"]
            )
            self.assertFalse(
                generation_params["tool_shadow"]["acceptance_eligible"]
            )
            captured = io.StringIO()
            with (
                patch(
                    "v1_runtime.state_rag._chat_extract",
                    return_value=(
                        extracted,
                        [],
                        {
                            "status": "ok",
                            "configured": True,
                            "model": "fixture-extractor",
                        },
                    ),
                ),
                patch.object(
                    hook,
                    "_spawn_extraction_worker",
                    return_value=False,
                ),
                patch.object(
                    hook,
                    "_extraction_model_binding",
                    return_value=model_binding,
                ),
                redirect_stdout(captured),
            ):
                self.assertEqual(
                    0,
                    hook._run_stop(
                        {
                            "hook_event_name": "Stop",
                            "cwd": str(project),
                            "last_assistant_message": assistant_text,
                            "session_id": "session-test",
                            "turn_id": "v15-sync-shadow",
                        }
                    ),
                )
            output = json.loads(captured.getvalue())
            message = output["systemMessage"]
            self.assertIn("status=proposed", message)
            self.assertIn(
                "execution_mode=sync_with_async_shadow",
                message,
            )
            self.assertIn("shadow_job=extract-", message)
            self.assertIn("sync_ms=", message)
            self.assertIn("enqueue_ms=", message)

            queue = ExtractionJobQueue(project)
            queued = queue.list_jobs(status="queued", limit=1)[0]
            self.assertEqual(
                generation_params,
                queued["generation_params"],
            )
            worker_binding = queued["artifact_context"]["_plot_rag_v15"]
            self.assertEqual(
                "async_shadow",
                worker_binding["extraction_execution_mode"],
            )
            authoritative_id = str(
                worker_binding["authoritative_proposal_id"]
            )
            self.assertTrue(authoritative_id)
            service = ContinuityService(project)
            authoritative = service.inspect_proposal(authoritative_id)
            self.assertEqual("proposed", authoritative["canon_status"])
            self.assertEqual(
                int(authoritative["artifact_revision"]) + 1,
                int(queued["artifact_context"]["artifact_revision"]),
            )
            self.assertEqual(
                int(authoritative["artifact_revision"]),
                int(worker_binding["authoritative_artifact_revision"]),
            )
            config = json.loads(
                (project / ".plot-rag" / "config.json").read_text(
                    encoding="utf-8"
                )
            )
            disabled_barrier = hook._latest_extraction_barrier(
                project,
                config=config,
                branch_id="main",
            )
            self.assertEqual("disabled", disabled_barrier["code"])
            self.assertFalse(disabled_barrier["blocking"])

            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    extracted,
                    [],
                    {
                        "status": "ok",
                        "configured": True,
                        "model": "fixture-extractor",
                    },
                ),
            ):
                self.assertEqual(
                    0,
                    hook._run_extraction_worker(
                        project,
                        worker_id="test-worker-shadow",
                        max_jobs=1,
                    ),
                )
            completed = queue.inspect(str(queued["job_id"]))
            self.assertEqual("succeeded", completed["status"])
            self.assertEqual("proposal", completed["result_kind"])
            self.assertEqual(
                "shadow_exact_match",
                completed["remote_status"],
            )
            shadow = service.inspect_proposal(
                str(completed["result_proposal_id"])
            )
            self.assertEqual("rejected", shadow["canon_status"])
            self.assertEqual(
                "async_shadow_non_accepting",
                shadow["status_reason"],
            )
            marker = shadow["payload"]["extraction_shadow"]
            self.assertFalse(marker["acceptable"])
            self.assertFalse(marker["barrier_blocking"])
            self.assertEqual(
                authoritative_id,
                marker["authoritative_proposal_id"],
            )
            self.assertTrue(marker["comparison"]["exact_match"])
            with closing(
                sqlite3.connect(project / ".plot-rag" / "state.sqlite3")
            ) as connection:
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM canon_commits"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM current_facts"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    "proposed",
                    connection.execute(
                        "SELECT status FROM turns WHERE turn_id=?",
                        ("v15-sync-shadow",),
                    ).fetchone()[0],
                )

    def test_v15_sync_without_shadow_creates_no_extraction_job(
        self,
    ) -> None:
        prompt = (
            "推演下一章：测试角色甲想避开守卫，"
            "以进入站台为终点，制造紧张后的短暂松弛。"
        )
        assistant_text = (
            "压迫感攀升后，测试角色甲抵达测试城南站，"
            "心中仍有希望与余悸。"
        )
        extracted = [
            {
                "category": "location",
                "subject": "测试角色甲",
                "field": "current",
                "operation": "set",
                "scope": "current",
                "effective_at": None,
                "value": "测试城南站",
                "confidence": 0.99,
                "evidence": assistant_text,
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            self.enable_v15_event_experience(
                project,
                extraction_mode="sync",
                async_shadow=False,
            )
            prepared = self.run_hook(
                project,
                prompt,
                turn_id="v15-sync-no-shadow",
            )
            self.assertIn("[STATE_RAG_RECEIPT]", prepared.stdout)
            captured = io.StringIO()
            with (
                patch(
                    "v1_runtime.state_rag._chat_extract",
                    return_value=(
                        extracted,
                        [],
                        {
                            "status": "ok",
                            "configured": True,
                            "model": "fixture-extractor",
                        },
                    ),
                ),
                redirect_stdout(captured),
            ):
                self.assertEqual(
                    0,
                    hook._run_stop(
                        {
                            "hook_event_name": "Stop",
                            "cwd": str(project),
                            "last_assistant_message": assistant_text,
                            "session_id": "session-test",
                            "turn_id": "v15-sync-no-shadow",
                        }
                    ),
                )
            message = json.loads(captured.getvalue())["systemMessage"]
            self.assertIn("execution_mode=sync", message)
            self.assertIn("shadow_job=;", message)
            self.assertEqual(
                [],
                ExtractionJobQueue(project).list_jobs(limit=10),
            )
            reviews = EventExperienceService.for_project(
                project
            ).list_reviews()
            self.assertEqual(1, len(reviews))
            self.assertEqual(
                assistant_text,
                reviews[0]["supporting_quotes"][0],
            )
            self.assertEqual(
                {"start": 0, "end": len(assistant_text)},
                reviews[0]["supporting_quote_offsets"][0],
            )

    def test_v15_story_control_leak_blocks_before_proposal_or_job(
        self,
    ) -> None:
        prompt = (
            "推演下一章：测试角色甲想避开守卫，"
            "以进入站台为终点，制造紧张后的短暂松弛。"
        )
        leaking_texts = (
            (
                "sentinel",
                "测试角色甲抵达站台。[LOCKED_EVENT_EXPERIENCE_MANIFEST]",
            ),
            ("event-seed-id", "测试角色甲抵达站台。event_seed_id: seed-leak"),
            ("contract-id", "测试角色甲抵达站台。contract_id = contract-leak"),
            ("arc-hash", "测试角色甲抵达站台。arc_hash: deadbeef"),
        )
        for label, assistant_text in leaking_texts:
            with (
                self.subTest(label=label),
                tempfile.TemporaryDirectory() as temporary,
            ):
                project = self.make_grill_project(Path(temporary))
                self.enable_v15_event_experience(
                    project,
                    extraction_mode="async",
                    async_shadow=False,
                )
                turn_id = f"v15-control-leak-{label}"
                prepared = self.run_hook(
                    project,
                    prompt,
                    turn_id=turn_id,
                )
                self.assertIn("[STATE_RAG_RECEIPT]", prepared.stdout)
                stopped = self.run_stop(
                    project,
                    assistant_text,
                    turn_id=turn_id,
                    disable_worker=True,
                )
                self.assertEqual(0, stopped.returncode, stopped.stderr)
                payload = json.loads(stopped.stdout)
                self.assertEqual("block", payload["decision"])
                self.assertIn(
                    "STORY_ARTIFACT_CONTROL_TERM_LEAKAGE",
                    payload["reason"],
                )
                with closing(
                    sqlite3.connect(
                        project / ".plot-rag" / "state.sqlite3"
                    )
                ) as connection:
                    self.assertEqual(
                        0,
                        connection.execute(
                            "SELECT COUNT(*) FROM proposals"
                        ).fetchone()[0],
                    )
                    self.assertEqual(
                        0,
                        connection.execute(
                            "SELECT COUNT(*) FROM extraction_jobs"
                        ).fetchone()[0],
                    )

    def test_v15_story_control_patterns_and_config_bypasses(self) -> None:
        turn = {
            "prompt": "推演下一章：测试角色甲避开守卫并进入站台。",
            "artifact_context": {
                "artifact_stage": "outline",
                "task": "outline",
            },
        }
        config = {
            "config_version": 3,
            "event_experience": {
                "enabled": True,
                "visible_in_story_artifacts": False,
            },
        }
        patterns = (
            "[PLOT_RAG_EVENT_EXPERIENCE]",
            "EventExperienceContract",
            "plot-rag-event-experience/v1",
            '"control_revision": 3',
            "event_seed_id: seed-1",
            "event_seed_revision = 2",
            "contract_id: contract-1",
            "contract_revision: 4",
            "contract_hash: deadbeef",
            "arc_id: arc-1",
            "arc_revision = 5",
            "arc_hash: cafebabe",
        )
        for text in patterns:
            with self.subTest(text=text):
                self.assertTrue(
                    hook._story_artifact_control_leaks(
                        config=config,
                        turn=turn,
                        assistant_text=text,
                    )
                )

        for text in (
            "测试角色甲把样本藏进袖中，合同只约束了商队的交付日期。",
            "他从灰烬里挑出三粒种子，决定先观察守卫换班。",
            "紧张在列车进站时达到峰值，随后只留下短暂余悸。",
        ):
            with self.subTest(benign=text):
                self.assertEqual(
                    [],
                    hook._story_artifact_control_leaks(
                        config=config,
                        turn=turn,
                        assistant_text=text,
                    ),
                )

        bypasses = (
            {
                "config_version": 3,
                "event_experience": {
                    "enabled": False,
                    "visible_in_story_artifacts": False,
                },
            },
            {
                "config_version": 2,
                "event_experience": {
                    "enabled": True,
                    "visible_in_story_artifacts": False,
                },
            },
            {
                "config_version": 3,
                "event_experience": {
                    "enabled": True,
                    "visible_in_story_artifacts": True,
                },
            },
        )
        for bypass in bypasses:
            with self.subTest(bypass=bypass):
                self.assertEqual(
                    [],
                    hook._story_artifact_control_leaks(
                        config=bypass,
                        turn=turn,
                        assistant_text="event_seed_id: documented-field",
                    ),
                )

        self.assertEqual(
            [],
            hook._story_artifact_control_leaks(
                config=config,
                turn={
                    **turn,
                    "prompt": "审查剧情 RAG 插件的 event_seed_id 门禁。",
                },
                assistant_text="event_seed_id: documented-field",
            ),
        )

    def test_v15_experience_review_failure_preserves_proposal_and_canon(
        self,
    ) -> None:
        prompt = (
            "推演下一章：测试角色甲想避开守卫，"
            "以进入站台为终点，制造紧张后的短暂松弛。"
        )
        assistant_text = "测试角色甲抵达测试城南站。"
        extracted = [
            {
                "category": "location",
                "subject": "测试角色甲",
                "field": "current",
                "operation": "set",
                "scope": "current",
                "effective_at": None,
                "value": "测试城南站",
                "confidence": 0.99,
                "evidence": assistant_text,
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            self.enable_v15_event_experience(
                project,
                extraction_mode="sync",
                async_shadow=False,
            )
            prepared = self.run_hook(
                project,
                prompt,
                turn_id="v15-review-failure",
            )
            self.assertIn("[STATE_RAG_RECEIPT]", prepared.stdout)
            captured = io.StringIO()
            with (
                patch(
                    "v1_runtime.state_rag._chat_extract",
                    return_value=(
                        extracted,
                        [],
                        {
                            "status": "ok",
                            "configured": True,
                            "model": "fixture-extractor",
                        },
                    ),
                ),
                patch.object(
                    EventExperienceService,
                    "record_review",
                    side_effect=RuntimeError(
                        "review persistence failed"
                    ),
                ),
                redirect_stdout(captured),
            ):
                self.assertEqual(
                    0,
                    hook._run_stop(
                        {
                            "hook_event_name": "Stop",
                            "cwd": str(project),
                            "last_assistant_message": assistant_text,
                            "session_id": "session-test",
                            "turn_id": "v15-review-failure",
                        }
                    ),
                )
            message = json.loads(captured.getvalue())["systemMessage"]
            self.assertIn("status=proposed", message)
            diagnostics_path = (
                project
                / ".plot-rag"
                / "experience-review-diagnostics.jsonl"
            )
            diagnostic = json.loads(
                diagnostics_path.read_text(encoding="utf-8").splitlines()[0]
            )
            self.assertEqual("sync_stop", diagnostic["source"])
            self.assertIn(
                "review persistence failed",
                diagnostic["diagnostics"][0],
            )
            with closing(
                sqlite3.connect(project / ".plot-rag" / "state.sqlite3")
            ) as connection:
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM proposals"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM canon_commits"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM current_facts"
                    ).fetchone()[0],
                )

    def test_v15_experience_review_setup_failure_is_sync_fail_soft(
        self,
    ) -> None:
        prompt = (
            "推演下一章：测试角色甲想避开守卫，"
            "以进入站台为终点，制造紧张后的短暂松弛。"
        )
        assistant_text = "测试角色甲抵达测试城南站。"
        extracted = [
            {
                "category": "location",
                "subject": "测试角色甲",
                "field": "current",
                "operation": "set",
                "scope": "current",
                "effective_at": None,
                "value": "测试城南站",
                "confidence": 0.99,
                "evidence": assistant_text,
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            self.enable_v15_event_experience(
                project,
                extraction_mode="sync",
                async_shadow=False,
            )
            prepared = self.run_hook(
                project,
                prompt,
                turn_id="v15-review-setup-sync",
            )
            self.assertIn("[STATE_RAG_RECEIPT]", prepared.stdout)
            captured = io.StringIO()
            with (
                patch(
                    "v1_runtime.state_rag._chat_extract",
                    return_value=(
                        extracted,
                        [],
                        {
                            "status": "ok",
                            "configured": True,
                            "model": "fixture-extractor",
                        },
                    ),
                ),
                patch.object(
                    hook,
                    "_load_event_experience_runtime",
                    side_effect=RuntimeError(
                        "event review service initialization failed"
                    ),
                ),
                redirect_stdout(captured),
            ):
                self.assertEqual(
                    0,
                    hook._run_stop(
                        {
                            "hook_event_name": "Stop",
                            "cwd": str(project),
                            "last_assistant_message": assistant_text,
                            "session_id": "session-test",
                            "turn_id": "v15-review-setup-sync",
                        }
                    ),
                )

            message = json.loads(captured.getvalue())["systemMessage"]
            self.assertIn("status=proposed", message)
            diagnostics_path = (
                project
                / ".plot-rag"
                / "experience-review-diagnostics.jsonl"
            )
            diagnostic = json.loads(
                diagnostics_path.read_text(encoding="utf-8").splitlines()[0]
            )
            self.assertEqual("sync_stop", diagnostic["source"])
            self.assertIn(
                "event review service initialization failed",
                diagnostic["diagnostics"][0],
            )
            with closing(
                sqlite3.connect(project / ".plot-rag" / "state.sqlite3")
            ) as connection:
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM proposals"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM canon_commits"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM current_facts"
                    ).fetchone()[0],
                )

    def test_v15_experience_review_setup_failure_is_async_fail_soft(
        self,
    ) -> None:
        prompt = (
            "推演下一章：测试角色甲想避开守卫，"
            "以进入站台为终点，制造紧张后的短暂松弛。"
        )
        assistant_text = "测试角色甲观察守卫片刻，没有形成新的持续状态。"
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            self.enable_v15_event_experience(
                project,
                extraction_mode="async",
                async_shadow=False,
            )
            prepared = self.run_hook(
                project,
                prompt,
                turn_id="v15-review-setup-async",
            )
            self.assertIn("[STATE_RAG_RECEIPT]", prepared.stdout)
            stopped = self.run_stop(
                project,
                assistant_text,
                turn_id="v15-review-setup-async",
                disable_worker=True,
            )
            self.assertIn("status=queued", stopped.stdout)
            queue = ExtractionJobQueue(project)
            queued = queue.list_jobs(status="queued", limit=1)[0]
            event_runtime = hook._load_event_experience_runtime()

            with (
                patch(
                    "v1_runtime.state_rag._chat_extract",
                    return_value=(
                        [],
                        [],
                        {
                            "status": "ok",
                            "configured": True,
                            "model": "fixture-extractor",
                        },
                    ),
                ),
                patch.object(
                    hook,
                    "_load_event_experience_runtime",
                    side_effect=[
                        event_runtime,
                        RuntimeError(
                            "async event review service initialization failed"
                        ),
                    ],
                ),
            ):
                self.assertEqual(
                    0,
                    hook._run_extraction_worker(
                        project,
                        worker_id="test-worker-review-setup",
                        max_jobs=1,
                    ),
                )

            completed = queue.inspect(str(queued["job_id"]))
            self.assertEqual("succeeded", completed["status"])
            self.assertEqual("no_delta", completed["result_kind"])
            diagnostics_path = (
                project
                / ".plot-rag"
                / "experience-review-diagnostics.jsonl"
            )
            diagnostic = json.loads(
                diagnostics_path.read_text(encoding="utf-8").splitlines()[0]
            )
            self.assertEqual("async_worker", diagnostic["source"])
            self.assertIn(
                "async event review service initialization failed",
                diagnostic["diagnostics"][0],
            )
            with closing(
                sqlite3.connect(project / ".plot-rag" / "state.sqlite3")
            ) as connection:
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM proposals"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM canon_commits"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM current_facts"
                    ).fetchone()[0],
                )

    def test_session_end_corrupt_pending_state_stays_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            self.enable_v15_event_experience(
                project,
                extraction_mode="async",
                async_shadow=False,
            )
            close_path = (
                project / ".plot-rag" / "session-close-pending.json"
            )
            close_path.write_text("{broken", encoding="utf-8")

            session_end = self.run_session_end(project)
            self.assertEqual(0, session_end.returncode, session_end.stderr)
            self.assertIn("fail_closed=true", session_end.stdout)
            self.assertIn("close_pending=unknown", session_end.stdout)
            self.assertEqual(
                "{broken",
                close_path.read_text(encoding="utf-8"),
            )

            blocked = self.run_hook(
                project,
                (
                    "推演下一章：测试角色甲离开车站，"
                    "以摆脱第一轮追查为终点，制造紧张后的短暂松弛。"
                ),
                turn_id="v15-corrupt-session-close",
            )
            blocked_payload = json.loads(blocked.stdout)
            self.assertEqual("block", blocked_payload["decision"])
            self.assertEqual(
                "extraction_barrier_blocking",
                blocked_payload["reason"],
            )
            self.assertIn(
                "status: failed",
                blocked_payload["hookSpecificOutput"][
                    "additionalContext"
                ],
            )

    def test_session_end_persists_running_extraction_barrier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            payload = {
                "hook_event_name": "SessionEnd",
                "cwd": str(project),
                "session_id": "session-running",
                "branch_id": "main",
            }
            with (
                patch.object(
                    hook,
                    "_latest_extraction_barrier",
                    return_value={
                        "code": "running",
                        "blocking": True,
                        "branch_id": "main",
                        "sequence_no": 7,
                        "job": {"job_id": "job-running"},
                    },
                ),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(0, hook._run_session_end(payload))

            self.assertIn("close_pending=1", output.getvalue())
            close_payload = json.loads(
                hook._session_close_path(project).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(1, len(close_payload["entries"]))
            entry = close_payload["entries"][0]
            self.assertEqual("session-running", entry["session_id"])
            self.assertEqual("running", entry["code"])
            self.assertTrue(entry["blocking"])
            self.assertEqual(7, entry["sequence_no"])
            self.assertEqual("job-running", entry["job_id"])

    def test_session_end_persists_failed_extraction_barrier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            payload = {
                "hook_event_name": "SessionEnd",
                "cwd": str(project),
                "session_id": "session-failed",
                "branch_id": "main",
            }
            with (
                patch.object(
                    hook,
                    "_latest_extraction_barrier",
                    return_value={
                        "code": "failed",
                        "blocking": True,
                        "branch_id": "main",
                        "sequence_no": 8,
                        "job": {"job_id": "job-failed"},
                        "reason": "fixture failure",
                    },
                ),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(0, hook._run_session_end(payload))

            self.assertIn("close_pending=1", output.getvalue())
            close_payload = json.loads(
                hook._session_close_path(project).read_text(
                    encoding="utf-8"
                )
            )
            entry = close_payload["entries"][0]
            self.assertEqual("failed", entry["code"])
            self.assertTrue(entry["blocking"])
            self.assertEqual("job-failed", entry["job_id"])
            self.assertEqual("fixture failure", entry["reason"])

    def test_session_end_no_delta_clears_existing_pending_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            hook._write_session_close_entries(
                project,
                [
                    {
                        "session_id": "session-no-delta",
                        "branch_id": "main",
                        "code": "queued",
                        "blocking": True,
                    }
                ],
            )
            payload = {
                "hook_event_name": "SessionEnd",
                "cwd": str(project),
                "session_id": "session-no-delta",
                "branch_id": "main",
            }
            with (
                patch.object(
                    hook,
                    "_latest_extraction_barrier",
                    return_value={
                        "code": "clear",
                        "blocking": False,
                        "branch_id": "main",
                        "job_count": 1,
                        "result_kind": "no_delta",
                    },
                ),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(0, hook._run_session_end(payload))

            self.assertIn("close_clear=true", output.getvalue())
            self.assertFalse(hook._session_close_path(project).exists())

    def test_concurrent_session_end_keeps_both_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            original_read = hook._read_session_close_entries
            read_guard = threading.Lock()
            second_read = threading.Event()
            read_count = 0

            def synchronized_read(
                project_root: Path,
            ) -> list[dict[str, object]]:
                nonlocal read_count
                entries = original_read(project_root)
                with read_guard:
                    read_count += 1
                    current = read_count
                    if current == 2:
                        second_read.set()
                if current == 1:
                    # With the lock, the second reader cannot enter until this
                    # call completes.  Without the lock both callers observe
                    # the same empty snapshot and the last writer drops one.
                    second_read.wait(timeout=0.25)
                return entries

            start = threading.Barrier(3)
            errors: list[BaseException] = []

            def close_session(session_id: str) -> None:
                try:
                    start.wait(timeout=2)
                    hook._run_session_end(
                        {
                            "hook_event_name": "SessionEnd",
                            "cwd": str(project),
                            "session_id": session_id,
                            "branch_id": "main",
                        }
                    )
                except BaseException as exc:  # pragma: no cover - assertion aid
                    errors.append(exc)

            with (
                patch.object(
                    hook,
                    "_read_session_close_entries",
                    side_effect=synchronized_read,
                ),
                patch.object(
                    hook,
                    "_latest_extraction_barrier",
                    return_value={
                        "code": "running",
                        "blocking": True,
                        "branch_id": "main",
                        "sequence_no": 9,
                        "job": {"job_id": "job-concurrent"},
                    },
                ),
                patch("builtins.print"),
            ):
                first = threading.Thread(
                    target=close_session,
                    args=("session-a",),
                    name="session-close-a",
                )
                second = threading.Thread(
                    target=close_session,
                    args=("session-b",),
                    name="session-close-b",
                )
                first.start()
                second.start()
                start.wait(timeout=2)
                first.join(timeout=3)
                second.join(timeout=3)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual([], errors)
            close_payload = json.loads(
                hook._session_close_path(project).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                {"session-a", "session-b"},
                {
                    str(entry["session_id"])
                    for entry in close_payload["entries"]
                },
            )

    @unittest.skipUnless(
        os.name == "nt",
        "Windows byte-range lock regression",
    )
    def test_session_end_lock_failure_persists_durable_marker(
        self,
    ) -> None:
        import msvcrt

        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            payload = {
                "hook_event_name": "SessionEnd",
                "cwd": str(project),
                "session_id": "session-lock-failed",
                "branch_id": "blocked-branch",
            }
            with (
                patch.object(
                    hook,
                    "_SESSION_CLOSE_LOCK_TIMEOUT_SECONDS",
                    0.01,
                ),
                patch.object(
                    hook,
                    "_SESSION_CLOSE_LOCK_POLL_SECONDS",
                    0.001,
                ),
                patch.object(
                    msvcrt,
                    "locking",
                    side_effect=OSError("fixture lock busy"),
                ),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(0, hook._run_session_end(payload))

            self.assertIn("durable_markers=1", output.getvalue())
            marker_dir = hook._session_close_failclosed_dir(project)
            markers = list(marker_dir.glob("*.json"))
            self.assertEqual(1, len(markers))
            marker = json.loads(markers[0].read_text(encoding="utf-8"))
            entry = marker["entry"]
            self.assertEqual("session-lock-failed", entry["session_id"])
            self.assertEqual("blocked-branch", entry["branch_id"])
            self.assertEqual("session_close_lock_failed", entry["code"])
            self.assertTrue(entry["blocking"])

            with patch.object(
                hook,
                "_latest_extraction_barrier",
                return_value={
                    "code": "running",
                    "blocking": True,
                    "branch_id": "blocked-branch",
                    "sequence_no": 12,
                    "job": {"job_id": "job-after-lock-failure"},
                },
            ):
                retained = hook._refresh_session_close_entries(
                    project,
                    config={},
                )
            self.assertEqual(1, len(retained))
            self.assertFalse(list(marker_dir.glob("*.json")))
            aggregate = json.loads(
                hook._session_close_path(project).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                "session-lock-failed",
                aggregate["entries"][0]["session_id"],
            )

            with patch.object(
                hook,
                "_latest_extraction_barrier",
                return_value={
                    "code": "clear",
                    "blocking": False,
                    "branch_id": "blocked-branch",
                },
            ):
                self.assertEqual(
                    [],
                    hook._refresh_session_close_entries(
                        project,
                        config={},
                    ),
                )
            self.assertFalse(hook._session_close_path(project).exists())

    @unittest.skipUnless(
        os.name == "nt",
        "Windows cross-process lock regression",
    )
    def test_session_close_lock_serializes_real_windows_processes(
        self,
    ) -> None:
        child_code = r"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "hooks"))
import plot_progression_gate as hook

project = Path(sys.argv[1])
session_id = sys.argv[2]
ready_path = Path(sys.argv[3])
go_path = Path(sys.argv[4])
original_read = hook._read_session_close_entries

def slow_read(project_root):
    entries = original_read(project_root)
    time.sleep(0.4)
    return entries

hook._read_session_close_entries = slow_read
hook._latest_extraction_barrier = lambda *args, **kwargs: {
    "code": "running",
    "blocking": True,
    "branch_id": "main",
    "sequence_no": 13,
    "job": {"job_id": "job-" + session_id},
}
ready_path.write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 5.0
while not go_path.exists():
    if time.monotonic() >= deadline:
        raise SystemExit("start gate timed out")
    time.sleep(0.01)
raise SystemExit(
    hook._run_session_end(
        {
            "hook_event_name": "SessionEnd",
            "cwd": str(project),
            "session_id": session_id,
            "branch_id": "main",
        }
    )
)
"""
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            gate_dir = project / ".plot-rag" / "process-lock-test"
            gate_dir.mkdir()
            go_path = gate_dir / "go"
            processes: list[subprocess.Popen[str]] = []
            for session_id in ("process-a", "process-b"):
                ready_path = gate_dir / f"ready-{session_id}"
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-B",
                            "-X",
                            "utf8",
                            "-c",
                            child_code,
                            str(project),
                            session_id,
                            str(ready_path),
                            str(go_path),
                        ],
                        cwd=PLUGIN_ROOT,
                        text=True,
                        encoding="utf-8",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                )
            try:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if all(
                        (gate_dir / f"ready-{session_id}").is_file()
                        for session_id in ("process-a", "process-b")
                    ):
                        break
                    time.sleep(0.01)
                self.assertTrue(
                    all(
                        (gate_dir / f"ready-{session_id}").is_file()
                        for session_id in ("process-a", "process-b")
                    )
                )
                go_path.write_text("go", encoding="utf-8")
                completed = [
                    process.communicate(timeout=8)
                    for process in processes
                ]
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=3)

            for process, (_stdout, stderr) in zip(processes, completed):
                self.assertEqual(0, process.returncode, stderr)
            close_payload = json.loads(
                hook._session_close_path(project).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                {"process-a", "process-b"},
                {
                    str(entry["session_id"])
                    for entry in close_payload["entries"]
                },
            )

    def test_refresh_and_session_end_do_not_resurrect_cleared_entry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            config = json.loads(
                (project / ".plot-rag" / "config.json").read_text(
                    encoding="utf-8"
                )
            )
            hook._write_session_close_entries(
                project,
                [
                    {
                        "session_id": "session-old",
                        "branch_id": "old",
                        "code": "queued",
                        "blocking": True,
                    }
                ],
            )
            original_read = hook._read_session_close_entries
            refresh_read = threading.Event()
            session_read = threading.Event()

            def synchronized_read(
                project_root: Path,
            ) -> list[dict[str, object]]:
                entries = original_read(project_root)
                if threading.current_thread().name == "session-refresh":
                    refresh_read.set()
                    # With the lock, SessionEnd waits until refresh clears the
                    # old entry. Without it, SessionEnd can later rewrite its
                    # stale snapshot and resurrect the cleared old entry.
                    session_read.wait(timeout=0.25)
                else:
                    session_read.set()
                return entries

            def barrier_for_branch(
                _project_root: Path,
                *,
                config: dict[str, object],
                branch_id: str,
            ) -> dict[str, object]:
                del config
                if branch_id == "old":
                    return {
                        "code": "clear",
                        "blocking": False,
                        "branch_id": branch_id,
                    }
                return {
                    "code": "running",
                    "blocking": True,
                    "branch_id": branch_id,
                    "sequence_no": 10,
                    "job": {"job_id": "job-new"},
                }

            errors: list[BaseException] = []

            def refresh() -> None:
                try:
                    hook._refresh_session_close_entries(
                        project,
                        config=config,
                    )
                except BaseException as exc:  # pragma: no cover - assertion aid
                    errors.append(exc)

            def close_new_session() -> None:
                try:
                    hook._run_session_end(
                        {
                            "hook_event_name": "SessionEnd",
                            "cwd": str(project),
                            "session_id": "session-new",
                            "branch_id": "new",
                        }
                    )
                except BaseException as exc:  # pragma: no cover - assertion aid
                    errors.append(exc)

            with (
                patch.object(
                    hook,
                    "_read_session_close_entries",
                    side_effect=synchronized_read,
                ),
                patch.object(
                    hook,
                    "_latest_extraction_barrier",
                    side_effect=barrier_for_branch,
                ),
                patch("builtins.print"),
            ):
                refresh_thread = threading.Thread(
                    target=refresh,
                    name="session-refresh",
                )
                close_thread = threading.Thread(
                    target=close_new_session,
                    name="session-close-new",
                )
                refresh_thread.start()
                self.assertTrue(refresh_read.wait(timeout=2))
                close_thread.start()
                refresh_thread.join(timeout=3)
                close_thread.join(timeout=3)

            self.assertFalse(refresh_thread.is_alive())
            self.assertFalse(close_thread.is_alive())
            self.assertEqual([], errors)
            close_payload = json.loads(
                hook._session_close_path(project).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                ["session-new"],
                [
                    str(entry["session_id"])
                    for entry in close_payload["entries"]
                ],
            )
            self.assertEqual("new", close_payload["entries"][0]["branch_id"])

    def test_latest_barrier_does_not_include_prior_for_legacy_unnumbered_job(
        self,
    ) -> None:
        captured: dict[str, object] = {}

        class LegacyQueue:
            @staticmethod
            def list_jobs(
                *,
                branch_id: str,
                limit: int,
            ) -> list[dict[str, object]]:
                self.assertEqual("main", branch_id)
                self.assertEqual(1, limit)
                return [{"sequence_no": None}]

            @staticmethod
            def barrier_status(**kwargs: object) -> dict[str, object]:
                captured.update(kwargs)
                return {"code": "queued", "blocking": True}

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".plot-rag").mkdir()
            (project / ".plot-rag" / "state.sqlite3").touch()
            with patch.object(
                hook,
                "_extraction_queue",
                return_value=LegacyQueue(),
            ):
                result = hook._latest_extraction_barrier(
                    project,
                    config={
                        "performance": {
                            "extraction": {
                                "mode": "async",
                                "next_plot_turn_barrier": True,
                            }
                        }
                    },
                    branch_id="main",
                )
        self.assertTrue(result["blocking"])
        self.assertIsNone(captured["sequence_no"])
        self.assertFalse(captured["include_prior"])

    def test_worker_diagnostic_redacts_common_and_environment_secrets(
        self,
    ) -> None:
        values = (
            "sf-abcdefghijklmnop",
            "ak-qrstuvwxyz123456",
            'Bearer "bearerfixture!@#$%^&*()"',
            "Authorization: Bearer authfixture123456",
            "password=fixture-password-123",
            'client_secret: "fixture-client-secret-123"',
            'token: "punctuation!@#$%^&*()_+{}:<>?"',
            'Cookie: "session=cookie!@#$%^&*()"',
            "Set-Cookie='session=set-cookie!@#$%^&*()'",
        )
        environment_secret = "value with spaces from environment"
        with patch.dict(
            os.environ,
            {
                "SILICONFLOW_API_KEY": environment_secret,
                "PLOT_RAG_COOKIE": "environment cookie secret",
            },
        ):
            diagnostic = hook._safe_worker_diagnostic(
                " | ".join(
                    (
                        *values,
                        environment_secret,
                        "environment cookie secret",
                    )
                )
            )
        self.assertIn("[REDACTED]", diagnostic)
        for secret in (
            "sf-abcdefghijklmnop",
            "ak-qrstuvwxyz123456",
            "bearerfixture!@#$%^&*()",
            "authfixture123456",
            "fixture-password-123",
            "fixture-client-secret-123",
            "punctuation!@#$%^&*()_+{}:<>?",
            "session=cookie!@#$%^&*()",
            "session=set-cookie!@#$%^&*()",
            environment_secret,
            "environment cookie secret",
        ):
            self.assertNotIn(secret, diagnostic)

    def test_extraction_worker_spawn_uses_managed_platform_flags(self) -> None:
        captured: dict[str, object] = {}

        class FakeProcess:
            pid = 4242
            returncode = None

            @staticmethod
            def poll() -> None:
                return None

            @staticmethod
            def communicate() -> tuple[None, bytes]:
                return None, b""

            @staticmethod
            def terminate() -> None:
                return None

        def fake_popen(
            command: list[str],
            **kwargs: object,
        ) -> FakeProcess:
            captured["command"] = list(command)
            captured.update(kwargs)
            status_path = Path(
                command[command.index("--startup-status") + 1]
            )
            hook._write_worker_startup_status(
                status_path,
                status="ready",
                worker_id=str(
                    command[command.index("--worker-id") + 1]
                ),
                code="WORKER_READY",
            )
            return FakeProcess()

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            hook.subprocess,
            "Popen",
            side_effect=fake_popen,
        ), patch.dict(
            os.environ,
            {"PLOT_RAG_GATE_WORKER_DISABLED": "0"},
        ):
            project = Path(temporary)
            self.assertTrue(hook._spawn_extraction_worker(project))

        self.assertIs(hook.subprocess.PIPE, captured["stderr"])
        if os.name == "nt":
            flags = int(captured["creationflags"])
            self.assertTrue(
                flags & int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            )
            self.assertTrue(
                flags
                & int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            )
            self.assertTrue(
                flags & int(getattr(subprocess, "DETACHED_PROCESS", 0))
            )
            self.assertFalse(bool(captured["start_new_session"]))
        else:
            self.assertEqual(0, int(captured["creationflags"]))
            self.assertTrue(bool(captured["start_new_session"]))

    def test_extraction_worker_immediate_exit_records_redacted_diagnostic(
        self,
    ) -> None:
        secret = "sk-abcdefghijklmnop"

        class FailedProcess:
            pid = 4343
            returncode = 7

            @staticmethod
            def poll() -> int:
                return 7

            @staticmethod
            def communicate() -> tuple[None, bytes]:
                return None, (
                    f"startup failed api_key={secret}"
                ).encode("utf-8")

            @staticmethod
            def terminate() -> None:
                return None

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            hook.subprocess,
            "Popen",
            return_value=FailedProcess(),
        ), patch.dict(
            os.environ,
            {"PLOT_RAG_GATE_WORKER_DISABLED": "0"},
        ):
            project = Path(temporary)
            self.assertFalse(hook._spawn_extraction_worker(project))
            status_root = project / ".plot-rag" / "worker-startup"
            deadline = time.time() + 2.0
            diagnostics: list[Path] = []
            while time.time() < deadline:
                diagnostics = list(status_root.glob("*.json"))
                if diagnostics:
                    break
                time.sleep(0.01)
            self.assertEqual(1, len(diagnostics))
            payload = json.loads(
                diagnostics[0].read_text(encoding="utf-8")
            )
            self.assertEqual("failed", payload["status"])
            self.assertEqual("WORKER_PROCESS_EXITED", payload["code"])
            self.assertNotIn(secret, payload["message"])
            self.assertIn("[REDACTED]", payload["message"])

    def test_grill_can_be_explicitly_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_v3_project(Path(temporary))
            completed = self.run_hook(
                project,
                "剧情推演",
                turn_id="grill-disabled",
            )

        context = json.loads(completed.stdout)["hookSpecificOutput"][
            "additionalContext"
        ]
        self.assertNotIn("[PLOT_RAG_GRILL]", context)
        self.assertIn("[PLOT_RAG_GATE:剧情推进检索门禁]", context)

    def test_configless_initialization_is_grilled_then_handed_to_init(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "new-story"
            project.mkdir()
            first = self.run_hook(
                project,
                "初始化一部作品：玄幻悬疑，主角从失效通行证开始。",
                turn_id="configless-grill-1",
            )
            second = self.run_hook(
                project,
                "跳过目的确认",
                turn_id="configless-grill-2",
            )
            service = PlotInitService(
                project,
                database_path=project / ".plot-rag" / "init.sqlite3",
            )
            before_replay = service.find_active_session(project_root=project)
            before_state = service.storage.load_session(
                str(before_replay["session_id"])
            )
            sessions = service.list(active_only=True)["sessions"]
            replayed = self.run_hook(
                project,
                "跳过目的确认",
                turn_id="configless-grill-2",
            )
            after_replay = service.find_active_session(project_root=project)
            after_state = service.storage.load_session(
                str(after_replay["session_id"])
            )

        first_context = json.loads(first.stdout)["hookSpecificOutput"][
            "additionalContext"
        ]
        second_context = json.loads(second.stdout)["hookSpecificOutput"][
            "additionalContext"
        ]
        replay_context = json.loads(replayed.stdout)["hookSpecificOutput"][
            "additionalContext"
        ]
        self.assertIn("[PLOT_RAG_GRILL]", first_context)
        self.assertNotIn("[PLOT_RAG_INITIALIZATION]", first_context)
        self.assertIn("shared_understanding_reached: true", second_context)
        self.assertIn("[PLOT_RAG_INITIALIZATION]", second_context)
        self.assertIn("[PLOT_RAG_INITIALIZATION]", replay_context)
        self.assertEqual(1, len(sessions))
        self.assertEqual(
            before_replay["session_revision"],
            after_replay["session_revision"],
        )
        self.assertEqual(before_replay["stage"], after_replay["stage"])
        self.assertEqual(
            before_state["current_questions"][0]["question_id"],
            after_state["current_questions"][0]["question_id"],
        )

    def test_active_initialization_is_isolated_by_host_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "new-story"
            project.mkdir()
            started = self.run_hook(
                project,
                "跳过目的确认，初始化一部作品：玄幻悬疑。",
                turn_id="host-a-turn-1",
                session_id="host-a",
            )
            service = PlotInitService(
                project,
                database_path=project / ".plot-rag" / "init.sqlite3",
            )
            before = service.find_active_session(
                project_root=project,
                host_session_id="host-a",
            )

            unrelated = self.run_hook(
                project,
                "继续",
                turn_id="host-b-turn-1",
                session_id="host-b",
            )
            after = service.find_active_session(
                project_root=project,
                host_session_id="host-a",
            )
            unrelated_session = service.find_active_session(
                project_root=project,
                host_session_id="host-b",
            )

        self.assertEqual(0, started.returncode, started.stderr)
        self.assertIsNotNone(before)
        self.assertEqual("host-a", before["host_session_id"])
        self.assertEqual("host-a-turn-1", before["host_turn_id"])
        self.assertNotIn("[PLOT_RAG_INITIALIZATION]", unrelated.stdout)
        self.assertIn("missing project config", unrelated.stdout)
        self.assertEqual(before["session_revision"], after["session_revision"])
        self.assertIsNone(unrelated_session)

    def test_unbound_initialization_is_not_adopted_by_hook_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_project(Path(temporary))
            service = PlotInitService(
                project,
                database_path=project / ".plot-rag" / "init.sqlite3",
            )
            started = service.start(
                project_root=project,
                mode="new",
                seed="玄幻",
                interaction_profile="minimal",
                idempotency_key="unbound-start",
            )

            submitted = self.run_hook(
                project,
                "继续",
                turn_id="unbound-hook-turn",
                session_id="different-host",
            )
            after = service.storage.load_session(started["session_id"])

        self.assertEqual(0, submitted.returncode, submitted.stderr)
        self.assertNotIn("[PLOT_RAG_INITIALIZATION]", submitted.stdout)
        self.assertEqual(
            started["session_revision"],
            after["session_revision"],
        )

    def test_active_initialization_pauses_for_repository_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "new-story"
            project.mkdir()
            started = self.run_hook(
                project,
                "跳过目的确认，从零初始化一部玄幻网文",
                turn_id="audit-bypass-start",
                session_id="audit-host",
            )
            self.assertEqual(0, started.returncode, started.stderr)
            service = PlotInitService(
                project,
                database_path=project / ".plot-rag" / "init.sqlite3",
            )
            before = service.find_active_session(
                project_root=project,
                host_session_id="audit-host",
            )

            audited = self.run_hook(
                project,
                "做一次全量审查",
                turn_id="audit-bypass-review",
                session_id="audit-host",
            )
            after = service.find_active_session(
                project_root=project,
                host_session_id="audit-host",
            )

        self.assertIsNotNone(before)
        self.assertEqual(0, audited.returncode, audited.stderr)
        self.assertEqual("", audited.stdout)
        self.assertEqual(before["session_revision"], after["session_revision"])
        self.assertEqual(before["stage"], after["stage"])

    def test_initialization_trigger_discussion_does_not_start_session(self) -> None:
        prompts = (
            "检查为什么说“初始化一部作品”会触发插件",
            "请审查“从零初始化”这个触发词是否合理",
            "现在当我提到“整理现有作品”的时候插件会做什么流程",
            "解释一下初始化一部作品的含义。",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                with tempfile.TemporaryDirectory() as temporary:
                    project = self.make_project(Path(temporary))
                    completed = self.run_hook(project, prompt)

                    self.assertEqual(0, completed.returncode, completed.stderr)
                    self.assertEqual("", completed.stdout)
                    self.assertFalse(
                        (project / ".plot-rag" / "init.sqlite3").exists()
                    )

    def test_explicit_initialization_start_still_starts_session(self) -> None:
        prompts = (
            "初始化一部作品：玄幻",
            "请启动插件，初始化一部作品：玄幻。",
            "调用插件从零初始化一部都市异能网文。",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                with tempfile.TemporaryDirectory() as temporary:
                    project = self.make_project(Path(temporary))
                    completed = self.run_hook(project, prompt)

                    self.assertEqual(0, completed.returncode, completed.stderr)
                    self.assertIn("[PLOT_RAG_INITIALIZATION]", completed.stdout)
                    self.assertTrue(
                        (project / ".plot-rag" / "init.sqlite3").is_file()
                    )

    def test_meta_detection_uses_token_boundaries_and_story_context(self) -> None:
        for prompt in (
            "digital cliff preview auditorium",
            "主角进入宗门仓库领取法器",
            "守卫检查门禁后放行",
            "检查脚本上的古老符文",
            "主角阅读项目文档。随后修复法器",
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(hook.is_unrelated_grill_meta_work(prompt))
        for prompt in (
            "检查 git 仓库",
            "修复插件的初始化流程",
            "检查当前实现有没有遗漏",
            "run a review of the CLI",
        ):
            with self.subTest(prompt=prompt):
                self.assertTrue(hook.is_unrelated_grill_meta_work(prompt))

    def test_meta_followup_chain_yields_to_current_story_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            meta_chain = base / "meta-chain.jsonl"
            self.write_transcript(
                meta_chain,
                [
                    ("审查插件代码", "meta-chain-1"),
                    ("把刚才那个问题彻底修掉", "meta-chain-2"),
                    ("继续查下去", "meta-chain-3"),
                ],
            )
            self.assertTrue(
                hook.is_unrelated_grill_meta_turn(
                    {
                        "transcript_path": str(meta_chain),
                        "turn_id": "meta-chain-3",
                    },
                    "继续查下去",
                )
            )

            for index, prompt in enumerate(
                (
                    "主角现在怎么样",
                    "它为什么背叛主角",
                    "重启后的世界如何发展",
                ),
                start=1,
            ):
                transcript = base / f"story-followup-{index}.jsonl"
                self.write_transcript(
                    transcript,
                    [
                        ("审查插件代码", f"story-meta-{index}"),
                        (prompt, f"story-current-{index}"),
                    ],
                )
                with self.subTest(prompt=prompt):
                    self.assertFalse(
                        hook.is_unrelated_grill_meta_turn(
                            {
                                "transcript_path": str(transcript),
                                "turn_id": f"story-current-{index}",
                            },
                            prompt,
                        )
                    )

    def test_corrupt_initialization_store_fails_closed_for_submit_and_stop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_project(Path(temporary))
            (project / ".plot-rag" / "init.sqlite3").write_bytes(
                b"corrupt-initialization-store"
            )

            submitted = self.run_hook(
                project,
                "剧情推演：设计下一章",
                turn_id="corrupt-init-submit",
            )
            stopped = self.run_stop_hook(
                project,
                "测试角色甲获得青铜钥匙。",
                turn_id="corrupt-init-stop",
            )

            self.assertFalse(
                (project / ".plot-rag" / "state.sqlite3").exists()
            )
            self.assertFalse(
                (project / ".plot-rag" / "index.sqlite3").exists()
            )

        submit_output = json.loads(submitted.stdout)
        self.assertEqual("block", submit_output["decision"])
        self.assertEqual(
            "initialization_state_lookup_failed",
            submit_output["reason"],
        )
        self.assertIn(
            "[PLOT_RAG_INITIALIZATION_ERROR]",
            submit_output["hookSpecificOutput"]["additionalContext"],
        )
        self.assertIn(
            "initialization state lookup failed",
            json.loads(stopped.stdout)["systemMessage"],
        )

    def test_multiple_exact_active_initialization_stores_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_project(Path(temporary))
            primary = PlotInitService(
                project,
                database_path=project / ".plot-rag" / "init.sqlite3",
            )
            secondary = PlotInitService(
                project,
                database_path=project / ".plot-rag-init" / "init.sqlite3",
            )
            first = primary.start(
                project_root=project,
                mode="new",
                seed="玄幻",
                interaction_profile="minimal",
                idempotency_key="ambiguous-primary",
                host_session_id="session-test",
            )
            second = secondary.start(
                project_root=project,
                mode="new",
                seed="悬疑",
                interaction_profile="minimal",
                idempotency_key="ambiguous-secondary",
                host_session_id="session-test",
            )

            submitted = self.run_hook(
                project,
                "继续",
                turn_id="ambiguous-turn",
            )
            primary_after = primary.storage.load_session(first["session_id"])
            secondary_after = secondary.storage.load_session(second["session_id"])

        output = json.loads(submitted.stdout)
        self.assertEqual("block", output["decision"])
        self.assertIn(
            "ambiguous_active_initialization",
            output["hookSpecificOutput"]["additionalContext"],
        )
        self.assertEqual(
            first["session_revision"],
            primary_after["session_revision"],
        )
        self.assertEqual(
            second["session_revision"],
            secondary_after["session_revision"],
        )

    def test_frozen_and_stale_propose_are_reported_as_read_only(self) -> None:
        class FakeStorage:
            @staticmethod
            def lookup_idempotency_key(scope: str, key: str) -> None:
                return None

        class FakeService:
            def __init__(self, status: str) -> None:
                self.status = status
                self.storage = FakeStorage()
                self.propose_called = False

            def inspect(self, session_id: str, *, view: str) -> dict[str, object]:
                return {
                    "status": self.status,
                    "stage": self.status,
                    "session_id": session_id,
                    "session_revision": 7,
                }

            def propose(self, *args: object, **kwargs: object) -> dict[str, object]:
                self.propose_called = True
                raise AssertionError("propose must remain read-only")

        def arbitrate(
            payload: dict[str, object],
            *,
            active_session: dict[str, object] | None = None,
        ) -> dict[str, str]:
            return {"action": "propose"}

        for status in ("PROPOSAL_FROZEN", "STALE_SOURCE", "STALE_CANON"):
            with self.subTest(status=status):
                service = FakeService(status)
                session = {
                    "session_id": f"session-{status}",
                    "session_revision": 7,
                    "status": status,
                    "stage": status,
                    "host_session_id": "session-test",
                }
                with patch.object(
                    hook,
                    "_load_initialization_runtime",
                    return_value=(object, arbitrate, object, object),
                ):
                    output = hook._handle_initialization_submit(
                        {
                            "session_id": "session-test",
                            "turn_id": f"turn-{status}",
                        },
                        cwd=Path.cwd(),
                        project_root=Path.cwd(),
                        prompt="生成初始化提案",
                        active_initialization=(service, session),
                    )
                context = output["hookSpecificOutput"]["additionalContext"]
                self.assertIn("classified_action: propose", context)
                self.assertIn("attempted_action: propose", context)
                self.assertIn("executed_action: inspect", context)
                self.assertFalse(service.propose_called)

    def test_initialization_mutation_error_reports_attempt_and_error(self) -> None:
        class FakeStorage:
            @staticmethod
            def lookup_idempotency_key(scope: str, key: str) -> None:
                return None

        class FakeService:
            storage = FakeStorage()

            @staticmethod
            def inspect(session_id: str, *, view: str) -> dict[str, object]:
                if view == "questions":
                    return {"questions": [{"question_id": "genre"}]}
                return {}

            @staticmethod
            def answer(*args: object, **kwargs: object) -> dict[str, object]:
                raise RuntimeError("synthetic write failure")

        def arbitrate(
            payload: dict[str, object],
            *,
            active_session: dict[str, object] | None = None,
        ) -> dict[str, str]:
            return {"action": "answer"}

        session = {
            "session_id": "error-session",
            "session_revision": 3,
            "status": "NEEDS_INPUT",
            "stage": "GENRE_CONTRACT",
            "host_session_id": "session-test",
        }
        with patch.object(
            hook,
            "_load_initialization_runtime",
            return_value=(object, arbitrate, object, object),
        ):
            output = hook._handle_initialization_submit(
                {
                    "session_id": "session-test",
                    "turn_id": "error-turn",
                },
                cwd=Path.cwd(),
                project_root=Path.cwd(),
                prompt="选择悬疑",
                active_initialization=(FakeService(), session),
            )
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("classified_action: answer", context)
        self.assertIn("attempted_action: answer", context)
        self.assertIn("executed_action: error", context)
        self.assertIn("synthetic write failure", context)

    def test_story_technical_words_do_not_bypass_active_initialization(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_project(Path(temporary))
            service = PlotInitService(
                project,
                database_path=project / ".plot-rag" / "init.sqlite3",
            )
            started = service.start(
                project_root=project,
                mode="new",
                seed="玄幻",
                interaction_profile="minimal",
                idempotency_key="story-technical-start",
                host_session_id="session-test",
            )

            submitted = self.run_hook(
                project,
                "请续写下一章，内容是主角测试新能力、阅读古籍文档并修复法器",
                turn_id="story-technical-answer",
            )
            after = service.find_active_session(
                project_root=project,
                host_session_id="session-test",
            )

            self.assertFalse((project / ".plot-rag" / "state.sqlite3").exists())
            self.assertFalse((project / ".plot-rag" / "index.sqlite3").exists())

        self.assertEqual(0, submitted.returncode, submitted.stderr)
        self.assertIn("[PLOT_RAG_INITIALIZATION]", submitted.stdout)
        self.assertIn("classified_action: answer", submitted.stdout)
        self.assertGreater(
            after["session_revision"],
            started["session_revision"],
        )

    def test_story_technical_words_do_not_bypass_corrupt_store_guard(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_project(Path(temporary))
            (project / ".plot-rag" / "init.sqlite3").write_bytes(
                b"corrupt-initialization-store"
            )

            submitted = self.run_hook(
                project,
                "请续写下一章，内容是主角测试新能力并读取记忆缓存",
                turn_id="corrupt-story-technical",
            )

            self.assertFalse((project / ".plot-rag" / "state.sqlite3").exists())
            self.assertFalse((project / ".plot-rag" / "index.sqlite3").exists())

        output = json.loads(submitted.stdout)
        self.assertEqual("block", output["decision"])
        self.assertEqual(
            "initialization_state_lookup_failed",
            output["reason"],
        )

    def test_initialization_requires_host_session_and_meta_followup_pauses(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            unbound_project = base / "unbound"
            unbound_project.mkdir()
            missing_host = self.run_hook(
                unbound_project,
                "从零初始化一部玄幻网文",
                turn_id="missing-host-start",
                session_id=None,
            )

            project = self.make_project(base / "bound")
            service = PlotInitService(
                project,
                database_path=project / ".plot-rag" / "init.sqlite3",
            )
            started = service.start(
                project_root=project,
                mode="new",
                seed="玄幻",
                interaction_profile="minimal",
                idempotency_key="meta-followup-start",
                host_session_id="session-test",
            )
            transcript = base / "meta-followup.jsonl"
            self.write_transcript(
                transcript,
                [
                    ("审查插件代码", "meta-previous"),
                    ("把刚才那个问题彻底修掉", "meta-current"),
                ],
            )
            paused = self.run_hook(
                project,
                "把刚才那个问题彻底修掉",
                transcript_path=transcript,
                turn_id="meta-current",
            )
            after = service.find_active_session(
                project_root=project,
                host_session_id="session-test",
            )

        self.assertEqual(0, missing_host.returncode, missing_host.stderr)
        self.assertEqual("", missing_host.stdout)
        self.assertFalse(
            (unbound_project / ".plot-rag" / "init.sqlite3").exists()
        )
        self.assertFalse(
            (unbound_project / ".plot-rag-init" / "init.sqlite3").exists()
        )
        self.assertEqual(0, paused.returncode, paused.stderr)
        self.assertEqual("", paused.stdout)
        self.assertEqual(
            started["session_revision"],
            after["session_revision"],
        )

    def test_missing_turn_id_without_transcript_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_project(Path(temporary))
            service = PlotInitService(
                project,
                database_path=project / ".plot-rag" / "init.sqlite3",
            )
            started = service.start(
                project_root=project,
                mode="new",
                seed="玄幻",
                interaction_profile="minimal",
                idempotency_key="missing-turn-start",
                host_session_id="session-test",
            )

            first = self.run_hook(project, "继续", turn_id=None)
            after_first = service.find_active_session(
                project_root=project,
                host_session_id="session-test",
            )
            second = self.run_hook(project, "继续", turn_id=None)
            after_second = service.find_active_session(
                project_root=project,
                host_session_id="session-test",
            )

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertNotIn("status: ERROR", first.stdout)
        self.assertNotIn("status: ERROR", second.stdout)
        self.assertIn("attempted_action: advance", first.stdout)
        self.assertIn("executed_action: inspect", first.stdout)
        self.assertEqual(
            started["session_revision"],
            after_first["session_revision"],
        )
        self.assertEqual(
            after_second["session_revision"],
            after_first["session_revision"],
        )

    def test_transcript_turn_identity_binds_start_and_replays_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = self.make_project(base)
            start_prompt = "初始化一部关于代码审查员的悬疑网文"
            start_transcript = base / "start-transcript.jsonl"
            self.write_transcript(
                start_transcript,
                [(start_prompt, "transcript-start-turn")],
            )
            started = self.run_hook(
                project,
                start_prompt,
                transcript_path=start_transcript,
                turn_id=None,
            )
            service = PlotInitService(
                project,
                database_path=project / ".plot-rag" / "init.sqlite3",
            )
            active = service.find_active_session(
                project_root=project,
                host_session_id="session-test",
            )

            continue_transcript = base / "continue-transcript.jsonl"
            self.write_transcript(
                continue_transcript,
                [("继续", "")],
            )
            first = self.run_hook(
                project,
                "继续",
                transcript_path=continue_transcript,
                turn_id=None,
            )
            after_first = service.find_active_session(
                project_root=project,
                host_session_id="session-test",
            )
            second = self.run_hook(
                project,
                "继续",
                transcript_path=continue_transcript,
                turn_id=None,
            )
            after_second = service.find_active_session(
                project_root=project,
                host_session_id="session-test",
            )

        self.assertEqual(0, started.returncode, started.stderr)
        self.assertEqual("transcript-start-turn", active["host_turn_id"])
        self.assertGreater(
            after_first["session_revision"],
            active["session_revision"],
        )
        self.assertEqual(
            after_first["session_revision"],
            after_second["session_revision"],
        )
        self.assertIn("executed_action: replay", second.stdout)

    def test_grill_disabled_initialization_replay_keeps_same_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_v3_project(Path(temporary))
            started = self.run_hook(
                project,
                "初始化一部作品：玄幻悬疑。",
                turn_id="init-no-grill-replay",
            )
            service = PlotInitService(
                project,
                database_path=project / ".plot-rag" / "init.sqlite3",
            )
            before = service.find_active_session(
                project_root=project,
                host_session_id="session-test",
            )
            before_state = service.storage.load_session(
                str(before["session_id"])
            )

            replayed = self.run_hook(
                project,
                "初始化一部作品：玄幻悬疑。",
                turn_id="init-no-grill-replay",
            )
            after = service.find_active_session(
                project_root=project,
                host_session_id="session-test",
            )
            after_state = service.storage.load_session(
                str(after["session_id"])
            )

        self.assertEqual(0, started.returncode, started.stderr)
        self.assertIn("[PLOT_RAG_INITIALIZATION]", replayed.stdout)
        self.assertEqual(before["session_revision"], after["session_revision"])
        self.assertEqual(before["stage"], after["stage"])
        self.assertEqual(
            before_state["current_questions"][0]["question_id"],
            after_state["current_questions"][0]["question_id"],
        )

    def test_active_initialization_rejects_same_turn_different_grill_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "new-story"
            project.mkdir()
            self.run_hook(
                project,
                "初始化一部作品：玄幻悬疑，主角从失效通行证开始。",
                turn_id="configless-grill-conflict-1",
            )
            self.run_hook(
                project,
                "跳过目的确认",
                turn_id="configless-grill-conflict-2",
            )
            service = PlotInitService(
                project,
                database_path=project / ".plot-rag" / "init.sqlite3",
            )
            before = service.find_active_session(project_root=project)
            before_state = service.storage.load_session(
                str(before["session_id"])
            )

            conflict = self.run_hook(
                project,
                "把题材改成科幻。",
                turn_id="configless-grill-conflict-2",
            )
            after = service.find_active_session(project_root=project)
            after_state = service.storage.load_session(
                str(after["session_id"])
            )

        context = json.loads(conflict.stdout)["hookSpecificOutput"][
            "additionalContext"
        ]
        self.assertIn("[PLOT_RAG_GRILL]", context)
        self.assertIn("action: conflict", context)
        self.assertIn("turn_id_request_conflict", context)
        self.assertEqual(before["session_revision"], after["session_revision"])
        self.assertEqual(before["stage"], after["stage"])
        self.assertEqual(
            before_state["current_questions"][0]["question_id"],
            after_state["current_questions"][0]["question_id"],
        )

    def test_grill_output_respects_recommendation_and_probe_switches(self) -> None:
        cases = (
            (
                {"recommend_answer": False},
                ("Recommended answer:", "Reason:"),
                ("project_probe:",),
            ),
            (
                {"explore_project_first": False},
                ("project_probe:", "角色位置、道具、力量状态"),
                ("Recommended answer:",),
            ),
            (
                {
                    "recommend_answer": False,
                    "explore_project_first": False,
                },
                (
                    "Recommended answer:",
                    "Reason:",
                    "project_probe:",
                    "角色位置、道具、力量状态",
                ),
                (),
            ),
        )
        for index, (overrides, absent, present) in enumerate(cases, start=1):
            with self.subTest(overrides=overrides):
                with tempfile.TemporaryDirectory() as temporary:
                    project = self.make_grill_project(Path(temporary))
                    config_path = project / ".plot-rag" / "config.json"
                    config = json.loads(config_path.read_text(encoding="utf-8"))
                    config["grill"] = overrides
                    config_path.write_text(
                        json.dumps(config, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    completed = self.run_hook(
                        project,
                        "剧情推演",
                        turn_id=f"grill-switch-{index}",
                    )

                context = json.loads(completed.stdout)["hookSpecificOutput"][
                    "additionalContext"
                ]
                self.assertEqual(1, context.count("Q1/"))
                for marker in absent:
                    self.assertNotIn(marker, context)
                for marker in present:
                    self.assertIn(marker, context)

    def test_initialization_grill_accepts_initialization_language_but_pauses_for_plugin_work(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "new-story"
            project.mkdir()
            started = self.run_hook(
                project,
                "初始化一部作品",
                turn_id="init-grill-language-1",
            )
            service_path = project / ".plot-rag" / "grill.sqlite3"
            with closing(sqlite3.connect(service_path)) as connection:
                before_plugin = json.loads(
                    connection.execute(
                        """
                        SELECT state_json
                        FROM grill_sessions
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """
                    ).fetchone()[0]
                )

            plugin_work = self.run_hook(
                project,
                "先解释这个插件的 hook 实现",
                turn_id="init-grill-language-2",
            )
            with closing(sqlite3.connect(service_path)) as connection:
                after_plugin = json.loads(
                    connection.execute(
                        """
                        SELECT state_json
                        FROM grill_sessions
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """
                    ).fetchone()[0]
                )

            answered = self.run_hook(
                project,
                "这次初始化要达到 plot_ready，只解决能开写第一卷的最小世界。",
                turn_id="init-grill-language-3",
            )
            with closing(sqlite3.connect(service_path)) as connection:
                after_answer = json.loads(
                    connection.execute(
                        """
                        SELECT state_json
                        FROM grill_sessions
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """
                    ).fetchone()[0]
                )

        self.assertIn("[PLOT_RAG_GRILL]", started.stdout)
        self.assertEqual("", plugin_work.stdout)
        self.assertEqual(before_plugin["revision"], after_plugin["revision"])
        self.assertEqual(
            before_plugin["current_field"],
            after_plugin["current_field"],
        )
        self.assertIn("[PLOT_RAG_GRILL]", answered.stdout)
        self.assertGreater(after_answer["revision"], after_plugin["revision"])
        self.assertNotEqual(
            after_answer["current_field"],
            after_plugin["current_field"],
        )

    def test_mark_prepared_failure_is_cached_and_stop_is_suppressed(self) -> None:
        class FailingMarkPrepared:
            def __init__(self, delegate):
                self.delegate = delegate

            def __getattr__(self, name):
                return getattr(self.delegate, name)

            def mark_prepared(self, **_kwargs):
                raise RuntimeError("injected handoff persistence failure")

        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            first = self.run_hook(
                project,
                "剧情推演",
                turn_id="grill-fail-1",
            )
            self.assertEqual(0, first.returncode, first.stderr)

            root, config, _ = hook._find_project(project)
            runtime, service, grill_config = hook._grill_service(root, config)
            wrapper = FailingMarkPrepared(service)
            payload = {
                "cwd": str(project),
                "prompt": "跳过目的确认",
                "session_id": "session-test",
                "turn_id": "grill-fail-2",
            }
            captured = io.StringIO()
            with (
                patch.object(
                    hook,
                    "_grill_service",
                    return_value=(runtime, wrapper, grill_config),
                ),
                patch.object(
                    sys,
                    "stdin",
                    io.StringIO(json.dumps(payload, ensure_ascii=False)),
                ),
                patch.object(
                    sys,
                    "argv",
                    [str(HOOKS / "plot_progression_gate.py")],
                ),
                redirect_stdout(captured),
            ):
                self.assertEqual(0, hook.main())

            output = json.loads(captured.getvalue())
            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn("action: conflict", context)
            self.assertIn("handoff_persistence_failed", context)

            replayed = self.run_hook(
                project,
                "跳过目的确认",
                turn_id="grill-fail-2",
            )
            stopped = self.run_stop(
                project,
                "测试角色甲获得青铜钥匙。",
                turn_id="grill-fail-2",
            )
            with closing(
                sqlite3.connect(project / ".plot-rag" / "state.sqlite3")
            ) as connection:
                prepared_turns = int(
                    connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
                )
                proposals = int(
                    connection.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
                )
            with closing(
                sqlite3.connect(project / ".plot-rag" / "grill.sqlite3")
            ) as connection:
                state = json.loads(
                    connection.execute(
                        """
                        SELECT state_json
                        FROM grill_sessions
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """
                    ).fetchone()[0]
                )

        replay_context = json.loads(replayed.stdout)["hookSpecificOutput"][
            "additionalContext"
        ]
        self.assertIn("action: conflict", replay_context)
        self.assertIn("Grill owns this turn", stopped.stdout)
        self.assertEqual("HANDOFF_FAILED", state["status"])
        self.assertEqual(1, prepared_turns)
        self.assertEqual(0, proposals)

    def test_initialization_handoff_failure_is_cached_and_stop_is_suppressed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            first = self.run_hook(
                project,
                "初始化一部作品",
                turn_id="init-handoff-fail-1",
            )
            self.assertEqual(0, first.returncode, first.stderr)

            payload = {
                "cwd": str(project),
                "prompt": "跳过目的确认",
                "session_id": "session-test",
                "turn_id": "init-handoff-fail-2",
            }
            failed_initialization = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": (
                        "[PLOT_RAG_INITIALIZATION]\n"
                        "status: ERROR\n"
                        "stage: START\n"
                        "session_id:\n"
                        "reason: injected initialization failure\n"
                        "[/PLOT_RAG_INITIALIZATION]"
                    ),
                }
            }
            captured = io.StringIO()
            with (
                patch.object(
                    hook,
                    "_handle_initialization_submit",
                    return_value=failed_initialization,
                ),
                patch.object(
                    sys,
                    "stdin",
                    io.StringIO(json.dumps(payload, ensure_ascii=False)),
                ),
                patch.object(
                    sys,
                    "argv",
                    [str(HOOKS / "plot_progression_gate.py")],
                ),
                redirect_stdout(captured),
            ):
                self.assertEqual(0, hook.main())

            output = json.loads(captured.getvalue())
            context = output["hookSpecificOutput"]["additionalContext"]
            stopped = self.run_stop(
                project,
                "测试角色甲获得青铜钥匙。",
                turn_id="init-handoff-fail-2",
            )
            replayed = self.run_hook(
                project,
                "跳过目的确认",
                turn_id="init-handoff-fail-2",
            )
            with closing(
                sqlite3.connect(project / ".plot-rag" / "grill.sqlite3")
            ) as connection:
                state = json.loads(
                    connection.execute(
                        """
                        SELECT state_json
                        FROM grill_sessions
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """
                    ).fetchone()[0]
                )

        replay_context = json.loads(replayed.stdout)["hookSpecificOutput"][
            "additionalContext"
        ]
        self.assertIn("action: conflict", context)
        self.assertIn("injected initialization failure", context)
        self.assertIn("action: conflict", replay_context)
        self.assertIn("Grill owns this turn", stopped.stdout)
        self.assertEqual("HANDOFF_FAILED", state["status"])
        self.assertEqual(
            "injected initialization failure",
            state["handoff_error"],
        )
        self.assertFalse(
            (project / ".plot-rag" / "state.sqlite3").exists()
        )

    def test_failed_stop_does_not_complete_or_reuse_intent_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_grill_project(Path(temporary))
            handed_off = self.run_hook(
                project,
                "跳过目的确认，推演下一章测试角色甲遭遇盘查。",
                turn_id="grill-stop-fail-1",
            )
            stopped = self.run_stop(
                project,
                "测试角色甲试图通过盘查。",
                turn_id="grill-stop-fail-1",
            )
            continued = self.run_hook(
                project,
                "继续",
                turn_id="grill-stop-fail-2",
            )
            with closing(
                sqlite3.connect(project / ".plot-rag" / "grill.sqlite3")
            ) as connection:
                statuses = [
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT status
                        FROM grill_sessions
                        ORDER BY created_at
                        """
                    ).fetchall()
                ]

        self.assertEqual(0, handed_off.returncode, handed_off.stderr)
        self.assertIn("shared_understanding_reached: true", handed_off.stdout)
        self.assertIn("status=failed", stopped.stdout)
        self.assertIn("EXECUTING", statuses)
        continued_context = json.loads(continued.stdout)["hookSpecificOutput"][
            "additionalContext"
        ]
        self.assertIn("action: ask", continued_context)
        self.assertNotIn("inherited_locked_contract", continued_context)

    def test_v3_hook_uses_strict_runtime_and_never_auto_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_v3_project(Path(temporary))
            prepared = self.run_hook(
                project,
                "写第一章正文终稿",
                turn_id="turn-v3",
            )
            self.assertEqual(0, prepared.returncode, prepared.stderr)
            context = json.loads(prepared.stdout)["hookSpecificOutput"][
                "additionalContext"
            ]
            self.assertIn("lifecycle_mode: strict_proposal", context)
            self.assertIn("[WEBNOVEL_CONTINUITY_CONTRACT]", context)

            stopped = self.run_stop_hook(
                project,
                "测试角色甲仍在测试城南站。",
                turn_id="turn-v3",
            )
            self.assertEqual(0, stopped.returncode, stopped.stderr)
            self.assertIn("recorded_events=0", stopped.stdout)
            with closing(
                sqlite3.connect(project / ".plot-rag" / "state.sqlite3")
            ) as connection:
                legacy_events = connection.execute(
                    "SELECT COUNT(*) FROM state_events"
                ).fetchone()[0]
                proposals = connection.execute(
                    "SELECT COUNT(*) FROM proposals"
                ).fetchone()[0]
                accepted = connection.execute(
                    "SELECT COUNT(*) FROM canon_commits"
                ).fetchone()[0]
            self.assertEqual(0, legacy_events)
            self.assertEqual(0, proposals)
            self.assertEqual(0, accepted)

    def test_non_plot_prompt_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_project(Path(temporary))
            completed = self.run_hook(project, "继续推进世界观")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stdout)

    def test_short_continue_after_plugin_work_is_silent_without_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = self.make_project(base)
            transcript = base / "transcript.jsonl"
            self.write_transcript(
                transcript,
                [
                    ("制作一份插件升级计划并整理到仓库", "turn-1"),
                    ("继续", "turn-2"),
                ],
            )
            completed = self.run_hook(
                project,
                "继续",
                transcript_path=transcript,
                turn_id="turn-2",
            )
            self.assertFalse((project / ".plot-rag" / "state.sqlite3").exists())
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stdout)

    def test_short_continue_after_initialization_design_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = self.make_project(base)
            transcript = base / "transcript.jsonl"
            self.write_transcript(
                transcript,
                [
                    (
                        "设计一套初始化流程，按题材-世界-剧情创建作品并整理已有内容",
                        "turn-1",
                    ),
                    ("开始吧", "turn-2"),
                    ("继续", "turn-3"),
                ],
            )
            completed = self.run_hook(
                project,
                "继续",
                transcript_path=transcript,
                turn_id="turn-3",
            )
            self.assertFalse((project / ".plot-rag" / "state.sqlite3").exists())
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stdout)

    def test_active_initialization_owns_continue_and_suppresses_plot_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "novel"
            project.mkdir()
            started = self.run_hook(
                project,
                "跳过目的确认，从零初始化一部都市异能网文",
                turn_id="init-turn-1",
            )
            self.assertEqual(0, started.returncode, started.stderr)
            start_context = json.loads(started.stdout)["hookSpecificOutput"][
                "additionalContext"
            ]
            self.assertIn("[PLOT_RAG_INITIALIZATION]", start_context)
            self.assertTrue(
                (project / ".plot-rag" / "init.sqlite3").is_file()
            )
            self.assertFalse((project / ".plot-rag" / "state.sqlite3").exists())

            continued = self.run_hook(
                project,
                "继续",
                turn_id="init-turn-2",
            )
            self.assertEqual(0, continued.returncode, continued.stderr)
            continue_context = json.loads(continued.stdout)["hookSpecificOutput"][
                "additionalContext"
            ]
            self.assertIn("[PLOT_RAG_INITIALIZATION]", continue_context)
            self.assertNotIn("[PLOT_RAG_GATE:剧情推进检索门禁]", continue_context)

            stopped = self.run_stop_hook(
                project,
                "这是初始化阶段生成的候选内容。",
                turn_id="init-turn-2",
            )
            self.assertEqual(0, stopped.returncode, stopped.stderr)
            self.assertIn("initialization owns this turn", stopped.stdout)
            self.assertFalse((project / ".plot-rag" / "state.sqlite3").exists())

    def test_short_continue_after_plot_task_prepares_exactly_one_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = self.make_project(base)
            transcript = base / "transcript.jsonl"
            self.write_transcript(
                transcript,
                [
                    ("推演下一章：测试角色甲抵达测试城后遭遇盘查", "turn-1"),
                    ("继续", "turn-2"),
                ],
            )
            completed = self.run_hook(
                project,
                "继续",
                transcript_path=transcript,
                turn_id="turn-2",
            )
            with closing(
                sqlite3.connect(project / ".plot-rag" / "state.sqlite3")
            ) as connection:
                turns = int(connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0])
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("[PLOT_RAG_GATE:剧情推进检索门禁]", completed.stdout)
        self.assertEqual(1, turns)

    def test_explicit_plot_command_overrides_recent_plugin_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = self.make_project(base)
            transcript = base / "transcript.jsonl"
            self.write_transcript(
                transcript,
                [
                    ("继续修改插件触发器和回归测试", "turn-1"),
                    ("现在请用这个插件推演下一章谈判场景", "turn-2"),
                ],
            )
            completed = self.run_hook(
                project,
                "现在请用这个插件推演下一章谈判场景",
                transcript_path=transcript,
                turn_id="turn-2",
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("[PLOT_RAG_GATE:剧情推进检索门禁]", completed.stdout)

    def test_missing_or_truncated_transcript_preserves_short_continue_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = self.make_project(base)
            missing = base / "missing.jsonl"
            missing_result = self.run_hook(
                project,
                "继续",
                transcript_path=missing,
                turn_id="turn-missing",
            )
            truncated = base / "truncated.jsonl"
            self.write_transcript(truncated, [], trailing_invalid_json=True)
            truncated_result = self.run_hook(
                project,
                "继续",
                transcript_path=truncated,
                turn_id="turn-truncated",
            )
        self.assertIn("[PLOT_RAG_GATE:剧情推进检索门禁]", missing_result.stdout)
        self.assertIn("[PLOT_RAG_GATE:剧情推进检索门禁]", truncated_result.stdout)

    def test_injected_context_does_not_override_recent_user_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = self.make_project(base)
            transcript = base / "transcript.jsonl"
            self.write_transcript(
                transcript,
                [
                    ("制作插件升级计划", "turn-1"),
                    (
                        "<environment_context>推演下一章剧情</environment_context>\n继续",
                        "turn-2",
                    ),
                ],
            )
            completed = self.run_hook(
                project,
                "继续",
                transcript_path=transcript,
                turn_id="turn-2",
            )
        self.assertEqual("", completed.stdout)

    def test_meta_plot_question_is_silent_and_does_not_prepare_state(self) -> None:
        prompts = [
            "现在当我提到“剧情推演”的时候，这个插件将会进行什么流程",
            "给剧情推演增加测试",
            "剧情推演的关键词有哪些",
            "审查剧情推演流程",
            "升级剧情写作功能",
            "优化剧情推演正则",
            "续一章的触发规则是什么",
            "再来一章会触发插件吗",
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt), tempfile.TemporaryDirectory() as temporary:
                project = self.make_project(Path(temporary))
                completed = self.run_hook(project, prompt)
                state_db = project / ".plot-rag" / "state.sqlite3"
                self.assertFalse(state_db.exists())
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual("", completed.stdout)

    def test_session_start_reports_health_without_creating_state_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_project(Path(temporary))
            completed = subprocess.run(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    str(HOOKS / "plot_progression_gate.py"),
                    "--session-start",
                ],
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
                cwd=project,
            )
            state_db = project / ".plot-rag" / "state.sqlite3"
            wal = Path(str(state_db) + "-wal")
            shm = Path(str(state_db) + "-shm")
            self.assertFalse(state_db.exists())
            self.assertFalse(wal.exists())
            self.assertFalse(shm.exists())
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("state=degraded", completed.stdout)

    def test_explicit_plugin_plot_command_still_prepares_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_project(Path(temporary))
            completed = self.run_hook(project, "请用这个插件推演下一章谈判场景")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("[PLOT_RAG_GATE:剧情推进检索门禁]", completed.stdout)

    def test_broken_config_injects_index_unavailable_instead_of_failing_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_project(Path(temporary), config="{broken")
            completed = self.run_hook(project, "继续推进剧情")
        self.assertEqual(0, completed.returncode, completed.stderr)
        output = json.loads(completed.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("INDEX_UNAVAILABLE", context)
        self.assertIn("不得继续推进剧情", context)

    def test_missing_config_injects_index_unavailable_instead_of_failing_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "novel"
            (project / ".plot-rag").mkdir(parents=True)
            completed = self.run_hook(project, "继续推进剧情")
        self.assertEqual(0, completed.returncode, completed.stderr)
        output = json.loads(completed.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("INDEX_UNAVAILABLE", context)
        self.assertIn("missing project config", context)

    def test_hooks_manifest_uses_codex_runtime_shape(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual({"hooks"}, set(manifest))
        self.assertIn("SessionStart", manifest["hooks"])
        self.assertIn("SessionEnd", manifest["hooks"])
        self.assertIn("UserPromptSubmit", manifest["hooks"])
        self.assertIn("Stop", manifest["hooks"])
        session_end = manifest["hooks"]["SessionEnd"][0]
        self.assertEqual("*", session_end["matcher"])
        self.assertEqual(
            10,
            session_end["hooks"][0]["timeout"],
        )
        self.assertTrue(
            session_end["hooks"][0]["command"].endswith(
                '--session-end'
            )
        )

    def test_active_initialization_owns_continue_and_suppresses_stop_extract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = self.make_project(base)
            initializer = PlotInitService(
                project,
                database_path=project / ".plot-rag" / "init.sqlite3",
            )
            started = initializer.start(
                project_root=project,
                mode="new",
                seed="玄幻",
                interaction_profile="minimal",
                idempotency_key="hook-init-start",
                host_session_id="session-test",
            )
            before_revision = started["session_revision"]

            submitted = self.run_hook(project, "继续", turn_id="init-turn-1")
            after = initializer.find_active_session(project_root=project)
            stopped = self.run_stop(
                project,
                "测试角色甲抵达测试城，并获得青铜钥匙。",
                turn_id="init-turn-1",
            )

            self.assertEqual(0, submitted.returncode, submitted.stderr)
            self.assertIn("[PLOT_RAG_INITIALIZATION]", submitted.stdout)
            self.assertGreater(after["session_revision"], before_revision)
            self.assertFalse((project / ".plot-rag" / "state.sqlite3").exists())
            self.assertEqual(0, stopped.returncode, stopped.stderr)
            self.assertIn("plot extraction suppressed", stopped.stdout)

    def test_explicit_initialization_can_start_without_existing_plot_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "new-story"
            project.mkdir()
            completed = self.run_hook(
                project,
                "跳过目的确认，初始化一部作品：玄幻悬疑，主角从一张失效通行证开始。",
                turn_id="init-start-turn",
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("[PLOT_RAG_INITIALIZATION]", completed.stdout)
            self.assertTrue(
                (project / ".plot-rag" / "init.sqlite3").is_file()
            )
            self.assertFalse((project / ".plot-rag" / "state.sqlite3").exists())

    def test_existing_project_initialization_command_routes_to_ingest_without_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "existing-story"
            (project / "正文").mkdir(parents=True)
            (project / "正文" / "第一章.md").write_text(
                "状态：已发布\n# 测试角色甲\n当前位置：测试城\n",
                encoding="utf-8",
            )

            completed = self.run_hook(
                project,
                "跳过目的确认，把现有正文和设定整理成标准结构",
                turn_id="init-existing-turn",
            )
            service = PlotInitService(
                project,
                database_path=project / ".plot-rag" / "init.sqlite3",
            )
            session = service.find_active_session(project_root=project)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("[PLOT_RAG_INITIALIZATION]", completed.stdout)
            self.assertIsNotNone(session)
            self.assertEqual("ingest", session["mode"])
            stored = service.storage.load_session(str(session["session_id"]))
            self.assertIsNone(stored["seed"])
            self.assertEqual([], stored["current_questions"])

    def test_config_v3_stop_never_falls_back_to_current_when_extract_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_project(Path(temporary))
            config_path = project / ".plot-rag" / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config.pop("version", None)
            config["config_version"] = 3
            config["lifecycle"] = {"strict": True}
            # This regression isolates strict Stop behavior when extraction is
            # disabled.  v1.5 enables the independent event-experience gate by
            # default, so disable it here to reach the extraction path.
            config["event_experience"] = {"enabled": False}
            config_path.write_text(
                json.dumps(config, ensure_ascii=False),
                encoding="utf-8",
            )

            prepared = self.run_hook(
                project,
                "写第一章正文：测试角色甲进入测试城。",
                turn_id="strict-turn",
            )
            stopped = self.run_stop(
                project,
                "测试角色甲进入测试城。",
                turn_id="strict-turn",
            )

            self.assertEqual(0, prepared.returncode, prepared.stderr)
            self.assertIn("只生成带逐字证据的状态 proposal", prepared.stdout)
            self.assertEqual(0, stopped.returncode, stopped.stderr)
            self.assertIn("status=failed", stopped.stdout)
            self.assertIn("extract is not configured", stopped.stdout)
            with closing(
                sqlite3.connect(project / ".plot-rag" / "state.sqlite3")
            ) as connection:
                proposals = int(
                    connection.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
                )
                current = int(
                    connection.execute("SELECT COUNT(*) FROM current_facts").fetchone()[0]
                )
            self.assertEqual(0, proposals)
            self.assertEqual(0, current)


if __name__ == "__main__":
    unittest.main()
