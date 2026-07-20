from __future__ import annotations

import json
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from scripts import v1_runtime
from scripts.continuity import (
    ContinuityError,
    ContinuityService,
    HostApprovalAuthority,
)
from tests.test_power_spec_import import power_aggregate


def database_fingerprints(path: Path) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    for candidate in (
        path,
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
    ):
        if candidate.is_file():
            payload = candidate.read_bytes()
            result[candidate.name] = (
                len(payload),
                hashlib.sha256(payload).hexdigest(),
            )
    return result


def make_project(base: Path) -> Path:
    root = base / "novel"
    (root / ".plot-rag").mkdir(parents=True)
    (root / "正文").mkdir()
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
    return root


class StandalonePowerSpecLifecycleTests(unittest.TestCase):
    def test_preview_without_state_fails_closed_without_creating_database(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            state_path = root / ".plot-rag" / "state.sqlite3"
            service = ContinuityService(root)

            with self.assertRaises(ContinuityError) as caught:
                service.preview_power_spec_change(
                    power_aggregate(),
                    expected_canon_revision=0,
                )

            self.assertEqual(
                "POWER_SPEC_STATE_NOT_CREATED",
                caught.exception.code,
            )
            self.assertFalse(state_path.exists())

    def test_validate_and_preview_are_read_only(self) -> None:
        validated = v1_runtime.validate_power_spec_change(power_aggregate())
        self.assertEqual("ready", validated["status"])
        self.assertTrue(validated["read_only"])

        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            service = ContinuityService(root)
            before = service.get_canon_revisions()
            with service.store.read_connection() as connection:
                before_counts = {
                    table: connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                    for table in ("entities", "proposals", "approval_grants")
                }
            state_path = root / ".plot-rag" / "state.sqlite3"
            before_storage = database_fingerprints(state_path)

            preview = v1_runtime.preview_power_spec_change(
                root,
                power_aggregate(),
                expected_canon_revision=0,
            )

            self.assertEqual("ready", preview["status"])
            self.assertTrue(preview["read_only"])
            self.assertEqual(before, preview["canon_revisions"])
            self.assertEqual(before, service.get_canon_revisions())
            self.assertEqual(
                before_storage,
                database_fingerprints(state_path),
            )
            with service.store.read_connection() as connection:
                after_counts = {
                    table: connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                    for table in ("entities", "proposals", "approval_grants")
                }
            self.assertEqual(before_counts, after_counts)

    def test_preview_rejects_foreign_database_without_byte_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            state_path = root / ".plot-rag" / "state.sqlite3"
            with closing(sqlite3.connect(state_path)) as connection:
                connection.execute(
                    "CREATE TABLE foreign_component(id TEXT PRIMARY KEY)"
                )
                connection.commit()
            before = database_fingerprints(state_path)
            service = ContinuityService(root)

            with self.assertRaises(ContinuityError) as caught:
                service.preview_power_spec_change(
                    power_aggregate(),
                    expected_canon_revision=0,
                )

            self.assertEqual(
                "SQLITE_COMPONENT_FOREIGN_TABLES",
                caught.exception.code,
            )
            self.assertEqual(before, database_fingerprints(state_path))

    def test_preview_rejects_partial_schema_without_byte_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            state_path = root / ".plot-rag" / "state.sqlite3"
            with closing(sqlite3.connect(state_path)) as connection:
                connection.execute(
                    """
                    CREATE TABLE state_meta(
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO state_meta(key, value, updated_at)
                    VALUES(?, ?, '2026-07-20T00:00:00+00:00')
                    """,
                    (
                        ("schema_version", "2"),
                        ("continuity_schema_version", "7"),
                        ("head_canon_revision", "0"),
                        ("active_canon_revision", "0"),
                    ),
                )
                connection.commit()
            before = database_fingerprints(state_path)
            service = ContinuityService(root)

            with self.assertRaises(ContinuityError) as caught:
                service.preview_power_spec_change(
                    power_aggregate(),
                    expected_canon_revision=0,
                )

            self.assertEqual(
                "POWER_SPEC_STATE_SCHEMA_INCOMPLETE",
                caught.exception.code,
            )
            self.assertEqual(before, database_fingerprints(state_path))

    def test_propose_is_atomic_when_proposal_save_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            service = ContinuityService(root)
            service.store.ensure_schema()
            package = service.preview_power_spec_change(
                power_aggregate(),
                expected_canon_revision=0,
            )["lifecycle_package"]
            entity_ids = [
                str(entity["entity_id"]) for entity in package["entities"]
            ]

            with patch.object(
                service,
                "save_proposal",
                side_effect=ContinuityError(
                    "SYNTHETIC_PROPOSAL_FAILURE",
                    "proposal persistence failed",
                ),
            ):
                with self.assertRaises(ContinuityError) as caught:
                    service.propose_power_spec_change(
                        power_aggregate(),
                        expected_canon_revision=0,
                        idempotency_key="power-spec-rollback",
                    )
            self.assertEqual(
                "SYNTHETIC_PROPOSAL_FAILURE",
                caught.exception.code,
            )
            with service.store.read_connection() as connection:
                placeholders = ",".join("?" for _ in entity_ids)
                entity_count = connection.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM entities
                    WHERE entity_id IN ({placeholders})
                    """,
                    entity_ids,
                ).fetchone()[0]
                proposal_count = connection.execute(
                    "SELECT COUNT(*) FROM proposals"
                ).fetchone()[0]
            self.assertEqual(0, entity_count)
            self.assertEqual(0, proposal_count)

    def test_entity_id_type_conflict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            service = ContinuityService(root)
            service.store.ensure_schema()
            package = service.preview_power_spec_change(
                power_aggregate(),
                expected_canon_revision=0,
            )["lifecycle_package"]
            conflicting = dict(package["entities"][0])
            service.register_entity(
                "character",
                "冲突实体",
                entity_id=str(conflicting["entity_id"]),
            )

            with self.assertRaises(ContinuityError) as caught:
                service.propose_power_spec_change(
                    power_aggregate(),
                    expected_canon_revision=0,
                    idempotency_key="power-spec-conflict",
                )
            self.assertEqual("ENTITY_ID_CONFLICT", caught.exception.code)
            with service.store.read_connection() as connection:
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM proposals"
                    ).fetchone()[0],
                )

    def test_revision_drift_between_preview_and_write_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            service = ContinuityService(root)
            service.get_canon_revisions()
            original_preview = service.preview_power_spec_change

            def drifting_preview(*args, **kwargs):
                result = original_preview(*args, **kwargs)
                with service.store.transaction() as connection:
                    service.store.set_meta_int(
                        connection,
                        "head_canon_revision",
                        1,
                    )
                    service.store.set_meta_int(
                        connection,
                        "active_canon_revision",
                        1,
                    )
                return result

            with patch.object(
                service,
                "preview_power_spec_change",
                side_effect=drifting_preview,
            ):
                with self.assertRaises(ContinuityError) as caught:
                    service.propose_power_spec_change(
                        power_aggregate(),
                        expected_canon_revision=0,
                        idempotency_key="power-spec-revision-drift",
                    )

            self.assertEqual("CANON_REVISION_CONFLICT", caught.exception.code)
            with service.store.read_connection() as connection:
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM entities"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM proposals"
                    ).fetchone()[0],
                )

    def test_propose_is_idempotent_and_does_not_issue_grant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            service = ContinuityService(root)
            service.store.ensure_schema()

            first = service.propose_power_spec_change(
                power_aggregate(),
                expected_canon_revision=0,
                idempotency_key="power-spec-idempotent",
            )
            second = service.propose_power_spec_change(
                power_aggregate(),
                expected_canon_revision=0,
                idempotency_key="power-spec-idempotent",
            )

            self.assertEqual(
                first["proposal"]["proposal_id"],
                second["proposal"]["proposal_id"],
            )
            with service.store.read_connection() as connection:
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM proposals"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM approval_grants"
                    ).fetchone()[0],
                )

    def test_generic_accept_power_spec_and_replay_expose_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            service = ContinuityService(root)
            service.store.ensure_schema()
            proposed = service.propose_power_spec_change(
                power_aggregate(),
                expected_canon_revision=0,
                idempotency_key="power-spec-accept",
            )
            proposal_id = str(proposed["proposal"]["proposal_id"])
            grant = HostApprovalAuthority(
                service,
                issuer="power-spec-test",
                channel="unit_test",
            ).issue(
                proposal_id,
                expected_canon_revision=0,
                operations=("accept_power_spec",),
            )

            accepted = v1_runtime.accept_plot_proposal(
                root,
                proposal_id,
                approval_id=str(grant["approval_id"]),
                expected_canon_revision=0,
            )

            self.assertEqual("accepted", accepted["status"])
            self.assertEqual(
                "accept_power_spec",
                accepted["required_operation"],
            )
            systems = service.list_power_systems()["systems"]
            self.assertEqual(2, len(systems))
            self.assertEqual(
                service.projection_hash(),
                service.replay()["projection_hash"],
            )


if __name__ == "__main__":
    unittest.main()
