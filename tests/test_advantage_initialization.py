from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from advantage_profiles import (  # noqa: E402
    ADVANTAGE_PROFILES,
    PROFILE_REGISTRY_SCHEMA_VERSION,
    advantage_profile_registry,
    advantage_profile_registry_hash,
    compile_advantage_query_terms,
    detect_advantage_profiles,
)
from plot_init.advantages import (  # noqa: E402
    ADVANTAGE_PACKAGE_ARRAY_FIELDS,
    ADVANTAGE_SCHEMA_VERSION,
    ADVANTAGE_SIDECAR_PATH,
    advantage_package_has_typed_content,
    advantage_sidecar_reference,
    build_advantage_package,
    build_advantage_sidecar_artifact,
    recompute_advantage_package_hash,
    render_advantage_package,
    validate_advantage_package,
    verify_materialized_advantage_sidecar,
)
from plot_init.canonical import stable_id  # noqa: E402
from plot_init.errors import PlotInitError  # noqa: E402
from continuity.advantages import (  # noqa: E402
    bootstrap_advantage_projection,
    query_advantage_context,
)


EXPECTED_PROFILES = {
    "system_panel",
    "task_reward",
    "reward_market",
    "pocket_domain",
    "resource_transformer",
    "companion_mentor",
    "growth_relic",
    "appraisal_copy",
    "simulator_branch",
    "foreknowledge",
    "inheritance",
    "time_causality",
    "contract_summon",
    "social_currency",
    "bloodline_constitution",
    "sign_in_lottery",
}
SYNTHETIC_OWNER_ENTITY_ID = stable_id(
    "ent",
    "character",
    "测试角色甲".casefold(),
)


def minimal_dossier() -> dict[str, Any]:
    return {
        "advantage_definitions": [
            {
                "title": "青铜演算仪",
                "profiles": ["simulator_branch", "foreknowledge"],
                "anchor_type": "item_instance",
                "acquisition_mode": "拾取后认主",
                "uniqueness": "unique_instance",
                "promise": "支付资源后获得有限假设分支信息",
                "counterplay": ["错误假设", "分支泄漏"],
            }
        ],
        "advantage_anchors": [
            {
                "advantage_name": "青铜演算仪",
                "anchor_type": "item_instance",
                "anchor_ref_id": "iteminst-bronze-simulator",
                "anchor_name": "青铜演算仪",
                "owner_entity_id": SYNTHETIC_OWNER_ENTITY_ID,
                "binding_state": "bound",
                "transfer_rule": "解除认主后可转移",
            }
        ],
        "advantage_modules": [
            {
                "advantage_name": "青铜演算仪",
                "profile": "simulator_branch",
                "name": "建立假设分支",
                "kind": "branch_create",
                "trigger": {"action": "simulate"},
                "preconditions": ["存在演算资源"],
                "costs": [{"resource": "演算点", "amount": 1}],
                "effects": ["创建隔离分支"],
                "failure_modes": ["资源不足"],
            }
        ],
        "runtime_slots": [
            {
                "advantage_name": "青铜演算仪",
                "name": "并发分支",
                "slot_kind": "branch_capacity",
                "stage": "initial",
                "capacity": 1,
            }
        ],
        "advantage_runtime": [
            {
                "advantage_name": "青铜演算仪",
                "branch_id": "main",
                "stage": "initial",
                "enabled": True,
                "resources": {"演算点": 2},
                "pollution": 0,
                "exposure": 0,
                "debt": 0,
            }
        ],
        "advantage_ledger": [
            {
                "advantage_name": "青铜演算仪",
                "entry_kind": "simulation_budget",
                "source_event_id": "event-acquire",
                "input": {},
                "output": {"演算点": 2},
                "loss": {},
            }
        ],
        "advantage_knowledge": [
            {
                "advantage_name": "青铜演算仪",
                "knowledge_plane": "actor_belief",
                "observer_entity_id": SYNTHETIC_OWNER_ENTITY_ID,
                "claim": "模拟结果不会直接改变现实。",
                "confidence": 0.9,
                "reveal_stage": "initial",
            }
        ],
        "advantage_contracts": [
            {
                "advantage_name": "青铜演算仪",
                "contract_kind": "branch_isolation",
                "parties": [SYNTHETIC_OWNER_ENTITY_ID],
                "terms": ["模拟状态留在分支"],
                "agency": {"owner": "retained"},
                "trust": {},
                "debt": {},
                "breach_effect": ["结果标记无效"],
            }
        ],
        "advantage_narrative_contracts": [
            {
                "advantage_name": "青铜演算仪",
                "reading_promise": "试错只带回有限信息。",
                "reward_loop": ["提出假设", "演算", "验证"],
                "risk_loop": ["资源损耗", "分支偏差"],
                "reveal_ladder": ["结果", "变量", "来源"],
                "experience_binding": {"primary": "期待与不安"},
            }
        ],
    }


def build_minimal_package(
    dossier: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return build_advantage_package(
        dossier or minimal_dossier(),
        (),
        work_id="work-advantage-test",
        source_initialization_schema_version="plot-rag-init/v2",
        source_snapshot_hash="a" * 64,
    )


class AdvantageProfileRegistryTests(unittest.TestCase):
    def test_advantage_initializer_supports_package_and_direct_imports(
        self,
    ) -> None:
        cases = (
            (
                "package",
                PLUGIN_ROOT,
                (
                    "import scripts.plot_init.advantages as module; "
                    "assert module.get_advantage_profile.__module__ "
                    "== 'scripts.advantage_profiles'"
                ),
            ),
            (
                "direct",
                SCRIPTS,
                (
                    "import plot_init.advantages as module; "
                    "assert module.get_advantage_profile.__module__ "
                    "== 'advantage_profiles'"
                ),
            ),
        )
        for mode, import_root, assertion in cases:
            with self.subTest(mode=mode):
                code = (
                    "import sys; "
                    f"sys.path.insert(0, {str(import_root)!r}); "
                    f"{assertion}; "
                    "assert module.ADVANTAGE_SCHEMA_VERSION "
                    "== 'plot-rag-advantage/v1'"
                )
                result = subprocess.run(
                    [sys.executable, "-I", "-c", code],
                    cwd=PLUGIN_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    0,
                    result.returncode,
                    msg=(
                        f"{mode} import failed\n"
                        f"stdout:\n{result.stdout}\n"
                        f"stderr:\n{result.stderr}"
                    ),
                )

    def test_registry_contains_exactly_all_sixteen_profiles(self) -> None:
        registry = advantage_profile_registry()
        self.assertEqual(EXPECTED_PROFILES, set(ADVANTAGE_PROFILES))
        self.assertEqual(EXPECTED_PROFILES, set(registry.profiles()))
        self.assertEqual(
            PROFILE_REGISTRY_SCHEMA_VERSION,
            registry.schema_version,
        )
        self.assertRegex(advantage_profile_registry_hash(), r"^[a-f0-9]{64}$")
        for spec in registry.all():
            with self.subTest(profile=spec.profile):
                self.assertTrue(spec.anchor_types)
                self.assertTrue(spec.module_kinds)
                self.assertTrue(spec.runtime_dimensions)
                self.assertTrue(spec.ledger_entry_kinds)
                self.assertTrue(spec.knowledge_requirements)
                self.assertTrue(spec.contract_kinds)
                self.assertTrue(spec.narrative_contract["reading_promise"])
                self.assertTrue(spec.narrative_contract["reward_loop"])
                self.assertTrue(spec.narrative_contract["risk_loop"])
                self.assertTrue(spec.narrative_contract["reveal_ladder"])

    def test_detection_is_recall_only_and_query_terms_are_stable(self) -> None:
        detected = detect_advantage_profiles(
            "主角在有效采集窗口用样例优势核心能力提取，随后把演算点炼成铭文。"
        )
        self.assertIn("resource_transformer", detected)
        explicit = detect_advantage_profiles(
            "无关文本",
            explicit_profiles=["growth_relic", "inheritance"],
        )
        self.assertEqual(("growth_relic", "inheritance"), explicit)
        first = compile_advantage_query_terms(
            ["inheritance", "resource_transformer"],
            project_terms=["样例优势核心"],
        )
        second = compile_advantage_query_terms(
            ["resource_transformer", "inheritance"],
            project_terms=["样例优势核心"],
        )
        self.assertEqual(first, second)
        self.assertIn("样例优势核心", first)
        self.assertIn("认主", first)
        self.assertIn("炼制", first)


class AdvantageSidecarTests(unittest.TestCase):
    def test_all_initialization_collections_are_typed_and_hash_bound(
        self,
    ) -> None:
        package = build_minimal_package()
        self.assertEqual(ADVANTAGE_SCHEMA_VERSION, package["schema_version"])
        self.assertTrue(advantage_package_has_typed_content(package))
        for field in ADVANTAGE_PACKAGE_ARRAY_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(1, len(package[field]))
        self.assertRegex(package["package_hash"], r"^[a-f0-9]{64}$")
        self.assertEqual(
            advantage_profile_registry_hash(),
            package["provenance"]["profile_registry_hash"],
        )
        module = package["modules"][0]
        self.assertEqual("canon", module["status"])
        self.assertEqual("available", module["module_status"])
        runtime = package["runtime_bootstrap"][0]
        self.assertIsInstance(runtime["pollution"], (int, float))
        self.assertIsInstance(runtime["exposure"], (int, float))
        self.assertIsInstance(runtime["debt"], (int, float))

    def test_input_order_does_not_change_ids_bytes_or_hash(self) -> None:
        dossier = minimal_dossier()
        dossier["advantage_definitions"].append(
            {
                "title": "第二遗物",
                "profiles": ["growth_relic"],
                "anchor_type": "item_instance",
                "promise": "成长",
            }
        )
        reordered = copy.deepcopy(dossier)
        for value in reordered.values():
            if isinstance(value, list):
                value.reverse()
        first = build_minimal_package(dossier)
        second = build_minimal_package(reordered)
        self.assertEqual(first["package_hash"], second["package_hash"])
        self.assertEqual(
            render_advantage_package(first),
            render_advantage_package(second),
        )
        self.assertEqual(
            [item["advantage_id"] for item in first["definitions"]],
            [item["advantage_id"] for item in second["definitions"]],
        )

    def test_claims_bind_to_stable_records_without_remote_ids(self) -> None:
        dossier = {
            "advantage_definitions": [
                {
                    "title": "回响戒",
                    "profiles": ["foreknowledge"],
                    "anchor_type": "knowledge_set",
                    "promise": "回忆未来片段",
                }
            ]
        }
        claims = [
            {
                "claim_id": "claim-anchor",
                "subject": "回响戒",
                "predicate": "advantage.anchor",
                "object_or_value": {
                    "anchor_type": "knowledge_set",
                    "anchor_ref_id": "knowledge-echo-ring",
                    "binding_state": "bound",
                },
            },
            {
                "claim_id": "claim-knowledge",
                "subject": "回响戒",
                "predicate": "advantage.knowledge",
                "object_or_value": {
                    "knowledge_plane": "actor_belief",
                    "observer_entity_id": "ent-protagonist",
                    "claim": "明日城门会关闭。",
                    "confidence": 0.6,
                    "reveal_stage": "chapter1",
                },
            },
        ]
        package = build_advantage_package(
            dossier,
            claims,
            work_id="work-claim-test",
            source_initialization_schema_version="plot-rag-init/v1",
            source_snapshot_hash="b" * 64,
        )
        self.assertEqual(1, len(package["anchors"]))
        self.assertEqual(1, len(package["knowledge"]))
        self.assertTrue(package["anchors"][0]["anchor_id"].startswith("advanchor-"))
        self.assertTrue(
            package["knowledge"][0]["knowledge_id"].startswith("advknow-")
        )
        self.assertEqual(
            ["claim-knowledge"],
            package["knowledge"][0]["source_claim_ids"],
        )
        self.assertEqual(
            ["claim-anchor", "claim-knowledge"],
            package["provenance"]["source_claim_ids"],
        )

    def test_invalid_status_runtime_and_hash_fail_closed(self) -> None:
        package = build_minimal_package()
        invalid_status = copy.deepcopy(package)
        invalid_status["knowledge"][0]["status"] = "accepted"
        invalid_status["package_hash"] = (
            "0" * 64
        )
        with self.assertRaises(PlotInitError) as status_error:
            validate_advantage_package(invalid_status)
        self.assertEqual(
            "ADVANTAGE_STATUS_INVALID",
            status_error.exception.code,
        )

        invalid_runtime = copy.deepcopy(package)
        invalid_runtime["runtime_bootstrap"][0]["pollution"] = {
            "unknown": True
        }
        invalid_runtime["package_hash"] = "0" * 64
        with self.assertRaises(PlotInitError) as runtime_error:
            validate_advantage_package(invalid_runtime)
        self.assertEqual(
            "ADVANTAGE_PACKAGE_SCHEMA_INVALID",
            runtime_error.exception.code,
        )
        self.assertEqual(
            "$.runtime_bootstrap[0].pollution",
            runtime_error.exception.details["instance_path"],
        )
        self.assertEqual("type", runtime_error.exception.details["keyword"])

        tampered = copy.deepcopy(package)
        tampered["definitions"][0]["promise"] = "tampered"
        with self.assertRaises(PlotInitError) as hash_error:
            validate_advantage_package(tampered)
        self.assertEqual(
            "ADVANTAGE_PACKAGE_HASH_MISMATCH",
            hash_error.exception.code,
        )

    def test_repository_schema_required_and_type_contract_runs_first(self) -> None:
        package = build_minimal_package()
        cases = []

        missing_title = copy.deepcopy(package)
        del missing_title["definitions"][0]["title"]
        missing_title["package_hash"] = recompute_advantage_package_hash(
            missing_title
        )
        cases.append(
            (
                "missing nested required field",
                missing_title,
                "$.definitions[0]",
                "required",
            )
        )

        integer_boolean = copy.deepcopy(package)
        integer_boolean["runtime_bootstrap"][0]["enabled"] = 1
        integer_boolean["package_hash"] = recompute_advantage_package_hash(
            integer_boolean
        )
        cases.append(
            (
                "integer is not a JSON boolean",
                integer_boolean,
                "$.runtime_bootstrap[0].enabled",
                "type",
            )
        )

        non_array_claims = copy.deepcopy(package)
        non_array_claims["modules"][0]["source_claim_ids"] = "claim-1"
        non_array_claims["package_hash"] = recompute_advantage_package_hash(
            non_array_claims
        )
        cases.append(
            (
                "nested collection type",
                non_array_claims,
                "$.modules[0].source_claim_ids",
                "type",
            )
        )

        for label, invalid, path, keyword in cases:
            with self.subTest(label=label):
                with self.assertRaises(PlotInitError) as caught:
                    validate_advantage_package(invalid)
                self.assertEqual(
                    "ADVANTAGE_PACKAGE_SCHEMA_INVALID",
                    caught.exception.code,
                )
                self.assertEqual(path, caught.exception.details["instance_path"])
                self.assertEqual(keyword, caught.exception.details["keyword"])

        validated = validate_advantage_package(package)
        reparsed = json.loads(render_advantage_package(validated))
        self.assertEqual(validated, reparsed)
        self.assertEqual(
            package["package_hash"],
            recompute_advantage_package_hash(reparsed),
        )

    def test_artifact_create_noop_update_and_materialized_verification(
        self,
    ) -> None:
        package = build_minimal_package()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            created = build_advantage_sidecar_artifact(package, project)
            self.assertEqual("create", created["operation"])
            self.assertEqual(ADVANTAGE_SIDECAR_PATH, created["path"])
            reference = advantage_sidecar_reference(created)
            self.assertEqual(package["package_hash"], reference["package_hash"])

            target = project / ADVANTAGE_SIDECAR_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(created["proposed_content"].encode("utf-8"))
            noop = build_advantage_sidecar_artifact(package, project)
            self.assertEqual("noop", noop["operation"])
            verified = verify_materialized_advantage_sidecar(created, project)
            self.assertEqual("verified", verified["status"])

            changed_dossier = minimal_dossier()
            changed_dossier["advantage_runtime"][0]["resources"]["演算点"] = 3
            changed = build_minimal_package(changed_dossier)
            updated = build_advantage_sidecar_artifact(changed, project)
            self.assertEqual("update", updated["operation"])
            self.assertNotEqual(
                created["advantage_package_hash"],
                updated["advantage_package_hash"],
            )
            self.assertTrue(updated["unified_diff"])

            target.write_bytes(target.read_bytes() + b" ")
            with self.assertRaises(PlotInitError) as materialized:
                verify_materialized_advantage_sidecar(created, project)
            self.assertEqual(
                "ADVANTAGE_SIDECAR_MATERIALIZED_HASH_MISMATCH",
                materialized.exception.code,
            )

    def test_json_schema_accepts_the_canonical_package(self) -> None:
        schema = json.loads(
            (PLUGIN_ROOT / "schemas" / "plot-rag-advantage.v1.json").read_text(
                encoding="utf-8-sig"
            )
        )
        self.assertIsInstance(schema, dict)
        package = build_minimal_package()
        try:
            import jsonschema
        except ImportError:
            return
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(package, schema)



if __name__ == "__main__":
    unittest.main()
