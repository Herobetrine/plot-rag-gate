from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import plot_rag


class PlotRagTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_project(
        self,
        text: str | None = "ordinary unrelated material\n",
        *,
        globs: list[str] | None = None,
    ) -> tuple[Path, Path | None]:
        root = self.base / "中文 小说项目"
        (root / ".plot-rag").mkdir(parents=True)
        config = {
            "version": 1,
            "enabled": True,
            "authority_globs": globs or ["docs/*.md"],
            "index_path": ".plot-rag/index.sqlite3",
            "max_chunk_chars": 500,
        }
        (root / ".plot-rag" / "config.json").write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8"
        )
        source = None
        if text is not None:
            (root / "docs").mkdir()
            source = root / "docs" / "facts.md"
            source.write_text(text, encoding="utf-8")
        return root, source

    def test_exact_match_returns_verifiable_path_and_line(self) -> None:
        root, source = self.make_project("# Facts\n\nThe cobalt key opens Gate Seven.\n")

        result = plot_rag.query_project(root, "The cobalt key opens Gate Seven")

        self.assertEqual(plot_rag.STATUS_HIT, result["status"])
        evidence = result["evidence"][0]
        self.assertEqual("docs/facts.md", evidence["path"])
        self.assertEqual(3, evidence["start_line"])
        self.assertEqual(3, evidence["end_line"])
        lines = source.read_text(encoding="utf-8").splitlines()
        quoted = "\n".join(lines[evidence["start_line"] - 1 : evidence["end_line"]])
        self.assertIn("cobalt key", quoted)

    def test_exact_match_can_use_a_bounded_multiline_evidence_span(self) -> None:
        root, _ = self.make_project(
            "# Facts\n\n"
            "The cobalt key opens\n"
            "Gate Seven.\n"
        )

        result = plot_rag.query_project(
            root,
            "The cobalt key opens Gate Seven",
        )

        self.assertEqual(plot_rag.STATUS_HIT, result["status"])
        evidence = result["evidence"][0]
        self.assertEqual(3, evidence["start_line"])
        self.assertEqual(4, evidence["end_line"])
        self.assertIn("opens\nGate Seven", evidence["excerpt"])

    def test_single_empty_query_is_ambiguous(self) -> None:
        root, _ = self.make_project()
        result = plot_rag.query_project(root, "UNSEEN_FACT_ALPHA_9127")
        self.assertEqual(plot_rag.STATUS_AMBIGUOUS, result["status"])

    def test_config_versions_require_exact_json_integers(self) -> None:
        root, _ = self.make_project()
        config_path = root / ".plot-rag" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))

        for invalid in (True, 1.0):
            with self.subTest(field="config_version", value=invalid):
                candidate = dict(config)
                candidate["config_version"] = invalid
                config_path.write_text(
                    json.dumps(candidate),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    plot_rag.PlotRagError,
                    "unsupported config version",
                ):
                    plot_rag.load_config(root)

            with self.subTest(field="version", value=invalid):
                candidate = dict(config)
                candidate.pop("config_version", None)
                candidate["version"] = invalid
                config_path.write_text(
                    json.dumps(candidate),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    plot_rag.PlotRagError,
                    "unsupported legacy config version",
                ):
                    plot_rag.load_config(root)

    def test_integer_config_fields_require_exact_json_integers(self) -> None:
        root, _ = self.make_project()
        config_path = root / ".plot-rag" / "config.json"
        base = json.loads(config_path.read_text(encoding="utf-8"))
        fields = (
            (("max_chunk_chars",), 500),
            (("state", "top_k"), 12),
            (("lifecycle", "approval_ttl_seconds"), 300),
            (("craft", "top_k"), 4),
            (("initialization", "source_max_bytes"), 16 * 1024 * 1024),
        )
        for path, valid in fields:
            for malformed in (True, float(valid), str(valid)):
                with self.subTest(
                    field=".".join(path),
                    value=repr(malformed),
                ):
                    candidate = json.loads(json.dumps(base))
                    target = candidate
                    for part in path[:-1]:
                        target = target.setdefault(part, {})
                    target[path[-1]] = malformed
                    config_path.write_text(
                        json.dumps(candidate),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        plot_rag.PlotRagError,
                        "must be an integer",
                    ):
                        plot_rag.load_config(root)

    def test_v2_state_paths_and_remote_secrets_are_validated(self) -> None:
        root, _ = self.make_project()
        config_path = root / ".plot-rag" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["version"] = 2
        config["state"] = {"db_path": "../escape.sqlite3"}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(plot_rag.PlotRagError, "must stay inside the project"):
            plot_rag.load_config(root)

        config["state"] = {"db_path": ".plot-rag/state.sqlite3"}
        config["remote"] = {"embedding": {"api_key": "must-not-be-stored"}}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(plot_rag.PlotRagError, "use api_key_env instead"):
            plot_rag.load_config(root)

        config["remote"] = {"embedding": {"api_key_env": "AWS_SECRET_ACCESS_KEY"}}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(plot_rag.PlotRagError, "must be one of"):
            plot_rag.load_config(root)

        config["remote"] = {}
        config["craft"] = {"top_k": 4, "candidate_pool": 2}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(plot_rag.PlotRagError, "candidate_pool"):
            plot_rag.load_config(root)

        config["craft"] = {
            "enabled": True,
            "auto_retrieve": True,
            "use_embedding": True,
            "use_rerank": True,
            "top_k": 3,
            "candidate_pool": 8,
            "max_context_chars": 5000,
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
        loaded = plot_rag.load_config(root)
        self.assertEqual(3, loaded["craft"]["top_k"])
        self.assertEqual(8, loaded["craft"]["candidate_pool"])

    def test_project_config_cannot_expand_remote_trust_or_credentials(
        self,
    ) -> None:
        root, _ = self.make_project()
        config_path = root / ".plot-rag" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["remote"] = {
            "trusted_hosts": ["evil.example"],
            "api_key_env_allowlist": ["UNSAFE_KEY"],
            "embedding": {
                "base_url": "https://evil.example/v1",
                "api_key_env": "PLOT_RAG_EMBED_API_KEY",
                "trusted_hosts": ["evil.example"],
                "headers": {"Authorization": "Bearer PROJECT_SECRET"},
                "credential_aliases": ["UNSAFE_KEY"],
            },
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")

        loaded = plot_rag.load_config(root)

        self.assertEqual(
            {
                "timeout_seconds",
                "embedding",
                "rerank",
                "extract",
            },
            set(loaded["remote"]),
        )
        self.assertEqual(
            {
                "enabled",
                "base_url",
                "base_url_env",
                "model",
                "model_env",
                "api_key_env",
                "api_key_required",
            },
            set(loaded["remote"]["embedding"]),
        )
        self.assertEqual(
            "PLOT_RAG_EMBED_API_KEY",
            loaded["remote"]["embedding"]["api_key_env"],
        )
        self.assertNotIn("trusted_hosts", loaded["remote"])
        self.assertNotIn("trusted_hosts", loaded["remote"]["embedding"])
        self.assertNotIn("headers", loaded["remote"]["embedding"])
        self.assertNotIn(
            "credential_aliases",
            loaded["remote"]["embedding"],
        )

    def test_runtime_output_paths_must_be_distinct_and_disjoint(
        self,
    ) -> None:
        root, _ = self.make_project()
        config_path = root / ".plot-rag" / "config.json"
        base = json.loads(config_path.read_text(encoding="utf-8"))

        collision = dict(base)
        collision["state"] = {
            "db_path": ".plot-rag/shared.sqlite3",
            "snapshot_path": ".plot-rag/shared.sqlite3",
        }
        config_path.write_text(json.dumps(collision), encoding="utf-8")
        with self.assertRaisesRegex(
            plot_rag.PlotRagError,
            "runtime paths must be distinct",
        ):
            plot_rag.load_config(root)

        derived_collision = dict(base)
        derived_collision["state"] = {
            "db_path": ".plot-rag/authority.v1.sqlite3",
        }
        config_path.write_text(
            json.dumps(derived_collision),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            plot_rag.PlotRagError,
            "state.db_path and authority.v1",
        ):
            plot_rag.load_config(root)

        commit_overlap = dict(base)
        commit_overlap["state"] = {
            "commit_dir": ".plot-rag",
        }
        config_path.write_text(
            json.dumps(commit_overlap),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            plot_rag.PlotRagError,
            "commit_dir must not contain runtime file",
        ):
            plot_rag.load_config(root)

        first = root / ".plot-rag" / "first.sqlite3"
        second = root / ".plot-rag" / "second.json"
        first.touch()
        try:
            os.link(first, second)
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")
        hardlink_collision = dict(base)
        hardlink_collision["index_path"] = ".plot-rag/first.sqlite3"
        hardlink_collision["state"] = {
            "snapshot_path": ".plot-rag/second.json",
        }
        config_path.write_text(
            json.dumps(hardlink_collision),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            plot_rag.PlotRagError,
            "runtime paths must be distinct",
        ):
            plot_rag.load_config(root)

    def test_v3_authority_sources_are_validated_and_normalized(self) -> None:
        root, _ = self.make_project()
        config_path = root / ".plot-rag" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config.pop("version", None)
        config.pop("authority_globs", None)
        config["config_version"] = 3
        config["authority_sources"] = [
            {
                "glob": "docs/*.md",
                "role": "canon",
                "scope_policy": "infer_and_review",
                "ingest_policy": "include",
                "priority": 100,
            },
            {
                "glob": "outline/*.md",
                "role": "outline",
                "scope_policy": "planned_only",
                "ingest_policy": "review",
                "priority": 60,
            },
            {
                "glob": "settings/*.md",
                "role": "setting",
                "scope_policy": "timeless_candidate",
                "ingest_policy": "include",
                "priority": 90,
            },
        ]
        config["lifecycle"] = {
            "strict": True,
            "longform_context_chars": 6400,
            "index_embeddings_on_prepare": False,
        }
        config["initialization"] = {
            "schema_version": "auto",
            "database_path": ".plot-rag/init.sqlite3",
            "proposal_only": True,
            "default_mode": "hybrid",
            "default_target_profile": "plot_ready",
            "default_interaction_profile": "deep",
            "source_max_bytes": 8 * 1024 * 1024,
            "exclude_globs": [".git/**", ".plot-rag-init/**"],
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")

        loaded = plot_rag.load_config(root)

        self.assertEqual(3, loaded["config_version"])
        self.assertEqual(
            ["docs/*.md", "outline/*.md", "settings/*.md"],
            loaded["authority_globs"],
        )
        self.assertEqual("canon", loaded["authority_sources"][0]["role"])
        self.assertEqual("outline", loaded["authority_sources"][1]["role"])
        self.assertEqual(
            "timeless_only", loaded["authority_sources"][2]["scope_policy"]
        )
        self.assertIn(".plot-rag/init-sessions/**", loaded["ignore_globs"])
        self.assertIn(".plot-rag-init/**", loaded["ignore_globs"])
        self.assertEqual(3, loaded["config_schema_version"])
        self.assertEqual(
            plot_rag.CONTINUITY_STATE_SCHEMA_VERSION,
            loaded["state_schema_version"],
        )
        self.assertEqual(1, loaded["authority_index_schema_version"])
        self.assertTrue(loaded["lifecycle"]["strict"])
        self.assertEqual(6400, loaded["lifecycle"]["longform_context_chars"])
        self.assertTrue(loaded["grill"]["enabled"])
        self.assertTrue(loaded["grill"]["one_question_per_turn"])
        self.assertEqual(
            "plot-rag-intent/v1",
            loaded["grill"]["schema_version"],
        )
        self.assertTrue(
            loaded["grill"]["database_path"].endswith(
                str(Path(".plot-rag") / "grill.sqlite3")
            )
        )
        self.assertEqual(6, loaded["grill"]["max_questions"])
        self.assertEqual("auto", loaded["initialization"]["schema_version"])
        self.assertEqual("hybrid", loaded["initialization"]["default_mode"])
        self.assertEqual(
            str((root / ".plot-rag" / "init.sqlite3").resolve()),
            loaded["initialization"]["database_path"],
        )

        config["authority_sources"][1]["role"] = "untrusted"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(plot_rag.PlotRagError, "role must be one of"):
            plot_rag.load_config(root)

        config["authority_sources"][1]["role"] = "outline"
        config["lifecycle"]["strict"] = False
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(plot_rag.PlotRagError, "proposal-only"):
            plot_rag.load_config(root)

        config["lifecycle"]["strict"] = True
        for schema_version in ("plot-rag-init/v1", "plot-rag-init/v2"):
            config["initialization"]["schema_version"] = schema_version
            config_path.write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(
                schema_version,
                plot_rag.load_config(root)["initialization"]["schema_version"],
            )

        config["initialization"]["schema_version"] = "plot-rag-init/v3"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(plot_rag.PlotRagError, "must be one of"):
            plot_rag.load_config(root)

        config["initialization"]["schema_version"] = "auto"
        config["initialization"]["database_path"] = "../escape.sqlite3"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(plot_rag.PlotRagError, "must stay inside"):
            plot_rag.load_config(root)

    def test_v1_and_v2_configs_without_grill_receive_complete_defaults(self) -> None:
        root, _ = self.make_project()
        config_path = root / ".plot-rag" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        template = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v3.json").read_text(
                encoding="utf-8"
            )
        )

        for version in (1, 2):
            with self.subTest(version=version):
                config["version"] = version
                config.pop("config_version", None)
                config.pop("grill", None)
                config_path.write_text(json.dumps(config), encoding="utf-8")

                loaded = plot_rag.load_config(root)

                self.assertEqual(
                    {
                        "enabled": True,
                        "one_question_per_turn": True,
                        "recommend_answer": True,
                        "explore_project_first": True,
                        "schema_version": "plot-rag-intent/v1",
                        "database_path": str(
                            (root / ".plot-rag" / "grill.sqlite3").resolve()
                        ),
                        "max_questions": 6,
                        "session_ttl_seconds": 21600,
                        "required_fields": list(plot_rag.GRILL_INTENT_FIELDS),
                        "skip_phrases": list(
                            plot_rag.DEFAULT_GRILL_SKIP_PHRASES
                        ),
                        "cancel_phrases": list(
                            plot_rag.DEFAULT_GRILL_CANCEL_PHRASES
                        ),
                    },
                    loaded["grill"],
                )
                self.assertEqual(
                    template["performance"],
                    loaded["performance"],
                )
                self.assertEqual(
                    template["event_experience"],
                    loaded["event_experience"],
                )
                self.assertEqual(
                    template["items"],
                    loaded["items"],
                )

    def test_v15_config_sections_are_deep_merged_and_normalized(self) -> None:
        root, _ = self.make_project()
        config_path = root / ".plot-rag" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["performance"] = {
            "prepare_v2": {
                "enabled": True,
                "shadow": False,
                "rerank_max_concurrency": 8,
                "trusted_hosts": ["evil.example"],
            },
            "extraction": {
                "mode": "async",
                "async_shadow": False,
                "headers": {"Authorization": "secret"},
            },
            "future_section": {"enabled": True},
        }
        config["event_experience"] = {
            "enabled": False,
            "session_ttl_seconds": 900,
            "trusted_hosts": ["evil.example"],
        }
        config["items"] = {
            "strict_runtime_validation": True,
            "api_key_env": "UNSAFE_KEY",
        }
        config["trusted_hosts"] = ["evil.example"]
        config_path.write_text(json.dumps(config), encoding="utf-8")

        loaded = plot_rag.load_config(root)

        self.assertEqual(
            {
                "enabled": True,
                "shadow": False,
                "single_read_snapshot": True,
                "exact_state_short_circuit": True,
                "batch_embedding": True,
                "batch_failure_fallback_single": True,
                "singleflight": True,
                "persistent_exact_cache": True,
                "http_keep_alive": True,
                "rerank_max_concurrency": 8,
                "remote_total_concurrency": 6,
            },
            loaded["performance"]["prepare_v2"],
        )
        self.assertEqual(
            {
                "mode": "async",
                "async_shadow": False,
                "next_plot_turn_barrier": True,
                "barrier_requires_proposal_resolution": True,
                "deterministic_repairs": [
                    "single_action_event_type_echo"
                ],
            },
            loaded["performance"]["extraction"],
        )
        self.assertEqual(900, loaded["event_experience"]["session_ttl_seconds"])
        self.assertFalse(loaded["event_experience"]["enabled"])
        self.assertTrue(loaded["event_experience"]["one_question_per_turn"])
        self.assertEqual(
            "plot-rag-item/v1",
            loaded["items"]["schema_version"],
        )
        self.assertEqual(
            "plot-rag-delta/v4",
            loaded["items"]["delta_version"],
        )
        self.assertTrue(loaded["items"]["strict_runtime_validation"])
        self.assertNotIn("future_section", loaded["performance"])
        self.assertNotIn(
            "trusted_hosts",
            loaded["performance"]["prepare_v2"],
        )
        self.assertNotIn(
            "headers",
            loaded["performance"]["extraction"],
        )
        self.assertNotIn("trusted_hosts", loaded["event_experience"])
        self.assertNotIn("api_key_env", loaded["items"])
        self.assertNotIn("trusted_hosts", loaded)

    def test_v15_config_sections_require_objects(self) -> None:
        root, _ = self.make_project()
        config_path = root / ".plot-rag" / "config.json"
        base = json.loads(config_path.read_text(encoding="utf-8"))
        invalid_sections = (
            (("performance",), []),
            (("performance", "prepare_v2"), None),
            (("performance", "extraction"), "sync"),
            (("event_experience",), []),
            (("items",), None),
        )

        for path, invalid in invalid_sections:
            with self.subTest(field=".".join(path), value=invalid):
                candidate = json.loads(json.dumps(base))
                target = candidate
                for part in path[:-1]:
                    target = target.setdefault(part, {})
                target[path[-1]] = invalid
                config_path.write_text(
                    json.dumps(candidate),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    plot_rag.PlotRagError,
                    "must be an object",
                ):
                    plot_rag.load_config(root)

    def test_v15_config_boolean_fields_are_strict(self) -> None:
        root, _ = self.make_project()
        config_path = root / ".plot-rag" / "config.json"
        base = json.loads(config_path.read_text(encoding="utf-8"))
        boolean_paths = (
            *(
                ("performance", "prepare_v2", key)
                for key in plot_rag.PREPARE_V2_BOOLEAN_DEFAULTS
            ),
            *(
                ("performance", "extraction", key)
                for key in plot_rag.EXTRACTION_BOOLEAN_DEFAULTS
            ),
            *(
                ("event_experience", key)
                for key in plot_rag.EVENT_EXPERIENCE_BOOLEAN_DEFAULTS
            ),
            *(("items", key) for key in plot_rag.ITEM_BOOLEAN_DEFAULTS),
        )

        for path in boolean_paths:
            with self.subTest(field=".".join(path)):
                candidate = json.loads(json.dumps(base))
                target = candidate
                for part in path[:-1]:
                    target = target.setdefault(part, {})
                target[path[-1]] = 1
                config_path.write_text(
                    json.dumps(candidate),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    plot_rag.PlotRagError,
                    "must be a boolean",
                ):
                    plot_rag.load_config(root)

    def test_v15_config_integers_enums_and_ranges_are_strict(self) -> None:
        root, _ = self.make_project()
        config_path = root / ".plot-rag" / "config.json"
        base = json.loads(config_path.read_text(encoding="utf-8"))
        integer_paths = (
            ("performance", "prepare_v2", "rerank_max_concurrency"),
            ("performance", "prepare_v2", "remote_total_concurrency"),
            ("event_experience", "max_questions_per_chain"),
            ("event_experience", "repeat_same_question_limit"),
            ("event_experience", "session_ttl_seconds"),
        )

        for path in integer_paths:
            for invalid in (True, 1.0, "1", None):
                with self.subTest(field=".".join(path), value=invalid):
                    candidate = json.loads(json.dumps(base))
                    target = candidate
                    for part in path[:-1]:
                        target = target.setdefault(part, {})
                    target[path[-1]] = invalid
                    config_path.write_text(
                        json.dumps(candidate),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        plot_rag.PlotRagError,
                        "must be an integer",
                    ):
                        plot_rag.load_config(root)

        invalid_values = (
            (
                ("performance", "prepare_v2", "rerank_max_concurrency"),
                0,
            ),
            (
                ("performance", "prepare_v2", "rerank_max_concurrency"),
                33,
            ),
            (
                ("performance", "prepare_v2", "remote_total_concurrency"),
                65,
            ),
            (("event_experience", "max_questions_per_chain"), 2),
            (("event_experience", "repeat_same_question_limit"), 0),
            (("event_experience", "repeat_same_question_limit"), 2),
            (("event_experience", "session_ttl_seconds"), 59),
            (("event_experience", "session_ttl_seconds"), 604801),
        )
        for path, invalid in invalid_values:
            with self.subTest(field=".".join(path), value=invalid):
                candidate = json.loads(json.dumps(base))
                target = candidate
                for part in path[:-1]:
                    target = target.setdefault(part, {})
                target[path[-1]] = invalid
                config_path.write_text(
                    json.dumps(candidate),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    plot_rag.PlotRagError,
                    "must be between",
                ):
                    plot_rag.load_config(root)

        enum_values = (
            (("performance", "extraction", "mode"), "background"),
            (("items", "schema_version"), "plot-rag-item/v2"),
            (("items", "delta_version"), "plot-rag-delta/v3"),
        )
        for path, invalid in enum_values:
            with self.subTest(field=".".join(path), value=invalid):
                candidate = json.loads(json.dumps(base))
                target = candidate
                for part in path[:-1]:
                    target = target.setdefault(part, {})
                target[path[-1]] = invalid
                config_path.write_text(
                    json.dumps(candidate),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    plot_rag.PlotRagError,
                    "must be one of",
                ):
                    plot_rag.load_config(root)

        for invalid in (
            "single_action_event_type_echo",
            ["unknown_repair"],
            [1],
        ):
            with self.subTest(
                field="performance.extraction.deterministic_repairs",
                value=invalid,
            ):
                candidate = json.loads(json.dumps(base))
                candidate["performance"] = {
                    "extraction": {"deterministic_repairs": invalid}
                }
                config_path.write_text(
                    json.dumps(candidate),
                    encoding="utf-8",
                )
                with self.assertRaises(plot_rag.PlotRagError):
                    plot_rag.load_config(root)

    def test_v15_config_rejects_precision_and_async_barrier_downgrades(
        self,
    ) -> None:
        root, _ = self.make_project()
        config_path = root / ".plot-rag" / "config.json"
        base = json.loads(config_path.read_text(encoding="utf-8"))
        variants = (
            {
                "performance": {
                    "prepare_v2": {
                        "enabled": True,
                        "single_read_snapshot": False,
                    }
                }
            },
            {
                "performance": {
                    "prepare_v2": {
                        "batch_embedding": True,
                        "batch_failure_fallback_single": False,
                    }
                }
            },
            {
                "performance": {
                    "extraction": {
                        "mode": "async",
                        "next_plot_turn_barrier": False,
                    }
                }
            },
            {
                "performance": {
                    "extraction": {
                        "mode": "async",
                        "barrier_requires_proposal_resolution": False,
                    }
                }
            },
            {
                "event_experience": {
                    "one_question_per_turn": False,
                }
            },
        )

        for index, update in enumerate(variants):
            with self.subTest(index=index):
                candidate = json.loads(json.dumps(base))
                candidate.update(update)
                config_path.write_text(
                    json.dumps(candidate),
                    encoding="utf-8",
                )
                with self.assertRaises(plot_rag.PlotRagError):
                    plot_rag.load_config(root)

    def test_grill_config_rejects_invalid_schema_path_and_question_policy(
        self,
    ) -> None:
        root, _ = self.make_project()
        config_path = root / ".plot-rag" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))

        config["grill"] = {"schema_version": "plot-rag-intent/v999"}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(plot_rag.PlotRagError, "schema_version"):
            plot_rag.load_config(root)

        for schema_version in (None, "", "   "):
            with self.subTest(schema_version=schema_version):
                config["grill"] = {"schema_version": schema_version}
                config_path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaisesRegex(
                    plot_rag.PlotRagError,
                    "schema_version",
                ):
                    plot_rag.load_config(root)

        config["grill"] = {"database_path": "../grill.sqlite3"}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(plot_rag.PlotRagError, "inside the project"):
            plot_rag.load_config(root)

        config["grill"] = {"max_questions": 0}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(plot_rag.PlotRagError, "max_questions"):
            plot_rag.load_config(root)

        config["grill"] = {"one_question_per_turn": False}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(
            plot_rag.PlotRagError,
            "one_question_per_turn",
        ):
            plot_rag.load_config(root)

    def test_grill_integer_limits_reject_bool_float_and_string_values(self) -> None:
        root, _ = self.make_project()
        config_path = root / ".plot-rag" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))

        for field, invalid_values in (
            ("max_questions", (True, False, 6.0, 1.5, "6")),
            (
                "session_ttl_seconds",
                (True, False, 21600.0, 300.5, "21600"),
            ),
        ):
            for invalid_value in invalid_values:
                with self.subTest(field=field, invalid_value=invalid_value):
                    config["grill"] = {field: invalid_value}
                    config_path.write_text(json.dumps(config), encoding="utf-8")
                    with self.assertRaisesRegex(
                        plot_rag.PlotRagError,
                        field,
                    ):
                        plot_rag.load_config(root)

    def test_grill_explicit_disable_and_custom_values_are_preserved(self) -> None:
        root, _ = self.make_project()
        config_path = root / ".plot-rag" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["grill"] = {
            "enabled": False,
            "schema_version": "plot-rag-intent/v1",
            "max_questions": 9,
            "session_ttl_seconds": 43200,
            "required_fields": [
                "problem_to_solve",
                "hard_constraints",
                "model_autonomy",
            ],
            "skip_phrases": ["直接按合同执行", "无需本轮盘问"],
            "cancel_phrases": ["取消意图合同"],
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
        loaded = plot_rag.load_config(root)
        self.assertFalse(loaded["grill"]["enabled"])
        self.assertEqual(9, loaded["grill"]["max_questions"])
        self.assertEqual(43200, loaded["grill"]["session_ttl_seconds"])
        self.assertEqual(
            [
                "problem_to_solve",
                "hard_constraints",
                "model_autonomy",
            ],
            loaded["grill"]["required_fields"],
        )
        self.assertEqual(
            ["直接按合同执行", "无需本轮盘问"],
            loaded["grill"]["skip_phrases"],
        )
        self.assertEqual(
            ["取消意图合同"],
            loaded["grill"]["cancel_phrases"],
        )

    def test_checked_in_v3_template_loads_without_manual_edits(self) -> None:
        root = self.base / "template-project"
        (root / ".plot-rag").mkdir(parents=True)
        template = PLUGIN_ROOT / "templates" / "config.v3.json"
        template_payload = json.loads(template.read_text(encoding="utf-8"))
        (root / ".plot-rag" / "config.json").write_bytes(
            template.read_bytes()
        )

        loaded = plot_rag.load_config(root)

        self.assertEqual(3, loaded["config_version"])
        self.assertTrue(loaded["lifecycle"]["strict"])
        self.assertEqual("auto", loaded["initialization"]["schema_version"])
        self.assertEqual(
            "timeless_only",
            next(
                source["scope_policy"]
                for source in loaded["authority_sources"]
                if source["role"] == "setting"
            ),
        )
        self.assertEqual(
            template_payload["performance"],
            loaded["performance"],
        )
        self.assertEqual(
            template_payload["event_experience"],
            loaded["event_experience"],
        )
        self.assertEqual(
            template_payload["items"],
            loaded["items"],
        )

    def test_legacy_authority_globs_have_deterministic_source_roles(self) -> None:
        root, _ = self.make_project(
            globs=["正文/*.md", "设定集/*.md", "大纲/*.md", "灵感/*.md"]
        )
        loaded = plot_rag.load_config(root)
        by_glob = {
            source["glob"]: source for source in loaded["authority_sources"]
        }
        self.assertEqual("canon", by_glob["正文/*.md"]["role"])
        self.assertEqual("setting", by_glob["设定集/*.md"]["role"])
        self.assertEqual("outline", by_glob["大纲/*.md"]["role"])
        self.assertEqual("note", by_glob["灵感/*.md"]["role"])
        self.assertEqual("exclude", by_glob["灵感/*.md"]["ingest_policy"])

    def test_legacy_index_exclude_overrides_overlapping_include_glob(self) -> None:
        root, _ = self.make_project(text=None)
        config_path = root / ".plot-rag" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config.pop("version", None)
        config.pop("authority_globs", None)
        config["config_version"] = 3
        config["authority_sources"] = [
            {
                "glob": "*.md",
                "role": "setting",
                "scope_policy": "infer_and_review",
                "ingest_policy": "include",
                "priority": 90,
            },
            {
                "glob": "*灵感记录.md",
                "role": "note",
                "scope_policy": "planned_only",
                "ingest_policy": "review",
                "priority": 20,
            },
        ]
        config_path.write_text(
            json.dumps(config, ensure_ascii=False),
            encoding="utf-8",
        )
        included = root / "权威设定.md"
        excluded = root / "第一章 检修室 灵感记录.md"
        included.write_text(
            "INCLUDED_CANON_MARKER_4455 is authoritative.\n",
            encoding="utf-8",
        )
        excluded.write_text(
            "EXCLUDED_IDEA_MARKER_7788 must stay out of retrieval.\n",
            encoding="utf-8",
        )

        allowed = plot_rag.query_project(
            root,
            "INCLUDED_CANON_MARKER_4455 is authoritative",
        )
        previously_indexed = plot_rag.query_project(
            root,
            "EXCLUDED_IDEA_MARKER_7788 must stay out of retrieval",
        )
        config["authority_sources"][1]["ingest_policy"] = "exclude"
        config_path.write_text(
            json.dumps(config, ensure_ascii=False),
            encoding="utf-8",
        )
        blocked = plot_rag.query_project(
            root,
            "EXCLUDED_IDEA_MARKER_7788 must stay out of retrieval",
            ["find excluded idea marker 7788"],
        )

        self.assertEqual(plot_rag.STATUS_HIT, allowed["status"])
        self.assertEqual(plot_rag.STATUS_HIT, previously_indexed["status"])
        self.assertEqual(
            excluded.name,
            previously_indexed["evidence"][0]["path"],
        )
        self.assertEqual(plot_rag.STATUS_MISS, blocked["status"])
        self.assertEqual(1, blocked["index"]["source_count"])
        self.assertEqual(1, blocked["index"]["removed_files"])
        self.assertEqual([], blocked["evidence"])
        self.assertEqual([], blocked.get("candidates", []))
        config = plot_rag.load_config(root)
        with closing(sqlite3.connect(config["index_path"])) as connection:
            indexed_paths = {
                str(row[0])
                for row in connection.execute("SELECT path FROM files")
            }
            excluded_chunks = int(
                connection.execute(
                    "SELECT COUNT(*) FROM chunks "
                    "WHERE search_text LIKE '%EXCLUDED_IDEA_MARKER_7788%'"
                ).fetchone()[0]
            )
        self.assertEqual({"权威设定.md"}, indexed_paths)
        self.assertEqual(0, excluded_chunks)

    def test_facility_query_does_not_accept_planet_naming_as_hit(self) -> None:
        root, _ = self.make_project(
            "# 测试设定记录\n\n"
            "## 开篇区域定位\n\n"
            "### 星桥城与基础设施\n\n"
            "- 外围聚落包含资源站、维修站和工业设施。\n"
            "- 行星名称：星桥城所在行星正式定名为苍穹星；"
            "苍穹星只是星桥城所在的具体行星。\n"
            "- 北部旧区保留停用交通线和废弃公共设施。\n"
        )
        config_path = root / ".plot-rag" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["reliable_coverage"] = 0.15
        config["weak_coverage"] = 0.05
        config_path.write_text(json.dumps(config), encoding="utf-8")

        result = plot_rag.query_project(
            root,
            "开篇时阿岚所在的具体设施是什么",
            ["阿岚醒来时身处哪里"],
        )

        self.assertEqual(plot_rag.STATUS_AMBIGUOUS, result["status"])
        self.assertEqual([], result["evidence"])
        self.assertIn("core anchor", result["reason"])
        self.assertTrue(
            any(
                "行星名称" in candidate["excerpt"]
                for candidate in result["candidates"]
            )
        )

    def test_injury_query_does_not_accept_future_principle_as_hit(self) -> None:
        root, _ = self.make_project(
            "# 测试设定记录\n\n"
            "## 河湾城（建设中）\n\n"
            "- 危机承接原则：河湾城的后续事件仍须承接星桥城"
            "留下的角色可见身体状态，并由外部伤情问题升级为"
            "感知或判断问题；具体受伤部位、伤势表现、触发方式"
            "与解决方案等待确认。\n"
            "- 危机载体：一份从星桥城转运而来的未定事故档案。\n"
        )
        config_path = root / ".plot-rag" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["reliable_coverage"] = 0.15
        config["weak_coverage"] = 0.05
        config_path.write_text(json.dumps(config), encoding="utf-8")

        result = plot_rag.query_project(
            root,
            "开篇时阿岚的可见身体状态是什么",
            ["阿岚醒来时身体受了什么伤"],
        )

        self.assertEqual(plot_rag.STATUS_AMBIGUOUS, result["status"])
        self.assertEqual([], result["evidence"])
        self.assertIn("core anchor", result["reason"])
        self.assertTrue(
            any(
                "危机承接原则" in candidate["excerpt"]
                for candidate in result["candidates"]
            )
        )

    def test_non_exact_hit_can_cover_subject_and_focus_anchors(self) -> None:
        root, _ = self.make_project(
            "# 开篇记录\n\n"
            "开篇时，测试角色甲醒来后所在的具体\n"
            "设施是北库区检修室。\n"
        )

        result = plot_rag.query_project(
            root,
            "开篇时测试角色甲所在的具体设施是什么",
            ["测试角色甲醒来时身处哪里"],
        )

        self.assertEqual(plot_rag.STATUS_HIT, result["status"])
        self.assertIn("北库区检修室", result["evidence"][0]["excerpt"])

    def test_alias_exact_without_primary_focus_is_ambiguous(self) -> None:
        root, _ = self.make_project(
            "# 待确认问题\n\n"
            "测试角色甲醒来时身处哪里仍待确认。\n"
        )

        result = plot_rag.query_project(
            root,
            "开篇时测试角色甲所在的具体设施是什么",
            ["测试角色甲醒来时身处哪里"],
        )

        self.assertEqual(plot_rag.STATUS_AMBIGUOUS, result["status"])
        self.assertEqual([], result["evidence"])
        self.assertIn("core anchor", result["reason"])
        self.assertIn("身处哪里", result["candidates"][0]["excerpt"])

    def test_primary_focus_pending_answer_is_ambiguous(self) -> None:
        root, _ = self.make_project(
            "# 待确认问题\n\n"
            "开篇时测试角色甲所在的具体设施尚未确定。\n"
        )

        result = plot_rag.query_project(
            root,
            "开篇时测试角色甲所在的具体设施是什么",
            ["测试角色甲醒来时身处哪里"],
        )

        self.assertEqual(plot_rag.STATUS_AMBIGUOUS, result["status"])
        self.assertEqual([], result["evidence"])
        self.assertIn("core anchor", result["reason"])
        self.assertIn("尚未确定", result["candidates"][0]["excerpt"])

    def test_primary_focus_negative_answer_is_ambiguous(self) -> None:
        root, _ = self.make_project(
            "# 排除项\n\n"
            "开篇时测试角色甲所在的具体设施不是检修室。\n"
        )

        result = plot_rag.query_project(
            root,
            "开篇时测试角色甲所在的具体设施是什么",
            ["测试角色甲醒来时身处哪里"],
        )

        self.assertEqual(plot_rag.STATUS_AMBIGUOUS, result["status"])
        self.assertEqual([], result["evidence"])
        self.assertIn("core anchor", result["reason"])
        self.assertIn("不是检修室", result["candidates"][0]["excerpt"])

    def test_duplicate_alias_does_not_count_as_second_query(self) -> None:
        root, _ = self.make_project()
        result = plot_rag.query_project(
            root,
            "UNSEEN FACT ALPHA",
            [" unseen fact alpha!!! "],
        )
        self.assertEqual(plot_rag.STATUS_AMBIGUOUS, result["status"])
        self.assertEqual(1, len(result["queries"]))

    def test_two_empty_queries_confirm_miss_on_healthy_empty_index(self) -> None:
        root, _ = self.make_project(text=None)
        result = plot_rag.query_project(
            root,
            "MISSING_BLUE_WHALE_701",
            ["ABSENT_CELESTIAL_PET_702"],
        )
        self.assertEqual(plot_rag.STATUS_MISS, result["status"])
        self.assertTrue(result["index"]["healthy"])
        self.assertEqual(0, result["index"]["source_count"])
        self.assertEqual([], result["evidence"])

    def test_missing_and_malformed_configs_are_unavailable(self) -> None:
        missing = self.base / "missing"
        missing.mkdir()
        result = plot_rag.query_project(missing, "fact", ["alias"])
        self.assertEqual(plot_rag.STATUS_UNAVAILABLE, result["status"])
        self.assertTrue(result["reason"])

        broken = self.base / "broken"
        (broken / ".plot-rag").mkdir(parents=True)
        (broken / ".plot-rag" / "config.json").write_text("{broken", encoding="utf-8")
        result = plot_rag.query_project(broken, "fact", ["alias"])
        self.assertEqual(plot_rag.STATUS_UNAVAILABLE, result["status"])
        self.assertIn("invalid JSON", result["reason"])

    def test_sha256_refresh_replaces_stale_chunks_even_with_same_mtime(self) -> None:
        root, source = self.make_project("OLD_MARKER_88 cobalt state\n")
        old_stat = source.stat()
        first = plot_rag.query_project(root, "OLD_MARKER_88 cobalt state")
        self.assertEqual(plot_rag.STATUS_HIT, first["status"])

        source.write_text("NEW_MARKER_99 amber state\n", encoding="utf-8")
        os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))

        new = plot_rag.query_project(root, "NEW_MARKER_99 amber state")
        old = plot_rag.query_project(
            root,
            "OLD_MARKER_88 cobalt state",
            ["retired OLD_MARKER_88 cobalt record"],
        )
        self.assertEqual(plot_rag.STATUS_HIT, new["status"])
        self.assertEqual(plot_rag.STATUS_MISS, old["status"])

        config = plot_rag.load_config(root)
        with closing(sqlite3.connect(config["index_path"])) as connection:
            stale = connection.execute(
                "SELECT COUNT(*) FROM chunks WHERE search_text LIKE '%OLD_MARKER_88%'"
            ).fetchone()[0]
        self.assertEqual(0, stale)

    def test_deleted_source_removes_all_chunks(self) -> None:
        root, source = self.make_project("DELETE_MARKER_440 exists here\n")
        first = plot_rag.query_project(root, "DELETE_MARKER_440 exists here")
        self.assertEqual(plot_rag.STATUS_HIT, first["status"])
        source.unlink()

        result = plot_rag.query_project(
            root,
            "DELETE_MARKER_440 exists here",
            ["find DELETE_MARKER_440 record"],
        )
        self.assertEqual(plot_rag.STATUS_MISS, result["status"])
        self.assertEqual(1, result["index"]["removed_files"])

    def test_pointer_is_found_from_nested_workspace(self) -> None:
        project, _ = self.make_project("POINTER_FACT_123 is authoritative\n")
        workspace = self.base / "workspace"
        nested = workspace / "one" / "two"
        nested.mkdir(parents=True)
        (workspace / plot_rag.POINTER_FILE).write_text(str(project) + "\n", encoding="utf-8")

        self.assertEqual(project.resolve(), plot_rag.locate_project_root(nested))

    def test_different_exact_authoritative_passages_are_ambiguous(self) -> None:
        root, first = self.make_project(
            "# First\n\nRecovery checkpoint: Chapter Two.\n",
            globs=["docs/*.md"],
        )
        second = root / "docs" / "other.md"
        second.write_text("# Second\n\nRecovery checkpoint: Chapter Three.\n", encoding="utf-8")

        result = plot_rag.query_project(root, "Recovery checkpoint", ["recovery checkpoint"])

        self.assertEqual(plot_rag.STATUS_AMBIGUOUS, result["status"])
        self.assertGreaterEqual(len(result["candidates"]), 2)


if __name__ == "__main__":
    unittest.main()
