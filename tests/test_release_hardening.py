from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from plot_init import (  # noqa: E402
    PlotInitError,
    PlotInitService,
    proposal_to_lifecycle_package,
)
from plot_init.canonical import canonical_hash  # noqa: E402
from plot_init.normalized import recompute_bundle_hash  # noqa: E402
from plot_init.remote_cache import (  # noqa: E402
    RemoteCacheIdentity,
    sanitize_remote_cache_value,
)
from tests.test_plot_init import complete_seed  # noqa: E402


class ReleaseHardeningTests(unittest.TestCase):
    def test_published_content_words_do_not_demote_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            sources = workspace / "sources"
            (sources / "正文").mkdir(parents=True)
            (sources / "设定集").mkdir()
            (sources / "正文" / "第一章.md").write_text(
                "状态：已发布\n# 测试角色甲\n"
                "测试角色甲参考旧计划后，决定把待定方案留给同伴。\n"
                "当前位置：测试城\n",
                encoding="utf-8",
            )
            (sources / "设定集" / "世界规则.md").write_text(
                "状态：已定稿\n# 通行规则\n"
                "这份规则参考历史资料，但内容已经定稿。\n"
                "核心规则：跨层只能乘列车。\n",
                encoding="utf-8",
            )

            result = PlotInitService(workspace).dry_run(
                project_root=workspace / "novel",
                mode="ingest",
                target_profile="continuity_ready",
                sources=[sources],
            )
            manifest = {
                Path(item["path"]).name: item for item in result["source_manifest"]
            }
            chapter = manifest["第一章.md"]
            setting = manifest["世界规则.md"]
            self.assertEqual(
                ("canon", "T1", "published", "include"),
                (
                    chapter["source_role"],
                    chapter["authority_tier"],
                    chapter["artifact_stage"],
                    chapter["ingest_policy"],
                ),
            )
            self.assertEqual(
                ("setting", "T1", "final", "include"),
                (
                    setting["source_role"],
                    setting["authority_tier"],
                    setting["artifact_stage"],
                    setting["ingest_policy"],
                ),
            )

    def test_project_config_materialization_preserves_custom_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            config_path = project / ".plot-rag" / "config.json"
            config_path.parent.mkdir(parents=True)
            original = {
                "config_version": 3,
                "enabled": False,
                "grill": {
                    "enabled": False,
                    "max_questions": 3,
                },
                "authority_sources": [
                    {
                        "glob": "原文/**/*.txt",
                        "role": "canon",
                        "scope_policy": "infer_and_review",
                        "ingest_policy": "include",
                        "priority": 777,
                    }
                ],
                "remote": {
                    "embedding": {
                        "enabled": True,
                        "model": "custom-embedding",
                    }
                },
                "initialization": {
                    "database_path": ".plot-rag/custom-init.sqlite3",
                },
                "custom_extension": {"keep": ["all", "values"]},
            }
            original_bytes = json.dumps(
                original,
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            config_path.write_bytes(original_bytes)

            result = PlotInitService(workspace).dry_run(
                project_root=project,
                mode="new",
                target_profile="plot_ready",
                seed=complete_seed(),
            )
            artifact = next(
                item
                for item in result["bundle"]["artifact_manifest"]
                if item["path"] == ".plot-rag/config.json"
            )
            proposed = json.loads(artifact["proposed_content"])
            self.assertEqual(original_bytes, config_path.read_bytes())
            self.assertFalse(proposed["enabled"])
            self.assertFalse(proposed["grill"]["enabled"])
            self.assertEqual(3, proposed["grill"]["max_questions"])
            self.assertEqual(
                "plot-rag-intent/v1",
                proposed["grill"]["schema_version"],
            )
            self.assertEqual(
                original["authority_sources"][0],
                proposed["authority_sources"][0],
            )
            self.assertEqual(
                {
                    "原文/**/*.txt",
                    "正文/**/*.md",
                    "设定集/**/*.md",
                    "剧情/**/*.md",
                },
                {
                    item["glob"]
                    for item in proposed["authority_sources"]
                },
            )
            self.assertEqual(original["remote"], proposed["remote"])
            self.assertEqual(original["custom_extension"], proposed["custom_extension"])
            self.assertEqual(
                ".plot-rag/custom-init.sqlite3",
                proposed["initialization"]["database_path"],
            )
            self.assertEqual(
                "plot-rag-init/v1",
                proposed["initialization"]["schema_version"],
            )
            self.assertTrue(proposed["initialization"]["proposal_only"])

    def test_rich_seed_separates_explicit_input_from_model_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            text = "\n".join(
                [
                    "题材：都市异能悬疑",
                    "读者承诺：每个案件都改变主角的资源与关系",
                    "核心规则：能力使用会留下可追踪的记忆裂痕",
                    "稀缺资源：未被污染的旧时代档案",
                    "主角：测试角色甲",
                    "对手：镜像客",
                    "外在目标：在七日内找回失踪档案",
                    "失败代价：妹妹的身份会被系统抹除",
                    "触发事件：封存档案在众目睽睽下自行改写",
                    "第一卷变化：测试角色甲从调查员变成制度通缉对象",
                    "故事时间：景历十二年三月初七",
                    "地点：测试城与测试枢纽站",
                    "连载：章级线索兑现、卷级身份翻转",
                    "当前压力：列车署正在清洗所有相关见证者",
                    "权力分配：列车署控制跨区交通与身份登记",
                    "核心能力：读取物品残留的短时记忆",
                    "差异化：制度悬疑与城市生存并行",
                    "终局问题：谁有权决定一段记忆是否真实",
                    "补充：" + "世界会在主角不行动时继续运转。" * 30,
                ]
            )
            result = PlotInitService(workspace).dry_run(
                project_root=workspace / "novel",
                mode="new",
                target_profile="plot_ready",
                seed=text,
            )
            self.assertEqual("READY_TO_PROPOSE", result["status"])
            states = result["bundle"]["field_states"]
            self.assertEqual(
                ("user_confirmed", "user_input"),
                (
                    states["/genre_contract/primary_engine"]["field_status"],
                    states["/genre_contract/primary_engine"]["origin"],
                ),
            )
            self.assertEqual(
                ("model_proposed", "model_suggestion"),
                (
                    states["/genre_contract/target_readers"]["field_status"],
                    states["/genre_contract/target_readers"]["origin"],
                ),
            )
            bundle = result["bundle"]
            frozen = {
                "schema_version": "plot-rag-init/v1",
                "proposal_id": "proposal-rich-seed",
                "package_hash": bundle["bundle_hash"],
                "status": "PROPOSAL_FROZEN",
                "target_project_real_path": str(workspace / "novel"),
                "source_manifest_hash": canonical_hash(bundle["source_manifest"]),
                "bundle": bundle,
                "apply_plan": {
                    "requires_approval_grant": True,
                    "authorized_operations_required": [
                        "accept_initialization",
                        "materialize",
                    ],
                    "artifacts": bundle["artifact_manifest"],
                    "executed": False,
                },
            }
            package = proposal_to_lifecycle_package(frozen)
            state_fields = {
                event.get("field")
                for event in package["events"]
                if event["event_type"] == "state"
            }
            self.assertIn("social_position", state_fields)

    def test_invalid_existing_config_fails_before_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            config_path = project / ".plot-rag" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(PlotInitError) as caught:
                PlotInitService(workspace).dry_run(
                    project_root=project,
                    mode="new",
                    seed=complete_seed(),
                )
            self.assertEqual(
                "INVALID_EXISTING_PROJECT_CONFIG",
                caught.exception.code,
            )

    def test_generic_hashes_bind_nested_business_fields(self) -> None:
        self.assertNotEqual(
            canonical_hash({"payload": {"session_id": "session-a"}}),
            canonical_hash({"payload": {"session_id": "session-b"}}),
        )
        self.assertNotEqual(
            canonical_hash({"source": {"active_revision": 1}}),
            canonical_hash({"source": {"active_revision": 2}}),
        )
        self.assertNotEqual(
            canonical_hash({"record": {"created_at": "story-day-1"}}),
            canonical_hash({"record": {"created_at": "story-day-2"}}),
        )
        first = {
            "schema_version": "plot-rag-init/v1",
            "meta": {
                "session_id": "session-a",
                "created_at": "2026-01-01T00:00:00Z",
            },
            "bundle_hash": "old",
        }
        second = {
            "schema_version": "plot-rag-init/v1",
            "meta": {
                "session_id": "session-b",
                "created_at": "2026-01-02T00:00:00Z",
            },
            "bundle_hash": "different-old-value",
        }
        self.assertEqual(
            recompute_bundle_hash(first),
            recompute_bundle_hash(second),
        )

    def test_remote_cache_identity_and_string_scrubber_bind_real_payload(self) -> None:
        first = RemoteCacheIdentity.build(
            model="fixture-model",
            prompt={"session_id": "a", "created_at": "story-day-1"},
            schema={"type": "object"},
            source_hash="source",
        )
        second = RemoteCacheIdentity.build(
            model="fixture-model",
            prompt={"session_id": "b", "created_at": "story-day-2"},
            schema={"type": "object"},
            source_hash="source",
        )
        self.assertNotEqual(first.cache_key, second.cache_key)
        leaked = (
            'model echoed {"token": "fixture-secret-value-12345", '
            '"note": "keep"}'
        )
        sanitized = sanitize_remote_cache_value({"text": leaked})
        encoded = json.dumps(sanitized, ensure_ascii=False)
        self.assertNotIn("fixture-secret-value-12345", encoded)
        self.assertIn("[REDACTED]", encoded)


if __name__ == "__main__":
    unittest.main()
