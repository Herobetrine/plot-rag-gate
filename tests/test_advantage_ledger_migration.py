from __future__ import annotations

import sqlite3
import unittest

from scripts.continuity.advantages import (
    ADVANTAGE_PROJECTION_INDEXES,
    advantage_schema_ready,
    ensure_advantage_schema,
    query_advantage_definition,
)
from scripts.continuity.validators import ContinuityError


class AdvantageLedgerMigrationTests(unittest.TestCase):
    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        ensure_advantage_schema(connection)
        connection.execute(
            """
            INSERT INTO advantage_definitions(
                advantage_id, title, profiles_json, anchor_type,
                acquisition_mode, uniqueness, advantage_status,
                lifecycle_status, promise_json, counterplay_json,
                definition_json, source_claim_ids_json,
                source_event_id, updated_order
            ) VALUES(
                'adv-migration', '迁移测试', '[]', 'item_instance',
                'fixture', 'unique_instance', 'canon', 'active',
                '{}', '[]', '{}', '[]', NULL, 1
            )
            """
        )
        return connection

    def test_fresh_schema_keeps_domain_ledger_kind(self) -> None:
        connection = self._connection()
        try:
            connection.execute(
                """
                INSERT INTO advantage_ledger(
                    entry_id, advantage_id, branch_id, entry_kind,
                    input_json, output_json, loss_json,
                    provenance_json, story_coordinate_json, updated_order
                ) VALUES(
                    'entry-domain', 'adv-migration', 'main',
                    'sample_resource_acquired', '{}', '{}', '{}',
                    '{}', '{}', 1
                )
                """
            )
            row = connection.execute(
                "SELECT entry_kind FROM advantage_ledger WHERE entry_id=?",
                ("entry-domain",),
            ).fetchone()
            self.assertEqual("sample_resource_acquired", row["entry_kind"])
        finally:
            connection.close()

    def test_legacy_closed_check_is_rebuilt_without_losing_rows(self) -> None:
        connection = self._connection()
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "DROP INDEX idx_advantage_ledger_advantage"
            )
            connection.execute("DROP INDEX idx_advantage_ledger_module")
            connection.execute(
                "ALTER TABLE advantage_ledger RENAME TO advantage_ledger_saved"
            )
            connection.execute(
                """
                CREATE TABLE advantage_ledger (
                    entry_id TEXT PRIMARY KEY,
                    advantage_id TEXT NOT NULL,
                    module_id TEXT,
                    branch_id TEXT NOT NULL,
                    entry_kind TEXT NOT NULL,
                    actor_entity_id TEXT,
                    target_entity_id TEXT,
                    input_json TEXT NOT NULL DEFAULT '{}',
                    output_json TEXT NOT NULL DEFAULT '{}',
                    loss_json TEXT NOT NULL DEFAULT '{}',
                    provenance_json TEXT NOT NULL DEFAULT '{}',
                    causal_event_id TEXT,
                    source_event_id TEXT,
                    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
                    updated_order INTEGER NOT NULL DEFAULT 0,
                    CHECK(
                        entry_kind IN ('bind', 'bootstrap')
                    ),
                    CHECK(
                        typeof(updated_order)='integer'
                        AND updated_order >= 0
                    ),
                    FOREIGN KEY(advantage_id)
                        REFERENCES advantage_definitions(advantage_id),
                    FOREIGN KEY(module_id)
                        REFERENCES advantage_module_definitions(module_id)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO advantage_ledger(
                    entry_id, advantage_id, branch_id, entry_kind,
                    input_json, output_json, loss_json,
                    provenance_json, story_coordinate_json, updated_order
                ) VALUES(
                    'entry-legacy', 'adv-migration', 'main',
                    'bootstrap', '{}', '{}', '{}',
                    '{}', '{}', 1
                )
                """
            )
            connection.execute("DROP TABLE advantage_ledger_saved")
            ensure_advantage_schema(connection)
            self.assertTrue(advantage_schema_ready(connection))
            migrated = connection.execute(
                "SELECT entry_kind FROM advantage_ledger WHERE entry_id=?",
                ("entry-legacy",),
            ).fetchone()
            self.assertEqual("bootstrap", migrated["entry_kind"])
            connection.execute(
                """
                INSERT INTO advantage_ledger(
                    entry_id, advantage_id, branch_id, entry_kind,
                    input_json, output_json, loss_json,
                    provenance_json, story_coordinate_json, updated_order
                ) VALUES(
                    'entry-after-migration', 'adv-migration', 'main',
                    'sample_resource_acquired', '{}', '{}', '{}',
                    '{}', '{}', 2
                )
                """
            )
            indexes = {
                str(row["name"])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='index' AND name LIKE 'idx_advantage_ledger_%'
                    """
                )
            }
            self.assertEqual(
                {
                    "idx_advantage_ledger_advantage",
                    "idx_advantage_ledger_module",
                },
                indexes,
            )
        finally:
            connection.close()

    def test_ensure_schema_repairs_missing_advantage_indexes(self) -> None:
        connection = self._connection()
        try:
            expected = set(ADVANTAGE_PROJECTION_INDEXES)
            canonical = {
                str(row["name"])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='index'
                      AND name LIKE 'idx_advantage_%'
                    """
                )
            }
            self.assertEqual(expected, canonical)

            for index_name in sorted(expected):
                with self.subTest(index_name=index_name):
                    connection.execute(f'DROP INDEX "{index_name}"')
                    self.assertFalse(advantage_schema_ready(connection))
                    ensure_advantage_schema(connection)
                    self.assertTrue(advantage_schema_ready(connection))
                    repaired = connection.execute(
                        """
                        SELECT 1 FROM sqlite_master
                        WHERE type='index' AND name=?
                        """,
                        (index_name,),
                    ).fetchone()
                    self.assertIsNotNone(repaired)
        finally:
            connection.close()

    def test_query_does_not_create_or_migrate_advantage_schema(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            before = list(
                connection.execute(
                    "SELECT type, name, sql FROM sqlite_master ORDER BY name"
                )
            )
            with self.assertRaises(ContinuityError) as raised:
                query_advantage_definition(connection, "adv-missing")
            self.assertEqual("ADVANTAGE_SCHEMA_MISSING", raised.exception.code)
            after = list(
                connection.execute(
                    "SELECT type, name, sql FROM sqlite_master ORDER BY name"
                )
            )
            self.assertEqual(before, after)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
