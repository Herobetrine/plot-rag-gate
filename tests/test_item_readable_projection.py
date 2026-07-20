from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from pathlib import PurePosixPath
from unittest import mock

from scripts.continuity import ContinuityService, HostApprovalAuthority
from scripts.continuity import item_readable


class ItemReadableProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        state_dir = self.root / ".plot-rag"
        state_dir.mkdir(parents=True)
        (state_dir / "config.json").write_text(
            json.dumps(
                {
                    "items": {
                        "strict_runtime_validation": True,
                        "power_binding_bridge": True,
                        "readable_projection": True,
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.service = ContinuityService(self.root)
        self.host = HostApprovalAuthority(
            self.service,
            issuer="item-readable-unittest-host",
            channel="interactive_test",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def coordinate(ordinal: int) -> dict[str, object]:
        return {"calendar_id": "readable-test-calendar", "ordinal": ordinal}

    def event(
        self,
        event_type: str,
        *,
        ordinal: int,
        quote: str,
        **fields: object,
    ) -> dict[str, object]:
        return {
            "schema_version": "plot-rag-delta/v4",
            "event_type": event_type,
            "story_coordinate": self.coordinate(ordinal),
            "knowledge_plane": "objective",
            "evidence": {"quote": quote},
            **fields,
        }

    def accept(
        self,
        events: list[dict[str, object]],
        *,
        artifact_id: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        proposal = self.service.save_proposal(
            events=events,
            artifact_id=artifact_id,
            artifact_stage="final",
            proposal_kind="story_delta",
            branch_id="main",
            chapter_no=1,
            scene_index=0,
        )
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            str(proposal["proposal_id"]),
            expected_canon_revision=revision,
        )
        commit = self.service.accept_proposal(
            str(proposal["proposal_id"]),
            approval_id=str(grant["approval_id"]),
            expected_canon_revision=revision,
        )
        return proposal, commit

    def full_item_events(
        self,
        *,
        owner: str,
        definition_id: str = "definition_blade",
        instance_id: str = "instance_blade",
    ) -> list[dict[str, object]]:
        function_id = "function_cut"
        return [
            self.event(
                "item_spec",
                ordinal=1,
                quote="黑刃被明确记录为武器。",
                action="define",
                spec_type="item_definition",
                spec_id=definition_id,
                definition={
                    "name": "黑刃",
                    "item_kind": "weapon",
                    "stack_policy": "non_stackable",
                    "uniqueness_policy": "unique_definition",
                    "max_durability": 10,
                },
            ),
            self.event(
                "item_spec",
                ordinal=1,
                quote="黑刃具有一次明确记载的切割功能。",
                action="define",
                spec_type="function_definition",
                spec_id=function_id,
                definition={
                    "item_definition_id": definition_id,
                    "effect_owner": "inline",
                    "inline_effects": [{"kind": "cut"}],
                    "charges": 2,
                },
            ),
            self.event(
                "item_spec",
                ordinal=1,
                quote="切割功能绑定到黑刃定义。",
                action="define",
                spec_type="function_binding",
                spec_id="binding_cut",
                definition={
                    "item_definition_id": definition_id,
                    "function_id": function_id,
                },
            ),
            self.event(
                "item_instance",
                ordinal=2,
                quote="测试角色甲取得编号为一的黑刃。",
                action="instantiate",
                subject_type="item_instance",
                subject_id=instance_id,
                item_instance_id=instance_id,
                item_definition_id=definition_id,
                attributes={"finish": "matte"},
            ),
            self.event(
                "item_custody",
                ordinal=2,
                quote="黑刃归测试角色甲所有并由他携带。",
                action="acquire",
                subject_type="item_instance",
                subject_id=instance_id,
                item_instance_id=instance_id,
                to_legal_owner_entity_id=owner,
                to_custodian_entity_id=owner,
                to_carrier_entity_id=owner,
            ),
            self.event(
                "item_runtime",
                ordinal=3,
                quote="测试角色甲把黑刃装备在右手。",
                action="equip",
                subject_type="item_instance",
                subject_id=instance_id,
                item_instance_id=instance_id,
                actor_entity_id=owner,
                slot_key="right_hand",
                delta={},
            ),
            self.event(
                "item_use",
                ordinal=4,
                quote="测试角色甲发动黑刃的切割功能。",
                action="use",
                subject_type="item_instance",
                subject_id=instance_id,
                item_instance_id=instance_id,
                actor_entity_id=owner,
                function_id=function_id,
                delta={},
            ),
            self.event(
                "item_observation",
                ordinal=4,
                quote="测试角色甲看到刃口出现一道浅痕。",
                action="observe",
                subject_type="item_instance",
                subject_id=instance_id,
                item_instance_id=instance_id,
                observer_entity_id=owner,
                function_id=function_id,
                knowledge_plane="actor_belief",
                observation={"edge": "shallow_mark"},
            ),
        ]

    @staticmethod
    def tree_bytes(path: Path) -> dict[str, bytes]:
        return {
            child.relative_to(path).as_posix(): child.read_bytes()
            for child in sorted(path.rglob("*"))
            if child.is_file()
        }

    def test_accept_replay_and_retract_refresh_complete_readable_tree(
        self,
    ) -> None:
        owner = self.service.register_entity("character", "测试角色甲")["entity_id"]
        continuity_hash = self.service.projection_hash()
        proposal, commit = self.accept(
            self.full_item_events(owner=str(owner)),
            artifact_id="readable-full-item",
        )

        receipt = commit["readable_item_projection"]
        self.assertEqual("completed", receipt["status"])
        self.assertEqual(commit["item_projection_hash"], receipt["item_projection_hash"])
        self.assertEqual(continuity_hash, commit["projection_hash"])

        item_dir = self.root / ".plot-rag" / "物品"
        index_path = item_dir / "物品索引.md"
        definition_path = item_dir / "definition_blade.md"
        instance_path = item_dir / "实例" / "instance_blade.md"
        self.assertTrue(index_path.is_file())
        self.assertTrue(definition_path.is_file())
        self.assertTrue(instance_path.is_file())
        self.assertFalse(any(item_dir.parent.glob(".item-readable-stage-*")))
        self.assertFalse((item_dir.parent / ".item-readable-backup").exists())

        index = index_path.read_text(encoding="utf-8")
        definition = definition_path.read_text(encoding="utf-8")
        instance = instance_path.read_text(encoding="utf-8")
        self.assertIn("definition_blade", index)
        self.assertIn("instance_blade", index)
        self.assertIn("function_cut", definition)
        self.assertIn("黑刃具有一次明确记载的切割功能。", definition)
        self.assertIn("## 保管与位置", instance)
        self.assertIn("测试角色甲", instance)
        self.assertIn("## 运行态", instance)
        self.assertIn("right_hand", instance)
        self.assertIn("## 使用历史", instance)
        self.assertIn("function_cut", instance)
        self.assertIn("## 观察记录", instance)
        self.assertIn("shallow_mark", instance)

        before_replay = self.tree_bytes(item_dir)
        replayed = self.service.replay()
        self.assertEqual("completed", replayed["readable_item_projection"]["status"])
        self.assertEqual(before_replay, self.tree_bytes(item_dir))
        self.assertEqual(continuity_hash, replayed["projection_hash"])
        self.assertEqual(
            commit["item_projection_hash"],
            replayed["item_projection_hash"],
        )

        revision = self.service.get_canon_revisions()["active"]
        retract_grant = self.host.issue(
            str(proposal["proposal_id"]),
            expected_canon_revision=revision,
            operations=("retract",),
        )
        retracted = self.service.retract_proposal(
            str(proposal["proposal_id"]),
            approval_id=str(retract_grant["approval_id"]),
            expected_canon_revision=revision,
            reason="readable projection retraction test",
        )
        self.assertEqual(
            "completed",
            retracted["readable_item_projection"]["status"],
        )
        self.assertFalse(definition_path.exists())
        self.assertFalse(instance_path.exists())
        self.assertIn(
            "当前 accepted item projection 没有物品定义",
            index_path.read_text(encoding="utf-8"),
        )
        self.assertEqual(continuity_hash, retracted["projection_hash"])

    def test_explicit_false_performs_no_readable_write_or_cleanup(self) -> None:
        self.temp_dir.cleanup()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        state_dir = self.root / ".plot-rag"
        item_dir = state_dir / "物品"
        item_dir.mkdir(parents=True)
        sentinel = item_dir / "人工保留.md"
        sentinel.write_text("keep", encoding="utf-8")
        (state_dir / "config.json").write_text(
            json.dumps(
                {
                    "items": {
                        "strict_runtime_validation": True,
                        "power_binding_bridge": True,
                        "readable_projection": False,
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.service = ContinuityService(self.root)
        self.host = HostApprovalAuthority(
            self.service,
            issuer="item-readable-disabled-host",
            channel="interactive_test",
        )
        owner = self.service.register_entity("character", "持有人")["entity_id"]
        _, commit = self.accept(
            self.full_item_events(owner=str(owner)),
            artifact_id="readable-disabled",
        )
        self.assertEqual(
            "disabled",
            commit["readable_item_projection"]["status"],
        )
        self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))
        self.assertFalse((item_dir / "物品索引.md").exists())
        replayed = self.service.replay()
        self.assertEqual(
            "disabled",
            replayed["readable_item_projection"]["status"],
        )
        self.assertEqual(["人工保留.md"], [path.name for path in item_dir.iterdir()])

    def test_missing_function_definition_is_not_inferred_from_name_or_attributes(
        self,
    ) -> None:
        events = [
            self.event(
                "item_spec",
                ordinal=1,
                quote="这件物品只被称为万能钥匙。",
                action="define",
                spec_type="item_definition",
                spec_id="definition_master_key",
                definition={
                    "name": "万能钥匙",
                    "item_kind": "tool",
                    "stack_policy": "non_stackable",
                    "uniqueness_policy": "ordinary",
                },
            )
        ]
        _, commit = self.accept(events, artifact_id="readable-no-inference")
        self.assertEqual(0, commit["readable_item_projection"]["function_count"])
        profile = (
            self.root / ".plot-rag" / "物品" / "definition_master_key.md"
        ).read_text(encoding="utf-8")
        self.assertIn("没有 accepted `ItemFunctionDefinition` 记录", profile)
        self.assertIn("不从物品名称、类型、legacy attributes 或空字段推断功能", profile)
        self.assertNotIn("开锁功能", profile)

    def test_unsafe_ids_use_safe_stable_filenames(self) -> None:
        owner = self.service.register_entity("character", "持有人")["entity_id"]
        unsafe_definition = "../definition/危险:CON"
        unsafe_instance = "..\\实例\\NUL"
        events = self.full_item_events(
            owner=str(owner),
            definition_id=unsafe_definition,
            instance_id=unsafe_instance,
        )
        _, commit = self.accept(events, artifact_id="readable-safe-path")
        self.assertEqual("completed", commit["readable_item_projection"]["status"])

        item_dir = self.root / ".plot-rag" / "物品"
        definition_profiles = [
            path
            for path in item_dir.glob("*.md")
            if path.name != "物品索引.md"
        ]
        instance_profiles = list((item_dir / "实例").glob("*.md"))
        self.assertEqual(1, len(definition_profiles))
        self.assertEqual(1, len(instance_profiles))
        self.assertRegex(
            definition_profiles[0].name,
            r"^definition-[0-9a-f]{64}\.md$",
        )
        self.assertRegex(
            instance_profiles[0].name,
            r"^instance-[0-9a-f]{64}\.md$",
        )
        self.assertIn(
            unsafe_definition,
            definition_profiles[0].read_text(encoding="utf-8"),
        )
        self.assertIn(
            unsafe_instance.replace("\\", "\\\\"),
            instance_profiles[0].read_text(encoding="utf-8"),
        )
        self.assertFalse((self.root / "definition").exists())
        self.assertFalse((self.root / "实例").exists())

    def test_publication_failure_degrades_after_commit_without_hash_mutation(
        self,
    ) -> None:
        owner = self.service.register_entity("character", "持有人")["entity_id"]
        continuity_hash = self.service.projection_hash()
        with mock.patch(
            "scripts.continuity.item_readable._publish_tree",
            side_effect=OSError("injected readable publication failure"),
        ):
            _, commit = self.accept(
                self.full_item_events(owner=str(owner)),
                artifact_id="readable-degraded",
            )

        receipt = commit["readable_item_projection"]
        self.assertEqual("degraded", receipt["status"])
        self.assertEqual("OSError", receipt["error_type"])
        self.assertEqual(1, commit["active_canon_revision"])
        self.assertEqual(continuity_hash, commit["projection_hash"])
        self.assertEqual(
            "definition_blade",
            self.service.query_item_instance("instance_blade")["instance"][
                "item_definition_id"
            ],
        )
        self.assertTrue(str(commit["item_projection_hash"]).startswith("item_projection_"))

    def test_directory_swap_restores_previous_complete_tree_on_publish_error(
        self,
    ) -> None:
        owner = self.service.register_entity("character", "持有人")["entity_id"]
        self.accept(
            self.full_item_events(owner=str(owner)),
            artifact_id="readable-atomic-seed",
        )
        item_dir = self.root / ".plot-rag" / "物品"
        previous = self.tree_bytes(item_dir)
        replacement = {
            PurePosixPath("物品索引.md"): "# replacement\n".encode("utf-8"),
            PurePosixPath("definition_replacement.md"): (
                "# replacement definition\n".encode("utf-8")
            ),
        }
        real_replace = item_readable.os.replace
        calls = 0

        def fail_second_replace(source: object, target: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected second rename failure")
            real_replace(source, target)

        with mock.patch.object(
            item_readable.os,
            "replace",
            side_effect=fail_second_replace,
        ):
            with self.assertRaises(OSError):
                item_readable._publish_tree(self.root, replacement)

        self.assertEqual(previous, self.tree_bytes(item_dir))
        self.assertFalse(any(item_dir.parent.glob(".item-readable-stage-*")))
        self.assertFalse((item_dir.parent / ".item-readable-backup").exists())

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 alias regression")
    def test_path_boundary_normalizes_windows_short_root_alias(self) -> None:
        import ctypes

        long_root = self.root.resolve(strict=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_short_path = kernel32.GetShortPathNameW
        get_short_path.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
        )
        get_short_path.restype = ctypes.c_uint
        required = int(get_short_path(str(long_root), None, 0))
        if required <= 0:
            self.skipTest("8.3 short paths are unavailable")
        buffer = ctypes.create_unicode_buffer(required + 1)
        written = int(get_short_path(str(long_root), buffer, len(buffer)))
        if written <= 0 or written >= len(buffer):
            self.skipTest("8.3 short path lookup failed")
        short_root = Path(buffer.value)
        if os.path.normcase(str(short_root)) == os.path.normcase(
            str(long_root)
        ):
            self.skipTest("the temporary directory has no distinct 8.3 alias")

        state_dir = (long_root / ".plot-rag").resolve(strict=True)
        item_readable._assert_safe_directory(state_dir, root=short_root)
        item_readable._assert_inside(
            short_root,
            long_root / "not-created" / "child",
        )

        outside = (
            long_root.parent
            / f"{long_root.name}-outside-not-created"
            / "child"
        )
        self.assertFalse(outside.exists())
        with self.assertRaises(item_readable.ItemReadableProjectionError):
            item_readable._assert_inside(short_root, outside)


if __name__ == "__main__":
    unittest.main()
