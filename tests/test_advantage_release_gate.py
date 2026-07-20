from __future__ import annotations

import json
import gc
import sqlite3
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import release_gate  # noqa: E402
from continuity import ContinuityService  # noqa: E402
from v1_runtime import migrate_state_schema  # noqa: E402


def _copy_advantage_contract_surface(target: Path) -> None:
    for relative in (
        "templates/config.v3.json",
        "templates/advantage_profiles.v1.json",
        "scripts/advantage_profiles.py",
        "scripts/continuity/advantages.py",
        "scripts/plot_init/advantages.py",
        "scripts/plot_state.py",
        "scripts/plot_rag_mcp.py",
        "scripts/v1_runtime.py",
    ):
        source = PLUGIN_ROOT / relative
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


class AdvantageReleaseGateTests(unittest.TestCase):
    def test_repository_advantage_contract_is_complete(self) -> None:
        config = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v3.json").read_text(
                encoding="utf-8"
            )
        )

        issues = release_gate._validate_advantage_v1_contract(
            PLUGIN_ROOT,
            config,
        )

        self.assertEqual([], issues)

    def test_advantage_config_defaults_fail_closed_on_drift(self) -> None:
        config = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v3.json").read_text(
                encoding="utf-8"
            )
        )
        config["advantage"]["mandatory_context"] = False

        issues = release_gate._validate_advantage_v1_contract(
            PLUGIN_ROOT,
            config,
        )

        self.assertIn(
            "ADVANTAGE_V1_CONFIG_CONTRACT_INVALID",
            {issue.code for issue in issues},
        )

    def test_profile_registry_requires_all_sixteen_frozen_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_advantage_contract_surface(root)
            profile_path = root / "templates" / "advantage_profiles.v1.json"
            registry = json.loads(profile_path.read_text(encoding="utf-8"))
            registry["profiles"] = registry["profiles"][:-1]
            profile_path.write_text(
                json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            config = json.loads(
                (root / "templates" / "config.v3.json").read_text(
                    encoding="utf-8"
                )
            )

            issues = release_gate._validate_advantage_v1_contract(root, config)

        self.assertIn(
            "ADVANTAGE_V1_PROFILE_SET_MISMATCH",
            {issue.code for issue in issues},
        )

    def test_mcp_advantage_tools_must_remain_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_advantage_contract_surface(root)
            mcp_path = root / "scripts" / "plot_rag_mcp.py"
            source = mcp_path.read_text(encoding="utf-8")
            marker = (
                '        "query_advantage_definition",\n'
                "        (\n"
            )
            start = source.index(marker)
            read_only = source.index("        read_only=True,", start)
            weakened = (
                source[:read_only]
                + source[read_only:].replace(
                    "        read_only=True,",
                    "        read_only=False,",
                    1,
                )
            )
            mcp_path.write_text(weakened, encoding="utf-8")
            config = json.loads(
                (root / "templates" / "config.v3.json").read_text(
                    encoding="utf-8"
                )
            )

            issues = release_gate._validate_advantage_v1_contract(root, config)

        self.assertIn(
            "ADVANTAGE_V1_MCP_CONTRACT_MISMATCH",
            {issue.code for issue in issues},
        )

    def test_advantage_anchor_cli_and_mcp_surfaces_are_release_locked(
        self,
    ) -> None:
        mutations = (
            (
                "scripts/plot_state.py",
                '        "anchors",\n        aliases=("anchor",),',
                '        "bindings",\n        aliases=("anchor",),',
                "ADVANTAGE_V1_CLI_CONTRACT_MISMATCH",
            ),
            (
                "scripts/plot_state.py",
                '                helper_name="query_advantage_anchors",',
                '                helper_name="query_advantage_bindings",',
                "ADVANTAGE_V1_CLI_CONTRACT_MISMATCH",
            ),
            (
                "scripts/plot_rag_mcp.py",
                '        "query_advantage_anchors",\n        (',
                '        "query_advantage_bindings",\n        (',
                "ADVANTAGE_V1_MCP_CONTRACT_MISMATCH",
            ),
        )
        for relative, marker, replacement, expected_code in mutations:
            with self.subTest(relative=relative, marker=marker):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    _copy_advantage_contract_surface(root)
                    target = root / relative
                    source = target.read_text(encoding="utf-8")
                    self.assertIn(marker, source)
                    target.write_text(
                        source.replace(marker, replacement, 1),
                        encoding="utf-8",
                    )
                    config = json.loads(
                        (
                            root / "templates" / "config.v3.json"
                        ).read_text(encoding="utf-8")
                    )

                    issues = release_gate._validate_advantage_v1_contract(
                        root,
                        config,
                    )

                self.assertIn(
                    expected_code,
                    {issue.code for issue in issues},
                )

    def test_visibility_contract_is_closed_and_defaults_to_generation(
        self,
    ) -> None:
        mutations = (
            (
                "scripts/plot_state.py",
                'ADVANTAGE_VISIBILITIES = ("generation", "inspection", "raw")',
                'ADVANTAGE_VISIBILITIES = ("generation", "inspection")',
                "ADVANTAGE_V1_CLI_CONTRACT_MISMATCH",
            ),
            (
                "scripts/plot_state.py",
                (
                    "choices=ADVANTAGE_VISIBILITIES,\n"
                    '        default="generation",'
                ),
                (
                    "choices=ADVANTAGE_VISIBILITIES,\n"
                    '        default="inspection",'
                ),
                "ADVANTAGE_V1_CLI_CONTRACT_MISMATCH",
            ),
            (
                "scripts/plot_rag_mcp.py",
                (
                    '    "enum": ["generation", "inspection", "raw"],\n'
                    '    "default": "generation",'
                ),
                (
                    '    "enum": ["generation", "inspection", "raw"],\n'
                    '    "default": "inspection",'
                ),
                "ADVANTAGE_V1_MCP_CONTRACT_MISMATCH",
            ),
        )
        for relative, marker, replacement, expected_code in mutations:
            with self.subTest(relative=relative, replacement=replacement):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    _copy_advantage_contract_surface(root)
                    target = root / relative
                    source = target.read_text(encoding="utf-8")
                    self.assertIn(marker, source)
                    target.write_text(
                        source.replace(marker, replacement, 1),
                        encoding="utf-8",
                    )
                    config = json.loads(
                        (
                            root / "templates" / "config.v3.json"
                        ).read_text(encoding="utf-8")
                    )

                    issues = release_gate._validate_advantage_v1_contract(
                        root,
                        config,
                    )

                self.assertIn(
                    expected_code,
                    {issue.code for issue in issues},
                )

    def test_sidecar_path_and_schema_are_release_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_advantage_contract_surface(root)
            sidecar_path = root / "scripts" / "plot_init" / "advantages.py"
            source = sidecar_path.read_text(encoding="utf-8")
            sidecar_path.write_text(
                source.replace(
                    'ADVANTAGE_SIDECAR_PATH = ".plot-rag/advantages.v1.json"',
                    'ADVANTAGE_SIDECAR_PATH = ".plot-rag/advantages.json"',
                    1,
                ),
                encoding="utf-8",
            )
            config = json.loads(
                (root / "templates" / "config.v3.json").read_text(
                    encoding="utf-8"
                )
            )

            issues = release_gate._validate_advantage_v1_contract(root, config)

        self.assertIn(
            "ADVANTAGE_V1_SIDECAR_CONTRACT_MISMATCH",
            {issue.code for issue in issues},
        )

    def test_migration_receipt_binds_both_projection_hashes_and_cleanup(self) -> None:
        config = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v3.json").read_text(
                encoding="utf-8"
            )
        )

        issues = release_gate._validate_advantage_v1_contract(
            PLUGIN_ROOT,
            config,
        )

        self.assertNotIn(
            "ADVANTAGE_V1_MIGRATION_CONTRACT_MISMATCH",
            {issue.code for issue in issues},
        )

    def test_state_migration_receipt_contains_projection_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "novel"
            (root / ".plot-rag").mkdir(parents=True)
            (root / "正文").mkdir()
            (root / "正文" / "第一章.md").write_text(
                "测试角色甲从测试城南站开始行动。",
                encoding="utf-8",
            )
            config = {
                "config_version": 3,
                "enabled": True,
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
                "event_experience": {"enabled": False},
            }
            (root / ".plot-rag" / "config.json").write_text(
                json.dumps(config, ensure_ascii=False),
                encoding="utf-8",
            )
            ContinuityService(root).schema_status()
            database = root / ".plot-rag" / "state.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    """
                    UPDATE state_meta
                    SET value='6'
                    WHERE key='continuity_schema_version'
                    """
                )
                connection.commit()
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                connection.close()

            migrated = migrate_state_schema(root)
            rollback = migrated["rollback"]
            gc.collect()

        self.assertTrue(
            str(migrated["item_projection_hash"]).startswith(
                "item_projection_"
            )
        )
        self.assertTrue(
            str(migrated["advantage_projection_hash"]).startswith(
                "advantage_projection_"
            )
        )
        self.assertEqual(
            migrated["item_projection_hash"],
            rollback["expected_item_projection_hash"],
        )
        self.assertEqual(
            migrated["advantage_projection_hash"],
            rollback["expected_advantage_projection_hash"],
        )
        cleanup = rollback["readable_projection_cleanup"]
        self.assertEqual({"item", "advantage"}, {item["projection"] for item in cleanup})


if __name__ == "__main__":
    unittest.main()
