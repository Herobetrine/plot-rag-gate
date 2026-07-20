from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from plot_init import (  # noqa: E402
    PlotInitError,
    PlotInitService,
    arbitrate_initialization_hook,
    is_initialization_storage_path,
    proposal_to_lifecycle_package,
    resolve_initialization_intent,
)
from plot_init.canonical import canonical_hash  # noqa: E402


MVW_FIELDS = (
    "story_clock",
    "locations_and_routes",
    "base_rules",
    "core_capability",
    "survival_resource_chain",
    "power_scarcity_chain",
    "daily_cycles",
    "infrastructure_bottleneck",
    "formal_institution_chain",
    "power_actors",
    "harmed_group",
    "legitimacy_narrative",
    "important_secret",
    "historical_trauma",
    "pressure_horizons",
    "irreversible_trigger",
)


def complete_seed() -> dict[str, Any]:
    return {
        "genre_contract": {
            "primary_engine": "制度冲突与资源升级",
            "secondary_engines": ["悬疑"],
            "target_readers": "长篇网文读者",
            "platform_assumptions": "持续连载",
            "reading_promise": "每次破局都会改变资源、身份或关系",
            "recurring_rewards": ["破局", "成长", "揭示"],
            "differentiators": ["城际列车决定城市权力"],
            "tone": "紧张但保留人物温度",
            "scale_expectation": "长篇",
            "pacing_expectation": "章级反馈、卷级闭环",
            "hard_boundaries": ["不使用无代价能力"],
            "anti_promises": ["无条件碾压"],
        },
        "world_model": {
            "rules": ["跨层移动必须依赖列车并支付资格与暴露成本"],
            "scarce_resources": ["通行资格"],
            "power_distribution": "维护列车者控制资源、交通与执法",
            "current_pressures": ["城际线将在七天后停运"],
            "mvw": {
                "story_clock": "景历十二年三月初七",
                "locations_and_routes": ["测试城", "测试枢纽站", "旧港"],
                "base_rules": ["跨层移动必须依赖列车"],
                "core_capability": {
                    "capability": "短暂改写通行条件",
                    "cost": "消耗通行证并留下记录",
                    "boundary": "不能跨越停运线路",
                    "counter": "列车署可冻结资格",
                },
                "survival_resource_chain": "农场→列车→市场→家庭",
                "power_scarcity_chain": "通行资格→列车署→城市阶层",
                "daily_cycles": ["普通工人的一天", "列车署官员的一天"],
                "infrastructure_bottleneck": "单线列车容量有限",
                "formal_institution_chain": "发现→报告→裁决→执行",
                "power_actors": ["列车署", "旧港商会"],
                "harmed_group": "依赖跨层通勤的工人",
                "legitimacy_narrative": "停运是为了保护城市安全",
                "important_secret": "停运源于通行资格系统失控",
                "historical_trauma": "十年前列车事故仍塑造禁忌",
                "pressure_horizons": {
                    "near": "七天停运",
                    "volume": "城市断供",
                    "book": "跨层秩序失去合法性",
                },
                "irreversible_trigger": "主角取得一张被注销的通行证",
            },
        },
        "actor_system": {
            "protagonist": {
                "name": "测试角色甲",
                "location": "测试城南站",
                "external_goal": "在停运前查清通行证来源",
                "resources": ["青铜钥匙"],
                "world_blocker": "列车署冻结资格",
            },
            "opponents": [
                {
                    "name": "测试角色丁",
                    "goal": "回收通行证并封锁事故真相",
                    "offscreen_plan": "清查南站所有出入记录",
                }
            ],
            "third_parties": [
                {"name": "南站工人", "stake": "停运后失去生计"}
            ],
        },
        "story_engine": {
            "protagonist": "测试角色甲",
            "actionable_goal": "在七天内查清通行证来源并保住退路",
            "inciting_event": "测试角色甲取得一张被注销的通行证",
            "active_opposition": "测试角色丁",
            "stakes": "身份、退路与城市断供",
            "failure_cost": "测试角色甲被清算，工人失去生计",
            "world_constraints": ["跨层移动必须依赖列车"],
            "information_asymmetry": "测试角色丁知道事故真相，测试角色甲知道钥匙位置",
            "first_event_chain": ["取得通行证", "南站封锁", "测试角色甲被迫改道"],
            "escalation_loop": "局部成功引来更高层列车署介入",
            "irreversible_state_changes": ["测试角色甲身份被列为异常"],
            "volume_one_change": "测试城供给与权力格局不可逆改变",
            "endgame_direction": "追查跨层秩序的真实来源",
            "endgame_question": "改变秩序是否必然制造新的垄断者",
        },
        "serialization_contract": {
            "chapter_feedback_loop": "承接变化→行动→代价→钩子",
            "recurring_reward_types": ["破局", "资源变化", "关系变化"],
            "tension_cycle": "蓄压—行动—反制—新局面",
            "reveal_policy": "证据驱动揭示",
            "hook_policy": "章尾绑定期限或对手行动",
            "promise_windows": [{"promise": "通行证来源", "window": "十章内"}],
            "growth_accounts": ["资源", "身份", "认知"],
            "volume_loop": "目标—阻力—变化—新局面",
            "repetition_limits": ["不连续使用同构救场"],
            "pacing_guardrails": ["每章至少一个状态变化"],
        },
    }


def file_fingerprints(root: Path) -> dict[str, tuple[int, int, str]]:
    result: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        result[path.relative_to(root).as_posix()] = (
            int(stat.st_size),
            int(stat.st_mtime_ns),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return result


class PlotInitTestCase(unittest.TestCase):
    def test_dry_run_is_zero_write_and_bundle_hash_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            (project / ".plot-rag").mkdir(parents=True)
            (project / ".plot-rag" / "config.json").write_text(
                json.dumps({"version": 2, "canon_revision": 7}),
                encoding="utf-8",
            )
            before = file_fingerprints(workspace)
            service = PlotInitService(workspace)
            first = service.dry_run(
                project_root=project,
                mode="new",
                target_profile="plot_ready",
                interaction_profile="deep",
                seed=complete_seed(),
            )
            middle = file_fingerprints(workspace)
            second = service.dry_run(
                project_root=project,
                mode="new",
                target_profile="plot_ready",
                interaction_profile="deep",
                seed=complete_seed(),
            )
            after = file_fingerprints(workspace)

            self.assertEqual(before, middle)
            self.assertEqual(before, after)
            self.assertFalse(service.database_path.exists())
            self.assertFalse((workspace / ".plot-rag-init").exists())
            self.assertEqual("READY_TO_PROPOSE", first["status"])
            self.assertEqual([], first["current_questions"])
            self.assertEqual(
                first["bundle"]["bundle_hash"],
                second["bundle"]["bundle_hash"],
            )
            bundle = first["bundle"]
            self.assertEqual(set(MVW_FIELDS), set(bundle["world_model"]["mvw"]))
            self.assertEqual(9, len(bundle["world_model"]["ontology"]))
            self.assertEqual(13, len(bundle["world_model"]["modules"]))
            self.assertEqual(
                {"kernel", "regional", "local", "scene", "texture"},
                set(bundle["world_model"]["resolution"]),
            )
            self.assertEqual(10, len(bundle["world_model"]["pressure_tests"]))
            self.assertEqual(0, bundle["validation"]["preapproval_canon_delta"])
            self.assertTrue(bundle["validation"]["domain_plain_values"])
            self.assertTrue(
                all(not item["materialized"] for item in bundle["artifact_manifest"])
            )
            self.assertFalse(any((project / item["path"]).exists() for item in bundle["artifact_manifest"] if item["operation"] == "create"))

    def test_project_config_materialization_adds_complete_grill_defaults_and_preserves_custom_values(
        self,
    ) -> None:
        expected_default_grill = {
            "enabled": True,
            "schema_version": "plot-rag-intent/v1",
            "database_path": ".plot-rag/grill.sqlite3",
            "one_question_per_turn": True,
            "recommend_answer": True,
            "explore_project_first": True,
            "max_questions": 6,
            "session_ttl_seconds": 21600,
            "required_fields": [
                "problem_to_solve",
                "expected_deliverable",
                "reader_experience",
                "protagonist_drive_conflict",
                "scope_endpoint",
                "success_criteria",
                "hard_constraints",
                "model_autonomy",
            ],
            "skip_phrases": [
                "跳过 Grill",
                "跳过盘问",
                "跳过目的确认",
                "按现有要求直接执行",
                "直接执行，不要追问",
            ],
            "cancel_phrases": [
                "取消本轮 Grill",
                "结束本轮盘问",
                "停止本轮盘问",
                "放弃本轮任务",
            ],
        }

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            service = PlotInitService(workspace)

            def proposed_project_config(
                project: Path,
                existing_config: dict[str, Any],
            ) -> dict[str, Any]:
                (project / ".plot-rag").mkdir(parents=True)
                (project / ".plot-rag" / "config.json").write_text(
                    json.dumps(existing_config, ensure_ascii=False),
                    encoding="utf-8",
                )
                result = service.dry_run(
                    project_root=project,
                    mode="new",
                    seed=complete_seed(),
                )
                artifact = next(
                    item
                    for item in result["bundle"]["artifact_manifest"]
                    if item["path"] == ".plot-rag/config.json"
                )
                self.assertEqual("update", artifact["operation"])
                self.assertFalse(artifact["materialized"])
                return json.loads(artifact["proposed_content"])

            defaulted = proposed_project_config(
                workspace / "legacy-novel",
                {"version": 2, "canon_revision": 7},
            )
            self.assertEqual(3, defaulted["config_version"])
            self.assertEqual(expected_default_grill, defaulted["grill"])

            customized = proposed_project_config(
                workspace / "custom-novel",
                {
                    "config_version": 3,
                    "canon_revision": 11,
                    "grill": {
                        "enabled": False,
                        "recommend_answer": False,
                        "max_questions": 9,
                        "session_ttl_seconds": 43200,
                        "skip_phrases": ["直接执行自定义合同"],
                        "cancel_phrases": ["取消自定义合同"],
                    },
                },
            )
            expected_custom_grill = dict(expected_default_grill)
            expected_custom_grill.update(
                {
                    "enabled": False,
                    "recommend_answer": False,
                    "max_questions": 9,
                    "session_ttl_seconds": 43200,
                    "skip_phrases": ["直接执行自定义合同"],
                    "cancel_phrases": ["取消自定义合同"],
                }
            )
            self.assertEqual(expected_custom_grill, customized["grill"])
            self.assertEqual(11, customized["canon_revision"])

    def test_canon_guard_uses_authoritative_state_database_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            state_dir = project / ".plot-rag"
            state_dir.mkdir(parents=True)
            (state_dir / "config.json").write_text(
                json.dumps({"version": 2, "canon_revision": 99}),
                encoding="utf-8",
            )
            state_database = state_dir / "state.sqlite3"
            with closing(sqlite3.connect(state_database)) as connection:
                connection.execute(
                    "CREATE TABLE state_meta("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                    "updated_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO state_meta VALUES("
                    "'active_canon_revision', '0', 'fixture')"
                )
                connection.commit()
            before = file_fingerprints(project)

            service = PlotInitService(workspace)
            started = service.start(
                project_root=project,
                mode="new",
                seed=complete_seed(),
                idempotency_key="state-revision-zero",
            )
            self.assertEqual(0, started["expected_canon_revision"])
            self.assertEqual(before, file_fingerprints(project))
            self.assertFalse(Path(str(state_database) + "-wal").exists())
            self.assertFalse(Path(str(state_database) + "-shm").exists())

            with closing(sqlite3.connect(state_database)) as connection:
                connection.execute(
                    "UPDATE state_meta SET value='1' "
                    "WHERE key='active_canon_revision'"
                )
                connection.commit()
            with self.assertRaises(PlotInitError) as stale:
                service.propose(
                    started["session_id"],
                    expected_session_revision=started["session_revision"],
                    idempotency_key="state-revision-stale",
                )
            self.assertEqual("STALE_CANON", stale.exception.code)
            self.assertEqual(1, stale.exception.details["actual_canon_revision"])

    def test_canon_guard_reads_active_wal_without_touching_source_sidecars(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            state_dir = project / ".plot-rag"
            state_dir.mkdir(parents=True)
            (state_dir / "config.json").write_text(
                json.dumps({"version": 2, "canon_revision": 99}),
                encoding="utf-8",
            )
            state_database = state_dir / "state.sqlite3"
            connection = sqlite3.connect(state_database)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    "CREATE TABLE state_meta("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                    "updated_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO state_meta VALUES("
                    "'active_canon_revision', '4', 'fixture')"
                )
                connection.commit()
                before = file_fingerprints(project)

                service = PlotInitService(workspace)
                result = service.dry_run(
                    project_root=project,
                    mode="new",
                    seed=complete_seed(),
                )

                self.assertEqual(4, result["expected_canon_revision"])
                self.assertEqual(4, result["canon_guard"]["canon_revision"])
                self.assertEqual(before, file_fingerprints(project))
            finally:
                connection.close()

    def test_start_idempotency_cas_checkpoint_and_restart_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            service = PlotInitService(workspace)
            started = service.start(
                project_root=project,
                mode="new",
                seed=complete_seed(),
                idempotency_key="start-1",
            )
            replay = service.start(
                project_root=project,
                mode="new",
                seed=complete_seed(),
                idempotency_key="start-1",
            )
            self.assertEqual(started["session_id"], replay["session_id"])
            self.assertTrue(replay["idempotent"])
            self.assertTrue(service.database_path.is_file())

            with self.assertRaises(PlotInitError) as idempotency_error:
                service.start(
                    project_root=project,
                    mode="new",
                    seed={"genre_contract": {"primary_engine": "different"}},
                    idempotency_key="start-1",
                )
            self.assertEqual("IDEMPOTENCY_CONFLICT", idempotency_error.exception.code)

            restarted = PlotInitService(workspace)
            summary = restarted.inspect(started["session_id"], view="summary")
            self.assertEqual(started["session_revision"], summary["session_revision"])
            checkpoints = restarted.inspect(
                started["session_id"], view="checkpoints"
            )["checkpoints"]
            journal = restarted.inspect(started["session_id"], view="journal")[
                "journal"
            ]
            self.assertGreaterEqual(len(checkpoints), 5)
            self.assertTrue(any(item["event_type"] == "session_started" for item in journal))
            self.assertTrue(
                any(item["event_type"] == "stage_checkpoint" for item in journal)
            )

            proposed = restarted.propose(
                started["session_id"],
                expected_session_revision=started["session_revision"],
                idempotency_key="propose-1",
            )
            with self.assertRaises(PlotInitError) as revision_error:
                restarted.cancel(
                    started["session_id"],
                    expected_session_revision=started["session_revision"],
                    idempotency_key="cancel-stale",
                )
            self.assertEqual(
                "SESSION_REVISION_CONFLICT", revision_error.exception.code
            )
            self.assertEqual("PROPOSAL_FROZEN", proposed["status"])

    def test_public_revision_inputs_require_exact_non_negative_integers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            service = PlotInitService(workspace)
            invalid_revisions = (True, 1.0, "1", -1)

            for operation in ("dry_run", "start"):
                for invalid_revision in invalid_revisions:
                    with self.subTest(
                        operation=operation,
                        field="expected_canon_revision",
                        value=invalid_revision,
                    ):
                        kwargs = {
                            "project_root": project,
                            "mode": "new",
                            "seed": complete_seed(),
                            "expected_canon_revision": invalid_revision,
                        }
                        if operation == "start":
                            kwargs["idempotency_key"] = (
                                f"invalid-canon-{type(invalid_revision).__name__}-"
                                f"{invalid_revision}"
                            )
                        with self.assertRaises(PlotInitError) as invalid:
                            getattr(service, operation)(**kwargs)
                        self.assertEqual(
                            "INVALID_REVISION",
                            invalid.exception.code,
                        )
                        self.assertEqual(
                            "expected_canon_revision",
                            invalid.exception.details["field"],
                        )

            dry_run = service.dry_run(
                project_root=project,
                mode="new",
                seed=complete_seed(),
                expected_canon_revision=0,
            )
            self.assertEqual(0, dry_run["expected_canon_revision"])
            started = service.start(
                project_root=project,
                mode="new",
                seed=complete_seed(),
                expected_canon_revision=0,
                idempotency_key="valid-revision-start",
                session_id="init-exact-revision",
            )
            self.assertEqual(1, started["session_revision"])

            operations = {
                "advance": lambda revision, key: service.advance(
                    started["session_id"],
                    expected_session_revision=revision,
                    idempotency_key=key,
                ),
                "answer": lambda revision, key: service.answer(
                    started["session_id"],
                    {"goal": "推进剧情"},
                    expected_session_revision=revision,
                    idempotency_key=key,
                ),
                "propose": lambda revision, key: service.propose(
                    started["session_id"],
                    expected_session_revision=revision,
                    idempotency_key=key,
                ),
                "cancel": lambda revision, key: service.cancel(
                    started["session_id"],
                    expected_session_revision=revision,
                    idempotency_key=key,
                ),
            }
            for operation, invoke in operations.items():
                for index, invalid_revision in enumerate(invalid_revisions):
                    with self.subTest(
                        operation=operation,
                        field="expected_session_revision",
                        value=invalid_revision,
                    ):
                        with self.assertRaises(PlotInitError) as invalid:
                            invoke(
                                invalid_revision,
                                f"invalid-{operation}-{index}",
                            )
                        self.assertEqual(
                            "INVALID_REVISION",
                            invalid.exception.code,
                        )
                        self.assertEqual(
                            "expected_session_revision",
                            invalid.exception.details["field"],
                        )
                        self.assertEqual(
                            1,
                            service.inspect(
                                started["session_id"],
                                view="summary",
                            )["session_revision"],
                        )

            cancelled = service.cancel(
                started["session_id"],
                expected_session_revision=1,
                idempotency_key="valid-revision-cancel",
            )
            self.assertEqual("CANCELLED", cancelled["status"])

    def test_active_session_listing_preserves_host_session_and_turn_bindings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            service = PlotInitService(workspace)
            first = service.start(
                project_root=project,
                mode="new",
                seed="玄幻",
                idempotency_key="host-start-a",
                host_session_id="host-a",
                host_turn_id="turn-a",
            )
            second = service.start(
                project_root=project,
                mode="new",
                seed="科幻",
                idempotency_key="host-start-b",
                host_session_id="host-b",
                host_turn_id="turn-b",
            )

            sessions = service.list(
                project_root=project,
                active_only=True,
            )["sessions"]
            by_id = {session["session_id"]: session for session in sessions}

            self.assertEqual("host-a", by_id[first["session_id"]]["host_session_id"])
            self.assertEqual("turn-a", by_id[first["session_id"]]["host_turn_id"])
            self.assertEqual("host-b", by_id[second["session_id"]]["host_session_id"])
            self.assertEqual("turn-b", by_id[second["session_id"]]["host_turn_id"])
            self.assertEqual(
                first["session_id"],
                service.find_active_session(
                    project_root=project,
                    host_session_id="host-a",
                )["session_id"],
            )
            self.assertEqual(
                second["session_id"],
                service.find_active_session(
                    project_root=project,
                    host_session_id="host-b",
                )["session_id"],
            )
            self.assertIsNone(
                service.find_active_session(
                    project_root=project,
                    host_session_id="host-c",
                )
            )

    def test_sparse_seed_finishes_in_three_decision_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            service = PlotInitService(workspace)
            result = service.start(
                project_root=workspace / "novel",
                mode="new",
                seed="玄幻",
                interaction_profile="minimal",
                idempotency_key="sparse-start",
            )
            seen: list[str] = []
            while result["status"] == "NEEDS_INPUT":
                question = result["current_questions"][0]
                seen.append(question["question_id"])
                result = service.answer(
                    result["session_id"],
                    {question["question_id"]: question["default_option_id"]},
                    expected_session_revision=result["session_revision"],
                    idempotency_key=f"answer-{len(seen)}",
                )
            self.assertEqual(
                ["genre-contract", "world-causal-kernel", "story-engine"],
                seen,
            )
            self.assertEqual("READY_TO_PROPOSE", result["status"])
            self.assertLessEqual(result["decision_package_count"], 3)
            self.assertEqual([], result["current_questions"])

            revised = service.answer(
                result["session_id"],
                {"genre-contract": "mystery-discovery"},
                expected_session_revision=result["session_revision"],
                idempotency_key="revise-genre",
            )
            self.assertEqual("NEEDS_INPUT", revised["status"])
            self.assertEqual(
                "world-causal-kernel",
                revised["current_questions"][0]["question_id"],
            )
            self.assertIn("world_model", revised["invalidated_nodes"])
            self.assertLessEqual(revised["decision_package_count"], 3)

    def test_auto_routing_distinguishes_empty_ingest_and_hybrid_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            empty = workspace / "empty"
            empty.mkdir()
            source = workspace / "source"
            source.mkdir()
            (source / "正文.md").write_text(
                "状态：已发布\n# 测试角色甲\n当前位置：测试城\n",
                encoding="utf-8",
            )
            service = PlotInitService(workspace)
            routed_new = service.dry_run(
                project_root=workspace / "new-project",
                mode="auto",
                seed="玄幻",
                sources=[empty],
            )
            routed_ingest = service.dry_run(
                project_root=workspace / "ingest-project",
                mode="auto",
                sources=[source],
            )
            routed_hybrid = service.dry_run(
                project_root=workspace / "hybrid-project",
                mode="auto",
                seed="强化城市制度张力",
                sources=[source],
            )
            self.assertEqual("new", routed_new["mode"])
            self.assertEqual("ingest", routed_ingest["mode"])
            self.assertEqual("hybrid", routed_hybrid["mode"])

    def test_rich_ingest_is_read_only_supports_encodings_and_needs_no_questions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            sources = workspace / "sources"
            (sources / "正文").mkdir(parents=True)
            (sources / "设定集").mkdir()
            (sources / "角色").mkdir()
            (sources / "大纲").mkdir()
            (sources / "正文" / "第1章.md").write_text(
                "\n".join(
                    [
                        "状态：已发布",
                        "# 测试角色甲",
                        "当前位置：测试城",
                        "测试角色甲持有青铜钥匙。",
                        "测试角色甲与镜像客形成死敌。",
                        "此刻是景历十二年三月初七。",
                    ]
                ),
                encoding="utf-8",
            )
            (sources / "设定集" / "作品.json").write_text(
                json.dumps(complete_seed(), ensure_ascii=False, indent=2),
                encoding="utf-8-sig",
            )
            (sources / "角色" / "人物.txt").write_bytes(
                "# 测试角色甲\n测试角色甲（别名：小云、云哥）\n当前压力：测试角色甲认为列车署会放行。".encode(
                    "gbk"
                )
            )
            (sources / "大纲" / "第一卷.md").write_text(
                "状态：已确认\n下一章测试角色甲将前往测试枢纽站。\n",
                encoding="utf-8",
            )
            (sources / "空文件.md").write_bytes(b"")
            (sources / "二进制.txt").write_bytes(b"\x00\x01\x02")
            before = file_fingerprints(sources)

            service = PlotInitService(workspace)
            result = service.dry_run(
                project_root=workspace / "novel",
                mode="ingest",
                target_profile="continuity_ready",
                sources=[sources],
            )
            after = file_fingerprints(sources)
            self.assertEqual(before, after)
            self.assertEqual("READY_TO_PROPOSE", result["status"])
            self.assertEqual([], result["current_questions"])
            self.assertEqual(0, result["decision_package_count"])
            manifest = result["source_manifest"]
            encodings = {item["encoding"] for item in manifest}
            self.assertTrue({"utf-8", "utf-8-bom", "gbk"}.issubset(encodings))
            self.assertTrue(
                any(item["parse_status"] == "excluded" for item in manifest)
            )
            claims = result["bundle"]["provenance"]["claims"]
            self.assertGreater(len(claims), 10)
            self.assertTrue(all(claim["exact_evidence"] for claim in claims))
            self.assertTrue(all(claim["line_start"] >= 1 for claim in claims))
            self.assertTrue(
                any(claim["knowledge_plane"] == "author_plan" for claim in claims)
            )
            self.assertTrue(
                any(claim["story_time"] is not None for claim in claims)
            )
            aliases = result["bundle"]["provenance"]["entity_aliases"]
            self.assertTrue(any(alias["alias"] == "小云" for alias in aliases))
            entity_ids = {
                entity["entity_id"] for entity in result["bundle"]["entities"]
            }
            self.assertTrue(
                any(
                    entity["canonical_name"] == "镜像客"
                    for entity in result["bundle"]["entities"]
                )
            )
            self.assertTrue(
                all(
                    relation["source_entity_id"] in entity_ids
                    and relation["target_entity_id"] in entity_ids
                    for relation in result["bundle"]["relations"]
                )
            )
            self.assertEqual(
                len(manifest),
                len({item["source_id"] for item in manifest}),
            )
            self.assertTrue(
                all(item["source_version_id"].startswith("srcv-") for item in manifest)
            )

    def test_inventory_excludes_entire_plot_rag_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            (project / ".plot-rag" / "cache").mkdir(parents=True)
            (project / ".plot-rag" / "config.json").write_text(
                json.dumps({"config_version": 3}, ensure_ascii=False),
                encoding="utf-8",
            )
            (project / ".plot-rag" / "notes.md").write_text(
                "这也是运行时文件，不是作品来源。",
                encoding="utf-8",
            )
            (project / ".plot-rag" / "cache" / "derived.md").write_text(
                "派生缓存。",
                encoding="utf-8",
            )
            (project / "设定集").mkdir()
            source = project / "设定集" / "世界.md"
            source.write_text(
                "状态：已定稿\n核心规则：力量必有代价。\n",
                encoding="utf-8",
            )

            result = PlotInitService(workspace).dry_run(
                project_root=project,
                mode="ingest",
                sources=[project],
            )

            real_paths = {
                Path(item["real_path"]).resolve()
                for item in result["source_manifest"]
            }
            self.assertEqual({source.resolve()}, real_paths)
            self.assertTrue(
                any(
                    Path(item["path"]).resolve() == (project / ".plot-rag").resolve()
                    and item["reason"] == "generated_plot_rag_storage"
                    for item in result["bundle"]["provenance"]["source_issues"]
                )
            )

            explicit_runtime = PlotInitService(workspace).dry_run(
                project_root=project,
                mode="ingest",
                sources=[project / ".plot-rag"],
            )
            self.assertEqual([], explicit_runtime["source_manifest"])
            self.assertTrue(
                any(
                    item["reason"] == "generated_plot_rag_storage"
                    for item in explicit_runtime["bundle"]["provenance"][
                        "source_issues"
                    ]
                )
            )

    def test_lifecycle_manifest_normalizes_internal_and_external_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            internal = project / "设定集" / "世界.md"
            internal.parent.mkdir(parents=True)
            internal.write_text(
                "状态：已定稿\n核心规则：力量必有代价。\n",
                encoding="utf-8",
            )
            external = workspace / "external.md"
            external.write_text(
                "状态：已确认\n第一卷变化：主角失去旧身份。\n",
                encoding="utf-8",
            )
            service = PlotInitService(workspace)
            started = service.start(
                project_root=project,
                mode="ingest",
                sources=[project, external],
                idempotency_key="manifest-normalize-start",
            )
            proposal = service.propose(
                started["session_id"],
                expected_session_revision=started["session_revision"],
                idempotency_key="manifest-normalize-propose",
            )["proposal"]

            package = proposal_to_lifecycle_package(proposal)
            by_real_path = {
                Path(item["real_path"]).resolve(): item
                for item in package["source_manifest"]
            }
            internal_item = by_real_path[internal.resolve()]
            self.assertEqual("设定集/世界.md", internal_item["path"])
            self.assertTrue(internal_item["inventory_path"].startswith("source-1/"))
            self.assertFalse(internal_item["external_source"])
            self.assertEqual(
                "project_relative",
                internal_item["accepted_path_kind"],
            )

            external_item = by_real_path[external.resolve()]
            self.assertEqual(
                external.resolve(),
                Path(external_item["path"]).resolve(),
            )
            self.assertTrue(external_item["inventory_path"].startswith("source-2/"))
            self.assertTrue(external_item["external_source"])
            self.assertEqual(
                "external_absolute",
                external_item["accepted_path_kind"],
            )
            self.assertEqual(
                canonical_hash(package["source_manifest"]),
                package["source_manifest_hash"],
            )

    def test_source_change_reuses_unchanged_claims_and_precisely_invalidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            sources = workspace / "sources"
            sources.mkdir()
            first_path = sources / "正文.md"
            second_path = sources / "设定.md"
            first_path.write_text(
                "状态：已发布\n# 测试角色甲\n当前位置：测试城\n",
                encoding="utf-8",
            )
            second_path.write_text(
                "状态：已定稿\n核心规则：跨层移动必须依赖列车。\n",
                encoding="utf-8",
            )
            service = PlotInitService(workspace)
            started = service.start(
                project_root=workspace / "novel",
                mode="ingest",
                sources=[sources],
                idempotency_key="source-start",
            )
            old_by_path = {item["path"]: item for item in started["source_manifest"]}
            first_path.write_text(
                "状态：已发布\n# 测试角色甲\n当前位置：测试枢纽站\n",
                encoding="utf-8",
            )
            advanced = service.advance(
                started["session_id"],
                expected_session_revision=started["session_revision"],
                idempotency_key="source-advance",
            )
            changed = advanced["source_diff"]["changed"]
            self.assertEqual(1, len(changed))
            self.assertEqual(1, len(advanced["reprocessed_source_ids"]))
            self.assertEqual(
                changed[0]["source_id"], advanced["reprocessed_source_ids"][0]
            )
            new_by_path = {item["path"]: item for item in advanced["source_manifest"]}
            changed_path = next(
                path
                for path, old in old_by_path.items()
                if old["source_id"] == changed[0]["source_id"]
            )
            unchanged_path = next(path for path in old_by_path if path != changed_path)
            self.assertEqual(
                old_by_path[changed_path]["source_id"],
                new_by_path[changed_path]["source_id"],
            )
            self.assertNotEqual(
                old_by_path[changed_path]["source_version_id"],
                new_by_path[changed_path]["source_version_id"],
            )
            self.assertEqual(2, new_by_path[changed_path]["head_revision"])
            self.assertEqual(
                old_by_path[unchanged_path]["source_version_id"],
                new_by_path[unchanged_path]["source_version_id"],
            )
            self.assertEqual(1, new_by_path[unchanged_path]["head_revision"])
            self.assertIn("normalize", advanced["invalidated_nodes"])
            self.assertNotIn(
                f"source:{old_by_path[unchanged_path]['source_id']}:extract",
                advanced["invalidated_nodes"],
            )
            frozen = service.propose(
                started["session_id"],
                expected_session_revision=advanced["session_revision"],
                idempotency_key="source-propose",
            )
            first_path.write_text(
                "状态：已发布\n# 测试角色甲\n当前位置：旧港\n",
                encoding="utf-8",
            )
            inspected = service.inspect(started["session_id"], view="proposal")
            self.assertEqual("STALE_SOURCE", inspected["staleness"]["status"])
            self.assertTrue(inspected["staleness"]["source_changed"])
            self.assertEqual(
                frozen["proposal"]["proposal_id"],
                inspected["proposal"]["proposal_id"],
            )

    def test_conflicts_planes_and_story_time_remain_proposed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            sources = workspace / "sources"
            sources.mkdir()
            (sources / "正文甲.md").write_text(
                "状态：已发布\n# 测试角色甲\n当前位置：测试城\n"
                "众所周知，核心规则不得绕过列车。\n",
                encoding="utf-8",
            )
            (sources / "正文乙.md").write_text(
                "状态：已发布\n# 测试角色甲\n当前位置：测试枢纽站\n"
                "此刻是景历十二年三月初七。\n",
                encoding="utf-8",
            )
            result = PlotInitService(workspace).dry_run(
                project_root=workspace / "novel",
                mode="ingest",
                sources=[sources],
            )
            conflicts = result["bundle"]["conflicts"]
            self.assertTrue(
                any(item["type"] == "semantic_contradiction" for item in conflicts)
            )
            claims = result["bundle"]["provenance"]["claims"]
            self.assertTrue(
                any(claim["knowledge_plane"] == "public_narrative" for claim in claims)
            )
            self.assertTrue(all(claim["canon_status"] == "proposed" for claim in claims))
            self.assertEqual(0, result["bundle"]["validation"]["preapproval_canon_delta"])

    def test_proposal_is_immutable_and_does_not_materialize_standard_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            service = PlotInitService(workspace)
            started = service.start(
                project_root=project,
                mode="new",
                seed=complete_seed(),
                idempotency_key="start",
            )
            proposed = service.propose(
                started["session_id"],
                expected_session_revision=started["session_revision"],
                idempotency_key="propose",
            )
            proposal = proposed["proposal"]
            self.assertEqual("PROPOSAL_FROZEN", proposal["status"])
            self.assertFalse(proposal["apply_plan"]["executed"])
            self.assertTrue(proposal["apply_plan"]["requires_approval_grant"])
            self.assertTrue(
                all(
                    not (project / item["path"]).exists()
                    for item in proposal["bundle"]["artifact_manifest"]
                    if item["operation"] == "create"
                )
            )
            replay = service.propose(
                started["session_id"],
                expected_session_revision=started["session_revision"],
                idempotency_key="propose",
            )
            self.assertTrue(replay["idempotent"])
            self.assertEqual(proposal["proposal_id"], replay["proposal"]["proposal_id"])
            with self.assertRaises(PlotInitError) as immutable_error:
                service.answer(
                    started["session_id"],
                    {"genre-contract": "mystery-discovery"},
                    expected_session_revision=proposed["session_revision"],
                    idempotency_key="late-answer",
                )
            self.assertEqual("PROPOSAL_IMMUTABLE", immutable_error.exception.code)
            active_review = service.find_active_session(project_root=project)
            self.assertIsNotNone(active_review)
            self.assertEqual("PROPOSAL_FROZEN", active_review["status"])
            with closing(sqlite3.connect(service.database_path)) as connection:
                proposal_count = connection.execute(
                    "SELECT COUNT(*) FROM initialization_proposals"
                ).fetchone()[0]
            self.assertEqual(1, proposal_count)

    def test_frozen_proposal_has_deterministic_lifecycle_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            seed = complete_seed()
            seed["actor_system"]["protagonist"]["capabilities"] = [
                {
                    "name": "改写通行条件",
                    "cost": "消耗通行证",
                    "cooldown": "一日",
                }
            ]
            seed["open_loops"] = [
                {
                    "description": "查清被注销通行证的来源",
                    "status": "open",
                    "loop_type": "mystery",
                }
            ]
            service = PlotInitService(workspace)
            started = service.start(
                project_root=workspace / "novel",
                mode="new",
                seed=seed,
                idempotency_key="start-adapter",
            )
            proposal = service.propose(
                started["session_id"],
                expected_session_revision=started["session_revision"],
                idempotency_key="propose-adapter",
            )["proposal"]
            first = proposal_to_lifecycle_package(proposal)
            second = proposal_to_lifecycle_package(proposal)
            self.assertEqual(first, second)
            self.assertEqual(proposal["proposal_id"], first["proposal_id"])
            self.assertEqual(proposal["package_hash"], first["package_hash"])
            self.assertTrue(first["entities"])
            event_types = {event["event_type"] for event in first["events"]}
            self.assertTrue(
                {"entity", "world_rule", "state", "movement", "inventory", "ability", "open_loop"}
                .issubset(event_types)
            )
            self.assertTrue(
                all(event["scope"] in {"current", "planned", "historical", "timeless"} for event in first["events"])
            )
            tampered = json.loads(json.dumps(proposal, ensure_ascii=False))
            tampered["bundle"]["story_engine"]["actionable_goal"] = "篡改"
            with self.assertRaises(PlotInitError) as mismatch:
                proposal_to_lifecycle_package(tampered)
            self.assertEqual("PACKAGE_HASH_MISMATCH", mismatch.exception.code)

    def test_inspect_is_read_only_and_cancel_is_journaled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            service = PlotInitService(workspace)
            started = service.start(
                project_root=workspace / "novel",
                mode="new",
                seed="都市异能",
                idempotency_key="start",
            )
            before = file_fingerprints(workspace)
            inspected = service.inspect(started["session_id"], view="questions")
            after = file_fingerprints(workspace)
            self.assertEqual(before, after)
            self.assertTrue(inspected["read_only"])
            cancelled = service.cancel(
                started["session_id"],
                expected_session_revision=started["session_revision"],
                idempotency_key="cancel",
                reason="用户终止",
            )
            self.assertEqual("CANCELLED", cancelled["status"])
            journal = service.inspect(started["session_id"], view="journal")["journal"]
            self.assertTrue(any(item["event_type"] == "cancel" for item in journal))

    def test_modes_profiles_and_project_path_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            service = PlotInitService(workspace)
            with self.assertRaises(PlotInitError) as mismatch:
                service.dry_run(
                    project_root=workspace / "novel",
                    mode="new",
                    target_profile="normalize_only",
                    seed="玄幻",
                )
            self.assertEqual("PROFILE_MODE_MISMATCH", mismatch.exception.code)
            normalized = service.dry_run(
                project_root=workspace / "novel",
                mode="hybrid",
                target_profile="normalize_only",
                seed="玄幻",
            )
            self.assertEqual("ingest", normalized["mode"])
            self.assertTrue(
                any(
                    decision.get("kind") == "deterministic_normalization"
                    for decision in normalized["decisions"]
                )
            )
            outside = workspace.parent / "outside-target"
            with self.assertRaises(PlotInitError) as unsafe:
                service.dry_run(project_root=outside, mode="new", seed="玄幻")
            self.assertEqual("UNSAFE_PROJECT_ROOT", unsafe.exception.code)

    def test_canon_guard_change_blocks_proposal_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            (project / ".plot-rag").mkdir(parents=True)
            config_path = project / ".plot-rag" / "config.json"
            config_path.write_text(
                json.dumps({"config_version": 3, "canon_revision": 2}),
                encoding="utf-8",
            )
            service = PlotInitService(workspace)
            started = service.start(
                project_root=project,
                mode="new",
                seed=complete_seed(),
                idempotency_key="canon-start",
            )
            config_path.write_text(
                json.dumps({"config_version": 3, "canon_revision": 3}),
                encoding="utf-8",
            )
            with self.assertRaises(PlotInitError) as stale:
                service.propose(
                    started["session_id"],
                    expected_session_revision=started["session_revision"],
                    idempotency_key="canon-propose",
                )
            self.assertEqual("STALE_CANON", stale.exception.code)

    def test_hook_arbiter_prioritizes_active_initialization(self) -> None:
        active = {
            "session_id": "init-1",
            "status": "NEEDS_INPUT",
            "stage": "WORLD_CAUSAL_KERNEL",
        }
        self.assertEqual(
            "none",
            resolve_initialization_intent("继续优化初始化框架", active_session=None),
        )
        self.assertEqual(
            "advance",
            resolve_initialization_intent("继续", active_session=active),
        )
        self.assertEqual(
            "answer",
            resolve_initialization_intent("选第二个", active_session=active),
        )
        for story_answer in (
            "主角测试新能力后阅读古籍文档",
            "先修复法器，再核对旧版本功法代码",
            "这段记忆会进入角色自己的缓存",
            "主角进入宗门仓库领取法器",
            "守卫检查门禁后放行",
            "检查脚本上的古老符文",
            "主角阅读项目文档。随后修复法器",
            "digital cliff preview auditorium",
        ):
            with self.subTest(story_answer=story_answer):
                self.assertEqual(
                    "answer",
                    resolve_initialization_intent(
                        story_answer,
                        active_session=active,
                    ),
                )
        self.assertEqual(
            "none",
            resolve_initialization_intent("做一次全量审查", active_session=active),
        )
        self.assertEqual(
            "none",
            resolve_initialization_intent("审查插件代码", active_session=active),
        )
        self.assertEqual(
            "none",
            resolve_initialization_intent(
                "修复插件的初始化流程",
                active_session=active,
            ),
        )
        self.assertEqual(
            "none",
            resolve_initialization_intent(
                "检查当前实现有没有遗漏",
                active_session=active,
            ),
        )
        self.assertEqual(
            "start",
            resolve_initialization_intent(
                "初始化一部关于代码审查员的悬疑网文",
            ),
        )
        frozen = {
            **active,
            "status": "PROPOSAL_FROZEN",
            "stage": "PROPOSAL_FROZEN",
        }
        self.assertEqual(
            "wait",
            resolve_initialization_intent("继续", active_session=frozen),
        )
        self.assertEqual(
            "wait",
            resolve_initialization_intent("这是新的创作要求", active_session=frozen),
        )
        decision = arbitrate_initialization_hook(
            {"hook_event_name": "UserPromptSubmit", "prompt": "继续"},
            active_session=active,
        )
        self.assertEqual("initialization", decision["workflow"])
        self.assertTrue(decision["suppress_plot_receipt"])
        stopped = arbitrate_initialization_hook(
            {"hook_event_name": "Stop"},
            active_session=active,
        )
        self.assertTrue(stopped["suppress_plot_stop_extract"])
        self.assertTrue(
            is_initialization_storage_path(
                "C:/novel/.plot-rag/init-sessions/init-1/session.json"
            )
        )
        self.assertTrue(
            is_initialization_storage_path("C:/novel/.plot-rag-init/init.sqlite3")
        )
        self.assertFalse(
            is_initialization_storage_path("C:/novel/正文/第一章.md")
        )

    def test_schema_and_template_json_are_present_and_well_formed(self) -> None:
        schema_root = PLUGIN_ROOT / "schemas" / "plot-rag-init" / "v1"
        expected = {
            "common.schema.json",
            "source-descriptor.schema.json",
            "claim.schema.json",
            "session.schema.json",
            "initialization-bundle.schema.json",
            "proposal.schema.json",
        }
        self.assertEqual(expected, {path.name for path in schema_root.glob("*.json")})
        for path in schema_root.glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                "https://json-schema.org/draft/2020-12/schema",
                payload["$schema"],
            )
        config = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v3.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(3, config["config_version"])
        self.assertTrue(config["initialization"]["proposal_only"])
        self.assertEqual(
            "auto", config["initialization"]["schema_version"]
        )


if __name__ == "__main__":
    unittest.main()
