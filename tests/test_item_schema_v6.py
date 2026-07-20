from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scripts.continuity.items import (
    migrate_legacy_item_projection,
    read_item_projection_metadata,
)
from scripts.continuity.schema import (
    CONTINUITY_V5_SCHEMA_SQL,
    CONTINUITY_V6_ADDITIVE_SCHEMA_SQL,
    LEGACY_V2_SCHEMA_SQL,
    PROJECTION_TABLES,
)
from scripts.continuity.store import ContinuityStore, StoreError
from scripts.continuity.validators import ContinuityError, normalize_event


NOW = "2026-07-17T00:00:00.000000Z"


class ItemSchemaV6Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database = self.root / ".plot-rag" / "state.sqlite3"
        self.database.parent.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_v5_fixture(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(
                LEGACY_V2_SCHEMA_SQL + "\n" + CONTINUITY_V5_SCHEMA_SQL
            )
            connection.executemany(
                """
                INSERT INTO state_meta(key, value, updated_at)
                VALUES(?, ?, ?)
                """,
                (
                    ("schema_version", "2", NOW),
                    ("continuity_schema_version", "5", NOW),
                    ("head_canon_revision", "1", NOW),
                    ("active_canon_revision", "1", NOW),
                    ("legacy_query_hash", "query_hash_must_survive", NOW),
                ),
            )
            connection.executemany(
                """
                INSERT INTO entities(
                    entity_id, entity_type, canonical_name, normalized_name,
                    attributes_json, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        "actor_owner",
                        "character",
                        "持有人",
                        "持有人",
                        "{}",
                        NOW,
                        NOW,
                    ),
                    (
                        "item_unique",
                        "item",
                        "唯一旧物",
                        "唯一旧物",
                        json.dumps(
                            {"claimed_function": "不得据此猜测功能"},
                            ensure_ascii=False,
                        ),
                        NOW,
                        NOW,
                    ),
                    (
                        "item_ordinary",
                        "item",
                        "普通旧物",
                        "普通旧物",
                        json.dumps(
                            {"claimed_function": "同样不得猜测"},
                            ensure_ascii=False,
                        ),
                        NOW,
                        NOW,
                    ),
                    (
                        "item_without_inventory",
                        "item",
                        "无库存旧物",
                        "无库存旧物",
                        "{}",
                        NOW,
                        NOW,
                    ),
                ),
            )
            connection.execute(
                """
                INSERT INTO artifacts(
                    artifact_version_id, artifact_id, artifact_kind,
                    artifact_stage, canon_status, branch_id, chapter_no,
                    scene_index, artifact_revision, source_role, content_hash,
                    content_json, active, created_at, updated_at
                ) VALUES(
                    'artifact-version-1', 'artifact-1', 'story_delta', 'final',
                    'accepted', 'main', 1, 0, 1, 'canon', 'content-hash',
                    '{}', 1, ?, ?
                )
                """,
                (NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO proposals(
                    proposal_id, artifact_version_id, artifact_id,
                    artifact_stage, canon_status, branch_id, chapter_no,
                    scene_index, artifact_revision, prepared_canon_revision,
                    source_role, proposal_kind, payload_hash, payload_json,
                    events_json, validation_status, status_reason,
                    accepted_commit_id, created_at, updated_at
                ) VALUES(
                    'proposal-1', 'artifact-version-1', 'artifact-1', 'final',
                    'accepted', 'main', 1, 0, 1, 0, 'canon', 'story_delta',
                    'payload-hash', '{}', '[]', 'valid', '', 'commit-1', ?, ?
                )
                """,
                (NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO approval_grants(
                    token_hash, proposal_id, binding_hash, binding_json,
                    authorized_operations_json, expected_canon_revision,
                    issuer, channel, expires_at, consumed_request_hash,
                    accepted_commit_id, consumed_at, created_at
                ) VALUES(
                    'grant-1', 'proposal-1', 'binding-hash', '{}', '["accept"]',
                    0, 'fixture', 'test', ?, 'request-hash', 'commit-1', ?, ?
                )
                """,
                (NOW, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO canon_commits(
                    commit_id, proposal_id, operation, artifact_id,
                    artifact_stage, branch_id, chapter_no, scene_index,
                    artifact_revision, head_revision_before,
                    head_revision_after, active_revision_before,
                    active_revision_after, changes_authority,
                    accepted_request_hash, grant_token_hash, payload_hash,
                    projection_hash, acceptance_source_json, created_at
                ) VALUES(
                    'commit-1', 'proposal-1', 'accept', 'artifact-1', 'final',
                    'main', 1, 0, 1, 0, 1, 0, 1, 1, 'accepted-request-hash',
                    'grant-1', 'payload-hash', 'legacy-projection-hash', '{}', ?
                )
                """,
                (NOW,),
            )
            for ordinal, item_id in enumerate(
                ("item_unique", "item_ordinary")
            ):
                event_id = f"event-{ordinal + 1}"
                payload = {
                    "event_type": "inventory",
                    "item_entity_id": item_id,
                    "action": "set",
                    "to_owner_entity_id": "actor_owner",
                    "quantity": 1 if item_id == "item_unique" else 3,
                    "unique": item_id == "item_unique",
                }
                connection.execute(
                    """
                    INSERT INTO continuity_events(
                        event_id, commit_id, event_ordinal, event_type, scope,
                        branch_id, artifact_id, artifact_revision, chapter_no,
                        scene_index, story_time, narrative_mode, entity_id,
                        subject_entity_id, target_entity_id, payload_json,
                        evidence_json, created_at
                    ) VALUES(
                        ?, 'commit-1', ?, 'inventory', 'current', 'main',
                        'artifact-1', 1, 1, 0, NULL, 'linear', ?, ?,
                        'actor_owner', ?, '{"quote":"fixture"}', ?
                    )
                    """,
                    (
                        event_id,
                        ordinal,
                        item_id,
                        item_id,
                        json.dumps(payload, ensure_ascii=False),
                        NOW,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO inventory_state(
                        inventory_key, item_entity_id, owner_entity_id,
                        quantity, is_unique, item_status, source_event_id,
                        updated_order
                    ) VALUES(?, ?, 'actor_owner', ?, ?, 'owned', ?, ?)
                    """,
                    (
                        f"inventory-{ordinal + 1}",
                        item_id,
                        payload["quantity"],
                        int(payload["unique"]),
                        event_id,
                        ordinal + 1,
                    ),
                )
            connection.execute(
                """
                INSERT INTO event_links(
                    link_id, source_commit_id, source_event_id,
                    target_event_id, link_type, created_at
                ) VALUES(
                    'commit-level-link', 'commit-1', NULL,
                    'event-1', 'supersedes', ?
                )
                """,
                (NOW,),
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _rows(
        connection: sqlite3.Connection,
        table: str,
    ) -> list[tuple[object, ...]]:
        return [
            tuple(row)
            for row in connection.execute(
                f'SELECT * FROM "{table}" ORDER BY rowid'
            ).fetchall()
        ]

    @staticmethod
    def _normalized_database_hash(path: Path) -> str:
        with closing(sqlite3.connect(path)) as connection:
            payload = "\n".join(connection.iterdump()).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _legacy_query_snapshot(path: Path) -> dict[str, object]:
        with closing(sqlite3.connect(path)) as connection:
            return {
                "meta": dict(
                    connection.execute(
                        """
                        SELECT key, value FROM state_meta
                        WHERE key IN (
                            'continuity_schema_version',
                            'head_canon_revision',
                            'active_canon_revision',
                            'legacy_query_hash'
                        )
                        ORDER BY key
                        """
                    )
                ),
                "inventory": [
                    tuple(row)
                    for row in connection.execute(
                        """
                        SELECT
                            inventory_state.inventory_key,
                            entities.canonical_name,
                            inventory_state.owner_entity_id,
                            inventory_state.quantity,
                            inventory_state.is_unique,
                            inventory_state.item_status,
                            inventory_state.source_event_id
                        FROM inventory_state
                        JOIN entities
                          ON entities.entity_id =
                             inventory_state.item_entity_id
                        ORDER BY inventory_state.inventory_key
                        """
                    )
                ],
                "commit_projection": [
                    tuple(row)
                    for row in connection.execute(
                        """
                        SELECT
                            commit_id, head_revision_after,
                            active_revision_after, projection_hash
                        FROM canon_commits
                        ORDER BY head_revision_after, commit_id
                        """
                    )
                ],
            }

    def test_v5_to_v6_is_backed_up_and_preserves_every_legacy_surface(
        self,
    ) -> None:
        self._create_v5_fixture()
        before = sqlite3.connect(self.database)
        try:
            immutable_before = {
                table: self._rows(before, table)
                for table in (
                    "canon_commits",
                    "continuity_events",
                    "event_links",
                    "inventory_state",
                )
            }
            projection_before = {
                table: self._rows(before, table)
                for table in PROJECTION_TABLES
            }
            meta_before = dict(
                before.execute("SELECT key, value FROM state_meta")
            )
        finally:
            before.close()

        backup = ContinuityStore(self.root).ensure_schema()
        self.assertIsNotNone(backup)
        self.assertTrue(Path(backup).is_file())

        with closing(sqlite3.connect(Path(backup))) as archived:
            self.assertEqual(
                "5",
                archived.execute(
                    """
                    SELECT value FROM state_meta
                    WHERE key='continuity_schema_version'
                    """
                ).fetchone()[0],
            )
            self.assertIsNone(
                archived.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='item_definitions'
                    """
                ).fetchone()
            )

        migrated = sqlite3.connect(self.database)
        migrated.row_factory = sqlite3.Row
        try:
            meta_after = dict(
                migrated.execute("SELECT key, value FROM state_meta")
            )
            self.assertEqual("2", meta_after["schema_version"])
            self.assertEqual("7", meta_after["continuity_schema_version"])
            for key in (
                "schema_version",
                "head_canon_revision",
                "active_canon_revision",
                "legacy_query_hash",
            ):
                self.assertEqual(meta_before[key], meta_after[key])
            for table, expected in immutable_before.items():
                self.assertEqual(expected, self._rows(migrated, table))
            for table, expected in projection_before.items():
                self.assertEqual(expected, self._rows(migrated, table))

            definitions = {
                row["item_entity_id"]: dict(row)
                for row in migrated.execute(
                    "SELECT * FROM item_definitions ORDER BY item_entity_id"
                )
            }
            self.assertEqual(
                "legacy_self_instance",
                definitions["item_unique"]["item_status"],
            )
            self.assertEqual(
                "legacy_unmodeled",
                definitions["item_ordinary"]["item_status"],
            )
            self.assertEqual(
                "legacy_unmodeled",
                definitions["item_without_inventory"]["item_status"],
            )
            self.assertIsNone(
                definitions["item_without_inventory"]["source_event_id"]
            )
            instances = migrated.execute(
                "SELECT * FROM item_instances"
            ).fetchall()
            self.assertEqual(1, len(instances))
            self.assertEqual("item_unique", instances[0]["item_entity_id"])
            self.assertEqual(
                "legacy_self_instance",
                instances[0]["instance_status"],
            )
            for table in (
                "item_stacks",
                "item_function_definitions",
                "item_function_bindings",
                "item_custody_state",
                "item_runtime_state",
                "item_function_runtime_state",
                "item_use_history",
                "item_observations",
            ):
                self.assertEqual(
                    0,
                    migrated.execute(
                        f'SELECT COUNT(*) FROM "{table}"'
                    ).fetchone()[0],
                )
            self.assertEqual(
                1,
                migrated.execute(
                    """
                    SELECT COUNT(*) FROM canon_commits
                    WHERE operation='accept'
                    """
                ).fetchone()[0],
            )
            self.assertEqual(
                2,
                migrated.execute(
                    "SELECT COUNT(*) FROM continuity_events"
                ).fetchone()[0],
            )
            ordinary_payload = json.loads(
                definitions["item_ordinary"]["definition_json"]
            )
            self.assertEqual(
                {"claimed_function": "同样不得猜测"},
                ordinary_payload["legacy"]["attributes"],
            )
            self.assertIn("functions", ordinary_payload["unmodeled_fields"])
            item_meta = read_item_projection_metadata(migrated)
            self.assertEqual(1, item_meta["schema_version"])
            self.assertTrue(
                item_meta["projection_hash"].startswith("item_projection_")
            )
            self.assertEqual([], migrated.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            migrated.close()

    def test_v5_v7_rollback_restores_original_queries_and_projection_hash(
        self,
    ) -> None:
        self._create_v5_fixture()
        normalized_before = self._normalized_database_hash(self.database)
        query_before = self._legacy_query_snapshot(self.database)

        backup = ContinuityStore(self.root).ensure_schema()
        self.assertIsNotNone(backup)
        backup_path = Path(backup)
        self.assertTrue(backup_path.is_file())

        with closing(sqlite3.connect(self.database)) as migrated:
            self.assertEqual(
                "7",
                migrated.execute(
                    """
                    SELECT value FROM state_meta
                    WHERE key='continuity_schema_version'
                    """
                ).fetchone()[0],
            )
            self.assertIsNotNone(
                migrated.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='item_definitions'
                    """
                ).fetchone()
            )
            self.assertEqual("ok", migrated.execute("PRAGMA integrity_check").fetchone()[0])

        with closing(sqlite3.connect(backup_path)) as archived:
            self.assertEqual("ok", archived.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual(
                "5",
                archived.execute(
                    """
                    SELECT value FROM state_meta
                    WHERE key='continuity_schema_version'
                    """
                ).fetchone()[0],
            )
            self.assertIsNone(
                archived.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='item_definitions'
                    """
                ).fetchone()
            )

        failed_copy = self.database.with_name("state.failed-v6.sqlite3")
        shutil.copyfile(self.database, failed_copy)
        for sidecar in (
            Path(f"{self.database}-wal"),
            Path(f"{self.database}-shm"),
        ):
            sidecar.unlink(missing_ok=True)
        self.database.unlink()
        shutil.copyfile(backup_path, self.database)

        self.assertEqual(
            hashlib.sha256(backup_path.read_bytes()).hexdigest(),
            hashlib.sha256(self.database.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            normalized_before,
            self._normalized_database_hash(self.database),
        )
        self.assertEqual(query_before, self._legacy_query_snapshot(self.database))
        with closing(sqlite3.connect(self.database)) as restored:
            self.assertEqual("ok", restored.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual(
                "legacy-projection-hash",
                restored.execute(
                    """
                    SELECT projection_hash FROM canon_commits
                    WHERE commit_id='commit-1'
                    """
                ).fetchone()[0],
            )
            self.assertIsNone(
                restored.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='item_definitions'
                    """
                ).fetchone()
            )

    def test_legacy_projection_helper_supports_default_tuple_rows(self) -> None:
        self._create_v5_fixture()
        connection = sqlite3.connect(self.database)
        try:
            self.assertIsNone(connection.row_factory)
            connection.executescript(CONTINUITY_V6_ADDITIVE_SCHEMA_SQL)
            migrate_legacy_item_projection(connection, from_version=5)
            self.assertIsNone(connection.row_factory)
            source_event_id = connection.execute(
                """
                SELECT source_event_id FROM item_definitions
                WHERE item_entity_id='item_without_inventory'
                """
            ).fetchone()[0]
            self.assertIsNone(source_event_id)
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM item_function_definitions"
                ).fetchone()[0],
            )
        finally:
            connection.close()

    def test_item_ddl_rejects_ambiguous_fk_boolean_numeric_and_status_rows(
        self,
    ) -> None:
        self._create_v5_fixture()
        store = ContinuityStore(self.root)
        store.ensure_schema()
        with store.transaction() as connection:
            unique_definition = connection.execute(
                """
                SELECT item_definition_id FROM item_definitions
                WHERE item_entity_id='item_unique'
                """
            ).fetchone()[0]
            ordinary_definition = connection.execute(
                """
                SELECT item_definition_id FROM item_definitions
                WHERE item_entity_id='item_ordinary'
                """
            ).fetchone()[0]
            unique_instance = connection.execute(
                "SELECT item_instance_id FROM item_instances"
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO item_stacks(
                    stack_id, item_definition_id, quantity, stack_status,
                    batch_json, source_event_id, story_coordinate_json,
                    updated_order
                ) VALUES(
                    'stack-valid', ?, 3, 'active', '{}', 'event-2', '{}', 3
                )
                """,
                (ordinary_definition,),
            )
            connection.execute(
                """
                INSERT INTO item_function_definitions(
                    function_id, item_definition_id, function_status,
                    effect_owner, definition_json, source_event_id,
                    updated_order
                ) VALUES(
                    'function-valid', ?, 'active', 'inline', '{}', 'event-1', 3
                )
                """,
                (unique_definition,),
            )

            invalid_statements = (
                (
                    """
                    INSERT INTO item_function_bindings(
                        binding_id, item_definition_id, item_instance_id,
                        function_id, binding_status, binding_json,
                        source_event_id, updated_order
                    ) VALUES(
                        'binding-ambiguous', ?, ?, 'function-valid', 'active',
                        '{}', 'event-1', 4
                    )
                    """,
                    (unique_definition, unique_instance),
                ),
                (
                    """
                    INSERT INTO item_instances(
                        item_instance_id, item_definition_id, instance_status,
                        instance_json, story_coordinate_json, updated_order
                    ) VALUES(
                        'instance-bad-fk', 'definition-missing', 'active',
                        '{}', '{}', 4
                    )
                    """,
                    (),
                ),
                (
                    """
                    INSERT INTO item_runtime_state(
                        item_instance_id, sealed, damaged, destroyed, active,
                        state_json, source_event_id, story_coordinate_json,
                        updated_order
                    ) VALUES(?, 2, 0, 0, 0, '{}', 'event-1', '{}', 4)
                    """,
                    (unique_instance,),
                ),
                (
                    """
                    INSERT INTO item_stacks(
                        stack_id, item_definition_id, quantity, stack_status,
                        batch_json, source_event_id, story_coordinate_json,
                        updated_order
                    ) VALUES(
                        'stack-infinite', ?, ?, 'active', '{}', 'event-2',
                        '{}', 4
                    )
                    """,
                    (ordinary_definition, float("inf")),
                ),
                (
                    """
                    INSERT INTO item_stacks(
                        stack_id, item_definition_id, quantity, stack_status,
                        batch_json, source_event_id, story_coordinate_json,
                        updated_order
                    ) VALUES(
                        'stack-bad-status', ?, 1, 'invented', '{}', 'event-2',
                        '{}', 4
                    )
                    """,
                    (ordinary_definition,),
                ),
                (
                    """
                    INSERT INTO item_custody_state(
                        custody_key, subject_type, subject_id,
                        item_instance_id, stack_id, custody_status, quantity,
                        state_json, source_event_id, story_coordinate_json,
                        updated_order
                    ) VALUES(
                        'custody-ambiguous', 'item_instance', ?, ?,
                        'stack-valid', 'possessed', 1, '{}', 'event-1', '{}', 4
                    )
                    """,
                    (unique_instance, unique_instance),
                ),
                (
                    """
                    INSERT INTO item_use_history(
                        source_event_id, item_instance_id, stack_id,
                        function_id, actor_entity_id, action, delta_json,
                        before_json, after_json, story_coordinate_json,
                        updated_order
                    ) VALUES(
                        'event-2', ?, 'stack-valid', 'function-valid',
                        'actor_owner', 'use', '{}', '{}', '{}', '{}', 4
                    )
                    """,
                    (unique_instance,),
                ),
                (
                    """
                    INSERT INTO item_observations(
                        observation_key, observer_entity_id, item_instance_id,
                        stack_id, observation_action, knowledge_plane,
                        observation_json, source_event_id,
                        story_coordinate_json, updated_order
                    ) VALUES(
                        'observation-ambiguous', 'actor_owner', ?,
                        'stack-valid', 'observe', 'objective', '{}',
                        'event-1', '{}', 4
                    )
                    """,
                    (unique_instance,),
                ),
            )
            for sql, parameters in invalid_statements:
                with self.subTest(sql=" ".join(sql.split())[:80]):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(sql, parameters)

    def test_extraction_control_schema_surface_is_complete(self) -> None:
        ContinuityStore(self.root).ensure_schema()
        with closing(sqlite3.connect(self.database)) as connection:
            extraction_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(extraction_jobs)"
                )
            }
            self.assertEqual(
                {
                    "job_id",
                    "receipt_id",
                    "request_id",
                    "assistant_sha256",
                    "prompt_hash",
                    "retrieved_context_digest",
                    "prepared_canon_revision",
                    "active_projection_hash",
                    "intent_contract_hash",
                    "event_seed_manifest_hash",
                    "event_experience_control_revision",
                    "event_seed_references_json",
                    "experience_contract_hashes_json",
                    "artifact_context_json",
                    "branch_id",
                    "sequence_no",
                    "extract_provider",
                    "extract_base_url",
                    "extract_model",
                    "extract_schema_hash",
                    "extract_prompt_template_hash",
                    "min_confidence",
                    "generation_params_json",
                    "job_binding_hash",
                    "job_status",
                    "attempt_count",
                    "remote_status",
                    "result_kind",
                    "result_proposal_id",
                    "error",
                    "lease_owner",
                    "lease_expires_at",
                    "heartbeat_at",
                    "next_attempt_at",
                    "created_at",
                    "updated_at",
                    "started_at",
                    "completed_at",
                },
                extraction_columns,
            )
            self.assertEqual(
                {
                    "job_id",
                    "assistant_text",
                    "assistant_sha256",
                    "payload_bytes",
                    "created_at",
                    "updated_at",
                },
                {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(extraction_job_payloads)"
                    )
                },
            )
            self.assertEqual(
                {
                    "resolution_id",
                    "job_id",
                    "branch_id",
                    "sequence_no",
                    "expected_attempt_count",
                    "action",
                    "replacement_job_id",
                    "target_branch_id",
                    "reason",
                    "binding_hash",
                    "created_at",
                },
                {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(extraction_barrier_resolutions)"
                    )
                },
            )
            extraction_fks = {
                (str(row[3]), str(row[2]), str(row[4]), str(row[6]))
                for row in connection.execute(
                    "PRAGMA foreign_key_list(extraction_jobs)"
                )
            }
            self.assertIn(
                ("receipt_id", "turns", "receipt_id", "NO ACTION"),
                extraction_fks,
            )
            self.assertIn(
                (
                    "result_proposal_id",
                    "proposals",
                    "proposal_id",
                    "NO ACTION",
                ),
                extraction_fks,
            )
            self.assertIn(
                ("job_id", "extraction_jobs", "job_id", "CASCADE"),
                {
                    (str(row[3]), str(row[2]), str(row[4]), str(row[6]))
                    for row in connection.execute(
                        "PRAGMA foreign_key_list(extraction_job_payloads)"
                    )
                },
            )
            resolution_fks = {
                (str(row[3]), str(row[2]), str(row[4]))
                for row in connection.execute(
                    "PRAGMA foreign_key_list(extraction_barrier_resolutions)"
                )
            }
            self.assertEqual(
                {
                    ("job_id", "extraction_jobs", "job_id"),
                    ("replacement_job_id", "extraction_jobs", "job_id"),
                },
                resolution_fks,
            )
            extraction_indexes = {
                str(row[1]): (int(row[2]), int(row[4]))
                for row in connection.execute(
                    "PRAGMA index_list(extraction_jobs)"
                )
            }
            self.assertEqual(
                (1, 1),
                extraction_indexes["idx_extraction_jobs_result_proposal"],
            )
            self.assertEqual(
                ("branch_id", "sequence_no", "job_status", "created_at"),
                tuple(
                    str(row[2])
                    for row in connection.execute(
                        "PRAGMA index_info(idx_extraction_jobs_barrier)"
                    )
                ),
            )
            self.assertEqual(
                ("branch_id", "sequence_no", "created_at"),
                tuple(
                    str(row[2])
                    for row in connection.execute(
                        "PRAGMA index_info("
                        "idx_extraction_barrier_resolutions_branch_sequence"
                        ")"
                    )
                ),
            )

    def test_extraction_ddl_rejects_invalid_result_and_resolution_states(
        self,
    ) -> None:
        store = ContinuityStore(self.root)
        store.ensure_schema()
        with store.transaction() as connection:
            for index in (1, 2):
                connection.execute(
                    """
                    INSERT INTO turns(
                        receipt_id, request_id, prompt_hash, status, started_at
                    ) VALUES(?, ?, ?, 'completed', ?)
                    """,
                    (
                        f"receipt-{index}",
                        f"request-{index}",
                        f"prompt-{index}",
                        NOW,
                    ),
                )

            def insert_job(
                job_id: str,
                receipt_id: str,
                request_id: str,
                *,
                status: str,
                result_kind: str,
            ) -> None:
                connection.execute(
                    """
                    INSERT INTO extraction_jobs(
                        job_id, receipt_id, request_id, assistant_sha256,
                        prompt_hash, retrieved_context_digest,
                        prepared_canon_revision, active_projection_hash,
                        extract_provider, extract_base_url, extract_model,
                        extract_schema_hash, extract_prompt_template_hash,
                        min_confidence, job_binding_hash, job_status,
                        result_kind, created_at, updated_at
                    ) VALUES(
                        ?, ?, ?, ?, ?, ?, 0, ?, 'siliconflow',
                        'https://api.siliconflow.cn/v1', 'fixture-model',
                        'schema-hash', 'template-hash', 0.8, ?,
                        ?, ?, ?, ?
                    )
                    """,
                    (
                        job_id,
                        receipt_id,
                        request_id,
                        f"assistant-{job_id}",
                        f"prompt-{job_id}",
                        f"context-{job_id}",
                        f"projection-{job_id}",
                        f"binding-{job_id}",
                        status,
                        result_kind,
                        NOW,
                        NOW,
                    ),
                )

            with self.assertRaises(sqlite3.IntegrityError):
                insert_job(
                    "job-invalid",
                    "receipt-1",
                    "request-invalid",
                    status="queued",
                    result_kind="no_delta",
                )
            with self.assertRaises(sqlite3.IntegrityError):
                insert_job(
                    "job-missing-proposal",
                    "receipt-1",
                    "request-missing-proposal",
                    status="succeeded",
                    result_kind="proposal",
                )
            insert_job(
                "job-1",
                "receipt-1",
                "request-job-1",
                status="succeeded",
                result_kind="no_delta",
            )
            insert_job(
                "job-2",
                "receipt-2",
                "request-job-2",
                status="succeeded",
                result_kind="no_delta",
            )

            invalid_resolutions = (
                (
                    "resolution-attempt-negative",
                    -1,
                    "rewrite",
                    "job-2",
                    "",
                ),
                (
                    "resolution-rewrite-missing",
                    1,
                    "rewrite",
                    None,
                    "",
                ),
                (
                    "resolution-discard-target",
                    1,
                    "discard",
                    None,
                    "other",
                ),
                (
                    "resolution-branch-empty",
                    1,
                    "branch_switch",
                    None,
                    "",
                ),
            )
            for (
                resolution_id,
                attempt,
                action,
                replacement_job_id,
                target_branch_id,
            ) in invalid_resolutions:
                with self.subTest(resolution_id=resolution_id):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            """
                            INSERT INTO extraction_barrier_resolutions(
                                resolution_id, job_id, branch_id, sequence_no,
                                expected_attempt_count, action,
                                replacement_job_id, target_branch_id, reason,
                                binding_hash, created_at
                            ) VALUES(
                                ?, 'job-1', 'main', 8, ?, ?, ?, ?,
                                'fixture', 'binding-resolution', ?
                            )
                            """,
                            (
                                resolution_id,
                                attempt,
                                action,
                                replacement_job_id,
                                target_branch_id,
                                NOW,
                            ),
                        )
            connection.execute(
                """
                INSERT INTO extraction_barrier_resolutions(
                    resolution_id, job_id, branch_id, sequence_no,
                    expected_attempt_count, action, replacement_job_id,
                    target_branch_id, reason, binding_hash, created_at
                ) VALUES(
                    'resolution-valid', 'job-1', 'main', 8, 0, 'discard',
                    NULL, '', 'fixture', 'binding-resolution', ?
                )
                """,
                (NOW,),
            )

    def test_existing_v6_missing_extraction_payload_table_is_not_repaired(
        self,
    ) -> None:
        ContinuityStore(self.root).ensure_schema()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DROP TABLE extraction_job_payloads")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        finally:
            connection.close()
        before = self.database.read_bytes()
        with self.assertRaisesRegex(
            StoreError,
            "STATE_SCHEMA_V6_INCOMPLETE",
        ):
            ContinuityStore(self.root).ensure_schema()
        self.assertEqual(before, self.database.read_bytes())

    def test_future_and_corrupt_versions_fail_closed(self) -> None:
        for corrupt_value, expected in (
            ("999", "STATE_SCHEMA_TOO_NEW"),
            ("not-an-integer", "STATE_SCHEMA_UNREADABLE"),
        ):
            with self.subTest(corrupt_value=corrupt_value):
                self.database.unlink(missing_ok=True)
                self._create_v5_fixture()
                connection = sqlite3.connect(self.database)
                try:
                    connection.execute(
                        """
                        UPDATE state_meta SET value=?
                        WHERE key='continuity_schema_version'
                        """,
                        (corrupt_value,),
                    )
                    connection.commit()
                finally:
                    connection.close()
                before = self.database.read_bytes()
                with self.assertRaisesRegex(StoreError, expected):
                    ContinuityStore(self.root).ensure_schema()
                self.assertEqual(before, self.database.read_bytes())

    def test_existing_v6_projection_hash_corruption_is_not_repaired(self) -> None:
        ContinuityStore(self.root).ensure_schema()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                """
                UPDATE item_projection_meta
                SET value_json='"item_projection_corrupt"'
                WHERE meta_key='projection_hash'
                """
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        finally:
            connection.close()
        before = self.database.read_bytes()
        with self.assertRaisesRegex(
            StoreError,
            "STATE_ITEM_PROJECTION_HASH_MISMATCH",
        ):
            ContinuityStore(self.root).ensure_schema()
        self.assertEqual(before, self.database.read_bytes())

    def test_existing_v6_missing_index_is_not_recreated(self) -> None:
        ContinuityStore(self.root).ensure_schema()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DROP INDEX idx_item_use_history_actor")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        finally:
            connection.close()
        before = self.database.read_bytes()
        with self.assertRaisesRegex(
            StoreError,
            "STATE_SCHEMA_V6_INDEXES_INCOMPLETE",
        ):
            ContinuityStore(self.root).ensure_schema()
        self.assertEqual(before, self.database.read_bytes())
        with closing(sqlite3.connect(self.database)) as read:
            self.assertIsNone(
                read.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type='index' AND name='idx_item_use_history_actor'
                    """
                ).fetchone()
            )


class ItemDeltaV4ValidatorTests(unittest.TestCase):
    @staticmethod
    def _base(event_type: str, **overrides: object) -> dict[str, object]:
        event: dict[str, object] = {
            "schema_version": "plot-rag-delta/v4",
            "event_type": event_type,
            "scope": "current",
            "story_coordinate": {
                "calendar_id": "fixture-calendar",
                "ordinal": 12,
            },
            "knowledge_plane": "objective",
            "evidence": "他将旧物收入囊中。",
        }
        event.update(overrides)
        return event

    @staticmethod
    def _normalize(event: dict[str, object]) -> dict[str, object]:
        return normalize_event(
            event,
            artifact_stage="final",
            branch_id="main",
            chapter_no=1,
            scene_index=0,
        )

    def test_item_events_require_v4_coordinate_knowledge_and_evidence(
        self,
    ) -> None:
        valid = self._base(
            "item_runtime",
            action="damage",
            item_instance_id="instance-1",
            delta={"durability": 1},
        )
        normalized = self._normalize(valid)
        self.assertEqual("plot-rag-delta/v4", normalized["schema_version"])
        self.assertEqual(
            {"quote": "他将旧物收入囊中。"},
            normalized["evidence"],
        )

        for field, value, code in (
            ("schema_version", "plot-rag-delta/v3", "ITEM_DELTA_SCHEMA_REQUIRED"),
            ("story_coordinate", None, "ITEM_STORY_COORDINATE_REQUIRED"),
            ("knowledge_plane", None, "INVALID_FIELD"),
            ("evidence", "", "ITEM_EVIDENCE_REQUIRED"),
        ):
            with self.subTest(field=field):
                invalid = dict(valid)
                invalid[field] = value
                with self.assertRaisesRegex(ContinuityError, code):
                    self._normalize(invalid)

    def test_v3_inventory_remains_compatible_but_v4_inventory_is_rejected(
        self,
    ) -> None:
        legacy = {
            "schema_version": "plot-rag-delta/v3",
            "event_type": "inventory",
            "item_entity_id": "legacy-item",
            "action": "set",
            "to_owner_entity_id": "actor-1",
            "quantity": 1,
            "unique": True,
        }
        self.assertEqual("inventory", self._normalize(legacy)["event_type"])
        invalid = dict(legacy)
        invalid["schema_version"] = "plot-rag-delta/v4"
        with self.assertRaisesRegex(
            ContinuityError,
            "INVENTORY_DELTA_SCHEMA_UNSUPPORTED",
        ):
            self._normalize(invalid)

    def test_item_subject_and_function_binding_are_exactly_one(self) -> None:
        binding = self._base(
            "item_spec",
            action="define",
            spec_type="function_binding",
            binding_id="binding-1",
            definition={
                "function_id": "function-1",
                "stack_id": "stack-1",
            },
        )
        normalized = self._normalize(binding)
        self.assertEqual(
            "stack-1",
            normalized["definition"]["stack_id"],
        )

        ambiguous_binding = dict(binding)
        ambiguous_binding["definition"] = {
            "function_id": "function-1",
            "stack_id": "stack-1",
            "item_instance_id": "instance-1",
        }
        with self.assertRaisesRegex(
            ContinuityError,
            "ITEM_BINDING_TARGET_REQUIRED",
        ):
            self._normalize(ambiguous_binding)

        mismatched_subject = self._base(
            "item_runtime",
            action="damage",
            subject_type="item_instance",
            subject_id="instance-1",
            stack_id="stack-1",
            delta={"durability": 1},
        )
        with self.assertRaisesRegex(ContinuityError, "ITEM_SUBJECT_MISMATCH"):
            self._normalize(mismatched_subject)

    def test_item_custody_accepts_custodian_as_destination_anchor(self) -> None:
        custody = self._base(
            "item_custody",
            action="handover",
            item_instance_id="instance-1",
            to_custodian_entity_id="custodian-1",
        )
        normalized = self._normalize(custody)
        self.assertEqual(
            "custodian-1",
            normalized["to_custodian_entity_id"],
        )

    def test_item_events_reject_model_computed_before_after_and_bool_quantity(
        self,
    ) -> None:
        computed = self._base(
            "item_runtime",
            action="damage",
            item_instance_id="instance-1",
            delta={"durability": 1},
            after_state={"durability": 9},
        )
        with self.assertRaisesRegex(
            ContinuityError,
            "ITEM_COMPUTED_STATE_FORBIDDEN",
        ):
            self._normalize(computed)

        boolean_quantity = self._base(
            "item_instance",
            action="instantiate",
            item_instance_id="instance-1",
            item_definition_id="definition-1",
            quantity=True,
        )
        with self.assertRaisesRegex(ContinuityError, "INVALID_QUANTITY"):
            self._normalize(boolean_quantity)

    def test_item_correction_rejects_ambiguous_target_and_unknown_field(
        self,
    ) -> None:
        replacement = self._base(
            "item_runtime",
            action="damage",
            item_instance_id="instance-1",
            delta={"durability": 1},
        )
        correction = self._base(
            "item_correction",
            action="correct",
            target_event_id="event-direct",
            supersedes=["event-other"],
            replacement=replacement,
        )
        with self.assertRaises(ContinuityError) as caught:
            self._normalize(correction)
        self.assertEqual(
            "ITEM_CORRECTION_TARGET_AMBIGUOUS",
            caught.exception.code,
        )

        unknown = self._base(
            "item_runtime",
            action="damage",
            item_instance_id="instance-1",
            delta={"durability": 1},
            hallucinated_after=9,
        )
        with self.assertRaisesRegex(
            ContinuityError,
            "ITEM_EVENT_FIELD_UNSUPPORTED",
        ):
            self._normalize(unknown)

    def test_item_definition_can_remain_explicitly_unknown(self) -> None:
        event = self._base(
            "item_spec",
            action="define",
            spec_type="item_definition",
            item_definition_id="definition-unknown",
            definition={
                "stack_policy": "unknown",
                "uniqueness_policy": "unknown",
            },
        )
        normalized = self._normalize(event)
        self.assertEqual(
            "unknown",
            normalized["definition"]["stack_policy"],
        )
        self.assertEqual(
            "unknown",
            normalized["definition"]["uniqueness_policy"],
        )


if __name__ == "__main__":
    unittest.main()
