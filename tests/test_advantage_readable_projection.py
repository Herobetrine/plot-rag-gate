from __future__ import annotations

import contextlib
import copy
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest import mock

from scripts.continuity import advantage_readable


class _Store:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    @contextlib.contextmanager
    def read_connection(self):
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()


class AdvantageReadableProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        state_dir = self.root / ".plot-rag"
        state_dir.mkdir(parents=True)
        (state_dir / "config.json").write_text(
            json.dumps(
                {
                    "advantage": {
                        "enabled": True,
                        "shadow": True,
                        "readable_projection": True,
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.store = _Store(self.root)
        self.payload = self.full_payload()
        self.metadata = {
            "schema_version": 1,
            "projection_hash": "advantage_projection_test_hash",
            "source_head_revision": 12,
            "source_active_revision": 9,
        }
        self.core = SimpleNamespace(
            advantage_projection_payload=lambda _connection: copy.deepcopy(
                self.payload
            ),
            read_advantage_projection_metadata=lambda _connection: copy.deepcopy(
                self.metadata
            ),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def full_payload() -> dict[str, object]:
        return {
            "schema_version": 1,
            "tables": {
                "definitions": [
                    {
                        "advantage_id": "advantage_sample_core",
                        "title": "样例优势核心",
                        "profiles_json": [
                            "inheritance",
                            "resource_transformer",
                            "growth_relic",
                        ],
                        "anchor_type": "item_instance",
                        "acquisition_mode": "directed_inheritance",
                        "uniqueness": "unique",
                        "advantage_status": "canon",
                        "promise_json": {
                            "reading_promise": "执行样本分析并记录限制"
                        },
                        "counterplay_json": [
                            "误差",
                            "有效输入",
                            "主动确认",
                        ],
                        "reveal_stage": "initial",
                        "source_event_id": "event_advantage_spec",
                    }
                ],
                "anchors": [
                    {
                        "advantage_id": "advantage_sample_core",
                        "anchor_id": "anchor_sample_core_item",
                        "anchor_type": "item_instance",
                        "owner_entity_id": "actor_test_actor_a",
                        "binding_state": "bound",
                        "transfer_rule_json": {
                            "kind": "inheritance_contract"
                        },
                        "source_event_id": "event_advantage_anchor",
                    }
                ],
                "module_definitions": [
                    {
                        "advantage_id": "advantage_sample_core",
                        "module_id": "module_inspect_sample",
                        "title": "状态解析",
                        "module_kind": "appraisal",
                        "module_status": "canon",
                        "stage": "initial",
                        "trigger_json": {"action": "observe"},
                        "preconditions_json": ["激活完成"],
                        "targets_json": ["能力核心"],
                        "costs_json": [],
                        "effects_json": ["识别异常样本与误差"],
                        "side_effects_json": [],
                        "failure_modes_json": ["证据不足时保持未知"],
                        "source_event_id": "event_module_appraise",
                    },
                    {
                        "advantage_id": "advantage_sample_core",
                        "module_id": "module_transform_sample",
                        "title": "样本转换",
                        "module_kind": "resource_transform",
                        "module_status": "canon",
                        "stage": "initial",
                        "trigger_json": {"action": "extract"},
                        "preconditions_json": [
                            "输入可用",
                            "校验通过",
                            "主动确认",
                        ],
                        "targets_json": ["标准样本"],
                        "costs_json": ["处理误差"],
                        "effects_json": ["生成测试资源"],
                        "side_effects_json": ["日志残留"],
                        "failure_modes_json": ["输入过期"],
                        "source_event_id": "event_module_extract",
                    },
                ],
                "runtime_slots": [
                    {
                        "advantage_id": "advantage_sample_core",
                        "slot_id": "slot_sample_resource",
                        "stage": "initial",
                        "capacity": 1,
                        "unlock_graph_json": ["initial", "expanded"],
                    }
                ],
                "runtime_state": [
                    {
                        "advantage_id": "advantage_sample_core",
                        "branch_id": "main",
                        "stage": "initial",
                        "enabled": True,
                        "runtime_status": "active",
                        "charges": 1,
                        "max_charges": 1,
                        "cooldown_json": None,
                        "resources_json": {"sample_resource": 1},
                        "pollution_json": {"level": "trace"},
                        "exposure_json": {"public": 0},
                        "debt_json": {"inheritance": 1},
                        "unlocked_modules_json": [
                            "module_inspect_sample",
                            "module_transform_sample",
                        ],
                        "source_event_id": "event_runtime",
                    },
                    {
                        "advantage_id": "advantage_sample_core",
                        "module_id": "module_transform_sample",
                        "branch_id": "main",
                        "enabled": True,
                        "charges": 1,
                        "source_event_id": "event_module_runtime",
                    },
                ],
                "ledger": [
                    {
                        "advantage_id": "advantage_sample_core",
                        "entry_id": "ledger_first_sample",
                        "branch_id": "main",
                        "entry_kind": "resource_gain",
                        "input_json": {"sample_input": 1},
                        "output_json": {"sample_resource": 1},
                        "loss_json": {"error": "trace"},
                        "provenance_json": {
                            "source_event_id": "event_first_sample"
                        },
                    }
                ],
                "knowledge": [
                    {
                        "advantage_id": "advantage_sample_core",
                        "knowledge_id": "knowledge_current_boundary",
                        "branch_id": "main",
                        "plane": "objective",
                        "status": "canon",
                        "claim_json": "样本转换需要有效输入与主动确认。",
                        "confidence": 1.0,
                        "evidence_json": {"quote": "测试角色甲主动确认样本转换。"},
                        "reveal_stage": "initial",
                    },
                    {
                        "advantage_id": "advantage_sample_core",
                        "knowledge_id": "knowledge_future_stage",
                        "plane": "author_plan",
                        "status": "planned",
                        "claim_json": "未来高阶能力可进入完整模式。",
                        "confidence": 0.6,
                        "evidence_json": {"source": "setting_plan"},
                        "reveal_stage": "unrevealed",
                    },
                ],
                "contracts": [
                    {
                        "advantage_id": "advantage_sample_core",
                        "contract_id": "contract_inheritance",
                        "branch_id": "main",
                        "terms_json": ["显式授权"],
                        "agency": "owner_choice_required",
                        "trust": "unknown",
                        "debt_json": {"obligation": 1},
                        "breach_effect_json": ["binding_loss"],
                    }
                ],
                "narrative_contracts": [
                    {
                        "advantage_id": "advantage_sample_core",
                        "reading_promise": "获得新测试资源时同时展示结果与限制。",
                        "reward_loop_json": ["状态解析", "模块调用", "结果验证"],
                        "risk_loop_json": ["误差", "追踪", "配置偏移"],
                        "reveal_ladder_json": ["激活", "扩展", "完整模式"],
                        "experience_binding_json": {
                            "peak": "转换成功",
                            "aftertaste": "误差疑问",
                        },
                    }
                ],
            },
        }

    @contextlib.contextmanager
    def loaded_core(self):
        with mock.patch.object(
            advantage_readable,
            "_load_advantage_core",
            return_value=self.core,
        ):
            yield

    @staticmethod
    def tree_bytes(path: Path) -> dict[str, bytes]:
        return {
            child.relative_to(path).as_posix(): child.read_bytes()
            for child in sorted(path.rglob("*"))
            if child.is_file()
        }

    @staticmethod
    def tree_mtimes(path: Path) -> dict[str, int]:
        return {
            child.relative_to(path).as_posix() or ".": int(
                child.stat().st_mtime_ns
            )
            for child in [path, *sorted(path.rglob("*"))]
        }

    def test_complete_projection_has_index_definition_module_and_runtime_cards(
        self,
    ) -> None:
        with self.loaded_core():
            receipt = (
                advantage_readable.refresh_advantage_readable_projection_safe(
                    self.store
                )
            )

        self.assertEqual("completed", receipt["status"])
        self.assertTrue(receipt["enabled"])
        self.assertEqual(1, receipt["definition_count"])
        self.assertEqual(2, receipt["module_count"])
        self.assertEqual(2, receipt["runtime_count"])
        self.assertEqual(1, receipt["runtime_card_count"])
        self.assertEqual(2, receipt["knowledge_count"])
        self.assertEqual(
            "advantage_projection_test_hash",
            receipt["advantage_projection_hash"],
        )
        self.assertTrue(
            str(receipt["readable_tree_hash"]).startswith(
                "advantage_readable_"
            )
        )

        readable_dir = self.root / ".plot-rag" / "金手指"
        index_path = readable_dir / "金手指索引.md"
        definition_path = (
            readable_dir / "定义" / "advantage_sample_core.md"
        )
        module_path = (
            readable_dir / "模块" / "module_transform_sample.md"
        )
        runtime_path = (
            readable_dir
            / "运行态"
            / "advantage_sample_core--main.md"
        )
        self.assertTrue(index_path.is_file())
        self.assertTrue(definition_path.is_file())
        self.assertTrue(module_path.is_file())
        self.assertTrue(runtime_path.is_file())
        self.assertFalse(
            any(
                readable_dir.parent.glob(
                    ".advantage-readable-stage-*"
                )
            )
        )
        self.assertFalse(
            (readable_dir.parent / ".advantage-readable-backup").exists()
        )

        index = index_path.read_text(encoding="utf-8")
        definition = definition_path.read_text(encoding="utf-8")
        module = module_path.read_text(encoding="utf-8")
        runtime = runtime_path.read_text(encoding="utf-8")
        self.assertIn("样例优势核心", index)
        self.assertIn("resource_transformer", index)
        self.assertIn("module_transform_sample", index)
        self.assertIn("objective", definition)
        self.assertIn("planned", definition)
        self.assertIn("未来高阶能力可进入完整模式", definition)
        self.assertIn("显式授权", definition)
        self.assertIn("输入可用", module)
        self.assertIn("校验通过", module)
        self.assertIn("处理误差", module)
        self.assertIn("sample_resource", runtime)
        self.assertIn("pollution", runtime)
        self.assertIn("ledger_first_sample", runtime)
        self.assertIn("样本转换需要有效输入与主动确认", runtime)

        before = self.tree_bytes(readable_dir)
        before_mtimes = self.tree_mtimes(readable_dir)
        with self.loaded_core():
            with mock.patch.object(
                advantage_readable.tempfile,
                "mkdtemp",
                side_effect=AssertionError("unchanged tree was restaged"),
            ):
                repeated = (
                    advantage_readable.refresh_advantage_readable_projection_safe(
                        self.store
                    )
                )
        self.assertEqual(receipt["readable_tree_hash"], repeated["readable_tree_hash"])
        self.assertEqual(before, self.tree_bytes(readable_dir))
        self.assertEqual(before_mtimes, self.tree_mtimes(readable_dir))

    def test_refresh_rejects_symlinked_lock_without_touching_target(
        self,
    ) -> None:
        state_dir = self.root / ".plot-rag"
        lock_path = state_dir / ".advantage-readable.lock"
        outside = self.root.parent / f"{self.root.name}-outside.lock"
        outside.write_bytes(b"")
        outside_mtime = outside.stat().st_mtime_ns
        try:
            try:
                lock_path.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            with self.loaded_core():
                receipt = (
                    advantage_readable.refresh_advantage_readable_projection_safe(
                        self.store
                    )
                )
            self.assertEqual("degraded", receipt["status"])
            self.assertEqual(
                "AdvantageReadableProjectionError",
                receipt["error_type"],
            )
            self.assertEqual(b"", outside.read_bytes())
            self.assertEqual(outside_mtime, outside.stat().st_mtime_ns)
            self.assertFalse((state_dir / "金手指").exists())
            self.assertFalse(
                any(state_dir.glob(".advantage-readable-stage-*"))
            )
            self.assertFalse(
                (state_dir / ".advantage-readable-backup").exists()
            )
        finally:
            lock_path.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)

    def test_explicit_false_is_noop_and_preserves_existing_tree(self) -> None:
        state_dir = self.root / ".plot-rag"
        readable_dir = state_dir / "金手指"
        readable_dir.mkdir()
        sentinel = readable_dir / "人工保留.md"
        sentinel.write_text("keep", encoding="utf-8")
        (state_dir / "config.json").write_text(
            json.dumps(
                {"advantage": {"readable_projection": False}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with mock.patch.object(
            advantage_readable,
            "_load_advantage_core",
            side_effect=AssertionError("disabled projection loaded core"),
        ):
            receipt = (
                advantage_readable.refresh_advantage_readable_projection_safe(
                    self.store
                )
            )
        self.assertEqual("disabled", receipt["status"])
        self.assertFalse(receipt["enabled"])
        self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))
        self.assertEqual(["人工保留.md"], [path.name for path in readable_dir.iterdir()])

    def test_publication_failure_degrades_without_partial_tree(self) -> None:
        with self.loaded_core(), mock.patch.object(
            advantage_readable,
            "_publish_tree",
            side_effect=OSError("injected Advantage readable failure"),
        ):
            receipt = (
                advantage_readable.refresh_advantage_readable_projection_safe(
                    self.store
                )
            )
        self.assertEqual("degraded", receipt["status"])
        self.assertEqual("OSError", receipt["error_type"])
        self.assertIn("injected Advantage readable failure", receipt["message"])
        self.assertFalse((self.root / ".plot-rag" / "金手指").exists())

    def test_directory_swap_restores_previous_complete_tree_on_error(
        self,
    ) -> None:
        with self.loaded_core():
            advantage_readable.refresh_advantage_readable_projection(
                self.store
            )
        readable_dir = self.root / ".plot-rag" / "金手指"
        previous = self.tree_bytes(readable_dir)
        replacement = {
            PurePosixPath("金手指索引.md"): b"# replacement\n",
            PurePosixPath("定义", "replacement.md"): b"# replacement\n",
        }
        real_replace = advantage_readable.os.replace
        calls = 0

        def fail_second_replace(source: object, target: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected second rename failure")
            real_replace(source, target)

        with mock.patch.object(
            advantage_readable.os,
            "replace",
            side_effect=fail_second_replace,
        ):
            with self.assertRaises(OSError):
                advantage_readable._publish_tree(self.root, replacement)

        self.assertEqual(previous, self.tree_bytes(readable_dir))
        self.assertFalse(
            any(
                readable_dir.parent.glob(
                    ".advantage-readable-stage-*"
                )
            )
        )
        self.assertFalse(
            (readable_dir.parent / ".advantage-readable-backup").exists()
        )

    def test_unsafe_stable_ids_use_portable_hashed_filenames(self) -> None:
        definition = self.payload["tables"]["definitions"][0]
        definition["advantage_id"] = "../危险:CON"
        module = self.payload["tables"]["module_definitions"][0]
        second_module = self.payload["tables"]["module_definitions"][1]
        module["advantage_id"] = "../危险:CON"
        second_module["advantage_id"] = "../危险:CON"
        module["module_id"] = "..\\模块\\NUL"
        for table_name in (
            "anchors",
            "runtime_slots",
            "runtime_state",
            "ledger",
            "knowledge",
            "contracts",
            "narrative_contracts",
        ):
            for row in self.payload["tables"][table_name]:
                row["advantage_id"] = "../危险:CON"
        self.payload["tables"]["runtime_state"][1][
            "module_id"
        ] = "module_transform_sample"

        with self.loaded_core():
            receipt = (
                advantage_readable.refresh_advantage_readable_projection_safe(
                    self.store
                )
            )
        self.assertEqual("completed", receipt["status"])
        readable_dir = self.root / ".plot-rag" / "金手指"
        definitions = list((readable_dir / "定义").glob("*.md"))
        modules = list((readable_dir / "模块").glob("*.md"))
        runtime = list((readable_dir / "运行态").glob("*.md"))
        self.assertEqual(1, len(definitions))
        self.assertEqual(2, len(modules))
        self.assertEqual(1, len(runtime))
        self.assertRegex(
            definitions[0].name,
            r"^advantage-[0-9a-f]{64}\.md$",
        )
        self.assertTrue(
            any(
                path.name.startswith("module-")
                and len(path.stem.removeprefix("module-")) == 64
                for path in modules
            )
        )
        self.assertRegex(
            runtime[0].name,
            r"^runtime-[0-9a-f]{64}\.md$",
        )
        self.assertFalse((self.root / "危险:CON").exists())
        self.assertFalse((self.root / "模块").exists())

    def test_cleanup_removes_tree_backup_and_staging_debris(self) -> None:
        with self.loaded_core():
            advantage_readable.refresh_advantage_readable_projection(
                self.store
            )
        state_dir = self.root / ".plot-rag"
        (state_dir / ".advantage-readable-backup").mkdir()
        (state_dir / ".advantage-readable-stage-orphan").mkdir()
        receipt = (
            advantage_readable.remove_advantage_readable_projection_safe(
                self.root
            )
        )
        self.assertEqual("completed", receipt["status"])
        self.assertTrue(receipt["removed"])
        self.assertFalse((state_dir / "金手指").exists())
        self.assertFalse(
            (state_dir / ".advantage-readable-backup").exists()
        )
        self.assertFalse(
            (state_dir / ".advantage-readable-stage-orphan").exists()
        )
        self.assertFalse(
            (state_dir / ".advantage-readable.lock").exists()
        )

    def test_dynamic_loader_uses_optional_advantage_module(self) -> None:
        missing = ModuleNotFoundError(
            "missing continuity.advantages",
            name="continuity.advantages",
        )
        sentinel = object()
        with mock.patch.object(
            advantage_readable.importlib,
            "import_module",
            side_effect=[missing, sentinel],
        ) as loader:
            loaded = advantage_readable._load_advantage_core()
        self.assertIs(sentinel, loaded)
        self.assertEqual(
            [
                mock.call("continuity.advantages"),
                mock.call("scripts.continuity.advantages"),
            ],
            loader.call_args_list,
        )

    def test_invalid_readable_switch_is_a_degraded_receipt(self) -> None:
        config_path = self.root / ".plot-rag" / "config.json"
        config_path.write_text(
            json.dumps(
                {"advantage": {"readable_projection": "yes"}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        receipt = (
            advantage_readable.refresh_advantage_readable_projection_safe(
                self.store
            )
        )
        self.assertEqual("degraded", receipt["status"])
        self.assertEqual(
            "AdvantageReadableProjectionError",
            receipt["error_type"],
        )
        self.assertIn("must be a boolean", receipt["message"])


if __name__ == "__main__":
    unittest.main()
