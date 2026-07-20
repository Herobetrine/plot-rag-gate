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

import plot_rag  # noqa: E402
from v1_runtime import migrate_project_config  # noqa: E402


ADVANTAGE_DEFAULTS = {
    "enabled": False,
    "shadow": True,
    "schema_version": "plot-rag-advantage/v1",
    "strict_runtime_validation": False,
    "readable_projection": True,
    "mandatory_context": True,
}


class AdvantageConfigTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "novel"
        self.config_path = self.root / ".plot-rag" / "config.json"
        self.config_path.parent.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_config(self, payload: object) -> None:
        rendered = payload
        if isinstance(payload, dict):
            rendered = dict(payload)
            if (
                "authority_sources" not in rendered
                and "authority_globs" not in rendered
            ):
                rendered["authority_globs"] = ["设定集/**/*.md"]
        self.config_path.write_text(
            json.dumps(rendered, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def test_missing_advantage_section_receives_complete_defaults(self) -> None:
        for version in (1, 2, 3):
            with self.subTest(config_version=version):
                self.write_config({"config_version": version})

                loaded = plot_rag.load_config(self.root)

                self.assertEqual(ADVANTAGE_DEFAULTS, loaded["advantage"])

    def test_checked_in_template_and_custom_values_round_trip(self) -> None:
        template = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v3.json").read_text(
                encoding="utf-8"
            )
        )
        self.write_config(template)

        loaded_template = plot_rag.load_config(self.root)

        self.assertEqual(ADVANTAGE_DEFAULTS, template["advantage"])
        self.assertEqual(template["advantage"], loaded_template["advantage"])

        custom = {
            "enabled": True,
            "shadow": False,
            "schema_version": "plot-rag-advantage/v1",
            "strict_runtime_validation": True,
            "readable_projection": False,
            "mandatory_context": False,
        }
        self.write_config(
            {
                "config_version": 3,
                "advantage": {
                    **custom,
                    "future_extension": {"must_not_reach_runtime": True},
                },
            }
        )
        first = plot_rag.load_config(self.root)
        self.assertEqual(custom, first["advantage"])
        self.assertNotIn("future_extension", first["advantage"])

        self.write_config(
            {
                "config_version": 3,
                "advantage": first["advantage"],
            }
        )
        second = plot_rag.load_config(self.root)
        self.assertEqual(first["advantage"], second["advantage"])

    def test_advantage_section_and_boolean_fields_are_strictly_typed(self) -> None:
        for invalid in (None, [], "enabled", True, 1):
            with self.subTest(section=invalid):
                self.write_config(
                    {
                        "config_version": 3,
                        "advantage": invalid,
                    }
                )
                with self.assertRaisesRegex(
                    plot_rag.PlotRagError,
                    r"config\.advantage must be an object",
                ):
                    plot_rag.load_config(self.root)

        boolean_fields = (
            "enabled",
            "shadow",
            "strict_runtime_validation",
            "readable_projection",
            "mandatory_context",
        )
        for field in boolean_fields:
            for invalid in (0, 1, 0.0, "false", None, []):
                with self.subTest(field=field, value=invalid):
                    self.write_config(
                        {
                            "config_version": 3,
                            "advantage": {field: invalid},
                        }
                    )
                    with self.assertRaisesRegex(
                        plot_rag.PlotRagError,
                        rf"config\.advantage\.{field} must be a boolean",
                    ):
                        plot_rag.load_config(self.root)

    def test_advantage_schema_version_is_strict_and_supported(self) -> None:
        for invalid in (None, True, 1, [], {}):
            with self.subTest(value=invalid):
                self.write_config(
                    {
                        "config_version": 3,
                        "advantage": {"schema_version": invalid},
                    }
                )
                with self.assertRaisesRegex(
                    plot_rag.PlotRagError,
                    r"config\.advantage\.schema_version must be a non-empty string",
                ):
                    plot_rag.load_config(self.root)

        self.write_config(
            {
                "config_version": 3,
                "advantage": {
                    "schema_version": "plot-rag-advantage/v2",
                },
            }
        )
        with self.assertRaisesRegex(
            plot_rag.PlotRagError,
            r"config\.advantage\.schema_version must be one of",
        ):
            plot_rag.load_config(self.root)

    def test_v2_migration_materializes_advantage_defaults_and_preserves_values(
        self,
    ) -> None:
        legacy = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v2.json").read_text(
                encoding="utf-8"
            )
        )
        legacy["advantage"] = {
            "enabled": True,
            "mandatory_context": False,
            "vendor_extension": {"preserve": True},
        }
        self.write_config(legacy)

        result = migrate_project_config(self.root)
        migrated = json.loads(self.config_path.read_text(encoding="utf-8"))

        expected = {
            **ADVANTAGE_DEFAULTS,
            "enabled": True,
            "mandatory_context": False,
        }
        self.assertEqual("migrated", result["status"])
        for key, value in expected.items():
            self.assertEqual(value, migrated["advantage"][key], key)
        self.assertEqual(
            {"preserve": True},
            migrated["advantage"]["vendor_extension"],
        )
        self.assertEqual(
            expected,
            plot_rag.load_config(self.root)["advantage"],
        )


if __name__ == "__main__":
    unittest.main()
