from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from plot_init import PlotInitService, proposal_to_lifecycle_package  # noqa: E402
from plot_init.canonical import canonical_hash  # noqa: E402
from plot_init.normalized import recompute_bundle_hash  # noqa: E402


def _freeze(result: dict) -> dict:
    bundle = result["bundle"]
    return {
        "schema_version": "plot-rag-init/v1",
        "proposal_id": f"proposal-{bundle['bundle_hash'][:16]}",
        "package_hash": bundle["bundle_hash"],
        "status": "PROPOSAL_FROZEN",
        "target_project_real_path": bundle["meta"].get("target_project_real_path"),
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


class PlotInitAuthorityGateTests(unittest.TestCase):
    def test_draft_never_enters_current_and_outline_is_planned_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            sources = workspace / "sources"
            (sources / "正文").mkdir(parents=True)
            (sources / "草稿").mkdir()
            (sources / "大纲").mkdir()
            (sources / "正文" / "第一章.md").write_text(
                "状态：已发布\n# 测试角色甲\n当前位置：测试城\n测试角色甲持有青铜钥匙。\n",
                encoding="utf-8",
            )
            (sources / "草稿" / "角色草稿.md").write_text(
                "状态：草稿\n# 测试角色甲\n当前位置：禁地\n测试角色甲持有禁物。\n",
                encoding="utf-8",
            )
            (sources / "大纲" / "第二章.md").write_text(
                "状态：已确认\n# 测试角色甲\n当前位置：测试枢纽站\n",
                encoding="utf-8",
            )

            result = PlotInitService(workspace).dry_run(
                project_root=workspace / "novel",
                mode="ingest",
                target_profile="continuity_ready",
                sources=[sources],
            )
            package = proposal_to_lifecycle_package(_freeze(result))
            movements = [
                event for event in package["events"] if event["event_type"] == "movement"
            ]
            inventory = [
                event for event in package["events"] if event["event_type"] == "inventory"
            ]
            entity_names = {
                entity["entity_id"]: entity["canonical_name"]
                for entity in package["entities"]
            }

            movement_targets = [
                (event["scope"], entity_names[event["to_location_entity_id"]])
                for event in movements
            ]
            inventory_items = [
                entity_names[event["item_entity_id"]]
                for event in inventory
            ]
            self.assertIn(("current", "测试城"), movement_targets)
            self.assertIn(("planned", "测试枢纽站"), movement_targets)
            self.assertNotIn(("current", "测试枢纽站"), movement_targets)
            self.assertFalse(any(name == "禁地" for _, name in movement_targets))
            self.assertIn("青铜钥匙", inventory_items)
            self.assertNotIn("禁物", inventory_items)

    def test_open_conflict_blocks_all_candidate_canon_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            sources = workspace / "sources"
            (sources / "正文").mkdir(parents=True)
            (sources / "正文" / "第一章.md").write_text(
                "状态：已发布\n# 测试角色甲\n当前位置：测试城\n",
                encoding="utf-8",
            )
            (sources / "正文" / "第二章.md").write_text(
                "状态：已发布\n# 测试角色甲\n当前位置：旧港\n",
                encoding="utf-8",
            )

            result = PlotInitService(workspace).dry_run(
                project_root=workspace / "novel",
                mode="ingest",
                target_profile="continuity_ready",
                sources=[sources],
            )
            self.assertTrue(
                any(
                    conflict["resolution_status"] == "open"
                    and conflict["predicate"] == "actor.location"
                    for conflict in result["bundle"]["conflicts"]
                )
            )
            package = proposal_to_lifecycle_package(_freeze(result))
            movements = [
                event for event in package["events"] if event["event_type"] == "movement"
            ]
            self.assertEqual([], movements)

    def test_review_only_claim_cannot_leak_an_entity_into_accepted_events(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "ambiguous.txt"
            source.write_text(
                "叶舟把霜河城视为最后退路。",
                encoding="utf-8",
            )

            result = PlotInitService(workspace).dry_run(
                project_root=workspace / "novel",
                mode="ingest",
                target_profile="continuity_ready",
                sources=[source],
            )
            bundle = result["bundle"]
            descriptor = bundle["source_manifest"][0]
            claim_id = "claim-review-only-entity-leak"
            bundle["provenance"]["claims"].append(
                {
                    "claim_id": claim_id,
                    "source_id": descriptor["source_id"],
                    "source_version_id": descriptor["source_version_id"],
                    "subject": "叶舟",
                    "predicate": "actor.goal",
                    "object_or_value": "霜河城",
                    "exact_evidence": "叶舟把霜河城视为最后退路。",
                    "path": descriptor["path"],
                    "line_start": 1,
                    "line_end": 1,
                    "source_hash": descriptor["content_hash"],
                    "support_type": "exact",
                    "source_role": "note",
                    "authority_tier": "T4",
                    "field_status": "model_proposed",
                    "canon_status": "proposed",
                    "origin": "remote_ambiguity_proposal",
                    "scope": None,
                    "knowledge_plane": "objective",
                    "modality": "asserted",
                    "branch_id": "main",
                    "story_time": None,
                    "confidence": 0.98,
                }
            )
            bundle["entities"].extend(
                [
                    {
                        "entity_id": "entity-review-only-actor",
                        "entity_type": "character",
                        "canonical_name": "叶舟",
                        "aliases": [],
                        "source_refs": [claim_id],
                    },
                    {
                        "entity_id": "entity-review-only-location",
                        "entity_type": "location",
                        "canonical_name": "霜河城",
                        "aliases": [],
                        "source_refs": [claim_id],
                    },
                ]
            )
            bundle["bundle_hash"] = recompute_bundle_hash(bundle)

            package = proposal_to_lifecycle_package(_freeze(result))
            entity_names = {
                entity["canonical_name"] for entity in package["entities"]
            }
            entity_event_names = {
                event["canonical_name"]
                for event in package["events"]
                if event["event_type"] == "entity"
            }

            self.assertNotIn("叶舟", entity_names)
            self.assertNotIn("霜河城", entity_names)
            self.assertNotIn("叶舟", entity_event_names)
            self.assertNotIn("霜河城", entity_event_names)


if __name__ == "__main__":
    unittest.main()
