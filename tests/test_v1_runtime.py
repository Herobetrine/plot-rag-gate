from __future__ import annotations

import json
import hashlib
import os
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from continuity import (  # noqa: E402
    ContinuityError,
    ContinuityService,
    SCHEMA_VERSION as CONTINUITY_SCHEMA_VERSION,
)
import longform.projections as projection_module  # noqa: E402
from longform import (  # noqa: E402
    AuthorityIndex,
    AcceptedSummaryStore,
    LayeredMemoryStore,
    ProjectPatternStore,
    ProjectionJournal,
)
from plot_init.storage import InitStorage  # noqa: E402
from plot_rag import STATE_CATEGORIES, load_config  # noqa: E402
from plot_init import PlotInitService  # noqa: E402
from event_experience_runtime import ensure_locked_manifest  # noqa: E402
import state_rag as state_runtime  # noqa: E402
from state_rag import doctor as state_doctor  # noqa: E402
import v1_runtime as v1_runtime_module  # noqa: E402
from v1_runtime import (  # noqa: E402
    accept_plot_proposal,
    apply_initialization_proposal,
    doctor_v1,
    infer_artifact_context,
    init_service,
    issue_host_approval,
    longform_status,
    migrate_project_config,
    migrate_state_schema,
    prepare_plot_turn,
    propose_plot_turn,
    query_continuity,
    query_continuity_text,
    refresh_longform_index,
    register_initialization_proposal,
    replay_continuity,
    retract_plot_proposal,
    verify_initialization,
)


class V1RuntimeTestCase(unittest.TestCase):
    def test_state_migration_dry_run_and_write_share_version_gate(
        self,
    ) -> None:
        cases = (
            (
                "legacy-negative",
                "UPDATE state_meta SET value='-1' "
                "WHERE key='schema_version'",
                "STATE_SCHEMA_UNREADABLE",
            ),
            (
                "legacy-unsupported",
                "UPDATE state_meta SET value='1' "
                "WHERE key='schema_version'",
                "STATE_LEGACY_SCHEMA_UNSUPPORTED",
            ),
            (
                "legacy-future",
                "UPDATE state_meta SET value='999' "
                "WHERE key='schema_version'",
                "STATE_LEGACY_SCHEMA_TOO_NEW",
            ),
            (
                "continuity-negative",
                "UPDATE state_meta SET value='-1' "
                "WHERE key='continuity_schema_version'",
                "STATE_SCHEMA_UNREADABLE",
            ),
            (
                "continuity-future",
                "UPDATE state_meta SET value='999' "
                "WHERE key='continuity_schema_version'",
                "STATE_SCHEMA_TOO_NEW",
            ),
            (
                "missing-legacy",
                "DELETE FROM state_meta WHERE key='schema_version'",
                "STATE_SCHEMA_VERSION_MISSING",
            ),
        )
        for label, mutation, expected_code in cases:
            with (
                self.subTest(label=label),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = self.make_project(Path(temporary))
                ContinuityService(root).schema_status()
                database = root / ".plot-rag" / "state.sqlite3"
                with closing(sqlite3.connect(database)) as connection:
                    connection.execute(mutation)
                    connection.commit()
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                before = {
                    path.name: (
                        path.read_bytes(),
                        path.stat().st_mtime_ns,
                    )
                    for path in database.parent.iterdir()
                    if path.is_file()
                }

                messages: list[str] = []
                for dry_run in (True, False):
                    with self.assertRaises(ValueError) as raised:
                        migrate_state_schema(root, dry_run=dry_run)
                    messages.append(str(raised.exception))
                    self.assertIn(expected_code, messages[-1])

                self.assertEqual(messages[0], messages[1])
                self.assertEqual(
                    before,
                    {
                        path.name: (
                            path.read_bytes(),
                            path.stat().st_mtime_ns,
                        )
                        for path in database.parent.iterdir()
                        if path.is_file()
                    },
                )

    def test_state_migration_dry_run_accepts_supported_legacy_pairs(
        self,
    ) -> None:
        for continuity_version in (0, 4):
            with (
                self.subTest(continuity_version=continuity_version),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = self.make_project(Path(temporary))
                ContinuityService(root).schema_status()
                database = root / ".plot-rag" / "state.sqlite3"
                with closing(sqlite3.connect(database)) as connection:
                    connection.execute(
                        """
                        UPDATE state_meta
                        SET value=?
                        WHERE key='continuity_schema_version'
                        """,
                        (str(continuity_version),),
                    )
                    connection.commit()
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

                result = migrate_state_schema(root, dry_run=True)

                self.assertEqual("dry_run", result["status"])
                self.assertTrue(result["changed"])
                self.assertEqual(2, result["legacy_schema_version"])
                self.assertEqual(continuity_version, result["from_version"])
                self.assertEqual(
                    CONTINUITY_SCHEMA_VERSION,
                    result["to_version"],
                )

    def test_state_migration_dry_run_and_write_share_ownership_gate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            ContinuityService(root).schema_status()
            database = root / ".plot-rag" / "state.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "CREATE TABLE user_finance(id INTEGER PRIMARY KEY)"
                )
                connection.commit()
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            before = self.tree_fingerprints(root)

            messages: list[str] = []
            for dry_run in (True, False):
                with self.assertRaises(ValueError) as raised:
                    migrate_state_schema(root, dry_run=dry_run)
                messages.append(str(raised.exception))
                self.assertIn("STATE_DATABASE_UNOWNED", messages[-1])

            self.assertEqual(messages[0], messages[1])
            self.assertEqual(before, self.tree_fingerprints(root))

    def test_doctor_reports_foreign_tables_for_every_owned_database(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            plot_rag_dir = root / ".plot-rag"
            ContinuityService(root).schema_status()
            AuthorityIndex(plot_rag_dir / "authority.v1.sqlite3")
            InitStorage(plot_rag_dir / "init.sqlite3")._initialize()
            LayeredMemoryStore(plot_rag_dir / "longform.v1.sqlite3")
            AcceptedSummaryStore(plot_rag_dir / "longform.v1.sqlite3")
            ProjectPatternStore(plot_rag_dir / "longform.v1.sqlite3")
            ProjectionJournal(
                plot_rag_dir / "projection-runs.v1.sqlite3",
                auto_recover=False,
            )

            for database in (
                plot_rag_dir / "state.sqlite3",
                plot_rag_dir / "authority.v1.sqlite3",
                plot_rag_dir / "init.sqlite3",
                plot_rag_dir / "longform.v1.sqlite3",
                plot_rag_dir / "projection-runs.v1.sqlite3",
            ):
                with closing(sqlite3.connect(database)) as connection:
                    connection.execute(
                        "CREATE TABLE user_finance(id INTEGER PRIMARY KEY)"
                    )
                    connection.commit()
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            before = self.tree_fingerprints(root)
            health = doctor_v1(root)

            self.assertEqual(before, self.tree_fingerprints(root))
            self.assertTrue(health["zero_write"])
            for name in (
                "state",
                "continuity",
                "authority_index",
                "initialization_store",
                "longform_memory",
                "longform_summary",
                "longform_projection",
            ):
                with self.subTest(component=name):
                    component = health["components"][name]
                    self.assertEqual("failed", component["status"])
                    self.assertEqual(
                        ["user_finance"],
                        component["unexpected_tables"],
                    )
            self.assertEqual(
                "failed",
                health["components"]["longform_method"]["status"],
            )
            self.assertEqual(
                ["user_finance"],
                health["components"]["longform_method"]["project_memory"][
                    "unexpected_tables"
                ],
            )

    def test_v2_config_migration_materializes_v3_state_and_power_defaults(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "novel"
            config_path = root / ".plot-rag" / "config.json"
            config_path.parent.mkdir(parents=True)
            legacy = json.loads(
                (PLUGIN_ROOT / "templates" / "config.v2.json").read_text(
                    encoding="utf-8"
                )
            )
            legacy["custom_extension"] = {"preserve": True}
            legacy["state"]["custom_state_extension"] = {
                "preserve": "state"
            }
            config_path.write_text(
                json.dumps(legacy, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            migrated = migrate_project_config(root)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            template = json.loads(
                (PLUGIN_ROOT / "templates" / "config.v3.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual("migrated", migrated["status"])
            for key, value in template["state"].items():
                self.assertEqual(value, payload["state"][key], key)
            self.assertEqual(
                list(STATE_CATEGORIES),
                payload["state"]["categories"],
            )
            self.assertEqual(
                template["power_system"],
                payload["power_system"],
            )
            self.assertEqual(
                {"preserve": True},
                payload["custom_extension"],
            )
            self.assertEqual(
                {"preserve": "state"},
                payload["state"]["custom_state_extension"],
            )

            normalized = load_config(root)
            self.assertEqual(
                list(STATE_CATEGORIES),
                normalized["state"]["categories"],
            )
            self.assertEqual(
                template["power_system"],
                normalized["power_system"],
            )
            for key, value in template["initialization"].items():
                self.assertEqual(
                    value,
                    payload["initialization"][key],
                    key,
                )
            self.assertEqual(
                legacy["authority_globs"],
                [
                    source["glob"]
                    for source in payload["authority_sources"]
                ],
            )
            assistant_text = "测试角色甲掌握了御风术。"
            extracted = {
                "schema_version": "plot-rag-delta/v3",
                "deltas": [
                    {
                        "event_type": "ability",
                        "action": "gain",
                        "subject": "测试角色甲",
                        "object": "御风术",
                        "field": "ownership",
                        "value": {"rank": "初阶"},
                        "confidence": 0.95,
                        "evidence": assistant_text,
                    }
                ],
            }
            deltas, skipped = state_runtime._validate_v3_deltas(
                extracted,
                assistant_text,
                state_runtime._load_runtime_config(root),
            )
            self.assertEqual([], skipped)
            self.assertEqual("ability", deltas[0]["category"])

    def test_config_migration_source_version_requires_exact_json_integer(
        self,
    ) -> None:
        template = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v2.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for field in ("config_version", "version"):
                for index, malformed in enumerate(
                    (True, 2.0, "2"),
                    start=1,
                ):
                    with self.subTest(
                        field=field,
                        value=repr(malformed),
                    ):
                        root = base / f"{field}-{index}"
                        config_path = root / ".plot-rag" / "config.json"
                        config_path.parent.mkdir(parents=True)
                        candidate = json.loads(
                            json.dumps(template, ensure_ascii=False)
                        )
                        if field == "config_version":
                            candidate["config_version"] = malformed
                        else:
                            candidate.pop("config_version", None)
                            candidate["version"] = malformed
                        config_path.write_text(
                            json.dumps(
                                candidate,
                                ensure_ascii=False,
                                indent=2,
                            ),
                            encoding="utf-8",
                        )
                        before = self.tree_fingerprints(root)
                        with self.assertRaisesRegex(
                            ValueError,
                            "exact JSON integer",
                        ):
                            migrate_project_config(root)
                        self.assertEqual(
                            before,
                            self.tree_fingerprints(root),
                        )

    def test_v2_config_migration_preserves_custom_values_and_receipts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "novel"
            config_path = root / ".plot-rag" / "config.json"
            config_path.parent.mkdir(parents=True)
            legacy = json.loads(
                (PLUGIN_ROOT / "templates" / "config.v2.json").read_text(
                    encoding="utf-8"
                )
            )
            legacy_categories = [
                "world_state",
                "story_time",
                "inventory",
                "location",
                "relationship",
                "character_state",
            ]
            legacy["custom_top_level"] = {"preserve": [1, 2, 3]}
            legacy.pop("authority_globs")
            legacy["authority_sources"] = [
                {
                    "glob": "正文/**/*.md",
                    "role": "CANON",
                    "scope_policy": "current",
                    "ingest_policy": "include",
                    "priority": 321,
                    "custom_source_key": {"preserve": "canon"},
                },
                {
                    "glob": "设定集/**/*.md",
                    "role": "setting",
                    "custom_source_key": {"preserve": "setting"},
                },
            ]
            legacy["state"].update(
                {
                    "categories": legacy_categories,
                    "top_k": 7,
                    "custom_state_key": "preserved",
                }
            )
            legacy["power_system"] = {
                "mode": "enabled",
                "strict_progression": False,
                "unknown_policy": "preserve",
                "profiles": ["cultivation"],
                "custom_power_key": {"preserve": True},
            }
            legacy["lifecycle"] = {
                "strict": False,
                "longform_context_chars": 4567,
                "custom_lifecycle_key": {"preserve": True},
            }
            legacy["initialization"] = {
                "schema_version": "plot-rag-init/v1",
                "proposal_only": False,
                "exclude_globs": [
                    "自定义资料/**",
                    ".plot-rag/**",
                ],
                "custom_initialization_key": {"preserve": True},
            }
            source_bytes = (
                json.dumps(legacy, ensure_ascii=False, indent=2).encode(
                    "utf-8"
                )
            )
            config_path.write_bytes(source_bytes)
            old_hash = hashlib.sha256(source_bytes).hexdigest()
            before_tree = self.tree_fingerprints(root)

            dry_run = migrate_project_config(root, dry_run=True)
            self.assertEqual("dry_run", dry_run["status"])
            self.assertEqual(old_hash, dry_run["old_sha256"])
            self.assertEqual(before_tree, self.tree_fingerprints(root))

            migrated = migrate_project_config(root)
            migrated_bytes = config_path.read_bytes()
            migrated_hash = hashlib.sha256(migrated_bytes).hexdigest()
            payload = json.loads(migrated_bytes.decode("utf-8"))
            expected_categories = [
                *legacy_categories,
                *[
                    category
                    for category in STATE_CATEGORIES
                    if category not in legacy_categories
                ],
            ]

            self.assertEqual(old_hash, migrated["old_sha256"])
            self.assertEqual(dry_run["new_sha256"], migrated_hash)
            self.assertEqual(migrated_hash, migrated["new_sha256"])
            self.assertEqual(expected_categories, payload["state"]["categories"])
            self.assertEqual(7, payload["state"]["top_k"])
            self.assertEqual(
                "preserved",
                payload["state"]["custom_state_key"],
            )
            self.assertEqual(
                {"preserve": [1, 2, 3]},
                payload["custom_top_level"],
            )
            self.assertEqual("enabled", payload["power_system"]["mode"])
            self.assertFalse(
                payload["power_system"]["strict_progression"]
            )
            self.assertEqual(
                "preserve",
                payload["power_system"]["unknown_policy"],
            )
            self.assertEqual(
                ["cultivation"],
                payload["power_system"]["profiles"],
            )
            self.assertEqual(
                "plot-rag-power/v1",
                payload["power_system"]["schema_version"],
            )
            self.assertEqual(
                "conditional",
                payload["power_system"]["comparison_mode"],
            )
            self.assertEqual(
                {"preserve": True},
                payload["power_system"]["custom_power_key"],
            )
            self.assertTrue(payload["lifecycle"]["strict"])
            self.assertEqual(
                4567,
                payload["lifecycle"]["longform_context_chars"],
            )
            self.assertEqual(
                {"preserve": True},
                payload["lifecycle"]["custom_lifecycle_key"],
            )
            self.assertNotIn("authority_globs", payload)
            self.assertEqual(
                {
                    "glob": "正文/**/*.md",
                    "role": "CANON",
                    "scope_policy": "current",
                    "ingest_policy": "include",
                    "priority": 321,
                    "custom_source_key": {"preserve": "canon"},
                },
                payload["authority_sources"][0],
            )
            self.assertEqual(
                {
                    "glob": "设定集/**/*.md",
                    "role": "setting",
                    "scope_policy": "infer_and_review",
                    "ingest_policy": "include",
                    "priority": 50,
                    "custom_source_key": {"preserve": "setting"},
                },
                payload["authority_sources"][1],
            )
            self.assertEqual(
                "plot-rag-init/v1",
                payload["initialization"]["schema_version"],
            )
            self.assertTrue(
                payload["initialization"]["proposal_only"]
            )
            self.assertEqual(
                [
                    "自定义资料/**",
                    ".plot-rag/**",
                ],
                payload["initialization"]["exclude_globs"],
            )
            self.assertEqual(
                {"preserve": True},
                payload["initialization"]["custom_initialization_key"],
            )
            handwritten_root = Path(temporary) / "handwritten-v3"
            handwritten_config_path = (
                handwritten_root / ".plot-rag" / "config.json"
            )
            handwritten_config_path.parent.mkdir(parents=True)
            handwritten = json.loads(
                (PLUGIN_ROOT / "templates" / "config.v3.json").read_text(
                    encoding="utf-8"
                )
            )
            handwritten["initialization"]["schema_version"] = (
                "plot-rag-init/v1"
            )
            handwritten["initialization"]["exclude_globs"] = [
                "自定义资料/**",
                ".plot-rag/**",
            ]
            handwritten_config_path.write_text(
                json.dumps(handwritten, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.assertEqual(
                load_config(handwritten_root)["initialization"][
                    "exclude_globs"
                ],
                load_config(root)["initialization"]["exclude_globs"],
            )

            backup_path = Path(migrated["backup_path"])
            record_path = Path(migrated["migration_record"])
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(source_bytes, backup_path.read_bytes())
            self.assertEqual(
                old_hash,
                hashlib.sha256(backup_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(old_hash, record["old_sha256"])
            self.assertEqual(migrated_hash, record["new_sha256"])
            self.assertEqual(
                config_path.resolve(),
                Path(record["rollback"]["target_path"]).resolve(),
            )
            self.assertEqual(
                backup_path.resolve(),
                Path(record["rollback"]["backup_path"]).resolve(),
            )
            self.assertEqual(
                migrated_hash,
                record["rollback"]["expected_current_sha256"],
            )

            backups_before = sorted(
                (root / ".plot-rag" / "backups").iterdir()
            )
            records_before = sorted(
                (root / ".plot-rag" / "migrations").iterdir()
            )
            repeated = migrate_project_config(root)
            self.assertEqual("current", repeated["status"])
            self.assertFalse(repeated["changed"])
            self.assertEqual(migrated_hash, repeated["sha256"])
            self.assertEqual(migrated_bytes, config_path.read_bytes())
            self.assertEqual(
                backups_before,
                sorted((root / ".plot-rag" / "backups").iterdir()),
            )
            self.assertEqual(
                records_before,
                sorted((root / ".plot-rag" / "migrations").iterdir()),
            )

            rollback = record["rollback"]
            self.assertEqual(
                rollback["expected_current_sha256"],
                hashlib.sha256(Path(rollback["target_path"]).read_bytes()).hexdigest(),
            )
            Path(rollback["target_path"]).write_bytes(
                Path(rollback["backup_path"]).read_bytes()
            )
            self.assertEqual(source_bytes, config_path.read_bytes())
            restored = migrate_project_config(root)
            self.assertEqual(old_hash, restored["old_sha256"])
            self.assertEqual(migrated_hash, restored["new_sha256"])

    def test_v2_config_migration_preserves_explicit_init_schema(self) -> None:
        template = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v2.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            for schema_version in (
                "auto",
                "plot-rag-init/v1",
                "plot-rag-init/v2",
            ):
                with self.subTest(schema_version=schema_version):
                    root = Path(temporary) / schema_version.replace("/", "-")
                    config_path = root / ".plot-rag" / "config.json"
                    config_path.parent.mkdir(parents=True)
                    legacy = json.loads(
                        json.dumps(template, ensure_ascii=False)
                    )
                    legacy["initialization"] = {
                        "schema_version": schema_version,
                    }
                    config_path.write_text(
                        json.dumps(
                            legacy,
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )

                    migrate_project_config(root)
                    migrated = json.loads(
                        config_path.read_text(encoding="utf-8")
                    )
                    self.assertEqual(
                        schema_version,
                        migrated["initialization"]["schema_version"],
                    )

    def test_config_migration_receipts_accept_directory_aliases(
        self,
    ) -> None:
        template = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v2.json").read_text(
                encoding="utf-8"
            )
        )
        source = json.dumps(
            template,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            real_parent = Path(temporary) / "real-parent"
            real_root = real_parent / "novel"
            real_config = real_root / ".plot-rag" / "config.json"
            real_config.parent.mkdir(parents=True)
            real_config.write_bytes(source)
            alias_parent = Path(temporary) / "alias-parent"
            if os.name == "nt":
                completed = subprocess.run(
                    [
                        "cmd.exe",
                        "/d",
                        "/c",
                        "mklink",
                        "/J",
                        alias_parent.name,
                        real_parent.name,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    cwd=temporary,
                )
                if completed.returncode != 0:
                    self.skipTest(
                        "directory junction unavailable: "
                        + completed.stderr.decode(errors="replace")
                    )
            else:
                try:
                    alias_parent.symlink_to(
                        real_parent,
                        target_is_directory=True,
                    )
                except OSError as exc:
                    self.skipTest(f"directory symlink unavailable: {exc}")

            try:
                alias_root = alias_parent / "novel"
                alias_config = alias_root / ".plot-rag" / "config.json"
                migrated = migrate_project_config(alias_root)
                record_path = Path(migrated["migration_record"])
                record = json.loads(
                    record_path.read_text(encoding="utf-8")
                )
                recorded_target = Path(
                    record["rollback"]["target_path"]
                )
                self.assertNotEqual(
                    str(alias_config),
                    str(recorded_target),
                )
                self.assertEqual(
                    alias_config.resolve(),
                    recorded_target.resolve(),
                )
                self.assertTrue(alias_config.samefile(recorded_target))
                self.assertEqual(
                    Path(migrated["backup_path"]).resolve(),
                    Path(record["rollback"]["backup_path"]).resolve(),
                )
            finally:
                if alias_parent.exists() or alias_parent.is_symlink():
                    if os.name == "nt":
                        alias_parent.rmdir()
                    else:
                        alias_parent.unlink()

    def test_initialization_apply_rejects_accept_without_materialization(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ContinuityError) as raised:
                apply_initialization_proposal(
                    Path(temporary) / "novel",
                    "initp-fixture",
                    approval_id="approval-fixture",
                    expected_canon_revision=0,
                    idempotency_key="no-materialize",
                    materialize=False,
                )
        self.assertEqual("MATERIALIZATION_REQUIRED", raised.exception.code)

    def make_project(self, base: Path) -> Path:
        root = base / "novel"
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
            "event_experience": {
                "enabled": False,
            },
        }
        (root / ".plot-rag" / "config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return root

    @staticmethod
    def continuity_write_counts(root: Path) -> dict[str, int]:
        tables = (
            "entities",
            "entity_aliases",
            "mention_resolutions",
            "artifacts",
            "proposals",
            "proposal_issues",
            "idempotency_records",
        )
        service = ContinuityService(root)
        with service.store.read_connection() as connection:
            return {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                )
                for table in tables
            }

    @staticmethod
    def turn_record(root: Path, request_id: str) -> dict[str, object]:
        service = ContinuityService(root)
        with service.store.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM turns WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise AssertionError(f"turn not found: {request_id}")
            return dict(row)

    @staticmethod
    def lifecycle_identity() -> dict[str, object]:
        return {
            "intent_contract_hash": "1" * 64,
            "event_seed_manifest_hash": "2" * 64,
            "experience_contract_hashes": ["4" * 64, "3" * 64],
            "event_experience_control_revision": 7,
            "event_seed_references": [
                {
                    "event_seed_id": "event-seed-b",
                    "event_seed_revision": 2,
                },
                {
                    "event_seed_id": "event-seed-a",
                    "event_seed_revision": 1,
                },
            ],
        }

    @staticmethod
    def normalized_lifecycle_identity() -> dict[str, object]:
        return {
            "intent_contract_hash": "1" * 64,
            "event_seed_manifest_hash": "2" * 64,
            "experience_contract_hashes": ["3" * 64, "4" * 64],
            "event_experience_control_revision": 7,
            "event_seed_references": [
                {
                    "event_seed_id": "event-seed-a",
                    "event_seed_revision": 1,
                },
                {
                    "event_seed_id": "event-seed-b",
                    "event_seed_revision": 2,
                },
            ],
        }

    @staticmethod
    def enable_event_experience(root: Path) -> None:
        config_path = root / ".plot-rag" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["event_experience"] = {
            "enabled": True,
            "required_before_event_design": True,
        }
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def enable_advantage_stop(root: Path) -> None:
        config_path = root / ".plot-rag" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["advantage"] = {
            "enabled": True,
            "shadow": False,
            "schema_version": "plot-rag-advantage/v1",
            "strict_runtime_validation": True,
            "readable_projection": False,
            "mandatory_context": False,
        }
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def advantage_event(
        event_type: str,
        quote: str,
        **fields: object,
    ) -> dict[str, object]:
        return {
            "schema_version": "plot-rag-advantage/v1",
            "event_type": event_type,
            "evidence": {"quote": quote},
            **fields,
        }

    @staticmethod
    def advantage_candidate(
        event_type: str,
        action: str,
        evidence: str,
        *,
        subject_kind: str,
        subject_mention: str,
        objects: list[dict[str, str]] | None = None,
        changes: dict[str, object] | None = None,
        scope: str = "current",
        knowledge_plane: str = "objective",
        ordinal: int = 1,
    ) -> dict[str, object]:
        return {
            "schema_version": "plot-rag-delta/v4",
            "event_type": event_type,
            "action": action,
            "subject": {
                "kind": subject_kind,
                "mention": subject_mention,
            },
            "objects": list(objects or []),
            "changes": dict(changes or {}),
            "scope": scope,
            "story_coordinate": {
                "calendar_id": "story-main",
                "ordinal": ordinal,
            },
            "knowledge_plane": knowledge_plane,
            "confidence": 0.99,
            "evidence": evidence,
            "effective_at": None,
            "ambiguity": None,
        }

    @staticmethod
    def lock_event_experience_identity(
        root: Path,
        *,
        prompt: str,
        artifact_id: str,
        chapter_no: int,
        scene_index: int,
        suffix: str,
    ) -> dict[str, object]:
        intent_values = {
            "problem_to_solve": "让主角在压力中验证金手指并保住退路",
            "expected_deliverable": "一个可执行、可核验的事件链",
            "reader_experience": "压迫、发现能力、短暂释放与代价余悸",
            "protagonist_drive_conflict": "主角优先保命，对手持续压缩退路",
            "scope_endpoint": "推进到主角换取一次局部主动",
            "success_criteria": "形成不可逆状态变化并留下后续压力",
            "hard_constraints": "不改写 accepted 事实，不让主角舍己",
            "model_autonomy": "模型可决定场景实现与次级冲突",
        }
        gate = ensure_locked_manifest(
            root,
            prompt=prompt,
            artifact_context={
                "artifact_id": artifact_id,
                "branch_id": "main",
                "chapter_no": chapter_no,
                "scene_index": scene_index,
                "event_seeds": [
                    {
                        "dependency_order": 1,
                        "dramatic_function": "验证金手指能力并暴露一项代价",
                    }
                ],
            },
            intent_contract={
                "status": "EXECUTING",
                "grill_session_id": f"grill-{suffix}",
                "revision": 1,
                "contract": {
                    "schema_version": "plot-rag-intent/v1",
                    "task_family": "plot",
                    "fields": {
                        field: {
                            "value": value,
                            "source": "user_answer",
                        }
                        for field, value in intent_values.items()
                    },
                },
            },
            session_identity=f"experience-session-{suffix}",
            turn_identity=f"experience-turn-{suffix}",
        )
        manifest = dict(gate["manifest"])
        contracts = [
            dict(item) for item in manifest.get("contracts") or []
        ]
        return {
            "intent_contract_hash": str(
                manifest["source_intent_contract_hash"]
            ),
            "event_seed_manifest_hash": str(
                manifest["event_seed_manifest_hash"]
            ),
            "experience_contract_hashes": sorted(
                {
                    str(item["contract_hash"])
                    for item in contracts
                }
            ),
            "event_experience_control_revision": int(
                manifest["control_revision"]
            ),
            "event_seed_references": sorted(
                [
                    {
                        "event_seed_id": str(item["event_seed_id"]),
                        "event_seed_revision": int(
                            item["event_seed_revision"]
                        ),
                    }
                    for item in contracts
                ],
                key=lambda item: (
                    str(item["event_seed_id"]),
                    int(item["event_seed_revision"]),
                ),
            ),
        }

    @staticmethod
    def set_prepare_v2(
        root: Path,
        *,
        enabled: bool,
        shadow: bool,
    ) -> None:
        config_path = root / ".plot-rag" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["performance"] = {
            **dict(config.get("performance") or {}),
            "prepare_v2": {
                **dict(
                    (config.get("performance") or {}).get(
                        "prepare_v2"
                    )
                    or {}
                ),
                "enabled": enabled,
                "shadow": shadow,
            },
        }
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def extraction_proposal_binding(
        prepared: dict[str, object],
        assistant_text: str,
        *,
        artifact_revision: int = 1,
    ) -> dict[str, object]:
        lifecycle = dict(prepared.get("lifecycle_identity") or {})
        return {
            "extraction_job_id": "extraction-job-fixture",
            "job_binding_hash": "a" * 64,
            "receipt_id": str(prepared["receipt_id"]),
            "request_id": str(prepared["request_id"]),
            "assistant_sha256": hashlib.sha256(
                assistant_text.encode("utf-8")
            ).hexdigest(),
            "prompt_hash": str(prepared["prompt_hash"]),
            "retrieved_context_digest": str(
                prepared["retrieved_context_digest"]
            ),
            "prepared_canon_revision": int(
                prepared["prepared_canon_revision"]
            ),
            "active_projection_hash": str(
                prepared["active_projection_hash"]
            ),
            "intent_contract_hash": str(
                lifecycle.get("intent_contract_hash") or ""
            ),
            "event_seed_manifest_hash": str(
                lifecycle.get("event_seed_manifest_hash") or ""
            ),
            "event_experience_control_revision": int(
                lifecycle.get(
                    "event_experience_control_revision",
                    0,
                )
            ),
            "event_seed_references": list(
                lifecycle.get("event_seed_references") or []
            ),
            "experience_contract_hashes": list(
                lifecycle.get("experience_contract_hashes") or []
            ),
            "artifact_context": {
                **dict(prepared["artifact_context"]),
                "artifact_revision": artifact_revision,
                "_plot_rag_v15": {
                    "extraction_execution_mode": "async",
                    "fixture": True,
                },
            },
        }

    @classmethod
    def lifecycle_manifest(cls) -> dict[str, object]:
        identity = cls.normalized_lifecycle_identity()
        return {
            "event_seed_manifest_hash": identity[
                "event_seed_manifest_hash"
            ],
            "control_revision": identity[
                "event_experience_control_revision"
            ],
            "source_intent_contract_hash": identity[
                "intent_contract_hash"
            ],
            "contracts": [
                {"contract_hash": item}
                for item in identity["experience_contract_hashes"]
            ],
        }

    @classmethod
    def verified_lifecycle_manifest(cls) -> dict[str, object]:
        manifest = cls.lifecycle_manifest()
        return {
            "action": "verified",
            "ready": True,
            "zero_remote": True,
            "manifest": manifest,
            "control_revision": manifest["control_revision"],
        }

    def test_longform_status_separates_query_policy_and_bounds_run_payloads(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            config_path = root / ".plot-rag" / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["craft"] = {
                "enabled": True,
                "auto_retrieve": True,
                "use_embedding": True,
                "use_rerank": True,
            }
            config["remote"]["embedding"] = {
                "enabled": True,
                "model": "status-embedding-v1",
                "api_key_env": "PLOT_RAG_EMBED_API_KEY",
            }
            config["remote"]["rerank"] = {
                "enabled": True,
                "model": "status-rerank-v1",
                "api_key_env": "PLOT_RAG_RERANK_API_KEY",
            }
            config_path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            journal = ProjectionJournal(
                root / ".plot-rag" / "projection-runs.v1.sqlite3"
            )
            for ordinal in range(3):
                journal.run(
                    "snapshot",
                    {
                        "commit_id": f"status-commit-{ordinal}",
                        "canon_status": "accepted",
                        "events": [{"text": "x" * 120_000}],
                    },
                    lambda payload: {
                        "commit_id": payload["commit_id"],
                        "status": "ok",
                    },
                )

            with patch.dict(
                os.environ,
                {
                    "PLOT_RAG_EMBED_API_KEY": "fixture-key",
                    "PLOT_RAG_RERANK_API_KEY": "fixture-key",
                },
            ):
                status = longform_status(root)

            self.assertFalse(
                status["index"]["prepare_refresh"]["schema"][
                    "embedding_enabled"
                ]
            )
            self.assertEqual(
                {
                    "embedding_requested": True,
                    "embedding_enabled": True,
                    "embedding_model": "status-embedding-v1",
                    "rerank_requested": True,
                    "rerank_enabled": True,
                    "rerank_model": "status-rerank-v1",
                },
                status["index"]["query_policy"],
            )
            self.assertEqual(3, status["projection_run_summary"]["total_count"])
            self.assertEqual(
                3,
                status["projection_run_summary"]["returned_count"],
            )
            self.assertFalse(
                status["projection_run_summary"]["payloads_included"]
            )
            self.assertTrue(
                all(
                    "input_json" not in row and row["input_bytes"] > 100_000
                    for row in status["projection_runs"]
                )
            )
            self.assertLess(
                len(json.dumps(status, ensure_ascii=False)),
                30_000,
            )

    def test_longform_status_keeps_projection_rows_and_count_in_one_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            database = root / ".plot-rag" / "projection-runs.v1.sqlite3"
            journal = ProjectionJournal(database)
            for ordinal in range(10):
                journal.run(
                    "snapshot",
                    {
                        "commit_id": f"snapshot-status-{ordinal}",
                        "canon_status": "accepted",
                    },
                    lambda payload: {
                        "commit_id": payload["commit_id"],
                        "status": "ok",
                    },
                )

            original_connection = projection_module._ClosingConnection
            prune_injected = False

            class PruneAfterRowsConnection(original_connection):
                prune_after_close = False

                def execute(
                    self,
                    sql: str,
                    parameters: object = (),
                ) -> sqlite3.Cursor:
                    cursor = super().execute(sql, parameters)
                    normalized = " ".join(sql.split())
                    if (
                        "FROM projection_runs" in normalized
                        and "ORDER BY started_at" in normalized
                        and "WHERE status = 'running'" not in normalized
                    ):
                        self.prune_after_close = True
                    return cursor

                def __exit__(
                    self,
                    exc_type: object,
                    exc_value: object,
                    traceback: object,
                ) -> bool:
                    nonlocal prune_injected
                    should_prune = self.prune_after_close and not prune_injected
                    result = super().__exit__(
                        exc_type,
                        exc_value,
                        traceback,
                    )
                    if should_prune:
                        prune_injected = True
                        ProjectionJournal(
                            database,
                            auto_recover=False,
                        ).prune_derived_runs(
                            keep_successful_per_projection=1
                        )
                    return result

            with patch.object(
                projection_module,
                "_ClosingConnection",
                PruneAfterRowsConnection,
            ):
                status = longform_status(root)

            self.assertTrue(prune_injected)
            self.assertEqual(10, len(status["projection_runs"]))
            self.assertEqual(
                10,
                status["projection_run_summary"]["returned_count"],
            )
            self.assertEqual(
                10,
                status["projection_run_summary"]["total_count"],
            )
            self.assertEqual(1, journal.run_count())

    @staticmethod
    def tree_fingerprints(root: Path) -> dict[str, tuple[object, ...]]:
        result: dict[str, tuple[object, ...]] = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if path.is_dir():
                result[relative] = ("directory",)
                continue
            stat = path.stat()
            result[relative] = (
                "file",
                stat.st_size,
                stat.st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        return result

    @staticmethod
    def enable_test_embedding(root: Path) -> None:
        config_path = root / ".plot-rag" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["remote"]["embedding"] = {
            "enabled": True,
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "fixture-embedding-v1",
            "api_key_env": "PLOT_RAG_EMBED_API_KEY",
            "api_key_required": True,
        }
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def accept_projection_fixture(
        root: Path,
        *,
        fixture_id: str,
    ) -> dict[str, object]:
        service = ContinuityService(root)
        entity = service.register_entity(
            "world",
            f"投影测试-{fixture_id}",
        )
        revision = service.get_canon_revisions()["active"]
        proposal = service.save_proposal(
            events=[
                {
                    "event_type": "state",
                    "entity_id": entity["entity_id"],
                    "field": "projection_fixture",
                    "value": fixture_id,
                }
            ],
            artifact_id=f"projection-{fixture_id}",
            artifact_stage="final",
            prepared_canon_revision=revision,
        )
        grant = issue_host_approval(
            root,
            proposal["proposal_id"],
            expected_canon_revision=revision,
            issuer="unittest-host",
            channel="interactive_test",
        )
        return accept_plot_proposal(
            root,
            proposal["proposal_id"],
            approval_id=grant["grant"]["approval_id"],
            expected_canon_revision=revision,
        )

    @staticmethod
    def accept_continuity_fixture(
        root: Path,
        events: list[dict[str, object]],
        *,
        artifact_id: str,
        chapter_no: int | None = 1,
        scene_index: int | None = 0,
    ) -> dict[str, object]:
        service = ContinuityService(root)
        revision = service.get_canon_revisions()["active"]
        proposal = service.save_proposal(
            events=events,
            artifact_id=artifact_id,
            artifact_stage="final",
            branch_id="main",
            chapter_no=chapter_no,
            scene_index=scene_index,
            prepared_canon_revision=revision,
        )
        grant = issue_host_approval(
            root,
            proposal["proposal_id"],
            expected_canon_revision=revision,
            issuer="unittest-host",
            channel="interactive_test",
        )
        return accept_plot_proposal(
            root,
            proposal["proposal_id"],
            approval_id=grant["grant"]["approval_id"],
            expected_canon_revision=revision,
        )

    def test_artifact_classification_is_fail_closed(self) -> None:
        self.assertEqual(
            "brainstorm",
            infer_artifact_context("继续推进剧情")["artifact_stage"],
        )
        outline = infer_artifact_context("设计第十二章章纲")
        self.assertEqual("outline", outline["artifact_stage"])
        self.assertEqual(12, outline["chapter_no"])
        final = infer_artifact_context("完成第一章最终稿")
        self.assertEqual("final", final["artifact_stage"])
        self.assertEqual(1, final["chapter_no"])

    def test_prepare_receipt_binds_prompt_context_projection_and_telemetry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prompt = "完成第一章最终稿"
            prepared = prepare_plot_turn(
                root,
                prompt,
                session_id="identity-session",
                turn_id="identity-turn",
            )
            turn = self.turn_record(root, prepared["request_id"])

            self.assertEqual(
                hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                prepared["prompt_hash"],
            )
            self.assertEqual(
                prepared["prompt_hash"],
                turn["prompt_hash"],
            )
            self.assertEqual(
                prepared["retrieved_context_digest"],
                turn["retrieved_context_digest"],
            )
            self.assertEqual(
                prepared["active_projection_hash"],
                turn["active_projection_hash"],
            )
            self.assertEqual(
                prepared["context"],
                turn["prepared_context_text"],
            )
            self.assertEqual(
                prepared["prepared_canon_revision"],
                turn["prepared_canon_revision"],
            )
            self.assertEqual(
                {
                    "prompt_hash": prepared["prompt_hash"],
                    "retrieved_context_digest": prepared[
                        "retrieved_context_digest"
                    ],
                    "prepared_canon_revision": prepared[
                        "prepared_canon_revision"
                    ],
                    "active_projection_hash": prepared[
                        "active_projection_hash"
                    ],
                    "lifecycle_identity": {},
                },
                prepared["identity"],
            )
            self.assertEqual({}, prepared["lifecycle_identity"])
            self.assertEqual(
                {},
                json.loads(str(turn["lifecycle_identity_json"])),
            )
            telemetry = json.loads(turn["prepare_telemetry_json"])
            for key in (
                "authority_refresh_ms",
                "exact_state_ms",
                "context_assembly_ms",
                "prepare_total_ms",
                "identity_retries",
            ):
                self.assertIn(key, telemetry)
            self.assertEqual(0, telemetry["identity_retries"])

    def test_strict_prepare_defers_legacy_authority_preflight_and_refreshes_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            original_refresh = v1_runtime_module.refresh_longform_index
            refresh_calls: list[Path] = []

            def record_refresh(
                project_root: Path | str,
                *,
                with_embeddings: bool = False,
            ) -> dict[str, object]:
                refresh_calls.append(Path(project_root).resolve())
                return original_refresh(
                    project_root,
                    with_embeddings=with_embeddings,
                )

            with (
                patch(
                    "v1_runtime.state_rag._authority_preflight",
                    side_effect=AssertionError(
                        "strict prepare must defer legacy authority preflight"
                    ),
                ) as preflight,
                patch(
                    "v1_runtime.refresh_longform_index",
                    side_effect=record_refresh,
                ) as refresh,
            ):
                prepared = prepare_plot_turn(
                    root,
                    "完成第一章最终稿",
                    session_id="authority-once-session",
                    turn_id="authority-once-turn",
                )

            preflight.assert_not_called()
            refresh.assert_called_once()
            self.assertEqual([root.resolve()], refresh_calls)
            self.assertEqual(
                "DEFERRED_TO_LONGFORM",
                prepared["authority"]["status"],
            )
            self.assertIn(
                "authority_status: DEFERRED_TO_LONGFORM",
                prepared["context"],
            )
            self.assertEqual(
                "ready",
                prepared["longform"]["index"]["status"],
            )
            self.assertIn(
                "[WEBNOVEL_CONTINUITY_CONTRACT]",
                prepared["context"],
            )
            turn = self.turn_record(root, str(prepared["request_id"]))
            self.assertEqual(
                prepared["retrieved_context_digest"],
                turn["retrieved_context_digest"],
            )

    def test_prepare_v2_rollout_matrix_uses_the_frozen_chosen_path(
        self,
    ) -> None:
        cases = (
            (False, False, ["v1"], "v1"),
            (False, True, ["v1", "v2"], "v1"),
            (True, True, ["v1", "v2"], "v1"),
            (True, False, ["v2"], "v2"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for enabled, shadow, executed, chosen in cases:
                with self.subTest(enabled=enabled, shadow=shadow):
                    root = self.make_project(
                        base / f"{int(enabled)}-{int(shadow)}"
                    )
                    self.set_prepare_v2(
                        root,
                        enabled=enabled,
                        shadow=shadow,
                    )
                    result = v1_runtime_module.build_longform_context(
                        root,
                        "推演战斗并核对位置、道具、力量、时间与伏笔",
                    )
                    rollout = result["telemetry"]["prepare_v2"]

                    self.assertEqual(chosen, rollout["chosen_path"])
                    self.assertEqual(
                        executed,
                        rollout["executed_paths"],
                    )
                    self.assertEqual(
                        rollout,
                        result["contract"]["prepare_v2_rollout"],
                    )
                    self.assertEqual(
                        rollout,
                        result["index"]["prepare_v2_rollout"],
                    )
                    for path in executed:
                        self.assertEqual("ok", rollout[path]["status"])
                    for path in {"v1", "v2"}.difference(executed):
                        self.assertEqual(
                            "not_run",
                            rollout[path]["status"],
                        )
                    if len(executed) == 2:
                        self.assertEqual(
                            "equivalent",
                            rollout["comparison"]["status"],
                        )
                        self.assertTrue(
                            rollout["comparison"]["equivalent"]
                        )
                    else:
                        self.assertEqual(
                            "not_compared",
                            rollout["comparison"]["status"],
                        )

    def test_prepare_v2_semantic_equivalence_ignores_ranking_scores(
        self,
    ) -> None:
        legacy = {
            "contract_version": "plot-rag-context/v1",
            "task": "outline",
            "needs": [
                {
                    "need_index": 0,
                    "category": "current_state",
                    "query": "角色当前状态",
                    "mandatory": True,
                }
            ],
            "mandatory_quotas": {"current_state": 1},
            "missing_mandatory": [],
            "mandatory_shortfall": {},
            "accepted_authority_selected": 1,
            "sections": {
                "current_state": [
                    {
                        "need_index": 0,
                        "chunk_id": "chunk-1",
                        "path": "设定集/角色.md",
                        "ordinal": 0,
                        "start_line": 1,
                        "end_line": 2,
                        "content_sha256": "a" * 64,
                        "role": "setting",
                        "scope_policy": "timeless_candidate",
                        "content": "测试角色甲仍保持清醒。",
                        "base_score": 0.55798946,
                        "score": 0.90326456,
                        "semantic_score": 0.82618688,
                    }
                ]
            },
            "context_text": "测试角色甲仍保持清醒。",
        }
        batched = json.loads(json.dumps(legacy, ensure_ascii=False))
        item = batched["sections"]["current_state"][0]
        item["base_score"] = 0.55790914
        item["score"] = 0.90324849
        item["semantic_score"] = 0.82604840

        comparison = v1_runtime_module._prepare_v2_comparison(
            legacy,
            batched,
        )
        self.assertTrue(comparison["equivalent"])
        self.assertTrue(comparison["sections_equivalent"])
        self.assertTrue(comparison["semantic_equivalent"])

        item["content_sha256"] = "b" * 64
        drift = v1_runtime_module._prepare_v2_comparison(
            legacy,
            batched,
        )
        self.assertFalse(drift["equivalent"])
        self.assertFalse(drift["selected_chunks_equivalent"])

    def test_prepare_v2_shadow_drift_returns_v1_and_binds_one_receipt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            self.set_prepare_v2(
                root,
                enabled=True,
                shadow=True,
            )
            original_build = (
                v1_runtime_module.ContextContractBuilder.build
            )
            search_modes: list[str] = []

            def drift_shadow(
                builder: object,
                prompt: str,
                **kwargs: object,
            ) -> dict[str, object]:
                search_modes.append(str(kwargs["search_mode"]))
                contract = original_build(
                    builder,
                    prompt,
                    **kwargs,
                )
                if kwargs["search_mode"] == "v2":
                    contract = {
                        **contract,
                        "context_text": (
                            str(contract.get("context_text") or "")
                            + "\nSHADOW_ONLY_DRIFT"
                        ),
                    }
                return contract

            with patch.object(
                v1_runtime_module.ContextContractBuilder,
                "build",
                new=drift_shadow,
            ):
                prepared = prepare_plot_turn(
                    root,
                    "完成第一章最终稿",
                    session_id="shadow-drift-session",
                    turn_id="shadow-drift-turn",
                )

            self.assertEqual(["legacy", "v2"], search_modes)
            rollout = prepared["longform"]["telemetry"]["prepare_v2"]
            self.assertEqual("v1", rollout["chosen_path"])
            self.assertEqual("mismatch", rollout["comparison"]["status"])
            self.assertFalse(rollout["comparison"]["equivalent"])
            self.assertFalse(
                rollout["comparison"]["context_text_equivalent"]
            )
            self.assertNotIn("SHADOW_ONLY_DRIFT", prepared["context"])
            turn = self.turn_record(root, str(prepared["request_id"]))
            self.assertEqual(
                prepared["context"],
                turn["prepared_context_text"],
            )
            service = ContinuityService(root)
            self.assertEqual(
                {"head": 0, "active": 0},
                service.get_canon_revisions(),
            )
            self.assertEqual([], service.list_proposals())
            with service.store.read_connection() as connection:
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM turns"
                    ).fetchone()[0],
                )

    def test_prepare_v2_shadow_failure_returns_v1_with_diagnostic(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            self.set_prepare_v2(
                root,
                enabled=False,
                shadow=True,
            )
            original_build = (
                v1_runtime_module.ContextContractBuilder.build
            )

            def fail_shadow(
                builder: object,
                prompt: str,
                **kwargs: object,
            ) -> dict[str, object]:
                if kwargs["search_mode"] == "v2":
                    raise RuntimeError("fixture v2 failure")
                return original_build(builder, prompt, **kwargs)

            with patch.object(
                v1_runtime_module.ContextContractBuilder,
                "build",
                new=fail_shadow,
            ):
                prepared = prepare_plot_turn(
                    root,
                    "完成第一章最终稿",
                    session_id="shadow-failure-session",
                    turn_id="shadow-failure-turn",
                )

            self.assertNotEqual("failed", prepared["status"])
            self.assertEqual("ready", prepared["longform"]["status"])
            rollout = prepared["longform"]["telemetry"]["prepare_v2"]
            self.assertEqual("v1", rollout["chosen_path"])
            self.assertEqual("ok", rollout["v1"]["status"])
            self.assertEqual("error", rollout["v2"]["status"])
            self.assertEqual(
                "RuntimeError",
                rollout["v2"]["error_type"],
            )
            self.assertEqual(
                "v2_error",
                rollout["comparison"]["status"],
            )
            self.assertNotIn(
                "fixture v2 failure",
                prepared["context"],
            )

    def test_exact_state_short_circuit_cache_evidence_and_projection_key(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            service = ContinuityService(root)
            actor = service.register_entity("character", "测试角色甲")[
                "entity_id"
            ]
            self.accept_continuity_fixture(
                root,
                [
                    {
                        "event_type": "state",
                        "entity_id": actor,
                        "field": "current_goal",
                        "value": "查明南站异常",
                    },
                    {
                        "event_type": "state",
                        "entity_id": actor,
                        "field": "injury",
                        "value": "左臂轻伤",
                    },
                ],
                artifact_id="exact-state-seed",
            )
            prompt = "继续推演测试角色甲当前状态"

            with (
                patch.object(
                    AuthorityIndex,
                    "search",
                    side_effect=AssertionError(
                        "exact hit must skip per-need authority search"
                    ),
                ) as search,
                patch.object(
                    AuthorityIndex,
                    "search_many",
                    side_effect=AssertionError(
                        "exact hit must skip batch authority search"
                    ),
                ) as search_many,
            ):
                first = v1_runtime_module.build_longform_context(root, prompt)
            search.assert_not_called()
            search_many.assert_not_called()
            first_exact = first["exact_state"]
            self.assertEqual("HIT_CONFIRMED", first_exact["decision"])
            self.assertEqual("stored", first_exact["cache_status"])
            self.assertEqual([0], first_exact["skipped_need_indices"])
            self.assertNotIn(
                "current_state",
                first["contract"]["missing_mandatory"],
            )
            self.assertNotIn(
                "accepted_authority",
                first["contract"]["missing_mandatory"],
            )
            self.assertTrue(
                (root / ".plot-rag" / "exact-state-cache.v1.sqlite3").is_file()
            )

            second = v1_runtime_module.build_longform_context(root, prompt)
            self.assertEqual("hit", second["exact_state"]["cache_status"])
            self.assertEqual(
                first_exact["cache_key"],
                second["exact_state"]["cache_key"],
            )

            evidence = v1_runtime_module.build_longform_context(
                root,
                "引用原文证据说明测试角色甲当前状态",
            )
            self.assertTrue(evidence["exact_state"]["evidence_request"])
            self.assertEqual(
                [],
                evidence["exact_state"]["skipped_need_indices"],
            )
            self.assertEqual(
                "source_evidence_requires_authority",
                evidence["exact_state"]["reason"],
            )
            self.assertGreaterEqual(
                evidence["contract"]["accepted_authority_selected"],
                1,
            )

            self.accept_continuity_fixture(
                root,
                [
                    {
                        "event_type": "state",
                        "entity_id": actor,
                        "field": "current_goal",
                        "value": "进入南站封锁区",
                    }
                ],
                artifact_id="exact-state-advance",
            )
            advanced = v1_runtime_module.build_longform_context(root, prompt)
            self.assertEqual("stored", advanced["exact_state"]["cache_status"])
            self.assertNotEqual(
                first_exact["cache_key"],
                advanced["exact_state"]["cache_key"],
            )

    def test_exact_state_empty_authority_never_confirms_a_miss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            (root / "正文" / "第一章.md").unlink()

            result = v1_runtime_module.build_longform_context(
                root,
                "继续推演当前状态",
            )

            exact = result["exact_state"]
            self.assertEqual("empty", exact["authority_health"])
            self.assertEqual("MISS_UNCONFIRMED", exact["decision"])
            self.assertFalse(exact["miss_confirmed"])
            self.assertEqual([], exact["skipped_need_indices"])
            self.assertEqual(
                "exact_state_insufficient_authority_empty",
                exact["reason"],
            )

    def test_item_mandatory_context_uses_complete_accepted_lifecycle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            config_path = root / ".plot-rag" / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["items"] = {
                "strict_runtime_validation": True,
                "power_binding_bridge": True,
                "readable_projection": False,
            }
            config_path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            service = ContinuityService(root)
            actor = service.register_entity("character", "测试角色甲")[
                "entity_id"
            ]

            def item_event(
                event_type: str,
                ordinal: int,
                quote: str,
                **fields: object,
            ) -> dict[str, object]:
                return {
                    "schema_version": "plot-rag-delta/v4",
                    "event_type": event_type,
                    "story_coordinate": {
                        "calendar_id": "mandatory-item-test",
                        "ordinal": ordinal,
                    },
                    "knowledge_plane": "objective",
                    "evidence": {"quote": quote},
                    **fields,
                }

            events = [
                item_event(
                    "item_spec",
                    1,
                    "黑刃被明确记录为武器。",
                    action="define",
                    spec_type="item_definition",
                    spec_id="definition_blade",
                    definition={
                        "name": "黑刃",
                        "item_kind": "weapon",
                        "stack_policy": "non_stackable",
                        "uniqueness_policy": "unique_definition",
                        "max_durability": 10,
                    },
                ),
                item_event(
                    "item_spec",
                    1,
                    "黑刃具有切割功能。",
                    action="define",
                    spec_type="function_definition",
                    spec_id="function_cut",
                    definition={
                        "item_definition_id": "definition_blade",
                        "effect_owner": "inline",
                        "inline_effects": [{"kind": "cut"}],
                        "charges": 2,
                    },
                ),
                item_event(
                    "item_spec",
                    1,
                    "切割功能绑定到黑刃。",
                    action="define",
                    spec_type="function_binding",
                    spec_id="binding_cut",
                    definition={
                        "item_definition_id": "definition_blade",
                        "function_id": "function_cut",
                    },
                ),
                item_event(
                    "item_instance",
                    2,
                    "测试角色甲取得黑刃实例。",
                    action="instantiate",
                    subject_type="item_instance",
                    subject_id="instance_blade",
                    item_instance_id="instance_blade",
                    item_definition_id="definition_blade",
                    attributes={},
                ),
                item_event(
                    "item_custody",
                    2,
                    "黑刃归测试角色甲所有并由他携带。",
                    action="acquire",
                    subject_type="item_instance",
                    subject_id="instance_blade",
                    item_instance_id="instance_blade",
                    to_legal_owner_entity_id=actor,
                    to_custodian_entity_id=actor,
                    to_carrier_entity_id=actor,
                ),
                item_event(
                    "item_runtime",
                    3,
                    "测试角色甲把黑刃装备在右手。",
                    action="equip",
                    subject_type="item_instance",
                    subject_id="instance_blade",
                    item_instance_id="instance_blade",
                    actor_entity_id=actor,
                    slot_key="right_hand",
                    delta={},
                ),
                item_event(
                    "item_use",
                    4,
                    "测试角色甲发动黑刃的切割功能。",
                    action="use",
                    subject_type="item_instance",
                    subject_id="instance_blade",
                    item_instance_id="instance_blade",
                    actor_entity_id=actor,
                    function_id="function_cut",
                    delta={},
                ),
                item_event(
                    "item_observation",
                    4,
                    "测试角色甲看到刃口出现一道浅痕。",
                    action="observe",
                    subject_type="item_instance",
                    subject_id="instance_blade",
                    item_instance_id="instance_blade",
                    observer_entity_id=actor,
                    function_id="function_cut",
                    knowledge_plane="actor_belief",
                    observation={"edge": "shallow_mark"},
                ),
            ]
            self.accept_continuity_fixture(
                root,
                events,
                artifact_id="mandatory-item-lifecycle",
            )

            result = v1_runtime_module.build_longform_context(
                root,
                "推演测试角色甲使用黑刃战斗并脱困",
            )

            item_context = result["item_context"]
            self.assertTrue(item_context["required"])
            self.assertEqual("ready", item_context["status"])
            self.assertEqual([], item_context["errors"])
            self.assertEqual(1, item_context["accepted_record_count"])
            self.assertGreaterEqual(
                item_context["accepted_inventory_count"],
                1,
            )
            record = item_context["records"][0]
            self.assertEqual("instance_blade", record["subject_id"])
            self.assertEqual(
                "right_hand",
                record["runtime"]["slot_key"],
            )
            self.assertEqual(1, len(record["history"]))
            self.assertEqual(1, len(record["observations"]))
            self.assertEqual(
                "function_cut",
                record["functions"][0]["function"]["function_id"],
            )
            self.assertIn("[ACCEPTED_ITEM_CONTEXT]", result["context"])
            self.assertIn("function_cut", result["context"])
            self.assertIn("right_hand", result["context"])
            self.assertGreaterEqual(result["item_context_ms"], 0.0)
            budget = result["context_budget"]["item_context"]
            self.assertTrue(budget["required"])
            self.assertTrue(budget["included"])
            self.assertGreater(budget["reserved_chars"], 0)

    def test_item_mandatory_context_empty_projection_is_explicitly_unknown(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))

            result = v1_runtime_module.build_longform_context(
                root,
                "推演战斗中使用道具脱困",
            )

            item_context = result["item_context"]
            self.assertTrue(item_context["required"])
            self.assertEqual("empty", item_context["status"])
            self.assertEqual([], item_context["records"])
            self.assertIn("[ACCEPTED_ITEM_CONTEXT]", result["context"])
            self.assertIn("accepted 物品投影为空", result["context"])
            self.assertIn("不得从名称、类型或空 attributes 推断功能", result["context"])

            untriggered = v1_runtime_module.build_longform_context(
                root,
                "继续写角色之间的对话",
            )
            self.assertFalse(untriggered["item_context"]["required"])
            self.assertEqual(
                "not_required",
                untriggered["item_context"]["status"],
            )
            self.assertNotIn(
                "[ACCEPTED_ITEM_CONTEXT]",
                untriggered["context"],
            )

    def test_item_context_does_not_infer_function_from_item_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            config_path = root / ".plot-rag" / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["items"] = {
                "strict_runtime_validation": True,
                "power_binding_bridge": True,
                "readable_projection": False,
            }
            config_path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.accept_continuity_fixture(
                root,
                [
                    {
                        "schema_version": "plot-rag-delta/v4",
                        "event_type": "item_spec",
                        "story_coordinate": {
                            "calendar_id": "no-item-inference",
                            "ordinal": 1,
                        },
                        "knowledge_plane": "objective",
                        "evidence": {
                            "quote": "这件物品只被称为万能钥匙。"
                        },
                        "action": "define",
                        "spec_type": "item_definition",
                        "spec_id": "definition_master_key",
                        "definition": {
                            "name": "万能钥匙",
                            "item_kind": "tool",
                            "stack_policy": "non_stackable",
                            "uniqueness_policy": "ordinary",
                        },
                    }
                ],
                artifact_id="mandatory-no-function-inference",
            )

            result = v1_runtime_module.build_longform_context(
                root,
                "设计使用万能钥匙解谜的事件",
            )

            item_context = result["item_context"]
            self.assertEqual("ready", item_context["status"])
            self.assertEqual(1, len(item_context["records"]))
            self.assertEqual(
                [],
                item_context["records"][0]["functions"],
            )
            self.assertIn("definition_master_key", result["context"])
            self.assertNotIn("开锁功能", result["context"])
            self.assertNotIn("inline_effects", result["context"])

    def test_prepare_and_propose_atomically_bind_lifecycle_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            self.enable_event_experience(root)
            expected = self.normalized_lifecycle_identity()
            with patch(
                "v1_runtime.verify_locked_manifest",
                return_value=self.verified_lifecycle_manifest(),
            ) as verify:
                prepared = prepare_plot_turn(
                    root,
                    "完成第一章最终稿",
                    session_id="lifecycle-session",
                    turn_id="lifecycle-turn",
                    lifecycle_identity=self.lifecycle_identity(),
                )
            turn = self.turn_record(root, prepared["request_id"])

            self.assertEqual(expected, prepared["lifecycle_identity"])
            self.assertEqual(
                expected,
                prepared["identity"]["lifecycle_identity"],
            )
            self.assertEqual(
                expected,
                json.loads(str(turn["lifecycle_identity_json"])),
            )
            with (
                patch(
                    "v1_runtime.verify_locked_manifest",
                    return_value=self.verified_lifecycle_manifest(),
                ) as propose_verify,
                patch(
                    "v1_runtime.state_rag._chat_extract",
                    return_value=(
                        [],
                        [],
                        {
                            "status": "ok",
                            "configured": True,
                            "model": "fixture-extractor",
                        },
                    ),
                ),
            ):
                proposed = propose_plot_turn(
                    root,
                    "本轮只验证生命周期身份。",
                    request_id=prepared["receipt_id"],
                )

            self.assertEqual(2, verify.call_count)
            self.assertEqual(2, propose_verify.call_count)
            self.assertEqual(expected, proposed["lifecycle_identity"])
            self.assertEqual(
                expected,
                proposed["identity"]["lifecycle_identity"],
            )
            frozen = ContinuityService(root).inspect_proposal(
                str(proposed["proposal_id"])
            )
            self.assertEqual(
                expected,
                frozen["payload"]["lifecycle_identity"],
            )

    def test_required_event_experience_blocks_before_prepare_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            config_path = root / ".plot-rag" / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config.pop("event_experience", None)
            config_path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            database = root / ".plot-rag" / "state.sqlite3"
            with (
                patch(
                    "v1_runtime.state_rag.prepare_turn",
                    side_effect=AssertionError(
                        "state prepare must not run"
                    ),
                ) as state_prepare,
                patch(
                    "v1_runtime.build_longform_context",
                    side_effect=AssertionError(
                        "longform retrieval must not run"
                    ),
                ) as longform,
            ):
                failed = prepare_plot_turn(
                    root,
                    "完成第一章最终稿",
                    session_id="missing-lifecycle-session",
                    turn_id="missing-lifecycle-turn",
                )

            state_prepare.assert_not_called()
            longform.assert_not_called()
            self.assertEqual("failed", failed["status"])
            self.assertFalse(failed["receipt_created"])
            self.assertFalse(failed["remote_called"])
            self.assertIn(
                "EVENT_EXPERIENCE_BINDING_REQUIRED",
                failed["reason"],
            )
            self.assertFalse(database.exists())

    def test_event_experience_requirement_defaults_and_opt_outs(
        self,
    ) -> None:
        cases = (
            ("v3-missing", 3, None, True),
            ("v3-empty", 3, {}, True),
            ("v3-disabled", 3, {"enabled": False}, False),
            (
                "v3-not-required",
                3,
                {
                    "enabled": True,
                    "required_before_event_design": False,
                },
                False,
            ),
            ("v2-missing", 2, None, False),
            ("v1-missing", 1, None, False),
        )
        for label, config_version, settings, expected in cases:
            with (
                self.subTest(label=label),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = self.make_project(Path(temporary))
                config_path = root / ".plot-rag" / "config.json"
                config = json.loads(config_path.read_text(encoding="utf-8"))
                config["config_version"] = config_version
                if settings is None:
                    config.pop("event_experience", None)
                else:
                    config["event_experience"] = settings
                config_path.write_text(
                    json.dumps(config, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                self.assertEqual(
                    expected,
                    v1_runtime_module._event_experience_required(root),
                )

    def test_lifecycle_identity_tamper_blocks_remote_extract(
        self,
    ) -> None:
        mutations = {
            "intent_contract_hash": lambda value: value.update(
                {"intent_contract_hash": "a" * 64}
            ),
            "event_seed_manifest_hash": lambda value: value.update(
                {"event_seed_manifest_hash": "b" * 64}
            ),
            "experience_contract_hashes": lambda value: value.update(
                {"experience_contract_hashes": ["c" * 64]}
            ),
            "event_experience_control_revision": lambda value: value.update(
                {
                    "event_experience_control_revision": int(
                        value["event_experience_control_revision"]
                    )
                    + 1
                }
            ),
            "event_seed_references": lambda value: value.update(
                {
                    "event_seed_references": [
                        {
                            **dict(value["event_seed_references"][0]),
                            "event_seed_revision": 9,
                        },
                        dict(value["event_seed_references"][1]),
                    ]
                }
            ),
        }
        for label, mutate in mutations.items():
            with (
                self.subTest(field=label),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = self.make_project(Path(temporary))
                self.enable_event_experience(root)
                with patch(
                    "v1_runtime.verify_locked_manifest",
                    return_value=self.verified_lifecycle_manifest(),
                ):
                    prepared = prepare_plot_turn(
                        root,
                        "完成第一章最终稿",
                        session_id=f"tamper-{label}",
                        turn_id=f"tamper-{label}",
                        lifecycle_identity=self.lifecycle_identity(),
                    )
                tampered = self.normalized_lifecycle_identity()
                mutate(tampered)
                with closing(
                    sqlite3.connect(root / ".plot-rag" / "state.sqlite3")
                ) as connection:
                    connection.execute(
                        """
                        UPDATE turns
                        SET lifecycle_identity_json=?
                        WHERE request_id=?
                        """,
                        (
                            json.dumps(
                                tampered,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            prepared["request_id"],
                        ),
                    )
                    connection.commit()
                with patch(
                    "v1_runtime.state_rag._chat_extract",
                    side_effect=AssertionError(
                        "remote extract must not run"
                    ),
                ) as extract:
                    failed = propose_plot_turn(
                        root,
                        "测试角色甲抵达测试城南站。",
                        request_id=prepared["receipt_id"],
                    )

                extract.assert_not_called()
                self.assertEqual("failed", failed["status"])
                self.assertIn(
                    "PREPARED_CONTEXT_MISMATCH",
                    failed["reason"],
                )
                self.assertFalse(
                    failed["identity_check"]["remote_called"]
                )

    def test_propose_revalidates_event_manifest_before_remote_extract(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            self.enable_event_experience(root)
            with patch(
                "v1_runtime.verify_locked_manifest",
                return_value=self.verified_lifecycle_manifest(),
            ):
                prepared = prepare_plot_turn(
                    root,
                    "完成第一章最终稿",
                    session_id="manifest-stale-session",
                    turn_id="manifest-stale-turn",
                    lifecycle_identity=self.lifecycle_identity(),
                )
            with (
                patch(
                    "v1_runtime.verify_locked_manifest",
                    side_effect=RuntimeError("manifest changed"),
                ) as verify,
                patch(
                    "v1_runtime.state_rag._chat_extract",
                    side_effect=AssertionError(
                        "remote extract must not run"
                    ),
                ) as extract,
            ):
                failed = propose_plot_turn(
                    root,
                    "测试角色甲抵达测试城南站。",
                    request_id=prepared["receipt_id"],
                )

            self.assertEqual(1, verify.call_count)
            extract.assert_not_called()
            self.assertEqual("failed", failed["status"])
            self.assertIn(
                "PREPARED_LIFECYCLE_IDENTITY_STALE",
                failed["reason"],
            )
            self.assertFalse(failed["identity_check"]["remote_called"])

    def test_propose_revalidates_event_manifest_after_remote_extract(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            self.enable_event_experience(root)
            verified = self.verified_lifecycle_manifest()
            with patch(
                "v1_runtime.verify_locked_manifest",
                return_value=verified,
            ):
                prepared = prepare_plot_turn(
                    root,
                    "完成第一章最终稿",
                    session_id="manifest-post-session",
                    turn_id="manifest-post-turn",
                    lifecycle_identity=self.lifecycle_identity(),
                )
            with (
                patch(
                    "v1_runtime.verify_locked_manifest",
                    side_effect=[
                        verified,
                        RuntimeError("manifest changed"),
                    ],
                ) as verify,
                patch(
                    "v1_runtime.state_rag._chat_extract",
                    return_value=(
                        [],
                        [],
                        {"status": "ok", "configured": True},
                    ),
                ) as extract,
            ):
                failed = propose_plot_turn(
                    root,
                    "测试角色甲抵达测试城南站。",
                    request_id=prepared["receipt_id"],
                )

            self.assertEqual(2, verify.call_count)
            extract.assert_called_once()
            self.assertEqual("failed", failed["status"])
            self.assertIn(
                "PREPARED_LIFECYCLE_IDENTITY_STALE",
                failed["reason"],
            )
            self.assertTrue(failed["identity_check"]["remote_called"])
            self.assertEqual(
                [],
                ContinuityService(root).list_proposals(),
            )

    def test_prepare_retries_when_revision_projection_identity_drifts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            first = {
                "head_canon_revision": 0,
                "active_canon_revision": 0,
                "active_projection_hash": "projection-before",
            }
            second = {
                "head_canon_revision": 1,
                "active_canon_revision": 1,
                "active_projection_hash": "projection-after",
            }
            with patch.object(
                v1_runtime_module,
                "_active_continuity_identity",
                side_effect=[first, second, second, second],
            ) as identity:
                prepared = prepare_plot_turn(
                    root,
                    "继续推进第一章正文",
                    session_id="drift-session",
                    turn_id="drift-turn",
                )

            self.assertEqual(4, identity.call_count)
            self.assertEqual(1, prepared["telemetry"]["identity_retries"])
            self.assertEqual(1, prepared["prepared_canon_revision"])
            self.assertEqual(
                "projection-after",
                prepared["active_projection_hash"],
            )
            self.assertIn(
                "active_projection_hash: projection-after",
                prepared["context"],
            )

    def test_strict_propose_rejects_prompt_mismatch_before_remote_extract(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prepared = prepare_plot_turn(
                root,
                "完成第一章最终稿",
                session_id="prompt-bind-session",
                turn_id="prompt-bind-turn",
            )
            with patch(
                "v1_runtime.state_rag._chat_extract",
                side_effect=AssertionError("remote extract must not run"),
            ) as extract:
                failed = propose_plot_turn(
                    root,
                    "测试角色甲抵达测试城南站。",
                    request_id=prepared["receipt_id"],
                    prompt="不同的剧情任务",
                )

            extract.assert_not_called()
            self.assertEqual("failed", failed["status"])
            self.assertIn("PREPARED_PROMPT_MISMATCH", failed["reason"])
            self.assertFalse(failed["identity_check"]["remote_called"])

    def test_strict_propose_rejects_context_tamper_before_remote_extract(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prepared = prepare_plot_turn(
                root,
                "完成第一章最终稿",
                session_id="context-bind-session",
                turn_id="context-bind-turn",
            )
            with closing(
                sqlite3.connect(root / ".plot-rag" / "state.sqlite3")
            ) as connection:
                connection.execute(
                    """
                    UPDATE turns
                    SET prepared_context_text=prepared_context_text || ?
                    WHERE request_id=?
                    """,
                    ("\nTAMPERED_CONTEXT", prepared["request_id"]),
                )
                connection.commit()
            with patch(
                "v1_runtime.state_rag._chat_extract",
                side_effect=AssertionError("remote extract must not run"),
            ) as extract:
                failed = propose_plot_turn(
                    root,
                    "测试角色甲抵达测试城南站。",
                    request_id=prepared["receipt_id"],
                )

            extract.assert_not_called()
            self.assertEqual("failed", failed["status"])
            self.assertIn("PREPARED_CONTEXT_MISMATCH", failed["reason"])
            self.assertFalse(failed["identity_check"]["remote_called"])

    def test_strict_propose_rejects_stale_projection_before_remote_extract(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prepared = prepare_plot_turn(
                root,
                "完成第一章最终稿",
                session_id="projection-bind-session",
                turn_id="projection-bind-turn",
            )
            self.accept_projection_fixture(
                root,
                fixture_id="advance-after-prepare",
            )
            with patch(
                "v1_runtime.state_rag._chat_extract",
                side_effect=AssertionError("remote extract must not run"),
            ) as extract:
                failed = propose_plot_turn(
                    root,
                    "测试角色甲抵达测试城南站。",
                    request_id=prepared["receipt_id"],
                )

            extract.assert_not_called()
            self.assertEqual("failed", failed["status"])
            self.assertIn("PREPARED_IDENTITY_STALE", failed["reason"])
            self.assertFalse(failed["identity_check"]["remote_called"])

    def test_init_service_preserves_configless_hook_session_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "novel"
            root.mkdir()
            hook_service = PlotInitService(root)
            started = hook_service.start(
                project_root=root,
                mode="new",
                seed="都市异能",
                idempotency_key="hook-start",
            )
            (root / ".plot-rag").mkdir()
            (root / ".plot-rag" / "config.json").write_text(
                json.dumps(
                    {
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
                        "initialization": {
                            "database_path": ".plot-rag/init.sqlite3",
                        },
                        "remote": {
                            "embedding": {"enabled": False},
                            "rerank": {"enabled": False},
                            "extract": {"enabled": False},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            resumed = init_service(root, project_root=root)

            self.assertEqual(hook_service.database_path, resumed.database_path)
            inspected = resumed.inspect(started["session_id"], view="summary")
            self.assertEqual(started["session_id"], inspected["session_id"])

    def test_v3_doctor_reports_lifecycle_without_mutating_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            state_path = root / ".plot-rag" / "state.sqlite3"
            before = state_doctor(root)
            self.assertFalse(state_path.exists())
            self.assertEqual(
                "not_created",
                next(
                    item["status"]
                    for item in before["checks"]
                    if item["name"] == "continuity_lifecycle"
                ),
            )

            ContinuityService(root).schema_status()
            before_bytes = state_path.read_bytes()
            before_stat = state_path.stat()
            checked = state_doctor(root)
            after_stat = state_path.stat()

            continuity = next(
                item
                for item in checked["checks"]
                if item["name"] == "continuity_lifecycle"
            )
            self.assertEqual("ok", continuity["status"])
            self.assertEqual(
                CONTINUITY_SCHEMA_VERSION,
                continuity["schema_version"],
            )
            self.assertEqual(before_bytes, state_path.read_bytes())
            self.assertEqual(before_stat.st_size, after_stat.st_size)
            self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)

    def test_strict_stop_saves_proposal_and_accept_requires_grant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prepared = prepare_plot_turn(
                root,
                "完成第一章最终稿",
                session_id="session-v1",
                turn_id="turn-v1",
            )
            self.assertEqual("strict_proposal", prepared["lifecycle_mode"])
            self.assertEqual(0, prepared["prepared_canon_revision"])
            self.assertIn("[WEBNOVEL_CONTINUITY_CONTRACT]", prepared["context"])

            extracted = [
                {
                    "category": "location",
                    "subject": "测试角色甲",
                    "field": "current",
                    "operation": "set",
                    "scope": "current",
                    "effective_at": None,
                    "value": "测试城南站",
                    "confidence": 0.99,
                    "evidence": "测试角色甲抵达测试城南站。",
                },
                {
                    "category": "inventory",
                    "subject": "测试角色甲",
                    "field": "item:青铜钥匙",
                    "operation": "set",
                    "scope": "current",
                    "effective_at": None,
                    "value": {
                        "item": "青铜钥匙",
                        "status": "held",
                        "unique": True,
                    },
                    "confidence": 0.99,
                    "evidence": "青铜钥匙仍由测试角色甲持有。",
                },
            ]
            assistant = "测试角色甲抵达测试城南站。青铜钥匙仍由测试角色甲持有。"
            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    extracted,
                    [],
                    {
                        "status": "ok",
                        "configured": True,
                        "model": "fixture-extractor",
                    },
                ),
            ):
                proposed = propose_plot_turn(
                    root,
                    assistant,
                    request_id=prepared["receipt_id"],
                )
            self.assertEqual("proposed", proposed["status"])
            self.assertEqual([], proposed["recorded_events"])
            self.assertEqual(2, len(proposed["proposal_events"]))
            self.assertEqual(prepared["identity"], proposed["identity"])
            service = ContinuityService(root)
            self.assertEqual({"head": 0, "active": 0}, service.get_canon_revisions())
            self.assertEqual([], service.query_facts()["facts"])

            approval = issue_host_approval(
                root,
                proposed["proposal_id"],
                expected_canon_revision=0,
                issuer="unittest-host",
                channel="interactive_test",
            )
            accepted = accept_plot_proposal(
                root,
                proposed["proposal_id"],
                approval_id=approval["grant"]["approval_id"],
                expected_canon_revision=0,
            )
            self.assertEqual("accepted", accepted["status"])
            self.assertEqual(1, accepted["commit"]["active_canon_revision"])
            queried = query_continuity(root, mention="测试角色甲")
            fact_types = {item["fact_type"] for item in queried["facts"]}
            self.assertIn("location", fact_types)
            compatibility = query_continuity_text(
                root,
                "测试角色甲现在在哪里，持有什么道具",
                categories=["location", "inventory"],
                top_k=10,
            )
            self.assertEqual("RESOLVED", compatibility["resolution"]["status"])
            self.assertEqual(
                {"location", "inventory"},
                {item["fact_type"] for item in compatibility["facts"]},
            )
            self.assertTrue(
                all(
                    item["entity_name"] == "测试角色甲"
                    or item["fact_type"] == "inventory"
                    for item in compatibility["facts"]
                )
            )

            with closing(
                sqlite3.connect(root / ".plot-rag" / "state.sqlite3")
            ) as connection:
                legacy_count = connection.execute(
                    "SELECT COUNT(*) FROM state_events"
                ).fetchone()[0]
                lifecycle_count = connection.execute(
                    "SELECT COUNT(*) FROM continuity_events"
                ).fetchone()[0]
                turn = connection.execute(
                    "SELECT status, prepared_canon_revision FROM turns"
                ).fetchone()
            self.assertEqual(0, legacy_count)
            self.assertEqual(2, lifecycle_count)
            self.assertEqual(("proposed", 0), turn)

    def test_strict_stop_existing_advantage_accepts_and_replays_with_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            self.enable_advantage_stop(root)
            self.enable_event_experience(root)
            service = ContinuityService(root)
            actor_id = service.register_entity("character", "测试角色甲")[
                "entity_id"
            ]
            anchor_ref_id = service.register_entity("item", "示例核心")[
                "entity_id"
            ]
            self.accept_continuity_fixture(
                root,
                [
                    self.advantage_event(
                        "advantage_spec",
                        "样例优势核心寄宿于示例核心。",
                        advantage_id="adv_sample_core",
                        action="define",
                        spec_type="advantage_definition",
                        title="样例优势核心",
                        profiles=["resource_transformer"],
                        anchor_type="item_instance",
                        acquisition_mode="inheritance",
                        uniqueness="unique",
                        definition={
                            "initial_stage": "dormant",
                            "initial_charges": 2,
                            "max_charges": 3,
                        },
                    ),
                    self.advantage_event(
                        "advantage_anchor",
                        "示例核心成为样例优势核心的锚点。",
                        advantage_id="adv_sample_core",
                        action="define",
                        anchor_id="anchor_sample_core",
                        anchor_type="item_instance",
                        anchor_ref_id=anchor_ref_id,
                        owner_entity_id=actor_id,
                        binding_state="unbound",
                    ),
                    self.advantage_event(
                        "advantage_module",
                        "状态解析能够识破异常能量。",
                        advantage_id="adv_sample_core",
                        action="define",
                        module_id="module_discern",
                        title="状态解析",
                        kind="appraisal",
                        module_status="enabled",
                        stage="active",
                        costs={"charges": 1},
                        effects=["辨明异常能量"],
                    ),
                    self.advantage_event(
                        "advantage_bind",
                        "测试角色甲完成样例优势核心绑定。",
                        advantage_id="adv_sample_core",
                        action="bind",
                        anchor_id="anchor_sample_core",
                        owner_entity_id=actor_id,
                        story_coordinate={
                            "calendar_id": "story-main",
                            "ordinal": 1,
                        },
                    ),
                    self.advantage_event(
                        "advantage_activate",
                        "测试角色甲激活样例优势核心。",
                        advantage_id="adv_sample_core",
                        action="activate",
                        owner_entity_id=actor_id,
                        stage="active",
                        story_coordinate={
                            "calendar_id": "story-main",
                            "ordinal": 2,
                        },
                    ),
                ],
                artifact_id="advantage-existing-bootstrap",
            )
            runtime_before = service.query_advantage_runtime(
                "adv_sample_core"
            )
            hash_before = str(runtime_before["advantage_projection_hash"])
            self.assertEqual(2.0, runtime_before["runtime"]["charges"])
            self.assertEqual(
                [],
                service.query_advantage_knowledge(
                    "adv_sample_core",
                    visibility="inspection",
                    include_noncanon=True,
                )["knowledge"],
            )

            prompt = "完成第二章最终稿"
            artifact_id = "advantage-existing-stop"
            identity = self.lock_event_experience_identity(
                root,
                prompt=prompt,
                artifact_id=artifact_id,
                chapter_no=2,
                scene_index=0,
                suffix="existing",
            )
            prepared = prepare_plot_turn(
                root,
                prompt,
                session_id="advantage-existing-session",
                turn_id="advantage-existing-turn",
                artifact_stage="final",
                branch_id="main",
                chapter_no=2,
                scene_index=0,
                artifact_id=artifact_id,
                lifecycle_identity=identity,
            )
            self.assertEqual(identity, prepared["lifecycle_identity"])
            self.assertEqual(
                identity,
                prepared["identity"]["lifecycle_identity"],
            )

            use_evidence = "测试角色甲借样例优势核心的状态解析识破异常能量。"
            reveal_evidence = (
                "测试角色甲用状态解析确认：样例优势核心的代价是积累处理误差。"
            )
            candidates = [
                self.advantage_candidate(
                    "advantage_use",
                    "use",
                    use_evidence,
                    subject_kind="advantage",
                    subject_mention="样例优势核心",
                    objects=[
                        {"role": "module", "mention": "状态解析"},
                        {"role": "actor", "mention": "测试角色甲"},
                    ],
                    changes={
                        "costs": {"charges": 1},
                        "effects": ["辨明异常能量"],
                        "exposure_delta": 0.25,
                    },
                    ordinal=3,
                ),
                self.advantage_candidate(
                    "advantage_reveal",
                    "reveal",
                    reveal_evidence,
                    subject_kind="advantage_knowledge",
                    subject_mention="样例优势核心的代价",
                    objects=[
                        {"role": "advantage", "mention": "样例优势核心"},
                        {"role": "module", "mention": "状态解析"},
                        {"role": "observer", "mention": "测试角色甲"},
                    ],
                    changes={
                        "claim": {"fact": "样例优势核心会积累处理误差"},
                        "reveal_stage": "first_reveal",
                        "status": "canon",
                        "record_ledger": False,
                    },
                    knowledge_plane="actor_belief",
                    ordinal=3,
                ),
            ]
            assistant = use_evidence + reveal_evidence
            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    candidates,
                    [],
                    {
                        "status": "ok",
                        "configured": True,
                        "model": "fixture-advantage-extractor",
                    },
                ),
            ):
                proposed = propose_plot_turn(
                    root,
                    assistant,
                    request_id=prepared["receipt_id"],
                )

            self.assertEqual("proposed", proposed["status"])
            self.assertEqual(
                {
                    "ok": True,
                    "candidate_count": 2,
                    "adapted_count": 2,
                },
                proposed["advantage_candidate_adapter"],
            )
            self.assertEqual([], proposed["recorded_events"])
            self.assertEqual(identity, proposed["lifecycle_identity"])
            self.assertEqual(
                identity,
                proposed["identity"]["lifecycle_identity"],
            )
            proposed_events = proposed["proposal_events"]
            self.assertEqual(
                ["advantage_use", "advantage_reveal"],
                [event["event_type"] for event in proposed_events],
            )
            use_event, reveal_event = proposed_events
            self.assertEqual("adv_sample_core", use_event["advantage_id"])
            self.assertEqual("module_discern", use_event["module_id"])
            self.assertEqual(actor_id, use_event["actor_entity_id"])
            self.assertEqual(
                use_evidence,
                use_event["evidence"]["quote"],
            )
            self.assertEqual("adv_sample_core", reveal_event["advantage_id"])
            self.assertEqual("module_discern", reveal_event["module_id"])
            self.assertEqual(actor_id, reveal_event["observer_entity_id"])
            self.assertEqual(
                "actor_belief",
                reveal_event["knowledge_plane"],
            )
            self.assertEqual(
                reveal_evidence,
                reveal_event["evidence"]["quote"],
            )
            self.assertTrue(str(reveal_event["knowledge_id"]).strip())
            self.assertEqual(
                {"head": 1, "active": 1},
                service.get_canon_revisions(),
            )
            self.assertEqual(
                runtime_before["runtime"],
                service.query_advantage_runtime("adv_sample_core")["runtime"],
            )

            frozen = service.inspect_proposal(proposed["proposal_id"])
            self.assertEqual(
                identity,
                frozen["payload"]["lifecycle_identity"],
            )
            approval = issue_host_approval(
                root,
                proposed["proposal_id"],
                expected_canon_revision=1,
                issuer="advantage-existing-host",
                channel="interactive_test",
            )
            with service.store.read_connection() as connection:
                grant_row = connection.execute(
                    """
                    SELECT binding_json
                    FROM approval_grants
                    WHERE binding_hash=?
                    """,
                    (approval["grant"]["binding_hash"],),
                ).fetchone()
            self.assertIsNotNone(grant_row)
            grant_binding = json.loads(str(grant_row["binding_json"]))
            self.assertEqual(
                identity,
                grant_binding["lifecycle_identity"],
            )
            self.assertTrue(
                str(grant_binding["lifecycle_identity_hash"]).startswith(
                    "lifecycle_identity_"
                )
            )
            accepted = accept_plot_proposal(
                root,
                proposed["proposal_id"],
                approval_id=approval["grant"]["approval_id"],
                expected_canon_revision=1,
            )
            self.assertEqual("accepted", accepted["status"])
            self.assertEqual(
                2,
                accepted["commit"]["active_canon_revision"],
            )

            runtime_after = service.query_advantage_runtime("adv_sample_core")
            self.assertEqual(1.0, runtime_after["runtime"]["charges"])
            self.assertEqual(0.25, runtime_after["runtime"]["exposure"])
            knowledge = service.query_advantage_knowledge(
                "adv_sample_core",
                knowledge_plane="actor_belief",
                observer_entity_id=actor_id,
                visibility="inspection",
                include_noncanon=True,
            )["knowledge"]
            self.assertEqual(1, len(knowledge))
            self.assertEqual(
                {"fact": "样例优势核心会积累处理误差"},
                knowledge[0]["claim_json"],
            )
            self.assertEqual(
                reveal_evidence,
                knowledge[0]["evidence_json"]["quote"],
            )
            ledger = service.query_advantage_ledger(
                "adv_sample_core",
                visibility="inspection",
            )["ledger"]
            self.assertTrue(
                any(
                    row["entry_kind"] == "use"
                    and row["module_id"] == "module_discern"
                    for row in ledger
                )
            )
            replayed = service.replay()
            self.assertEqual(
                runtime_after["advantage_projection_hash"],
                replayed["advantage_projection_hash"],
            )
            self.assertEqual(
                runtime_after["runtime"],
                service.query_advantage_runtime("adv_sample_core")["runtime"],
            )

    def test_advantage_experience_bindings_single_contract_binds_all(
        self,
    ) -> None:
        contract = {
            "dependency_order": 1,
            "contract_id": "experience-single",
            "contract_hash": "a" * 64,
            "event_seed_id": "event-seed-single",
            "event_seed_revision": 3,
        }

        bindings = v1_runtime_module._advantage_experience_bindings(
            [
                {"event_type": "advantage_spec"},
                {"event_type": "advantage_activate"},
                {"event_type": "advantage_reveal"},
            ],
            {"contracts": [contract]},
        )

        expected = {
            "dependency_order": 1,
            "experience_contract_id": "experience-single",
            "experience_contract_hash": "a" * 64,
            "event_seed_id": "event-seed-single",
            "event_seed_revision": 3,
        }
        self.assertEqual({0, 1, 2}, set(bindings))
        self.assertTrue(
            all(binding == expected for binding in bindings.values())
        )
        self.assertIsNot(bindings[0], bindings[1])

    def test_advantage_experience_bindings_multi_group_uses_first_coordinate(
        self,
    ) -> None:
        first_coordinate = {
            "calendar_id": "story-main",
            "ordinal": 7,
        }
        second_coordinate = {
            "calendar_id": "story-main",
            "ordinal": 9,
        }
        bindings = v1_runtime_module._advantage_experience_bindings(
            [
                {"story_coordinate": first_coordinate},
                {"story_coordinate": second_coordinate},
                {"story_coordinate": dict(first_coordinate)},
            ],
            {
                "contracts": [
                    {
                        "dependency_order": 2,
                        "contract_id": "experience-second",
                        "contract_hash": "2" * 64,
                        "event_seed_id": "event-seed-second",
                        "event_seed_revision": 2,
                    },
                    {
                        "dependency_order": 1,
                        "contract_id": "experience-first",
                        "contract_hash": "1" * 64,
                        "event_seed_id": "event-seed-first",
                        "event_seed_revision": 1,
                    },
                ]
            },
        )

        self.assertEqual(
            "experience-first",
            bindings[0]["experience_contract_id"],
        )
        self.assertEqual(bindings[0], bindings[2])
        self.assertEqual(
            "experience-second",
            bindings[1]["experience_contract_id"],
        )
        self.assertEqual(1, bindings[0]["dependency_order"])
        self.assertEqual(2, bindings[1]["dependency_order"])

    def test_advantage_experience_bindings_fail_closed_on_group_mismatch(
        self,
    ) -> None:
        manifest = {
            "contracts": [
                {
                    "dependency_order": 1,
                    "contract_id": "experience-first",
                    "contract_hash": "1" * 64,
                    "event_seed_id": "event-seed-first",
                    "event_seed_revision": 1,
                },
                {
                    "dependency_order": 2,
                    "contract_id": "experience-second",
                    "contract_hash": "2" * 64,
                    "event_seed_id": "event-seed-second",
                    "event_seed_revision": 1,
                },
            ]
        }
        cases = (
            (
                "missing-coordinate",
                [
                    {
                        "event_type": "advantage_spec",
                    },
                    {
                        "story_coordinate": {
                            "calendar_id": "story-main",
                            "ordinal": 2,
                        }
                    },
                ],
                "ADVANTAGE_EXPERIENCE_BINDING_COORDINATE_REQUIRED",
            ),
            (
                "cardinality-mismatch",
                [
                    {
                        "story_coordinate": {
                            "calendar_id": "story-main",
                            "ordinal": 1,
                        }
                    },
                    {
                        "story_coordinate": {
                            "calendar_id": "story-main",
                            "ordinal": 1,
                        }
                    },
                ],
                "ADVANTAGE_EXPERIENCE_BINDING_CARDINALITY_MISMATCH",
            ),
        )
        for label, candidates, expected_code in cases:
            with self.subTest(label=label):
                with self.assertRaises(ContinuityError) as raised:
                    v1_runtime_module._advantage_experience_bindings(
                        candidates,
                        manifest,
                    )
                self.assertEqual(expected_code, raised.exception.code)

    def test_strict_stop_first_advantage_creation_and_planned_isolation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            self.enable_advantage_stop(root)
            self.enable_event_experience(root)
            service = ContinuityService(root)
            actor_id = service.register_entity("character", "测试角色甲")[
                "entity_id"
            ]

            prompt = "完成第一章最终稿"
            artifact_id = "advantage-first-stop"
            identity = self.lock_event_experience_identity(
                root,
                prompt=prompt,
                artifact_id=artifact_id,
                chapter_no=1,
                scene_index=0,
                suffix="first",
            )
            prepared = prepare_plot_turn(
                root,
                prompt,
                session_id="advantage-first-session",
                turn_id="advantage-first-turn",
                artifact_stage="final",
                branch_id="main",
                chapter_no=1,
                scene_index=0,
                artifact_id=artifact_id,
                lifecycle_identity=identity,
            )

            evidence = {
                "spec": "样例解析器在测试角色甲体内启动。",
                "anchor": "样例解析器锚点把样例解析器固定在测试角色甲身上。",
                "module": "样例解析器提供异常解析，可识别被篡改的能力样本。",
                "bind": "测试角色甲通过样例解析器锚点绑定样例解析器。",
                "activate": "测试角色甲激活样例解析器。",
                "reveal": "异常解析揭示样例解析器的误差代价。",
            }
            candidates = [
                self.advantage_candidate(
                    "advantage_spec",
                    "define",
                    evidence["spec"],
                    subject_kind="advantage_definition",
                    subject_mention="样例解析器",
                    changes={
                        "title": "样例解析器",
                        "profiles": ["resource_transformer"],
                        "anchor_type": "actor",
                        "acquisition_mode": "awakening",
                        "uniqueness": "unique",
                        "definition": {
                            "initial_stage": "dormant",
                            "initial_charges": 2,
                            "max_charges": 3,
                        },
                    },
                ),
                self.advantage_candidate(
                    "advantage_anchor",
                    "define",
                    evidence["anchor"],
                    subject_kind="advantage_anchor",
                    subject_mention="样例解析器锚点",
                    objects=[
                        {"role": "advantage", "mention": "样例解析器"},
                        {"role": "anchor_ref", "mention": "测试角色甲"},
                    ],
                    changes={
                        "anchor_type": "actor",
                        "binding_state": "unbound",
                    },
                ),
                self.advantage_candidate(
                    "advantage_module",
                    "define",
                    evidence["module"],
                    subject_kind="advantage_module",
                    subject_mention="异常解析",
                    objects=[
                        {"role": "advantage", "mention": "样例解析器"},
                    ],
                    changes={
                        "title": "异常解析",
                        "kind": "appraisal",
                        "module_status": "enabled",
                        "stage": "active",
                        "costs": {"charges": 1},
                        "effects": ["识别被篡改的能力样本"],
                    },
                ),
                self.advantage_candidate(
                    "advantage_bind",
                    "bind",
                    evidence["bind"],
                    subject_kind="advantage",
                    subject_mention="样例解析器",
                    objects=[
                        {"role": "anchor", "mention": "样例解析器锚点"},
                        {"role": "owner", "mention": "测试角色甲"},
                    ],
                    changes={},
                ),
                self.advantage_candidate(
                    "advantage_activate",
                    "activate",
                    evidence["activate"],
                    subject_kind="advantage",
                    subject_mention="样例解析器",
                    objects=[
                        {"role": "owner", "mention": "测试角色甲"},
                    ],
                    changes={"stage": "active"},
                ),
                self.advantage_candidate(
                    "advantage_reveal",
                    "reveal",
                    evidence["reveal"],
                    subject_kind="advantage_knowledge",
                    subject_mention="误差代价",
                    objects=[
                        {"role": "advantage", "mention": "样例解析器"},
                        {"role": "module", "mention": "异常解析"},
                    ],
                    changes={
                        "claim": {"fact": "样例解析器的使用会积累误差"},
                        "reveal_stage": "first_reveal",
                        "status": "canon",
                        "record_ledger": False,
                    },
                ),
            ]
            forbidden_remote_fields = {
                "advantage_id",
                "anchor_id",
                "module_id",
                "knowledge_id",
                "owner_entity_id",
                "actor_entity_id",
                "observer_entity_id",
                "experience_contract_id",
                "event_seed_id",
            }

            def nested_keys(value: object) -> set[str]:
                if isinstance(value, dict):
                    result = set(value)
                    for nested in value.values():
                        result.update(nested_keys(nested))
                    return result
                if isinstance(value, list):
                    result: set[str] = set()
                    for nested in value:
                        result.update(nested_keys(nested))
                    return result
                return set()

            self.assertFalse(
                forbidden_remote_fields.intersection(nested_keys(candidates))
            )
            assistant = "".join(evidence.values())
            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    candidates,
                    [],
                    {
                        "status": "ok",
                        "configured": True,
                        "model": "fixture-advantage-extractor",
                    },
                ),
            ):
                proposed = propose_plot_turn(
                    root,
                    assistant,
                    request_id=prepared["receipt_id"],
                )

            self.assertEqual("proposed", proposed["status"], proposed)
            self.assertEqual(
                {
                    "ok": True,
                    "candidate_count": 6,
                    "adapted_count": 6,
                },
                proposed["advantage_candidate_adapter"],
            )
            self.assertEqual([], proposed["issues"])
            proposed_events = proposed["proposal_events"]
            self.assertEqual(
                [
                    "advantage_spec",
                    "advantage_anchor",
                    "advantage_module",
                    "advantage_bind",
                    "advantage_activate",
                    "advantage_reveal",
                ],
                [event["event_type"] for event in proposed_events],
            )
            (
                spec_event,
                anchor_event,
                module_event,
                bind_event,
                activate_event,
                reveal_event,
            ) = proposed_events
            advantage_id = str(spec_event["advantage_id"])
            anchor_id = str(anchor_event["anchor_id"])
            module_id = str(module_event["module_id"])
            knowledge_id = str(reveal_event["knowledge_id"])
            for stable_id in (
                advantage_id,
                anchor_id,
                module_id,
                knowledge_id,
            ):
                self.assertTrue(stable_id.strip())
            self.assertEqual(
                {advantage_id},
                {
                    str(event["advantage_id"])
                    for event in proposed_events
                },
            )
            self.assertEqual(anchor_id, bind_event["anchor_id"])
            self.assertEqual(module_id, reveal_event["module_id"])
            self.assertEqual(actor_id, anchor_event["anchor_ref_id"])
            self.assertEqual(actor_id, bind_event["owner_entity_id"])
            self.assertEqual(actor_id, activate_event["owner_entity_id"])

            experience_contract_ids = {
                str(event["experience_contract_id"])
                for event in proposed_events
            }
            experience_contract_hashes = {
                str(event["experience_contract_hash"])
                for event in proposed_events
            }
            event_seed_ids = {
                str(event["causal_provenance"]["event_seed_id"])
                for event in proposed_events
            }
            event_seed_revisions = {
                int(event["causal_provenance"]["event_seed_revision"])
                for event in proposed_events
            }
            self.assertEqual(1, len(experience_contract_ids))
            self.assertEqual(1, len(experience_contract_hashes))
            self.assertEqual(1, len(event_seed_ids))
            self.assertEqual(1, len(event_seed_revisions))
            self.assertTrue(
                experience_contract_hashes.issubset(
                    set(identity["experience_contract_hashes"])
                )
            )

            frozen = service.inspect_proposal(proposed["proposal_id"])
            self.assertEqual(identity, frozen["payload"]["lifecycle_identity"])
            self.assertEqual(proposed_events, frozen["events"])
            grant = issue_host_approval(
                root,
                proposed["proposal_id"],
                expected_canon_revision=0,
                issuer="advantage-first-host",
                channel="interactive_test",
            )
            accepted = accept_plot_proposal(
                root,
                proposed["proposal_id"],
                approval_id=grant["grant"]["approval_id"],
                expected_canon_revision=0,
            )
            self.assertEqual("accepted", accepted["status"])
            self.assertEqual(
                "样例解析器",
                service.query_advantage_definition(advantage_id)[
                    "definition"
                ]["title"],
            )
            inspection_context = service.query_special_item_context(
                advantage_id,
                visibility="inspection",
            )["contexts"][0]
            self.assertEqual(
                [anchor_id],
                [
                    row["anchor_id"]
                    for row in inspection_context["anchors"]
                ],
            )
            self.assertEqual(
                [module_id],
                [
                    row["module_id"]
                    for row in service.query_advantage_modules(
                        advantage_id,
                        enabled_only=True,
                    )["modules"]
                ],
            )
            runtime_current = service.query_advantage_runtime(advantage_id)
            self.assertEqual("active", runtime_current["runtime"]["stage"])
            self.assertEqual(2.0, runtime_current["runtime"]["charges"])
            current_hash = str(
                runtime_current["advantage_projection_hash"]
            )
            knowledge = service.query_advantage_knowledge(
                advantage_id,
                visibility="inspection",
                include_noncanon=True,
            )["knowledge"]
            self.assertEqual([knowledge_id], [
                row["knowledge_id"] for row in knowledge
            ])
            replayed_current = service.replay()
            self.assertEqual(
                current_hash,
                replayed_current["advantage_projection_hash"],
            )

            planned_prompt = "设计第二章章纲"
            planned_artifact_id = "advantage-planned-stop"
            planned_identity = self.lock_event_experience_identity(
                root,
                prompt=planned_prompt,
                artifact_id=planned_artifact_id,
                chapter_no=2,
                scene_index=0,
                suffix="planned",
            )
            planned_prepared = prepare_plot_turn(
                root,
                planned_prompt,
                session_id="advantage-planned-session",
                turn_id="advantage-planned-turn",
                artifact_stage="outline",
                branch_id="main",
                chapter_no=2,
                scene_index=0,
                artifact_id=planned_artifact_id,
                lifecycle_identity=planned_identity,
            )
            planned_use_evidence = (
                "计划中测试角色甲将用样例解析器的异常解析辨明封锁。"
            )
            planned_reveal_evidence = (
                "计划中作者将揭示样例解析器会干扰短期记忆。"
            )
            planned_candidates = [
                self.advantage_candidate(
                    "advantage_use",
                    "use",
                    planned_use_evidence,
                    subject_kind="advantage",
                    subject_mention="样例解析器",
                    objects=[
                        {"role": "module", "mention": "异常解析"},
                        {"role": "actor", "mention": "测试角色甲"},
                    ],
                    changes={
                        "costs": {"charges": 1},
                        "effects": ["辨明封锁"],
                        "exposure_delta": 9,
                    },
                    scope="planned",
                    ordinal=2,
                ),
                self.advantage_candidate(
                    "advantage_reveal",
                    "reveal",
                    planned_reveal_evidence,
                    subject_kind="advantage_knowledge",
                    subject_mention="干扰短期记忆",
                    objects=[
                        {"role": "advantage", "mention": "样例解析器"},
                    ],
                    changes={
                        "claim": {"fact": "样例解析器会干扰短期记忆"},
                        "reveal_stage": "future_reveal",
                        "status": "planned",
                        "record_ledger": False,
                    },
                    scope="planned",
                    knowledge_plane="author_plan",
                    ordinal=2,
                ),
            ]
            planned_assistant = (
                planned_use_evidence + planned_reveal_evidence
            )
            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    planned_candidates,
                    [],
                    {
                        "status": "ok",
                        "configured": True,
                        "model": "fixture-advantage-extractor",
                    },
                ),
            ):
                planned_proposed = propose_plot_turn(
                    root,
                    planned_assistant,
                    request_id=planned_prepared["receipt_id"],
                )

            self.assertEqual(
                "proposed",
                planned_proposed["status"],
                planned_proposed,
            )
            self.assertTrue(
                all(
                    event["scope"] == "planned"
                    for event in planned_proposed["proposal_events"]
                )
            )
            self.assertEqual(
                "author_plan",
                planned_proposed["proposal_events"][1][
                    "knowledge_plane"
                ],
            )
            planned_grant = issue_host_approval(
                root,
                planned_proposed["proposal_id"],
                expected_canon_revision=1,
                issuer="advantage-planned-host",
                channel="interactive_test",
            )
            planned_accepted = accept_plot_proposal(
                root,
                planned_proposed["proposal_id"],
                approval_id=planned_grant["grant"]["approval_id"],
                expected_canon_revision=1,
            )
            self.assertEqual("accepted", planned_accepted["status"])
            runtime_after_planned = service.query_advantage_runtime(
                advantage_id
            )
            self.assertEqual(
                runtime_current["runtime"],
                runtime_after_planned["runtime"],
            )
            self.assertEqual(
                current_hash,
                runtime_after_planned["advantage_projection_hash"],
            )
            planned_facts = service.query_facts(scope="planned")["facts"]
            self.assertTrue(
                any(
                    fact["fact_type"] == "advantage_use"
                    and fact["value"]["advantage_id"] == advantage_id
                    for fact in planned_facts
                )
            )
            self.assertTrue(
                any(
                    fact["fact_type"] == "advantage_reveal"
                    and fact["value"]["advantage_id"] == advantage_id
                    and fact["value"]["knowledge_plane"] == "author_plan"
                    for fact in planned_facts
                )
            )
            generation_context = service.query_special_item_context(
                advantage_id,
                visibility="generation",
            )
            serialized_generation = json.dumps(
                generation_context,
                ensure_ascii=False,
                sort_keys=True,
            )
            self.assertNotIn("author_plan", serialized_generation)
            self.assertNotIn("样例解析器会干扰短期记忆", serialized_generation)
            replayed_planned = service.replay()
            self.assertEqual(
                current_hash,
                replayed_planned["advantage_projection_hash"],
            )
            self.assertEqual(
                runtime_current["runtime"],
                service.query_advantage_runtime(advantage_id)["runtime"],
            )

    def test_worker_proposal_binding_is_strict_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prepared = prepare_plot_turn(
                root,
                "完成第一章最终稿",
                session_id="worker-binding-session",
                turn_id="worker-binding-turn",
            )
            assistant = "测试角色甲抵达测试城南站。"
            extracted = [
                {
                    "category": "location",
                    "subject": "测试角色甲",
                    "field": "current",
                    "operation": "set",
                    "scope": "current",
                    "effective_at": None,
                    "value": "测试城南站",
                    "confidence": 0.99,
                    "evidence": assistant,
                }
            ]
            binding = self.extraction_proposal_binding(
                prepared,
                assistant,
            )
            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    extracted,
                    [],
                    {"status": "ok", "configured": True},
                ),
            ) as extract:
                proposed = propose_plot_turn(
                    root,
                    assistant,
                    request_id=str(prepared["receipt_id"]),
                    proposal_binding=binding,
                )

            extract.assert_called_once()
            self.assertEqual("proposed", proposed["status"])
            payload = ContinuityService(root).inspect_proposal(
                str(proposed["proposal_id"])
            )["payload"]
            self.assertEqual(
                binding,
                {key: payload[key] for key in binding},
            )

            second_root = self.make_project(Path(temporary) / "mismatch")
            second_prepared = prepare_plot_turn(
                second_root,
                "完成第一章最终稿",
                session_id="worker-mismatch-session",
                turn_id="worker-mismatch-turn",
            )
            mismatched = self.extraction_proposal_binding(
                second_prepared,
                assistant,
            )
            mismatched["request_id"] = "different-request"
            with patch(
                "v1_runtime.state_rag._chat_extract",
                side_effect=AssertionError(
                    "binding mismatch must block remote extraction"
                ),
            ) as blocked_extract:
                failed = propose_plot_turn(
                    second_root,
                    assistant,
                    request_id=str(second_prepared["receipt_id"]),
                    proposal_binding=mismatched,
                )

            blocked_extract.assert_not_called()
            self.assertEqual("failed", failed["status"])
            self.assertIn(
                "EXTRACTION_PROPOSAL_BINDING_MISMATCH",
                failed["reason"],
            )
            self.assertEqual(
                [],
                ContinuityService(second_root).list_proposals(),
            )

    def test_worker_no_delta_is_idempotent_without_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prepared = prepare_plot_turn(
                root,
                "完成第一章最终稿",
                session_id="worker-no-delta-session",
                turn_id="worker-no-delta-turn",
            )
            assistant = "本轮没有产生需要持久化的连续性变化。"
            binding = self.extraction_proposal_binding(
                prepared,
                assistant,
            )
            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    [],
                    [],
                    {
                        "status": "no_delta",
                        "configured": True,
                    },
                ),
            ) as extract:
                first = propose_plot_turn(
                    root,
                    assistant,
                    request_id=str(prepared["receipt_id"]),
                    proposal_binding=binding,
                    no_delta_without_proposal=True,
                )
                repeated = propose_plot_turn(
                    root,
                    assistant,
                    request_id=str(prepared["receipt_id"]),
                    proposal_binding=binding,
                    no_delta_without_proposal=True,
                )

            extract.assert_called_once()
            self.assertEqual("no_delta", first["status"])
            self.assertEqual("no_delta", first["result_kind"])
            self.assertEqual("", first["proposal_id"])
            self.assertEqual([], first["proposal_events"])
            self.assertFalse(first["idempotent"])
            self.assertEqual("no_delta", repeated["status"])
            self.assertTrue(repeated["idempotent"])
            self.assertEqual([], ContinuityService(root).list_proposals())
            turn = self.turn_record(root, str(prepared["request_id"]))
            self.assertEqual("no_delta", turn["status"])

    def test_async_shadow_proposal_is_compared_rejected_and_turn_neutral(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prepared = prepare_plot_turn(
                root,
                "完成第一章最终稿",
                session_id="shadow-proposal-session",
                turn_id="shadow-proposal-turn",
            )
            assistant = "测试角色甲抵达测试城南站。"
            extracted = [
                {
                    "category": "location",
                    "subject": "测试角色甲",
                    "field": "current",
                    "operation": "set",
                    "scope": "current",
                    "effective_at": None,
                    "value": "测试城南站",
                    "confidence": 0.99,
                    "evidence": assistant,
                }
            ]
            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    extracted,
                    [],
                    {"status": "ok", "configured": True},
                ),
            ):
                authoritative = propose_plot_turn(
                    root,
                    assistant,
                    request_id=str(prepared["receipt_id"]),
                )
            turn_before = self.turn_record(
                root,
                str(prepared["request_id"]),
            )
            binding = self.extraction_proposal_binding(
                prepared,
                assistant,
                artifact_revision=2,
            )
            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    extracted,
                    [],
                    {"status": "ok", "configured": True},
                ),
            ) as extract:
                shadow = propose_plot_turn(
                    root,
                    assistant,
                    request_id=str(prepared["receipt_id"]),
                    proposal_binding=binding,
                    shadow_only=True,
                    authoritative_proposal_id=str(
                        authoritative["proposal_id"]
                    ),
                )
                repeated = propose_plot_turn(
                    root,
                    assistant,
                    request_id=str(prepared["receipt_id"]),
                    proposal_binding=binding,
                    shadow_only=True,
                    authoritative_proposal_id=str(
                        authoritative["proposal_id"]
                    ),
                )

            self.assertEqual(2, extract.call_count)
            self.assertEqual("proposed", shadow["status"])
            self.assertEqual("proposal", shadow["result_kind"])
            self.assertTrue(shadow["shadow_only"])
            self.assertEqual("rejected", shadow["canon_status"])
            self.assertEqual(
                "async_shadow_non_accepting",
                shadow["status_reason"],
            )
            self.assertTrue(shadow["comparison"]["exact_match"])
            self.assertEqual(
                shadow["comparison"]["authoritative_events_sha256"],
                shadow["comparison"]["shadow_events_sha256"],
            )
            self.assertEqual(
                shadow["proposal_id"],
                repeated["proposal_id"],
            )
            service = ContinuityService(root)
            persisted = service.inspect_proposal(
                str(shadow["proposal_id"])
            )
            self.assertEqual("rejected", persisted["canon_status"])
            self.assertEqual(
                "async_shadow_non_accepting",
                persisted["status_reason"],
            )
            self.assertEqual(
                {
                    "mode": "async_shadow",
                    "authoritative_proposal_id": authoritative[
                        "proposal_id"
                    ],
                    "acceptable": False,
                    "barrier_blocking": False,
                    "comparison": shadow["comparison"],
                },
                persisted["payload"]["extraction_shadow"],
            )
            with service.store.read_connection() as connection:
                artifact_status = connection.execute(
                    """
                    SELECT canon_status, active
                    FROM artifacts
                    WHERE artifact_version_id=(
                        SELECT artifact_version_id
                        FROM proposals
                        WHERE proposal_id=?
                    )
                    """,
                    (str(shadow["proposal_id"]),),
                ).fetchone()
            self.assertEqual(("rejected", 0), tuple(artifact_status))
            self.assertEqual(
                turn_before,
                self.turn_record(root, str(prepared["request_id"])),
            )
            self.assertEqual(
                {"head": 0, "active": 0},
                service.get_canon_revisions(),
            )
            self.assertEqual([], service.query_facts()["facts"])

    def test_async_shadow_no_delta_and_failure_do_not_mutate_turn(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prepared = prepare_plot_turn(
                root,
                "完成第一章最终稿",
                session_id="shadow-no-delta-session",
                turn_id="shadow-no-delta-turn",
            )
            assistant = "本轮没有形成新的持续状态。"
            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    [],
                    [],
                    {"status": "no_delta", "configured": True},
                ),
            ):
                authoritative = propose_plot_turn(
                    root,
                    assistant,
                    request_id=str(prepared["receipt_id"]),
                )
            turn_before = self.turn_record(
                root,
                str(prepared["request_id"]),
            )
            binding = self.extraction_proposal_binding(
                prepared,
                assistant,
                artifact_revision=2,
            )
            service = ContinuityService(root)
            proposal_count_before = len(service.list_proposals())
            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    [],
                    [],
                    {"status": "no_delta", "configured": True},
                ),
            ):
                no_delta = propose_plot_turn(
                    root,
                    assistant,
                    request_id=str(prepared["receipt_id"]),
                    proposal_binding=binding,
                    no_delta_without_proposal=True,
                    shadow_only=True,
                    authoritative_proposal_id=str(
                        authoritative["proposal_id"]
                    ),
                )

            self.assertEqual("no_delta", no_delta["status"])
            self.assertEqual("no_delta", no_delta["result_kind"])
            self.assertEqual("", no_delta["proposal_id"])
            self.assertTrue(no_delta["shadow_only"])
            self.assertEqual(
                proposal_count_before,
                len(service.list_proposals()),
            )
            self.assertEqual(
                turn_before,
                self.turn_record(root, str(prepared["request_id"])),
            )

            with patch(
                "v1_runtime.state_rag._chat_extract",
                side_effect=RuntimeError("shadow remote failure"),
            ):
                failed = propose_plot_turn(
                    root,
                    assistant,
                    request_id=str(prepared["receipt_id"]),
                    proposal_binding=binding,
                    shadow_only=True,
                    authoritative_proposal_id=str(
                        authoritative["proposal_id"]
                    ),
                )

            self.assertEqual("failed", failed["status"])
            self.assertIn("shadow remote failure", failed["reason"])
            self.assertEqual(
                turn_before,
                self.turn_record(root, str(prepared["request_id"])),
            )

    def test_strict_stop_conversion_failure_rolls_back_metadata_and_fails_turn(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prepared = prepare_plot_turn(
                root,
                "推进事务回滚测试剧情",
                session_id="session-conversion-rollback",
                turn_id="turn-conversion-rollback",
            )
            before = self.continuity_write_counts(root)
            extracted = [
                {
                    "category": "character_state",
                    "subject": "事务角色",
                    "field": "goal",
                    "operation": "set",
                    "scope": "current",
                    "effective_at": None,
                    "value": "验证转换回滚",
                    "confidence": 0.99,
                    "evidence": "事务角色决定验证转换回滚。",
                }
            ]
            real_converter = v1_runtime_module.legacy_deltas_to_events

            def fail_after_partial_conversion(
                service: ContinuityService,
                deltas: object,
                **kwargs: object,
            ) -> object:
                real_converter(service, deltas, **kwargs)
                sentinel = service.register_entity(
                    "item",
                    "事务哨兵道具",
                    aliases=("事务哨兵别名",),
                )
                service.resolve_mention(
                    "事务哨兵别名",
                    artifact_id="transaction-fixture",
                    context_entity_ids=(sentinel["entity_id"],),
                    persist=True,
                )
                raise RuntimeError("injected conversion failure")

            with (
                patch(
                    "v1_runtime.state_rag._chat_extract",
                    return_value=(
                        extracted,
                        [],
                        {"status": "ok", "configured": True},
                    ),
                ),
                patch.object(
                    v1_runtime_module,
                    "legacy_deltas_to_events",
                    side_effect=fail_after_partial_conversion,
                ),
            ):
                failed = propose_plot_turn(
                    root,
                    "事务角色决定验证转换回滚。",
                    request_id=prepared["receipt_id"],
                )

            self.assertEqual("failed", failed["status"])
            self.assertIn("injected conversion failure", failed["reason"])
            self.assertEqual(before, self.continuity_write_counts(root))
            turn = self.turn_record(root, prepared["request_id"])
            self.assertEqual("failed", turn["status"])
            self.assertIn("injected conversion failure", turn["error"])
            self.assertEqual("", turn["assistant_hash"])
            self.assertEqual({}, json.loads(turn["result_json"]))
            self.assertIsNotNone(turn["completed_at"])

    def test_strict_stop_proposal_tail_failure_rolls_back_every_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prepared = prepare_plot_turn(
                root,
                "推进提案保存回滚测试剧情",
                session_id="session-proposal-rollback",
                turn_id="turn-proposal-rollback",
            )
            before = self.continuity_write_counts(root)
            extracted = [
                {
                    "category": "inventory",
                    "subject": "提案角色",
                    "field": "item:回滚钥匙",
                    "operation": "set",
                    "scope": "current",
                    "effective_at": None,
                    "value": {
                        "item": "回滚钥匙",
                        "status": "held",
                        "unique": True,
                    },
                    "confidence": 0.99,
                    "evidence": "回滚钥匙由提案角色持有。",
                }
            ]
            with (
                patch(
                    "v1_runtime.state_rag._chat_extract",
                    return_value=(
                        extracted,
                        [{"reason": "fixture warning"}],
                        {"status": "ok", "configured": True},
                    ),
                ),
                patch.object(
                    ContinuityService,
                    "_idempotency_store",
                    side_effect=RuntimeError(
                        "injected proposal tail failure"
                    ),
                ),
            ):
                failed = propose_plot_turn(
                    root,
                    "回滚钥匙由提案角色持有。",
                    request_id=prepared["receipt_id"],
                )

            self.assertEqual("failed", failed["status"])
            self.assertIn("injected proposal tail failure", failed["reason"])
            self.assertEqual(before, self.continuity_write_counts(root))
            turn = self.turn_record(root, prepared["request_id"])
            self.assertEqual("failed", turn["status"])
            self.assertIn("injected proposal tail failure", turn["error"])
            self.assertEqual("", turn["assistant_hash"])
            self.assertEqual({}, json.loads(turn["result_json"]))

    def test_strict_stop_turn_update_failure_rolls_back_saved_proposal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prepared = prepare_plot_turn(
                root,
                "推进 turn 更新回滚测试剧情",
                session_id="session-turn-rollback",
                turn_id="turn-turn-rollback",
            )
            database = root / ".plot-rag" / "state.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """
                    CREATE TRIGGER fail_proposed_turn_update
                    BEFORE UPDATE OF status ON turns
                    WHEN NEW.status='proposed'
                    BEGIN
                        SELECT RAISE(ABORT, 'injected turn update failure');
                    END
                    """
                )
                connection.commit()
            before = self.continuity_write_counts(root)
            extracted = [
                {
                    "category": "location",
                    "subject": "更新角色",
                    "field": "current",
                    "operation": "set",
                    "scope": "current",
                    "effective_at": None,
                    "value": "更新地点",
                    "confidence": 0.99,
                    "evidence": "更新角色抵达更新地点。",
                }
            ]
            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    extracted,
                    [{"reason": "fixture warning"}],
                    {"status": "ok", "configured": True},
                ),
            ):
                failed = propose_plot_turn(
                    root,
                    "更新角色抵达更新地点。",
                    request_id=prepared["receipt_id"],
                )

            self.assertEqual("failed", failed["status"])
            self.assertIn("injected turn update failure", failed["reason"])
            self.assertEqual(before, self.continuity_write_counts(root))
            turn = self.turn_record(root, prepared["request_id"])
            self.assertEqual("failed", turn["status"])
            self.assertIn("injected turn update failure", turn["error"])
            self.assertEqual("", turn["assistant_hash"])
            self.assertEqual({}, json.loads(turn["result_json"]))

    def test_strict_stop_success_commits_turn_and_proposal_together(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prepared = prepare_plot_turn(
                root,
                "推进成功原子提交测试剧情",
                session_id="session-atomic-success",
                turn_id="turn-atomic-success",
            )
            before = self.continuity_write_counts(root)
            extracted = [
                {
                    "category": "character_state",
                    "subject": "原子角色",
                    "field": "goal",
                    "operation": "set",
                    "scope": "current",
                    "effective_at": None,
                    "value": "完成原子提交",
                    "confidence": 0.99,
                    "evidence": "原子角色决定完成原子提交。",
                }
            ]
            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    extracted,
                    [{"reason": "fixture warning"}],
                    {"status": "ok", "configured": True},
                ),
            ):
                proposed = propose_plot_turn(
                    root,
                    "原子角色决定完成原子提交。",
                    request_id=prepared["receipt_id"],
                )

            self.assertEqual("proposed", proposed["status"])
            after = self.continuity_write_counts(root)
            self.assertEqual(1, after["entities"] - before["entities"])
            self.assertEqual(
                1,
                after["mention_resolutions"]
                - before["mention_resolutions"],
            )
            self.assertEqual(1, after["artifacts"] - before["artifacts"])
            self.assertEqual(1, after["proposals"] - before["proposals"])
            self.assertEqual(
                1,
                after["proposal_issues"] - before["proposal_issues"],
            )
            self.assertEqual(
                1,
                after["idempotency_records"]
                - before["idempotency_records"],
            )
            self.assertEqual(
                before["entity_aliases"],
                after["entity_aliases"],
            )

            database = root / ".plot-rag" / "state.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.row_factory = sqlite3.Row
                turn = connection.execute(
                    "SELECT * FROM turns WHERE request_id=?",
                    (prepared["request_id"],),
                ).fetchone()
                proposal = connection.execute(
                    "SELECT * FROM proposals WHERE proposal_id=?",
                    (proposed["proposal_id"],),
                ).fetchone()
                artifact = connection.execute(
                    "SELECT * FROM artifacts WHERE artifact_version_id=?",
                    (proposal["artifact_version_id"],),
                ).fetchone()
                idempotency = connection.execute(
                    """
                    SELECT response_json
                    FROM idempotency_records
                    WHERE namespace='save_proposal'
                    """
                ).fetchone()

            stored_result = json.loads(turn["result_json"])
            stored_idempotency = json.loads(idempotency["response_json"])
            self.assertEqual("proposed", turn["status"])
            self.assertEqual(
                proposed["proposal_id"],
                stored_result["proposal_id"],
            )
            self.assertEqual(
                proposed["proposal_id"],
                proposal["proposal_id"],
            )
            self.assertEqual(
                proposal["artifact_version_id"],
                artifact["artifact_version_id"],
            )
            self.assertEqual(
                proposed["proposal_id"],
                stored_idempotency["proposal_id"],
            )

    def test_brainstorm_accept_stays_out_of_authoritative_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prepared = prepare_plot_turn(
                root,
                "继续推进剧情",
                session_id="session-brainstorm",
                turn_id="turn-brainstorm",
            )
            extracted = [
                {
                    "category": "character_state",
                    "subject": "测试角色甲",
                    "field": "goal",
                    "operation": "set",
                    "scope": "current",
                    "effective_at": None,
                    "value": "潜入城主府",
                    "confidence": 0.99,
                    "evidence": "测试角色甲决定潜入城主府。",
                }
            ]
            with patch(
                "v1_runtime.state_rag._chat_extract",
                return_value=(
                    extracted,
                    [],
                    {"status": "ok", "configured": True},
                ),
            ):
                proposal = propose_plot_turn(
                    root,
                    "测试角色甲决定潜入城主府。",
                    request_id=prepared["receipt_id"],
                )
            grant = issue_host_approval(
                root,
                proposal["proposal_id"],
                expected_canon_revision=0,
                issuer="unittest-host",
                channel="interactive_test",
            )
            accepted = accept_plot_proposal(
                root,
                proposal["proposal_id"],
                approval_id=grant["grant"]["approval_id"],
                expected_canon_revision=0,
            )
            self.assertFalse(accepted["commit"]["changes_authority"])
            self.assertEqual(
                {"head": 1, "active": 0},
                ContinuityService(root).get_canon_revisions(),
            )
            self.assertEqual([], ContinuityService(root).query_facts()["facts"])

    def test_retract_and_replay_remove_longform_derived_content(self) -> None:
        marker = "撤回标记_银轨余烬"
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            service = ContinuityService(root)
            entity = service.register_entity("world", "撤回派生测试")
            proposal = service.save_proposal(
                events=[
                    {
                        "event_type": "state",
                        "entity_id": entity["entity_id"],
                        "field": "retraction_fixture",
                        "value": marker,
                    }
                ],
                payload={
                    "assistant_text": f"{marker} 只属于即将撤回的第一章。",
                    "summary": f"{marker} 章节摘要。",
                    "success_pattern": f"{marker} 成功模式。",
                    "artifact_context": {"task": "prose"},
                    "genre": "fantasy",
                    "arc_id": "arc-retract",
                    "volume_id": "volume-retract",
                },
                artifact_id="chapter-retract-fixture",
                artifact_stage="final",
                chapter_no=1,
                prepared_canon_revision=0,
            )
            grant = issue_host_approval(
                root,
                proposal["proposal_id"],
                expected_canon_revision=0,
                issuer="unittest-host",
                channel="interactive_test",
            )
            accepted = accept_plot_proposal(
                root,
                proposal["proposal_id"],
                approval_id=grant["grant"]["approval_id"],
                expected_canon_revision=0,
            )

            longform_db = root / ".plot-rag" / "longform.v1.sqlite3"
            memory = LayeredMemoryStore(longform_db)
            summaries = AcceptedSummaryStore(longform_db)
            patterns = ProjectPatternStore(longform_db)
            self.assertTrue(memory.query(marker, branch_id="main"))
            self.assertTrue(
                summaries.query(
                    marker,
                    branch_id="main",
                    chapter_no=1,
                )
            )
            self.assertTrue(patterns.query(marker, task="prose"))

            active_revision = int(
                accepted["commit"]["active_canon_revision"]
            )
            retract_grant = issue_host_approval(
                root,
                proposal["proposal_id"],
                expected_canon_revision=active_revision,
                issuer="unittest-host",
                channel="interactive_test",
                operations=("retract",),
            )
            retracted = retract_plot_proposal(
                root,
                proposal["proposal_id"],
                approval_id=retract_grant["grant"]["approval_id"],
                expected_canon_revision=active_revision,
                reason="fixture withdrawal",
            )
            self.assertEqual("retracted", retracted["status"])
            self.assertEqual([], memory.query(marker, branch_id="main"))
            self.assertEqual(
                [],
                summaries.query(
                    marker,
                    branch_id="main",
                    chapter_no=1,
                ),
            )
            self.assertEqual([], patterns.query(marker, task="prose"))

            with closing(
                sqlite3.connect(
                    root / ".plot-rag" / "projection-runs.v1.sqlite3"
                )
            ) as connection:
                payload = json.loads(
                    connection.execute(
                        """
                        SELECT input_json
                        FROM projection_runs
                        WHERE commit_id=?
                        ORDER BY started_at
                        LIMIT 1
                        """,
                        (retracted["commit"]["commit_id"],),
                    ).fetchone()[0]
                )
            self.assertEqual("retract", payload["operation"])
            self.assertEqual("retracted", payload["canon_status"])

            stale_legacy_marker = "陈旧旧表标记_不应进入重放快照"
            with closing(
                sqlite3.connect(root / ".plot-rag" / "state.sqlite3")
            ) as connection:
                connection.execute(
                    """
                    INSERT INTO turns(
                        receipt_id, request_id, prompt_hash, status, started_at
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        "stale-legacy-receipt",
                        "stale-legacy-request",
                        "0" * 64,
                        "committed",
                        "2026-07-16T00:00:00+00:00",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO state_events(
                        event_id, request_id, receipt_id, category, subject,
                        field, operation, scope, value_json, confidence,
                        evidence, source_hash, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "stale-legacy-event",
                        "stale-legacy-request",
                        "stale-legacy-receipt",
                        "world_state",
                        "陈旧旧表",
                        "stale",
                        "set",
                        "current",
                        json.dumps(
                            stale_legacy_marker,
                            ensure_ascii=False,
                        ),
                        1.0,
                        stale_legacy_marker,
                        hashlib.sha256(
                            stale_legacy_marker.encode("utf-8")
                        ).hexdigest(),
                        "2026-07-16T00:00:00+00:00",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO current_facts(
                        fact_key, category, subject, field, value_json,
                        event_id, effective_at, confidence, evidence, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        "stale-legacy-snapshot-fixture",
                        "world_state",
                        "陈旧旧表",
                        "stale",
                        json.dumps(
                            stale_legacy_marker,
                            ensure_ascii=False,
                        ),
                        "stale-legacy-event",
                        1.0,
                        stale_legacy_marker,
                        "2026-07-16T00:00:00+00:00",
                    ),
                )
                connection.commit()

            replayed = replay_continuity(root)
            self.assertEqual("completed", replayed["status"])
            state_snapshot_path = Path(
                replayed["state_snapshot"]["path"]
            )
            self.assertTrue(state_snapshot_path.is_file())
            state_snapshot = json.loads(
                state_snapshot_path.read_text(encoding="utf-8")
            )
            self.assertEqual(2, state_snapshot["schema_version"])
            self.assertEqual(
                "continuity_v5",
                replayed["state_snapshot"]["authority"],
            )
            self.assertEqual(
                len(state_snapshot["facts"]),
                replayed["state_snapshot"]["facts_count"],
            )
            state_snapshot_text = json.dumps(
                state_snapshot,
                ensure_ascii=False,
                sort_keys=True,
            )
            self.assertNotIn(marker, state_snapshot_text)
            self.assertNotIn(stale_legacy_marker, state_snapshot_text)
            with closing(
                sqlite3.connect(root / ".plot-rag" / "state.sqlite3")
            ) as connection:
                stale_row = connection.execute(
                    """
                    SELECT value_json
                    FROM current_facts
                    WHERE fact_key='stale-legacy-snapshot-fixture'
                    """
                ).fetchone()
            self.assertIsNotNone(stale_row)
            self.assertIn(stale_legacy_marker, str(stale_row[0]))
            snapshot_check = next(
                check
                for check in state_doctor(root)["checks"]
                if check["name"] == "snapshot"
            )
            self.assertEqual("ok", snapshot_check["status"])
            self.assertEqual([], memory.query(marker, branch_id="main"))
            self.assertEqual(
                [],
                summaries.query(
                    marker,
                    branch_id="main",
                    chapter_no=1,
                ),
            )
            self.assertEqual([], patterns.query(marker, task="prose"))

    def test_superseding_revision_replaces_all_longform_derived_content(
        self,
    ) -> None:
        old_marker = "旧修订标记_沉钟旧港"
        new_marker = "新修订标记_星火新城"
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            service = ContinuityService(root)
            entity = service.register_entity("world", "修订替换测试")

            def accept_revision(
                marker: str,
                *,
                chapter_no: int,
                arc_id: str,
                volume_id: str,
            ) -> dict[str, object]:
                revision = service.get_canon_revisions()["active"]
                proposal = service.save_proposal(
                    events=[
                        {
                            "event_type": "state",
                            "entity_id": entity["entity_id"],
                            "field": "revision_fixture",
                            "value": marker,
                        },
                        {
                            "event_type": "world_rule",
                            "scope": "timeless",
                            "entity_id": entity["entity_id"],
                            "field": "timeless_revision_fixture",
                            "value": f"{marker}_TIMELESS",
                        }
                    ],
                    payload={
                        "assistant_text": f"{marker} 只属于当前修订正文。",
                        "summary": f"{marker} 当前修订摘要。",
                        "success_pattern": f"{marker} 当前修订方法。",
                        "artifact_context": {"task": "prose"},
                        "genre": "fantasy",
                        "arc_id": arc_id,
                        "volume_id": volume_id,
                    },
                    artifact_id="chapter-superseding-fixture",
                    artifact_stage="final",
                    chapter_no=chapter_no,
                    prepared_canon_revision=revision,
                )
                grant = issue_host_approval(
                    root,
                    proposal["proposal_id"],
                    expected_canon_revision=revision,
                    issuer="unittest-host",
                    channel="interactive_test",
                )
                return accept_plot_proposal(
                    root,
                    proposal["proposal_id"],
                    approval_id=grant["grant"]["approval_id"],
                    expected_canon_revision=revision,
                )

            first = accept_revision(
                old_marker,
                chapter_no=3,
                arc_id="arc-old",
                volume_id="volume-old",
            )
            longform_db = root / ".plot-rag" / "longform.v1.sqlite3"
            memory = LayeredMemoryStore(longform_db)
            summaries = AcceptedSummaryStore(longform_db)
            patterns = ProjectPatternStore(longform_db)
            self.assertTrue(memory.query(old_marker, branch_id="main"))
            self.assertTrue(summaries.query(old_marker, branch_id="main"))
            self.assertTrue(patterns.query(old_marker, task="prose"))

            second = accept_revision(
                new_marker,
                chapter_no=4,
                arc_id="arc-new",
                volume_id="volume-new",
            )
            self.assertGreater(
                int(second["commit"]["artifact_revision"]),
                int(first["commit"]["artifact_revision"]),
            )
            self.assertEqual(
                [second["commit"]["commit_id"]],
                [
                    commit["commit_id"]
                    for commit in service.list_active_accepted_commits()
                ],
            )

            def assert_only_new_revision_remains() -> None:
                old_memory = memory.query(old_marker, branch_id="main")
                old_summaries = summaries.query(
                    old_marker,
                    branch_id="main",
                )
                old_patterns = patterns.query(old_marker, task="prose")
                self.assertNotIn(
                    old_marker,
                    json.dumps(old_memory, ensure_ascii=False),
                )
                self.assertNotIn(
                    old_marker,
                    json.dumps(old_summaries, ensure_ascii=False),
                )
                self.assertNotIn(
                    old_marker,
                    json.dumps(old_patterns, ensure_ascii=False),
                )
                self.assertIn(
                    new_marker,
                    json.dumps(
                        memory.query(new_marker, branch_id="main"),
                        ensure_ascii=False,
                    ),
                )
                self.assertIn(
                    new_marker,
                    json.dumps(
                        summaries.query(new_marker, branch_id="main"),
                        ensure_ascii=False,
                    ),
                )
                self.assertIn(
                    new_marker,
                    json.dumps(
                        patterns.query(new_marker, task="prose"),
                        ensure_ascii=False,
                    ),
                )

                chapters = summaries.list("chapter")
                arcs = summaries.list("arc")
                volumes = summaries.list("volume")
                self.assertEqual(
                    ["main/volume-new/arc-new/000004"],
                    [item["subject_id"] for item in chapters],
                )
                self.assertEqual(
                    ["main/volume-new/arc-new"],
                    [item["subject_id"] for item in arcs],
                )
                self.assertEqual(
                    ["main/volume-new"],
                    [item["subject_id"] for item in volumes],
                )
                self.assertTrue(
                    all(
                        item["source_commits"]
                        == [second["commit"]["commit_id"]]
                        for item in chapters + arcs + volumes
                    )
                )

            assert_only_new_revision_remains()
            replayed = replay_continuity(root)
            self.assertEqual("completed", replayed["status"])
            assert_only_new_revision_remains()
            continuity_snapshot = json.loads(
                Path(replayed["snapshot_path"]).read_text(encoding="utf-8")
            )
            state_snapshot = json.loads(
                Path(replayed["state_snapshot"]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            state_snapshot_text = json.dumps(
                state_snapshot,
                ensure_ascii=False,
                sort_keys=True,
            )
            self.assertNotIn(old_marker, state_snapshot_text)
            self.assertIn(new_marker, state_snapshot_text)
            self.assertIn(f"{new_marker}_TIMELESS", state_snapshot_text)
            self.assertEqual(
                {"current", "timeless"},
                {fact["scope"] for fact in state_snapshot["facts"]},
            )
            self.assertEqual(
                len(continuity_snapshot["facts"]),
                len(state_snapshot["facts"]),
            )
            self.assertEqual(
                continuity_snapshot["updated_at"],
                state_snapshot["updated_at"],
            )

    def test_v3_replay_snapshot_preserves_relation_dimensions_and_scope_keys(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            service = ContinuityService(root)
            actor = service.register_entity("character", "甲")
            target = service.register_entity("character", "乙")
            proposal = service.save_proposal(
                events=[
                    {
                        "event_type": "relation",
                        "source_entity_id": actor["entity_id"],
                        "target_entity_id": target["entity_id"],
                        "dimension": "信任",
                        "value": {"score": 0.8},
                    },
                    {
                        "event_type": "relation",
                        "source_entity_id": actor["entity_id"],
                        "target_entity_id": target["entity_id"],
                        "dimension": "敌意",
                        "value": {"score": 0.6},
                    },
                    {
                        "event_type": "state",
                        "scope": "current",
                        "entity_id": actor["entity_id"],
                        "field": "双态语义键",
                        "value": "当前态",
                    },
                    {
                        "event_type": "state",
                        "scope": "timeless",
                        "entity_id": actor["entity_id"],
                        "field": "双态语义键",
                        "value": "恒常态",
                    },
                ],
                artifact_id="snapshot-key-collision-fixture",
                artifact_stage="final",
                prepared_canon_revision=0,
            )
            grant = issue_host_approval(
                root,
                proposal["proposal_id"],
                expected_canon_revision=0,
                issuer="unittest-host",
                channel="interactive_test",
            )
            accepted = accept_plot_proposal(
                root,
                proposal["proposal_id"],
                approval_id=grant["grant"]["approval_id"],
                expected_canon_revision=0,
            )
            self.assertEqual("accepted", accepted["status"])

            def inspect_replay() -> tuple[list[str], dict[str, object]]:
                replayed = replay_continuity(root)
                continuity_snapshot = json.loads(
                    Path(replayed["snapshot_path"]).read_text(
                        encoding="utf-8"
                    )
                )
                state_snapshot = json.loads(
                    Path(replayed["state_snapshot"]["path"]).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(2, state_snapshot["schema_version"])
                self.assertEqual(
                    "continuity_v5",
                    replayed["state_snapshot"]["authority"],
                )
                self.assertEqual(
                    continuity_snapshot["updated_at"],
                    state_snapshot["updated_at"],
                )
                self.assertEqual(
                    len(continuity_snapshot["facts"]),
                    len(state_snapshot["facts"]),
                )

                relations = [
                    fact
                    for fact in state_snapshot["facts"]
                    if fact["category"] == "relationship"
                ]
                self.assertEqual(
                    {"信任", "敌意"},
                    {fact["field"] for fact in relations},
                )
                self.assertTrue(
                    all(
                        fact["value"]["target"] == "乙"
                        for fact in relations
                    )
                )

                scoped = [
                    fact
                    for fact in state_snapshot["facts"]
                    if fact["subject"] == "甲"
                    and fact["field"] == "双态语义键"
                ]
                self.assertEqual(
                    {"current", "timeless"},
                    {fact["scope"] for fact in scoped},
                )
                self.assertEqual(2, len(scoped))
                self.assertEqual(
                    2,
                    len({fact["fact_key"] for fact in scoped}),
                )
                all_keys = [
                    str(fact["fact_key"])
                    for fact in state_snapshot["facts"]
                ]
                self.assertEqual(len(all_keys), len(set(all_keys)))

                source_scoped = [
                    fact
                    for fact in continuity_snapshot["facts"]
                    if fact["fact_type"] == "state"
                    and fact["field"] == "双态语义键"
                ]
                self.assertEqual(2, len(source_scoped))
                self.assertEqual(
                    1,
                    len({fact["fact_key"] for fact in source_scoped}),
                )
                return sorted(all_keys), replayed

            first_keys, _first = inspect_replay()
            second_keys, _second = inspect_replay()
            self.assertEqual(first_keys, second_keys)

    def test_longform_index_uses_only_active_manifest_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            unapproved = root / "正文" / "第二章.md"
            unapproved.write_text("未批准的第二章内容。", encoding="utf-8")
            approved = root / "正文" / "第一章.md"
            approved_hash = hashlib.sha256(approved.read_bytes()).hexdigest()
            service = ContinuityService(root)
            entity = service.register_entity("world", "索引门禁")
            proposal = service.save_proposal(
                events=[
                    {
                        "event_type": "state",
                        "entity_id": entity["entity_id"],
                        "field": "manifest_fixture",
                        "value": True,
                    }
                ],
                artifact_id="manifest-fixture",
                artifact_stage="final",
            )
            grant = issue_host_approval(
                root,
                proposal["proposal_id"],
                expected_canon_revision=0,
                issuer="unittest-host",
                channel="interactive_test",
            )
            accepted = accept_plot_proposal(
                root,
                proposal["proposal_id"],
                approval_id=grant["grant"]["approval_id"],
                expected_canon_revision=0,
            )
            with service.store.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO accepted_source_manifest(
                        manifest_entry_id, commit_id, source_id, source_path,
                        content_hash, source_role, manifest_status,
                        metadata_json, created_at, activated_at
                    ) VALUES(
                        'manifest-fixture', ?, 'source-fixture',
                        '正文/第一章.md', ?, 'canon', 'active', '{}',
                        'fixture', 'fixture'
                    )
                    """,
                    (accepted["commit"]["commit_id"], approved_hash),
                )

            refreshed = refresh_longform_index(root)
            self.assertEqual(
                "active_accepted_manifest",
                refreshed["source_gate"]["mode"],
            )
            self.assertEqual(1, refreshed["schema"]["file_count"])
            with closing(
                sqlite3.connect(
                    root / ".plot-rag" / "authority.v1.sqlite3"
                )
            ) as connection:
                paths = {
                    row[0]
                    for row in connection.execute(
                        "SELECT path FROM authority_files"
                    )
                }
            self.assertEqual({"正文/第一章.md"}, paths)

            approved.write_text("签发后发生漂移。", encoding="utf-8")
            drifted = refresh_longform_index(root)
            self.assertEqual(
                1,
                drifted["refresh"]["manifest_hash_mismatches"],
            )
            self.assertEqual(0, drifted["schema"]["file_count"])

    def test_vector_projection_materializes_valid_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            self.enable_test_embedding(root)
            with (
                patch.dict(
                    os.environ,
                    {
                        "PLOT_RAG_EMBED_API_KEY": (
                            "TOKEN_TEST_ONLY_VECTOR_PROJECTOR"
                        )
                    },
                    clear=False,
                ),
                patch(
                    "v1_runtime.state_rag._embedding_call",
                    return_value=(
                        [[0.125, 0.5, 0.875]],
                        {"status": "ok"},
                    ),
                ),
            ):
                accepted = self.accept_projection_fixture(
                    root,
                    fixture_id="embedding-success",
                )

        vector_run = accepted["projections"]["runs"]["vector"]
        vector = vector_run["output"]
        self.assertEqual("succeeded", vector_run["status"])
        self.assertEqual("success", vector["status"])
        self.assertTrue(vector["projected"])
        self.assertTrue(vector["lexical_ready"])
        self.assertTrue(vector["embedding_enabled"])
        self.assertGreater(vector["chunk_count"], 0)
        self.assertGreater(vector["vector_count"], 0)
        self.assertGreater(vector["refresh"]["embedding_attempts"], 0)
        self.assertGreater(vector["refresh"]["embedding_calls"], 0)
        self.assertEqual(0, vector["refresh"]["embedding_failures"])
        self.assertEqual("ok", vector["embedding_readiness"]["status"])

    def test_vector_projection_degrades_without_embedding_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            self.enable_test_embedding(root)
            with (
                patch.dict(
                    os.environ,
                    {
                        "PLOT_RAG_EMBED_API_KEY": (
                            "TOKEN_TEST_ONLY_VECTOR_PROJECTOR"
                        )
                    },
                    clear=False,
                ),
                patch(
                    "v1_runtime.state_rag._embedding_call",
                    return_value=(
                        [[0.125, 0.5, 0.875]],
                        {"status": "ok"},
                    ),
                ),
            ):
                seeded = refresh_longform_index(
                    root,
                    with_embeddings=True,
                )
            self.assertGreater(seeded["schema"]["vector_count"], 0)
            with patch.dict(
                os.environ,
                {"PLOT_RAG_EMBED_API_KEY": ""},
                clear=False,
            ):
                accepted = self.accept_projection_fixture(
                    root,
                    fixture_id="embedding-missing-key",
                )

        vector_run = accepted["projections"]["runs"]["vector"]
        vector = vector_run["output"]
        self.assertEqual("accepted", accepted["status"])
        self.assertEqual("degraded", accepted["projections"]["status"])
        self.assertEqual("degraded", vector_run["status"])
        self.assertEqual("degraded", vector["status"])
        self.assertTrue(vector["projected"])
        self.assertFalse(vector["semantic_ready"])
        self.assertTrue(vector["lexical_ready"])
        self.assertGreater(vector["chunk_count"], 0)
        self.assertGreater(vector["vector_count"], 0)
        self.assertEqual(0, vector["refresh"]["embedding_attempts"])
        self.assertEqual(0, vector["refresh"]["embedding_failures"])
        self.assertEqual(
            "unconfigured",
            vector["embedding_readiness"]["status"],
        )

    def test_vector_projection_degrades_when_remote_embedding_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            self.enable_test_embedding(root)
            with (
                patch.dict(
                    os.environ,
                    {
                        "PLOT_RAG_EMBED_API_KEY": (
                            "TOKEN_TEST_ONLY_VECTOR_PROJECTOR"
                        )
                    },
                    clear=False,
                ),
                patch(
                    "v1_runtime.state_rag._embedding_call",
                    side_effect=RuntimeError("fixture embedding outage"),
                ),
            ):
                accepted = self.accept_projection_fixture(
                    root,
                    fixture_id="embedding-remote-failure",
                )

        vector_run = accepted["projections"]["runs"]["vector"]
        vector = vector_run["output"]
        self.assertEqual("accepted", accepted["status"])
        self.assertEqual("degraded", accepted["projections"]["status"])
        self.assertEqual("failed", vector_run["status"])
        self.assertEqual("failed", vector["status"])
        self.assertFalse(vector["projected"])
        self.assertTrue(vector["lexical_ready"])
        self.assertGreater(vector["refresh"]["embedding_failures"], 0)
        self.assertEqual("failed", vector["embedding_readiness"]["status"])

    def test_vector_projection_refresh_exception_is_journaled_as_failed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            original_refresh = refresh_longform_index

            def refresh_with_vector_failure(
                project_root: Path | str,
                *,
                with_embeddings: bool = False,
            ) -> dict[str, object]:
                if with_embeddings:
                    raise RuntimeError("fixture base refresh failure")
                return original_refresh(
                    project_root,
                    with_embeddings=False,
                )

            with patch(
                "v1_runtime.refresh_longform_index",
                side_effect=refresh_with_vector_failure,
            ):
                accepted = self.accept_projection_fixture(
                    root,
                    fixture_id="embedding-refresh-failure",
                )

            self.assertEqual("accepted", accepted["status"])
            self.assertEqual("degraded", accepted["projections"]["status"])
            self.assertNotIn("vector", accepted["projections"]["runs"])
            failure = next(
                item
                for item in accepted["projections"]["failures"]
                if item["projection"] == "vector"
            )
            self.assertIn("fixture base refresh failure", failure["reason"])
            with closing(
                sqlite3.connect(
                    root / ".plot-rag" / "projection-runs.v1.sqlite3"
                )
            ) as connection:
                journal = connection.execute(
                    """
                    SELECT status, error_text
                    FROM projection_runs
                    WHERE projection_name = 'vector'
                    ORDER BY started_at DESC
                    LIMIT 1
                    """
                ).fetchone()
            self.assertEqual("failed", journal[0])
            self.assertIn("fixture base refresh failure", journal[1])

    def test_initialization_freeze_grant_apply_materialize_and_verify(self) -> None:
        complete_seed = runpy.run_path(
            str(PLUGIN_ROOT / "tests" / "test_plot_init.py")
        )["complete_seed"]
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = self.make_project(workspace)
            config_path = root / ".plot-rag" / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["initialization"] = {
                "schema_version": "plot-rag-init/v1",
                "database_path": ".plot-rag/init.sqlite3",
                "proposal_only": True,
            }
            config_path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.enable_test_embedding(root)
            initializer = PlotInitService(
                workspace,
                database_path=root / ".plot-rag" / "init.sqlite3",
            )
            started = initializer.start(
                project_root=root,
                mode="new",
                seed=complete_seed(),
                expected_canon_revision=0,
                idempotency_key="init-start",
            )
            frozen = initializer.propose(
                started["session_id"],
                expected_session_revision=started["session_revision"],
                idempotency_key="init-propose",
            )["proposal"]
            registered = register_initialization_proposal(
                root,
                frozen["proposal_id"],
                workspace_root=workspace,
            )
            self.assertEqual(
                "valid",
                registered["proposal"]["validation_status"],
            )
            with closing(
                sqlite3.connect(root / ".plot-rag" / "state.sqlite3")
            ) as connection:
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM entities"
                    ).fetchone()[0],
                )
            grant = issue_host_approval(
                root,
                frozen["proposal_id"],
                workspace_root=workspace,
                expected_canon_revision=0,
                issuer="unittest-host",
                channel="interactive_test",
            )
            self.assertEqual(
                {"accept_initialization", "materialize"},
                set(grant["grant"]["operations"]),
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "PLOT_RAG_EMBED_API_KEY": (
                            "TOKEN_TEST_ONLY_INITIALIZATION_PROJECTOR"
                        )
                    },
                    clear=False,
                ),
                patch(
                    "v1_runtime.state_rag._embedding_call",
                    return_value=(
                        [[0.125, 0.5, 0.875]],
                        {"status": "ok"},
                    ),
                ),
            ):
                applied = apply_initialization_proposal(
                    root,
                    frozen["proposal_id"],
                    workspace_root=workspace,
                    approval_id=grant["grant"]["approval_id"],
                    expected_canon_revision=0,
                    idempotency_key="init-apply",
                )
            self.assertEqual("completed", applied["status"])
            self.assertEqual(
                "completed",
                applied["materialization"]["status"],
            )
            self.assertTrue(applied["bootstrap_ready"])
            self.assertEqual(
                "COMPLETED",
                applied["initialization_session"]["status"],
            )
            self.assertIsNone(initializer.find_active_session(project_root=root))
            receipt = json.loads(
                (root / ".plot-rag" / "completion-receipt.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(receipt["bootstrap_ready"])
            self.assertEqual(
                applied["commit"]["commit_id"],
                receipt["commit_id"],
            )
            repeated = apply_initialization_proposal(
                root,
                frozen["proposal_id"],
                workspace_root=workspace,
                approval_id=grant["grant"]["approval_id"],
                expected_canon_revision=0,
                idempotency_key="init-apply",
            )
            self.assertEqual(
                applied["commit"]["commit_id"],
                repeated["commit"]["commit_id"],
            )
            verified = verify_initialization(
                root,
                applied["commit"]["commit_id"],
            )
            self.assertEqual("verified", verified["status"])
            self.assertTrue(verified["bootstrap_ready"])
            self.assertTrue((root / "设定集" / "世界内核.md").is_file())
            self.assertGreater(
                len(ContinuityService(root).query_facts()["facts"]),
                0,
            )
            with closing(
                sqlite3.connect(root / ".plot-rag" / "state.sqlite3")
            ) as connection:
                self.assertGreater(
                    connection.execute(
                        "SELECT COUNT(*) FROM entities"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    "1",
                    connection.execute(
                        "SELECT value FROM state_meta "
                        "WHERE key='bootstrap_ready'"
                    ).fetchone()[0],
                )

            before_doctor = self.tree_fingerprints(root)
            health = doctor_v1(root)
            after_doctor = self.tree_fingerprints(root)
            self.assertEqual(before_doctor, after_doctor)
            self.assertTrue(health["zero_write"])
            self.assertTrue(health["bootstrap_ready"])
            self.assertEqual(
                "ready",
                health["components"]["bootstrap_readiness"]["status"],
            )
            for name in (
                "config",
                "state",
                "continuity",
                "authority_index",
                "initialization_store",
                "longform_memory",
                "longform_summary",
                "longform_method",
                "longform_projection",
            ):
                self.assertEqual(
                    "ok",
                    health["components"][name]["status"],
                    name,
                )

            bootstrap_commit_id = receipt["commit_id"]
            bootstrap_projection_hash = receipt["projection_hash"]
            actor = ContinuityService(root).register_entity(
                "character",
                "初始化后推进角色",
            )["entity_id"]
            service = ContinuityService(root)
            for chapter_no in range(1, 4):
                active_revision = service.get_canon_revisions()["active"]
                proposal = service.save_proposal(
                    events=[
                        {
                            "event_type": "state",
                            "entity_id": actor,
                            "field": "progress",
                            "value": f"推进到第{chapter_no}章",
                        }
                    ],
                    artifact_id=f"post-bootstrap-chapter-{chapter_no}",
                    artifact_stage="final",
                    chapter_no=chapter_no,
                    scene_index=0,
                    prepared_canon_revision=active_revision,
                )
                chapter_grant = issue_host_approval(
                    root,
                    proposal["proposal_id"],
                    expected_canon_revision=active_revision,
                    issuer="unittest-host",
                    channel="interactive_test",
                )
                accepted = accept_plot_proposal(
                    root,
                    proposal["proposal_id"],
                    approval_id=chapter_grant["grant"]["approval_id"],
                    expected_canon_revision=active_revision,
                )
                self.assertEqual("accepted", accepted["status"])

                post_commit_health = doctor_v1(root)
                bootstrap = post_commit_health["components"][
                    "bootstrap_readiness"
                ]
                self.assertNotEqual(
                    bootstrap_projection_hash,
                    bootstrap["active_projection"]["projection_hash"],
                )
                self.assertEqual(
                    bootstrap_projection_hash,
                    bootstrap["bootstrap_projection"]["projection_hash"],
                )
                self.assertEqual(
                    bootstrap_commit_id,
                    bootstrap["commit_id"],
                )
                self.assertEqual(
                    "ready",
                    bootstrap["status"],
                    bootstrap["failed_validations"],
                )
                self.assertTrue(bootstrap["ready"])
                self.assertTrue(post_commit_health["bootstrap_ready"])
                self.assertNotIn(
                    "bootstrap_readiness",
                    post_commit_health["failed_checks"],
                )

    def test_rich_ingest_and_hybrid_apply_normalized_active_manifest(self) -> None:
        complete_seed = runpy.run_path(
            str(PLUGIN_ROOT / "tests" / "test_plot_init.py")
        )["complete_seed"]
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            for mode in ("ingest", "hybrid"):
                with self.subTest(mode=mode):
                    mode_workspace = workspace / mode
                    root = self.make_project(mode_workspace)
                    (root / "资料").mkdir()
                    (root / "资料" / "已有作品.json").write_text(
                        json.dumps(
                            complete_seed(),
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    config_path = root / ".plot-rag" / "config.json"
                    config = json.loads(config_path.read_text(encoding="utf-8"))
                    config["initialization"] = {
                        "schema_version": "plot-rag-init/v1",
                        "database_path": ".plot-rag/init.sqlite3",
                        "proposal_only": True,
                    }
                    config_path.write_text(
                        json.dumps(config, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    self.enable_test_embedding(root)
                    initializer = PlotInitService(
                        mode_workspace,
                        database_path=root / ".plot-rag" / "init.sqlite3",
                    )
                    start_kwargs = {
                        "project_root": root,
                        "mode": mode,
                        "sources": [root],
                        "expected_canon_revision": 0,
                        "idempotency_key": f"{mode}-start",
                    }
                    if mode == "hybrid":
                        start_kwargs["seed"] = complete_seed()
                    started = initializer.start(**start_kwargs)
                    frozen = initializer.propose(
                        started["session_id"],
                        expected_session_revision=started["session_revision"],
                        idempotency_key=f"{mode}-propose",
                    )["proposal"]
                    registered = register_initialization_proposal(
                        root,
                        frozen["proposal_id"],
                        workspace_root=mode_workspace,
                    )
                    self.assertEqual(
                        "valid",
                        registered["proposal"]["validation_status"],
                    )
                    grant = issue_host_approval(
                        root,
                        frozen["proposal_id"],
                        workspace_root=mode_workspace,
                        expected_canon_revision=0,
                        issuer="unittest-host",
                        channel="interactive_test",
                    )
                    with (
                        patch.dict(
                            os.environ,
                            {
                                "PLOT_RAG_EMBED_API_KEY": (
                                    "TOKEN_TEST_ONLY_INITIALIZATION_PROJECTOR"
                                )
                            },
                            clear=False,
                        ),
                        patch(
                            "v1_runtime.state_rag._embedding_call",
                            return_value=(
                                [[0.125, 0.5, 0.875]],
                                {"status": "ok"},
                            ),
                        ),
                    ):
                        applied = apply_initialization_proposal(
                            root,
                            frozen["proposal_id"],
                            workspace_root=mode_workspace,
                            approval_id=grant["grant"]["approval_id"],
                            expected_canon_revision=0,
                            idempotency_key=f"{mode}-apply",
                        )
                    self.assertEqual("completed", applied["status"])
                    self.assertTrue(applied["bootstrap_ready"])
                    verified = verify_initialization(
                        root,
                        applied["commit"]["commit_id"],
                    )
                    self.assertEqual("verified", verified["status"])
                    health = doctor_v1(root)
                    self.assertTrue(health["bootstrap_ready"])
                    self.assertEqual(
                        "ready",
                        health["components"]["bootstrap_readiness"]["status"],
                    )

                    active_manifest = (
                        ContinuityService(root).get_accepted_source_manifest()
                    )
                    self.assertTrue(active_manifest)
                    inventory_entries = [
                        item
                        for item in active_manifest
                        if item["metadata"].get("inventory_path")
                    ]
                    self.assertTrue(inventory_entries)
                    self.assertTrue(
                        all(
                            ".plot-rag"
                            not in Path(item["metadata"]["real_path"]).parts
                            for item in inventory_entries
                        )
                    )
                    for item in active_manifest:
                        accepted_path = Path(item["path"])
                        if accepted_path.is_absolute():
                            continue
                        target = (root / accepted_path).resolve()
                        self.assertTrue(target.is_file(), item["path"])
                        self.assertEqual(
                            item["content_hash"],
                            hashlib.sha256(target.read_bytes()).hexdigest(),
                            item["path"],
                        )
                        self.assertFalse(
                            item["path"].startswith("source-"),
                            item["path"],
                        )

                    refreshed = refresh_longform_index(root)
                    self.assertEqual(
                        0,
                        refreshed["refresh"]["manifest_hash_mismatches"],
                    )
                    self.assertGreater(refreshed["schema"]["file_count"], 0)

                    bootstrap_commit_id = applied["commit"]["commit_id"]
                    service = ContinuityService(root)
                    actor = service.register_entity(
                        "character",
                        f"{mode} 初始化后推进角色",
                    )["entity_id"]
                    for chapter_no in range(1, 4):
                        active_revision = service.get_canon_revisions()["active"]
                        proposal = service.save_proposal(
                            events=[
                                {
                                    "event_type": "state",
                                    "entity_id": actor,
                                    "field": "progress",
                                    "value": f"推进到第{chapter_no}章",
                                }
                            ],
                            artifact_id=(
                                f"{mode}-post-bootstrap-chapter-{chapter_no}"
                            ),
                            artifact_stage="final",
                            chapter_no=chapter_no,
                            scene_index=0,
                            prepared_canon_revision=active_revision,
                        )
                        chapter_grant = issue_host_approval(
                            root,
                            proposal["proposal_id"],
                            expected_canon_revision=active_revision,
                            issuer="unittest-host",
                            channel="interactive_test",
                        )
                        accepted = accept_plot_proposal(
                            root,
                            proposal["proposal_id"],
                            approval_id=chapter_grant["grant"]["approval_id"],
                            expected_canon_revision=active_revision,
                        )
                        self.assertEqual("accepted", accepted["status"])

                        post_commit_health = doctor_v1(root)
                        bootstrap = post_commit_health["components"][
                            "bootstrap_readiness"
                        ]
                        self.assertEqual(bootstrap_commit_id, bootstrap["commit_id"])
                        self.assertEqual(
                            "ready",
                            bootstrap["status"],
                            bootstrap["failed_validations"],
                        )
                        self.assertTrue(bootstrap["ready"])
                        self.assertTrue(post_commit_health["bootstrap_ready"])


if __name__ == "__main__":
    unittest.main()
