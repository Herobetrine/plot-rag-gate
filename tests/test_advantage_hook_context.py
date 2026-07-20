from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HOOKS = PLUGIN_ROOT / "hooks"
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import plot_progression_gate as hook  # noqa: E402
import v1_runtime  # noqa: E402
from continuity import ContinuityService  # noqa: E402
from plot_rag import PlotRagError, load_config  # noqa: E402


class _FakeStore:
    @contextmanager
    def read_connection(self):
        connection = sqlite3.connect(":memory:")
        try:
            yield connection
        finally:
            connection.close()


class _FakeService:
    store = _FakeStore()


class AdvantageHookContextTests(unittest.TestCase):
    def test_v1_runtime_supports_package_import_without_module_duplication(
        self,
    ) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-c",
                (
                    "import scripts.state_rag as state_rag; "
                    "import scripts.v1_runtime as runtime; "
                    "assert runtime.state_rag is state_rag"
                ),
            ],
            cwd=PLUGIN_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(
            0,
            completed.returncode,
            msg=completed.stdout + completed.stderr,
        )

    def make_project(self, base: Path) -> Path:
        root = base / "novel"
        (root / ".plot-rag").mkdir(parents=True)
        (root / "settings").mkdir()
        (root / "settings" / "world.md").write_text(
            "# 世界\n测试角色甲持有一件来源未知的遗物。",
            encoding="utf-8",
        )
        config = {
            "config_version": 3,
            "enabled": True,
            "grill": {"enabled": False},
            "event_experience": {"enabled": False},
            "authority_sources": [
                {
                    "glob": "settings/**/*.md",
                    "role": "setting",
                    "scope_policy": "infer_and_review",
                    "ingest_policy": "include",
                    "priority": 100,
                }
            ],
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
        (root / ".plot-rag" / "config.json").write_text(
            json.dumps(config, ensure_ascii=False),
            encoding="utf-8",
        )
        return root

    def test_advantage_config_defaults_and_strict_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            config = load_config(root)
            self.assertEqual(
                {
                    "enabled": False,
                    "shadow": True,
                    "schema_version": "plot-rag-advantage/v1",
                    "strict_runtime_validation": False,
                    "readable_projection": True,
                    "mandatory_context": True,
                },
                config["advantage"],
            )
            path = root / ".plot-rag" / "config.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["advantage"] = {"mandatory_context": "yes"}
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(
                PlotRagError,
                "config.advantage.mandatory_context must be a boolean",
            ):
                load_config(root)

    def test_trigger_signals_cover_stable_special_action_and_continuity(self) -> None:
        signals = v1_runtime._advantage_trigger_signals(
            "剧情推演：测试角色甲激活样例优势核心，演算点资源发生变化。"
            "advantage_id=advantage_example_core "
            "module_id=advantage_module_bianhuo"
        )
        self.assertTrue(signals["required"])
        self.assertEqual(
            ["stable_id", "special_term", "action", "continuity"],
            signals["layers"],
        )
        self.assertEqual(
            ["advantage_example_core"],
            signals["stable_ids"]["advantage_ids"],
        )
        self.assertEqual(
            ["advantage_module_bianhuo"],
            signals["stable_ids"]["module_ids"],
        )
        self.assertIn("演算点", signals["special_terms"])
        self.assertIn("激活", signals["actions"])
        self.assertEqual(["resource"], signals["continuity_signals"])

    def test_stable_ids_rank_first_and_render_every_mandatory_plane(self) -> None:
        records = [
            {
                "schema_version": "plot-rag-advantage/v1",
                "definition": {
                    "advantage_id": "advantage_secondary",
                    "title": "次要外挂",
                },
                "anchors": [{"owner_entity_id": "character_other"}],
                "modules": [
                    {
                        "module_id": "advantage_module_other",
                        "kind": "appraisal",
                    }
                ],
                "runtime": {"enabled": True},
                "ledger": [],
                "knowledge": [],
                "contracts": [],
                "narrative_contract": {},
                "progression": [],
                "exposure": {"exposure": 0},
            },
            {
                "schema_version": "plot-rag-advantage/v1",
                "definition": {
                    "advantage_id": "advantage_example_core",
                    "title": "样例优势核心",
                    "profiles": ["resource_transformer", "growth_relic"],
                },
                "anchors": [
                    {
                        "anchor_id": "advantage_anchor_example_core",
                        "owner_entity_id": "character_testactora",
                    }
                ],
                "modules": [
                    {
                        "module_id": "advantage_module_take_fire",
                        "kind": "resource_transform",
                        "trigger": {"action": "能力提取"},
                    },
                    {
                        "module_id": "advantage_module_bianhuo",
                        "kind": "appraisal",
                        "trigger": {"action": "状态解析"},
                        "preconditions": ["目标样本可观察"],
                        "costs": [{"kind": "attention"}],
                        "failure_modes": ["认知不足"],
                    },
                ],
                "runtime": {
                    "owner_entity_id": "character_testactora",
                    "enabled": True,
                    "stage": "初醒",
                    "resources": {"演算点": 1},
                    "pollution": 2,
                    "exposure": 3,
                },
                "module_runtime": [
                    {
                        "module_id": "advantage_module_bianhuo",
                        "cooldown": None,
                    }
                ],
                "ledger": [
                    {
                        "entry_id": "ledger_1",
                        "module_id": "advantage_module_bianhuo",
                        "input": {},
                        "output": {"观察": 1},
                    }
                ],
                "knowledge": [
                    {
                        "knowledge_plane": "actor_belief",
                        "observer_entity_id": "character_testactora",
                        "claim": "测试角色甲知道自己能够状态解析。",
                        "reveal_stage": "canon",
                    },
                    {
                        "knowledge_plane": "objective",
                        "claim": "样例装置仍有未揭示的上限。",
                        "reveal_stage": "hidden",
                    },
                ],
                "contracts": [{"debt": 1, "breach_effect": "污染上升"}],
                "narrative_contract": {
                    "reading_promise": "收益与代价同时升级"
                },
                "progression": [{"stage": "初醒"}],
                "exposure": {"pollution": 2, "exposure": 3},
            },
        ]

        def query_many(_connection, **_kwargs):
            return records

        api = SimpleNamespace(
            query_advantage_contexts=query_many,
            read_advantage_projection_metadata=lambda _connection: {
                "projection_hash": "a" * 64
            },
        )
        prompt = (
            "剧情推演：测试角色甲用 advantage_module_bianhuo 状态解析。"
            "advantage_id=advantage_example_core"
        )
        with patch(
            "v1_runtime._load_advantage_query_api",
            return_value=api,
        ):
            result = v1_runtime._build_advantage_context(
                _FakeService(),
                prompt,
                entity_resolution={
                    "entity_ids": ["character_testactora"],
                    "pov_entity_id": "character_testactora",
                },
                branch_id="main",
                policy={"enabled": True, "mandatory_context": True},
            )

        self.assertEqual("ready", result["status"])
        self.assertEqual(
            "advantage_example_core",
            result["selected_advantage_ids"][0],
        )
        self.assertEqual(
            "advantage_module_bianhuo",
            result["selected_module_ids"][0],
        )
        self.assertEqual("a" * 64, result["advantage_projection_hash"])
        rendered = result["context_text"]
        self.assertIn("[accepted-advantage:advantage_example_core]", rendered)
        for field in (
            "modules=",
            "runtime=",
            "module_runtime=",
            "ledger=",
            "knowledge=",
            "contracts=",
            "narrative_contract=",
            "progression=",
            "exposure=",
        ):
            self.assertIn(field, rendered)
        self.assertIn("knowledge=", rendered)
        self.assertIn("测试角色甲知道自己能够状态解析。", rendered)
        self.assertNotIn("样例装置仍有未揭示的上限。", rendered)
        self.assertNotIn("content_visibility", rendered)
        self.assertNotIn("reveal_stage", rendered)
        self.assertNotIn("observer_entity_id", rendered)
        self.assertEqual(
            [{"claim": "测试角色甲知道自己能够状态解析。"}],
            result["records"][0]["knowledge"],
        )
        self.assertEqual(
            {
                "source_count": 2,
                "visible_count": 1,
                "excluded_count": 1,
            },
            {
                key: result["knowledge_filter_telemetry"][key]
                for key in (
                    "source_count",
                    "visible_count",
                    "excluded_count",
                )
            },
        )
        self.assertEqual(
            64,
            len(result["knowledge_filter_telemetry"]["excluded_hash"]),
        )

    def test_missing_optional_core_degrades_without_dropping_unknown_guard(self) -> None:
        with patch(
            "v1_runtime._load_advantage_query_api",
            return_value=None,
        ):
            result = v1_runtime._build_advantage_context(
                _FakeService(),
                "剧情推演：主角激活系统面板。",
                entity_resolution={"entity_ids": []},
                policy={"enabled": True, "mandatory_context": True},
            )
        self.assertEqual("unavailable", result["status"])
        self.assertTrue(result["required"])
        self.assertIn("均按未知处理", result["context_text"])
        self.assertIn("不得", result["context_text"])

    def test_current_head_does_not_forge_chapter_scene_cursor(self) -> None:
        calls: list[dict[str, object]] = []

        def query_many(_connection, **kwargs):
            calls.append(dict(kwargs))
            return [
                {
                    "definition": {
                        "advantage_id": "advantage_cursor",
                        "title": "游标测试",
                    },
                    "knowledge": [],
                }
            ]

        api = SimpleNamespace(
            query_advantage_contexts=query_many,
            read_advantage_projection_metadata=lambda _connection: {
                "projection_hash": "b" * 64
            },
        )
        with patch(
            "v1_runtime._load_advantage_query_api",
            return_value=api,
        ):
            result = v1_runtime._build_advantage_context(
                _FakeService(),
                "剧情推演：继续当前故事。",
                entity_resolution={"entity_ids": []},
                chapter_no=12,
                scene_index=3,
                policy={"enabled": True, "mandatory_context": True},
            )

        self.assertEqual("ready", result["status"])
        self.assertEqual({}, result["story_cursor"])
        self.assertEqual("current_head", result["query_mode"])
        self.assertEqual("accepted_coordinate_or_current_head", result["cursor_policy"])
        self.assertEqual(1, len(calls))
        self.assertIsNone(calls[0]["story_cursor"])
        self.assertIsNone(calls[0]["chapter_no"])
        self.assertIsNone(calls[0]["scene_index"])

    def test_historical_query_requires_real_comparable_cursor(self) -> None:
        calls: list[dict[str, object]] = []

        def query_many(_connection, **kwargs):
            calls.append(dict(kwargs))
            return []

        api = SimpleNamespace(query_advantage_contexts=query_many)
        with patch(
            "v1_runtime._load_advantage_query_api",
            return_value=api,
        ):
            result = v1_runtime._build_advantage_context(
                _FakeService(),
                "剧情推演：复盘过去事件。",
                entity_resolution={"entity_ids": []},
                chapter_no=12,
                scene_index=3,
                policy={
                    "enabled": True,
                    "mandatory_context": True,
                    "_historical_query": True,
                    "_require_story_cursor": True,
                },
            )

        self.assertEqual("degraded", result["status"])
        self.assertEqual("historical", result["query_mode"])
        self.assertEqual("required_comparable", result["cursor_policy"])
        self.assertEqual(
            "AdvantageStoryCursorRequired",
            result["errors"][0]["error_type"],
        )
        self.assertEqual([], calls)

    def test_real_calendar_cursor_is_preserved_for_historical_query(self) -> None:
        calls: list[dict[str, object]] = []

        def query_many(_connection, **kwargs):
            calls.append(dict(kwargs))
            return [
                {
                    "definition": {
                        "advantage_id": "advantage_generic",
                        "title": "示例游标",
                    },
                    "knowledge": [],
                }
            ]

        api = SimpleNamespace(
            query_advantage_contexts=query_many,
            read_advantage_projection_metadata=lambda _connection: {
                "projection_hash": "c" * 64
            },
        )
        with patch(
            "v1_runtime._load_advantage_query_api",
            return_value=api,
        ):
            result = v1_runtime._build_advantage_context(
                _FakeService(),
                "剧情推演：复盘示例时间线。",
                entity_resolution={"entity_ids": []},
                story_cursor={
                    "calendar_id": "generic",
                    "ordinal": 42,
                    "label": "测试城",
                },
                chapter_no=12,
                scene_index=3,
                policy={
                    "enabled": True,
                    "mandatory_context": True,
                    "_historical_query": True,
                    "_require_story_cursor": True,
                },
            )

        self.assertEqual("ready", result["status"])
        self.assertEqual("generic", result["story_cursor"]["calendar_id"])
        self.assertEqual(42, result["story_cursor"]["ordinal"])
        self.assertEqual(1, len(calls))
        self.assertEqual(
            {"calendar_id": "generic", "ordinal": 42, "label": "测试城"},
            calls[0]["story_cursor"],
        )
        self.assertIsNone(calls[0]["chapter_no"])
        self.assertIsNone(calls[0]["scene_index"])

    def test_hook_reports_post_lock_advantage_prepare_summary(self) -> None:
        state_result = {
            "status": "ready",
            "receipt_id": "receipt-advantage",
            "lifecycle_mode": "strict_proposal",
            "context": (
                "[ACCEPTED_ADVANTAGE_CONTEXT]\n"
                "[accepted-advantage:advantage_example_core]"
            ),
            "longform": {
                "advantage_context": {
                    "required": True,
                    "status": "ready",
                    "triggers": {
                        "layers": ["stable_id", "action"],
                        "special_terms": ["炉"],
                        "actions": ["激活"],
                        "continuity_signals": [],
                    },
                    "stable_ids": {
                        "advantage_ids": ["advantage_example_core"],
                        "module_ids": ["advantage_module_bianhuo"],
                    },
                    "selected_advantage_ids": ["advantage_example_core"],
                    "selected_module_ids": ["advantage_module_bianhuo"],
                }
            },
        }
        context = hook._context(
            Path("C:/fixture"),
            "request-advantage",
            None,
            state_result,
        )
        self.assertIn("[PLOT_RAG_ADVANTAGE_HOOK]", context)
        self.assertIn(
            "phase: post_locked_intent_and_event_experience_prepare",
            context,
        )
        self.assertIn(
            "mandatory_context_marker: [ACCEPTED_ADVANTAGE_CONTEXT]",
            context,
        )
        self.assertIn("selected_advantage_ids: advantage_example_core", context)
        self.assertIn(
            "selected_module_ids: advantage_module_bianhuo",
            context,
        )

    def test_strict_propose_adapts_v4_item_candidate_into_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prepared = v1_runtime.prepare_plot_turn(
                root,
                "剧情推演：测试角色甲获得样例优势核心。",
                session_id="advantage-item-session",
                turn_id="advantage-item-turn",
            )
            evidence = "测试角色甲确认样例优势核心是一件唯一遗物。"
            candidate = {
                "schema_version": "plot-rag-delta/v4",
                "event_type": "item_spec",
                "action": "define",
                "subject": {
                    "kind": "item_definition",
                    "mention": "样例优势核心",
                },
                "objects": [],
                "changes": {
                    "definition": {
                        "item_kind": "artifact",
                        "stack_policy": "non_stackable",
                        "uniqueness_policy": "unique_definition",
                        "description": "唯一遗物",
                    }
                },
                "scope": "timeless",
                "story_coordinate": {
                    "calendar_id": "story-main",
                    "ordinal": 1,
                },
                "knowledge_plane": "objective",
                "confidence": 0.99,
                "evidence": evidence,
                "effective_at": None,
                "ambiguity": None,
            }
            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    [candidate],
                    [],
                    {
                        "status": "ok",
                        "configured": True,
                        "model": "fixture-extractor",
                    },
                ),
            ):
                proposed = v1_runtime.propose_plot_turn(
                    root,
                    evidence,
                    request_id=prepared["receipt_id"],
                )
            self.assertIn(proposed["status"], {"proposed", "quarantined"})
            self.assertEqual(
                {
                    "ok": True,
                    "candidate_count": 1,
                    "adapted_count": 1,
                },
                proposed["item_candidate_adapter"],
            )
            self.assertEqual(1, len(proposed["proposal_events"]))
            event = proposed["proposal_events"][0]
            self.assertEqual("item_spec", event["event_type"])
            self.assertTrue(
                str(event["item_definition_id"]).startswith(
                    "item_definition_"
                )
            )
            self.assertEqual(
                [],
                ContinuityService(root).query_facts()["facts"],
            )

    def test_item_entity_reference_requires_an_item_entity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            service = ContinuityService(root)
            item = service.register_entity("item", "样例优势核心")
            actor = service.register_entity("character", "测试角色甲")
            with service.store.read_connection() as connection:
                resolver = v1_runtime._item_candidate_resolver(
                    service,
                    connection,
                    [],
                    {"artifact_id": "item-entity-resolver"},
                )
                self.assertEqual(
                    {
                        "status": "RESOLVED",
                        "reference_id": item["entity_id"],
                    },
                    resolver("样例优势核心", "item", "item_entity"),
                )
                wrong_type = resolver("测试角色甲", "item", "item_entity")
            self.assertEqual("UNRESOLVED", wrong_type["status"])
            self.assertEqual(
                [
                    {
                        "entity_id": actor["entity_id"],
                        "entity_type": "character",
                    }
                ],
                wrong_type["candidates"],
            )

    def test_mixed_legacy_and_v4_items_share_atomic_entity_visibility(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prepared = v1_runtime.prepare_plot_turn(
                root,
                "剧情推演：测试角色甲取得事务物品。",
                session_id="mixed-item-session",
                turn_id="mixed-item-turn",
            )
            assistant = (
                "测试角色甲持有事务物品。"
                "事务物品定义为一件钥匙。"
                "事务物品甲依据事务物品完成实例化。"
            )
            legacy_inventory = {
                "schema_version": "plot-rag-delta/v3",
                "category": "inventory",
                "subject": "测试角色甲",
                "field": "inventory",
                "operation": "set",
                "value": {"item": "事务物品", "status": "held"},
                "confidence": 0.99,
                "evidence": "测试角色甲持有事务物品。",
            }
            item_definition = {
                "schema_version": "plot-rag-delta/v4",
                "event_type": "item_spec",
                "action": "define",
                "subject": {
                    "kind": "item_definition",
                    "mention": "事务物品",
                },
                "objects": [],
                "changes": {
                    "definition": {
                        "item_kind": "key",
                        "stack_policy": "non_stackable",
                        "uniqueness_policy": "ordinary",
                        "description": "一件钥匙",
                    }
                },
                "scope": "timeless",
                "story_coordinate": {
                    "calendar_id": "story-main",
                    "ordinal": 1,
                },
                "knowledge_plane": "objective",
                "confidence": 0.99,
                "evidence": "事务物品定义为一件钥匙。",
            }
            item_instance = {
                "schema_version": "plot-rag-delta/v4",
                "event_type": "item_instance",
                "action": "instantiate",
                "subject": {
                    "kind": "item_instance",
                    "mention": "事务物品甲",
                },
                "objects": [
                    {
                        "role": "item_definition",
                        "mention": "事务物品",
                    },
                    {
                        "role": "item_entity",
                        "mention": "事务物品",
                    },
                ],
                "changes": {"attributes": {}},
                "scope": "current",
                "story_coordinate": {
                    "calendar_id": "story-main",
                    "ordinal": 1,
                },
                "knowledge_plane": "objective",
                "confidence": 0.99,
                "evidence": "事务物品甲依据事务物品完成实例化。",
            }
            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    [legacy_inventory, item_definition, item_instance],
                    [],
                    {
                        "status": "ok",
                        "configured": True,
                        "model": "fixture-extractor",
                    },
                ),
            ):
                proposed = v1_runtime.propose_plot_turn(
                    root,
                    assistant,
                    request_id=prepared["receipt_id"],
                )
            self.assertEqual("proposed", proposed["status"])
            self.assertEqual(
                {
                    "ok": True,
                    "candidate_count": 2,
                    "adapted_count": 2,
                },
                proposed["item_candidate_adapter"],
            )
            inventory_event = next(
                event
                for event in proposed["proposal_events"]
                if event["event_type"] == "inventory"
            )
            instance_event = next(
                event
                for event in proposed["proposal_events"]
                if event["event_type"] == "item_instance"
            )
            self.assertEqual(
                inventory_event["item_entity_id"],
                instance_event["item_entity_id"],
            )
            self.assertNotIn(
                "ITEM_REFERENCE_UNRESOLVED",
                {issue["code"] for issue in proposed["issues"]},
            )


if __name__ == "__main__":
    unittest.main()
