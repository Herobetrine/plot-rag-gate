from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from scripts.continuity.power_spec import (
    POWER_SPEC_LIFECYCLE_SCHEMA,
    PowerSpecImportError,
    build_power_spec_lifecycle_package,
    compile_power_spec_change,
    preview_power_spec_import,
    validate_power_spec_import,
    validate_power_spec_lifecycle_package,
)
from scripts.continuity.validators import stable_hash
from scripts.power_system import normalize_power_package


def power_aggregate() -> dict[str, object]:
    return {
        "schema_version": "plot-rag-power/v1",
        "power_systems": [
            {
                "namespace": "power.cultivation",
                "name": "修行体系",
                "profile": "cultivation",
            },
            {
                "namespace": "power.technology",
                "name": "科技体系",
                "profile": "technology",
            },
        ],
        "progression_tracks": [
            {
                "namespace": "power.cultivation.main",
                "name": "境界",
                "system_namespace": "power.cultivation",
                "track_kind": "ordered_rank",
            }
        ],
        "rank_nodes": [
            {
                "track_namespace": "power.cultivation.main",
                "name": "激活",
            },
            {
                "track_namespace": "power.cultivation.main",
                "name": "第二阶段",
            },
        ],
        "rank_edges": [
            {
                "track_namespace": "power.cultivation.main",
                "from_node_ids": ["激活"],
                "to": "第二阶段",
                "prerequisites": {"all": ["完成基础校准"]},
            }
        ],
        "ability_definitions": [
            {
                "name": "目标标记",
                "system_namespace": "power.cultivation",
                "effects": ["标记无人设备"],
            }
        ],
        "resource_definitions": [
            {
                "name": "样本点",
                "system_namespace": "power.cultivation",
                "resource_kind": "stock",
            },
            {
                "name": "算力",
                "system_namespace": "power.technology",
                "resource_kind": "stock",
            },
        ],
        "status_definitions": [
            {
                "name": "能力源活跃",
                "system_namespace": "power.cultivation",
                "status_kind": "buff",
            }
        ],
        "qualification_definitions": [
            {
                "name": "激活资格",
                "system_namespace": "power.cultivation",
                "qualification_kind": "threshold",
                "max_quantity": 1,
            }
        ],
        "counter_rules": [
            {
                "name": "活性抑制",
                "system_namespace": "power.cultivation",
                "source_tags": ["抑制"],
                "target_tags": ["样本点"],
            }
        ],
        "bridge_rules": [
            {
                "source_namespace": "power.cultivation",
                "target_namespace": "power.technology",
                "direction": "two_way",
            }
        ],
        "conversion_rules": [
            {
                "source_resource": "样本点",
                "target_resource": "算力",
                "source_namespace": "power.cultivation",
                "target_namespace": "power.technology",
                "ratio": 0.5,
                "loss_ratio": 0.1,
            }
        ],
        "actor_power_bootstrap": [],
    }


class PowerSpecImportTests(unittest.TestCase):
    def test_build_is_deterministic_and_uses_stable_normalized_ids(self) -> None:
        raw = power_aggregate()
        first = build_power_spec_lifecycle_package(raw)
        second = compile_power_spec_change(copy.deepcopy(raw))

        self.assertEqual(first, second)
        self.assertEqual(
            POWER_SPEC_LIFECYCLE_SCHEMA,
            first["schema_version"],
        )
        self.assertEqual("power_spec_change", first["proposal_kind"])
        self.assertEqual("accept_power_spec", first["required_operation"])
        self.assertEqual("timeless", first["scope"])
        self.assertTrue(first["package_hash"])
        self.assertTrue(first["power_package_hash"])
        self.assertTrue(
            all(
                str(entity["entity_id"]).startswith("ent-")
                for entity in first["entities"]
            )
        )
        self.assertTrue(
            all(
                event["event_type"] == "power_spec"
                and event["action"] == "define"
                and event["scope"] == "timeless"
                for event in first["events"]
            )
        )
        validate_power_spec_lifecycle_package(first)

    def test_semantic_mapping_matches_power_lifecycle_contract(self) -> None:
        package = build_power_spec_lifecycle_package(power_aggregate())
        entities = {
            entity["entity_id"]: entity
            for entity in package["entities"]
        }
        events = {
            event["spec_type"]: event
            for event in package["events"]
        }

        self.assertEqual(
            "progression_track",
            entities[
                events["progression_track"]["spec_entity_id"]
            ]["entity_type"],
        )
        self.assertEqual(
            "ability",
            entities[
                events["ability_definition"]["spec_entity_id"]
            ]["entity_type"],
        )
        self.assertEqual(
            "resource_pool",
            entities[
                events["resource_definition"]["spec_entity_id"]
            ]["entity_type"],
        )
        edge = events["rank_edge"]["definition"]
        self.assertTrue(edge["track_entity_id"].startswith("ent-"))
        self.assertTrue(edge["from_rank_entity_ids"])
        self.assertTrue(edge["to_rank_entity_id"].startswith("ent-"))
        bridge = events["bridge_rule"]["definition"]
        self.assertTrue(
            bridge["source_system_entity_id"].startswith("ent-")
        )
        self.assertTrue(
            bridge["target_system_entity_id"].startswith("ent-")
        )
        conversion = events["conversion_rule"]["definition"]
        self.assertTrue(
            conversion["source_resource_entity_id"].startswith("ent-")
        )
        self.assertTrue(
            conversion["target_resource_entity_id"].startswith("ent-")
        )
        self.assertEqual(
            "power_spec_import",
            events["power_system"]["evidence"]["kind"],
        )

    def test_preview_is_read_only_and_exposes_normalized_aggregate(self) -> None:
        raw = power_aggregate()
        original = copy.deepcopy(raw)
        preview = preview_power_spec_import(raw)

        self.assertEqual(original, raw)
        self.assertEqual("ready", preview["status"])
        self.assertTrue(preview["read_only"])
        self.assertEqual(
            preview["normalized_power_package"]["power_package_hash"],
            preview["lifecycle_package"]["power_package_hash"],
        )
        self.assertEqual(
            len(preview["lifecycle_package"]["events"]),
            preview["summary"]["event_count"],
        )
        validate_power_spec_import(raw)

    def test_duplicate_ids_are_rejected(self) -> None:
        raw = power_aggregate()
        raw["ability_definitions"] = [
            {
                "ability_id": "ent-duplicate",
                "name": "能力甲",
                "system_namespace": "power.cultivation",
            },
            {
                "ability_id": "ent-duplicate",
                "name": "能力乙",
                "system_namespace": "power.cultivation",
            },
        ]
        with self.assertRaises(PowerSpecImportError) as caught:
            build_power_spec_lifecycle_package(raw)
        self.assertEqual("POWER_SPEC_DUPLICATE_ID", caught.exception.code)
        self.assertEqual(
            "ent-duplicate",
            caught.exception.details["entity_id"],
        )

    def test_empty_definition_set_is_rejected(self) -> None:
        with self.assertRaises(PowerSpecImportError) as caught:
            build_power_spec_lifecycle_package(
                {"schema_version": "plot-rag-power/v1"}
            )
        self.assertEqual("POWER_SPEC_EVENTS_EMPTY", caught.exception.code)

    def test_actor_runtime_is_not_silently_discarded(self) -> None:
        raw = power_aggregate()
        raw["actor_power_bootstrap"] = [{"actor_name": "测试角色甲"}]
        with self.assertRaises(PowerSpecImportError) as caught:
            build_power_spec_lifecycle_package(raw)
        self.assertEqual(
            "POWER_SPEC_RUNTIME_NOT_SUPPORTED",
            caught.exception.code,
        )
        self.assertEqual(
            "actor_power_bootstrap",
            caught.exception.details["collection"],
        )

    def test_stale_declared_power_package_hash_is_rejected(self) -> None:
        normalized = normalize_power_package(power_aggregate())
        normalized["ability_definitions"][0]["name"] = "篡改后的能力"
        with self.assertRaises(PowerSpecImportError) as caught:
            build_power_spec_lifecycle_package(normalized)
        self.assertEqual("POWER_PACKAGE_HASH_MISMATCH", caught.exception.code)

    def test_illegal_reference_preserves_stable_power_error_code(self) -> None:
        raw = power_aggregate()
        raw["progression_tracks"][0][
            "system_namespace"
        ] = "power.missing"
        with self.assertRaises(PowerSpecImportError) as caught:
            build_power_spec_lifecycle_package(raw)
        self.assertEqual("POWER_ENDPOINT_UNRESOLVED", caught.exception.code)

    def test_tampered_lifecycle_package_hash_is_rejected(self) -> None:
        package = build_power_spec_lifecycle_package(power_aggregate())
        package["events"][0]["definition"]["name"] = "被篡改"
        with self.assertRaises(PowerSpecImportError) as caught:
            validate_power_spec_lifecycle_package(package)
        self.assertEqual(
            "POWER_SPEC_PACKAGE_HASH_MISMATCH",
            caught.exception.code,
        )

    def test_hash_valid_package_rejects_unconsumed_fields(self) -> None:
        package = build_power_spec_lifecycle_package(power_aggregate())
        package["ignored_extension"] = {"value": 1}
        package["package_hash"] = stable_hash(
            {
                key: value
                for key, value in package.items()
                if key != "package_hash"
            }
        )
        with self.assertRaises(PowerSpecImportError) as caught:
            validate_power_spec_lifecycle_package(package)
        self.assertEqual(
            "POWER_SPEC_PACKAGE_FIELDS_UNSUPPORTED",
            caught.exception.code,
        )
        self.assertEqual(
            ["ignored_extension"],
            caught.exception.details["unexpected_fields"],
        )

    def test_hash_valid_event_requires_bootstrap_stage(self) -> None:
        package = build_power_spec_lifecycle_package(power_aggregate())
        package["events"][0]["artifact_stage"] = "final"
        package["package_hash"] = stable_hash(
            {
                key: value
                for key, value in package.items()
                if key != "package_hash"
            }
        )
        with self.assertRaises(PowerSpecImportError) as caught:
            validate_power_spec_lifecycle_package(package)
        self.assertEqual("POWER_SPEC_EVENT_INVALID", caught.exception.code)

    def test_validate_and_preview_do_not_touch_project_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sentinel = root / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            validate_power_spec_import(power_aggregate())
            preview_power_spec_import(power_aggregate())
            validate_power_spec_lifecycle_package(
                build_power_spec_lifecycle_package(power_aggregate())
            )

            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
