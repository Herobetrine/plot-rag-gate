from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from continuity.validators import normalize_event  # noqa: E402
from plot_init import (  # noqa: E402
    ITEM_SIDECAR_PATH,
    PlotInitError,
    PlotInitService,
    assert_item_sidecar_target_baseline,
    build_item_package,
    build_item_sidecar_artifact,
    export_normalized_bundle,
    item_package_from_artifact_manifest,
    item_package_from_frozen_proposal,
    item_package_has_typed_content,
    normalization_diff,
    proposal_to_lifecycle_package,
    render_item_package,
    verify_materialized_item_sidecar,
)
from tests.test_plot_init import complete_seed  # noqa: E402
from tests.test_power_initialization import cultivation_seed  # noqa: E402


V1_BUNDLE_KEYS = {
    "schema_version",
    "meta",
    "genre_contract",
    "world_model",
    "actor_system",
    "story_engine",
    "serialization_contract",
    "entities",
    "relations",
    "timeline",
    "open_loops",
    "field_states",
    "source_manifest",
    "source_ownership",
    "conflicts",
    "gaps",
    "decisions",
    "provenance",
    "artifact_manifest",
    "validation",
    "bundle_hash",
}

V2_ONLY_BUNDLE_KEYS = {
    "power_model",
    "power_systems",
    "progression_tracks",
    "rank_nodes",
    "rank_edges",
    "ability_definitions",
    "resource_definitions",
    "status_definitions",
    "counter_rules",
    "bridge_rules",
    "conversion_rules",
    "qualification_definitions",
    "actor_power_bootstrap",
}

ITEM_EVENT_TYPES = {
    "item_spec",
    "item_instance",
    "item_custody",
    "item_runtime",
    "item_observation",
}


def typed_item_fields() -> dict[str, Any]:
    return {
        "item_definitions": [
            {
                "item_definition_id": "itemdef-key",
                "name": "青铜钥匙",
                "item_kind": "magic_artifact",
                "stack_policy": "non_stackable",
                "uniqueness_policy": "unique_definition",
                "max_durability": 10,
                "max_energy": 5,
                "attributes": {
                    "材质": "青铜",
                    "铭文": {"层数": 2, "状态": "残缺"},
                    "可交易": False,
                    "旧字段空值": None,
                },
            }
        ],
        "item_instances": [
            {
                "item_instance_id": "iteminst-key",
                "item_definition_id": "itemdef-key",
                "instance_name": "测试角色甲的青铜钥匙",
                "serial_or_mark": "KEY-001",
                "unique": True,
                "attributes": {"锈蚀": False, "旧编号": "A-17"},
            }
        ],
        "item_functions": [
            {
                "function_id": "itemfn-unlock",
                "item_definition_id": "itemdef-key",
                "name": "开启封锁门",
                "function_kind": "utility",
                "activation_kind": "active",
                "effect_owner": "inline",
                "inline_effects": [{"effect": "unlock_legacy_gate"}],
                "charges": 3,
            }
        ],
        "item_function_bindings": [
            {
                "binding_id": "itembind-key-unlock",
                "item_instance_id": "iteminst-key",
                "function_id": "itemfn-unlock",
            }
        ],
        "item_custody_bootstrap": [
            {
                "item_instance_id": "iteminst-key",
                "legal_owner": "测试角色甲",
                "carrier": "测试角色甲",
                "location": "测试城南站",
                "custody_status": "possessed",
            }
        ],
        "item_runtime_bootstrap": [
            {
                "item_instance_id": "iteminst-key",
                "durability": 7,
                "energy": 2,
                "equipped_by": "测试角色甲",
                "slot_key": "腰间",
            }
        ],
        "item_function_runtime_bootstrap": [
            {
                "item_instance_id": "iteminst-key",
                "function_id": "itemfn-unlock",
                "remaining_charges": 1,
            }
        ],
        "item_observations": [
            {
                "item_instance_id": "iteminst-key",
                "observer": "测试角色甲",
                "observation": {
                    "thermal": "approaching_old_station",
                    "visible_glow": False,
                },
                "knowledge_plane": "actor_belief",
                "confidence": 0.8,
            }
        ],
    }


def typed_seed(
    base_factory: Callable[[], dict[str, Any]] = complete_seed,
) -> dict[str, Any]:
    seed = copy.deepcopy(base_factory())
    seed.update(typed_item_fields())
    return seed


def package_fixture(
    dossier: dict[str, Any],
    *,
    claims: list[dict[str, Any]] | None = None,
    schema_version: str = "plot-rag-init/v1",
) -> dict[str, Any]:
    return build_item_package(
        dossier,
        claims or [],
        work_id="work-item-initialization-test",
        source_initialization_schema_version=schema_version,
        source_snapshot_hash="a" * 64,
    )


def item_artifact(bundle: dict[str, Any]) -> dict[str, Any]:
    return next(
        item
        for item in bundle["artifact_manifest"]
        if item.get("logical_owner") == "item_sidecar"
    )


class ItemInitializationTests(unittest.TestCase):
    def test_v1_v2_roots_remain_frozen_and_sidecar_is_external(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            service = PlotInitService(workspace)
            v1 = service.dry_run(
                project_root=workspace / "v1-novel",
                mode="new",
                interaction_profile="deep",
                seed=typed_seed(),
                bundle_schema_version="plot-rag-init/v1",
            )["bundle"]
            v2 = service.dry_run(
                project_root=workspace / "v2-novel",
                mode="new",
                interaction_profile="deep",
                seed=typed_seed(cultivation_seed),
                bundle_schema_version="plot-rag-init/v2",
            )["bundle"]

            self.assertEqual(V1_BUNDLE_KEYS, set(v1))
            self.assertEqual(V1_BUNDLE_KEYS | V2_ONLY_BUNDLE_KEYS, set(v2))
            for bundle, version in (
                (v1, "plot-rag-init/v1"),
                (v2, "plot-rag-init/v2"),
            ):
                self.assertEqual(version, bundle["schema_version"])
                self.assertNotIn("items", bundle)
                self.assertNotIn("item_definitions", bundle)
                self.assertNotIn("item_sidecar", bundle["meta"])
                reference = bundle["provenance"]["item_sidecars"]
                self.assertEqual(1, len(reference))
                self.assertEqual(ITEM_SIDECAR_PATH, reference[0]["path"])
                self.assertEqual(
                    "plot-rag-item/v1",
                    reference[0]["schema_version"],
                )
                self.assertEqual(
                    ITEM_SIDECAR_PATH,
                    item_artifact(bundle)["path"],
                )

    def test_name_and_holder_only_remain_legacy_without_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            result = PlotInitService(workspace).dry_run(
                project_root=workspace / "novel",
                mode="new",
                interaction_profile="deep",
                seed=complete_seed(),
            )
            bundle = result["bundle"]
            self.assertNotIn("item_sidecars", bundle["provenance"])
            self.assertFalse(
                any(
                    artifact.get("path") == ITEM_SIDECAR_PATH
                    for artifact in bundle["artifact_manifest"]
                )
            )

            package = package_fixture(
                {"actor_system": complete_seed()["actor_system"]}
            )
            self.assertFalse(item_package_has_typed_content(package))
            self.assertEqual(1, len(package["legacy_inventory"]))
            self.assertEqual(
                "legacy_inventory_only",
                package["legacy_inventory"][0]["modeling_status"],
            )

    def test_explicit_function_and_single_observation_do_not_conflate(self) -> None:
        fields = typed_item_fields()
        fields["item_observations"].append(
            {
                "item_instance_id": "iteminst-key",
                "description": "钥匙在旧站门前只发热了一次",
                "knowledge_plane": "reader_disclosed",
            }
        )
        package = package_fixture(fields)

        self.assertEqual(1, len(package["item_functions"]))
        self.assertEqual("开启封锁门", package["item_functions"][0]["name"])
        self.assertEqual(2, len(package["item_observations"]))
        observations = [
            record["observation"] for record in package["item_observations"]
        ]
        self.assertIn(
            {"description": "钥匙在旧站门前只发热了一次"},
            observations,
        )
        self.assertNotIn(
            "钥匙在旧站门前只发热了一次",
            [
                effect
                for function in package["item_functions"]
                for effect in function["inline_effects"]
            ],
        )

    def test_definition_instance_unknown_policies_and_legacy_attributes(self) -> None:
        definition_attributes = {
            "旧材质": "不明合金",
            "嵌套": {"裂纹": [1, 3], "备注": None},
            "已鉴定": False,
        }
        instance_attributes = {
            "持有时长": "未知",
            "损伤记录": [{"位置": "边缘", "程度": 2}],
        }
        package = package_fixture(
            {
                "item_definitions": [
                    {
                        "name": "无名令牌",
                        "attributes": definition_attributes,
                    }
                ],
                "item_instances": [
                    {
                        "definition_name": "无名令牌",
                        "instance_name": "仓库中的无名令牌",
                        "attributes": instance_attributes,
                    }
                ],
            }
        )
        definition = package["item_definitions"][0]
        instance = package["item_instances"][0]

        self.assertTrue(definition["item_definition_id"].startswith("itemdef-"))
        self.assertTrue(instance["item_instance_id"].startswith("iteminst-"))
        self.assertNotEqual(
            definition["item_definition_id"],
            instance["item_instance_id"],
        )
        self.assertEqual("unknown", definition["stack_policy"])
        self.assertEqual("unknown", definition["uniqueness_policy"])
        self.assertEqual("unknown", instance["unique"])
        self.assertEqual(definition_attributes, definition["attributes"])
        self.assertEqual(
            definition_attributes,
            definition["legacy_attributes"],
        )
        self.assertEqual(instance_attributes, instance["legacy_attributes"])

    def test_input_order_does_not_change_package_bytes_or_hash(self) -> None:
        dossier = {
            "item_definitions": [
                {
                    "item_definition_id": "itemdef-a",
                    "name": "甲令",
                    "stack_policy": "non_stackable",
                },
                {
                    "item_definition_id": "itemdef-b",
                    "name": "乙令",
                    "stack_policy": "non_stackable",
                },
            ],
            "item_instances": [
                {
                    "item_instance_id": "iteminst-a",
                    "item_definition_id": "itemdef-a",
                    "instance_name": "甲令一号",
                },
                {
                    "item_instance_id": "iteminst-b",
                    "item_definition_id": "itemdef-b",
                    "instance_name": "乙令一号",
                },
            ],
        }
        reordered = copy.deepcopy(dossier)
        for records in reordered.values():
            records.reverse()

        first = package_fixture(dossier)
        second = package_fixture(reordered)
        self.assertEqual(first["package_hash"], second["package_hash"])
        self.assertEqual(render_item_package(first), render_item_package(second))

    def test_function_custody_and_runtime_changes_rebind_all_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"

            def freeze(seed: dict[str, Any]) -> dict[str, Any]:
                service = PlotInitService(workspace)
                storage_root = service.database_path.parent
                if storage_root.exists():
                    shutil.rmtree(storage_root)
                service = PlotInitService(workspace)
                started = service.start(
                    project_root=project,
                    mode="new",
                    interaction_profile="deep",
                    seed=seed,
                    bundle_schema_version="plot-rag-init/v1",
                    idempotency_key="item-hash-start",
                    session_id="init-item-hash",
                )
                return service.propose(
                    started["session_id"],
                    expected_session_revision=started["session_revision"],
                    idempotency_key="item-hash-propose",
                )["proposal"]

            baseline_seed = typed_seed()
            variants: list[dict[str, Any]] = []
            function_change = copy.deepcopy(baseline_seed)
            function_change["item_functions"][0]["limits"] = [
                "每次只能开启一扇门"
            ]
            variants.append(function_change)
            custody_change = copy.deepcopy(baseline_seed)
            custody_change["item_custody_bootstrap"][0]["location"] = "测试枢纽站"
            variants.append(custody_change)
            runtime_change = copy.deepcopy(baseline_seed)
            runtime_change["item_runtime_bootstrap"][0]["durability"] = 6
            variants.append(runtime_change)

            baseline = freeze(baseline_seed)
            baseline_ref = baseline["apply_plan"]["item_sidecar"]
            for changed_seed in variants:
                changed = freeze(changed_seed)
                changed_ref = changed["apply_plan"]["item_sidecar"]
                with self.subTest(change=changed_seed):
                    self.assertNotEqual(
                        baseline_ref["package_hash"],
                        changed_ref["package_hash"],
                    )
                    self.assertNotEqual(
                        baseline_ref["content_hash"],
                        changed_ref["content_hash"],
                    )
                    self.assertNotEqual(
                        baseline_ref["artifact_id"],
                        changed_ref["artifact_id"],
                    )
                    self.assertNotEqual(
                        baseline["bundle"]["bundle_hash"],
                        changed["bundle"]["bundle_hash"],
                    )
                    self.assertNotEqual(
                        baseline["proposal_id"],
                        changed["proposal_id"],
                    )
                    self.assertEqual(
                        changed["package_hash"],
                        changed["bundle"]["bundle_hash"],
                    )

    def test_artifact_and_reference_tampering_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            service = PlotInitService(workspace)
            started = service.start(
                project_root=workspace / "novel",
                mode="new",
                interaction_profile="deep",
                seed=typed_seed(),
                idempotency_key="item-tamper-start",
            )
            proposal = service.propose(
                started["session_id"],
                expected_session_revision=started["session_revision"],
                idempotency_key="item-tamper-propose",
            )["proposal"]

            content_tampered = copy.deepcopy(proposal)
            artifact = item_artifact(content_tampered["bundle"])
            artifact["proposed_content"] = " " + artifact["proposed_content"]
            with self.assertRaises(PlotInitError) as content_error:
                item_package_from_frozen_proposal(content_tampered)
            self.assertEqual(
                "ITEM_SIDECAR_CONTENT_HASH_MISMATCH",
                content_error.exception.code,
            )

            reference_tampered = copy.deepcopy(proposal)
            reference_tampered["apply_plan"]["item_sidecar"][
                "package_hash"
            ] = "0" * 64
            with self.assertRaises(PlotInitError) as reference_error:
                item_package_from_frozen_proposal(reference_tampered)
            self.assertEqual(
                "ITEM_SIDECAR_REFERENCE_MISMATCH",
                reference_error.exception.code,
            )

    def test_target_baseline_and_materialized_bytes_are_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            service = PlotInitService(workspace)
            started = service.start(
                project_root=project,
                mode="new",
                interaction_profile="deep",
                seed=typed_seed(),
                idempotency_key="item-materialize-start",
            )
            proposal = service.propose(
                started["session_id"],
                expected_session_revision=started["session_revision"],
                idempotency_key="item-materialize-propose",
            )["proposal"]
            artifact = item_artifact(proposal["bundle"])
            target = project / ITEM_SIDECAR_PATH
            target.parent.mkdir(parents=True, exist_ok=True)

            self.assertEqual(
                "current",
                assert_item_sidecar_target_baseline(proposal, project)["status"],
            )
            target.write_text('{"drift":true}\n', encoding="utf-8")
            with self.assertRaises(PlotInitError) as drift:
                assert_item_sidecar_target_baseline(proposal, project)
            self.assertEqual("ITEM_SIDECAR_TARGET_DRIFT", drift.exception.code)

            target.unlink()
            target.write_bytes(artifact["proposed_content"].encode("utf-8"))
            verified = verify_materialized_item_sidecar(proposal, project)
            self.assertEqual("verified", verified["status"])
            self.assertEqual(
                proposal["apply_plan"]["item_sidecar"]["package_hash"],
                verified["package_hash"],
            )

            target.write_bytes(target.read_bytes() + b" ")
            with self.assertRaises(PlotInitError) as materialized:
                verify_materialized_item_sidecar(proposal, project)
            self.assertEqual(
                "ITEM_SIDECAR_MATERIALIZED_HASH_MISMATCH",
                materialized.exception.code,
            )

    def test_artifact_operations_cover_create_noop_and_update(self) -> None:
        package = package_fixture(typed_item_fields())
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "novel"
            project.mkdir()
            created = build_item_sidecar_artifact(package, project)
            self.assertEqual("create", created["operation"])
            target = project / ITEM_SIDECAR_PATH
            target.parent.mkdir(parents=True)
            target.write_bytes(created["proposed_content"].encode("utf-8"))

            noop = build_item_sidecar_artifact(package, project)
            self.assertEqual("noop", noop["operation"])
            self.assertEqual(
                noop["expected_old_hash"],
                noop["proposed_new_hash"],
            )

            target.write_text('{"old":"content"}\n', encoding="utf-8")
            updated = build_item_sidecar_artifact(package, project)
            self.assertEqual("update", updated["operation"])
            self.assertNotEqual(
                updated["expected_old_hash"],
                updated["proposed_new_hash"],
            )
            self.assertTrue(updated["unified_diff"])

    def test_start_and_propose_idempotency_preserve_sidecar_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            service = PlotInitService(workspace)
            kwargs = {
                "project_root": workspace / "novel",
                "mode": "new",
                "interaction_profile": "deep",
                "seed": typed_seed(),
                "idempotency_key": "item-idempotent-start",
                "session_id": "init-item-idempotent",
            }
            started = service.start(**kwargs)
            replayed_start = service.start(**kwargs)
            self.assertTrue(replayed_start["idempotent"])
            self.assertEqual(
                started["session_id"],
                replayed_start["session_id"],
            )

            proposed = service.propose(
                started["session_id"],
                expected_session_revision=started["session_revision"],
                idempotency_key="item-idempotent-propose",
            )
            replayed_proposal = service.propose(
                started["session_id"],
                expected_session_revision=started["session_revision"],
                idempotency_key="item-idempotent-propose",
            )
            self.assertTrue(replayed_proposal["idempotent"])
            self.assertEqual(
                proposed["proposal"]["apply_plan"]["item_sidecar"],
                replayed_proposal["proposal"]["apply_plan"]["item_sidecar"],
            )

    def test_normalized_roundtrip_preserves_sidecar_bytes_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            service = PlotInitService(workspace)
            original = service.dry_run(
                project_root=project,
                mode="new",
                interaction_profile="deep",
                seed=typed_seed(),
            )["bundle"]
            export_path = workspace / "normalized-item-export.json"
            export_path.write_text(
                json.dumps(
                    export_normalized_bundle(original),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            imported_result = service.dry_run(
                project_root=project,
                mode="ingest",
                sources=[export_path],
            )
            imported = imported_result["bundle"]
            original_package, original_ref = item_package_from_artifact_manifest(
                original["artifact_manifest"]
            )
            imported_package, imported_ref = item_package_from_artifact_manifest(
                imported["artifact_manifest"]
            )

            self.assertEqual(
                original_package["package_hash"],
                imported_package["package_hash"],
            )
            self.assertEqual(original_ref, imported_ref)
            self.assertEqual(original["bundle_hash"], imported["bundle_hash"])
            self.assertEqual([], normalization_diff(original, imported))
            self.assertTrue(
                imported_result["normalization_roundtrip"]["zero_diff"]
            )
            self.assertTrue(
                imported_result["normalization_roundtrip"]["stable_hash"]
            )
            self.assertTrue(
                imported_result["normalization_roundtrip"][
                    "bundle_hash_stable"
                ]
            )

    def test_lifecycle_events_are_dependency_ordered_and_normalizable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            service = PlotInitService(workspace)
            started = service.start(
                project_root=workspace / "novel",
                mode="new",
                interaction_profile="deep",
                seed=typed_seed(),
                idempotency_key="item-lifecycle-start",
            )
            proposal = service.propose(
                started["session_id"],
                expected_session_revision=started["session_revision"],
                idempotency_key="item-lifecycle-propose",
            )["proposal"]
            lifecycle = proposal_to_lifecycle_package(proposal)
            events = [
                event
                for event in lifecycle["events"]
                if event["event_type"] in ITEM_EVENT_TYPES
            ]
            ranks = {
                ("item_spec", "item_definition"): 10,
                ("item_spec", "function_definition"): 20,
                ("item_instance", ""): 30,
                ("item_spec", "function_binding"): 40,
                ("item_custody", ""): 50,
                ("item_runtime", ""): 60,
                ("item_observation", ""): 70,
            }
            actual_ranks = [
                ranks[(event["event_type"], event.get("spec_type", ""))]
                for event in events
            ]

            self.assertEqual(sorted(actual_ranks), actual_ranks)
            self.assertEqual(
                {
                    "item_definition",
                    "function_definition",
                    "function_binding",
                },
                {
                    event["spec_type"]
                    for event in events
                    if event["event_type"] == "item_spec"
                },
            )
            self.assertTrue(
                {"item_instance", "item_custody", "item_runtime", "item_observation"}
                .issubset({event["event_type"] for event in events})
            )
            self.assertTrue(all("event_id" not in event for event in events))
            for event in events:
                normalized = normalize_event(
                    event,
                    artifact_stage="bootstrap",
                    branch_id="main",
                    chapter_no=None,
                    scene_index=None,
                )
                self.assertEqual(event["event_type"], normalized["event_type"])


if __name__ == "__main__":
    unittest.main()
