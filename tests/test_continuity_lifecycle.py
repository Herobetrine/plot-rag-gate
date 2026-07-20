from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from scripts.continuity import (
    ContinuityError,
    ContinuityService,
    HostApprovalAuthority,
    SCHEMA_VERSION,
)
import scripts.continuity.service as continuity_service_module
from scripts.plot_init.canonical import canonical_hash


class ContinuityLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.service = ContinuityService(self.root)
        self.host = HostApprovalAuthority(
            self.service,
            issuer="unittest-host",
            channel="interactive_test",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def entity(self, entity_type: str, name: str, aliases=()) -> str:
        return self.service.register_entity(
            entity_type, name, aliases=aliases
        )["entity_id"]

    def proposal(
        self,
        events,
        *,
        artifact_id: str,
        stage: str = "final",
        branch: str = "main",
        chapter: int | None = 1,
        scene: int | None = 0,
        revision: int | None = None,
    ):
        return self.service.save_proposal(
            events=events,
            artifact_id=artifact_id,
            artifact_stage=stage,
            branch_id=branch,
            chapter_no=chapter,
            scene_index=scene,
            artifact_revision=revision,
        )

    def accept(self, proposal, *, operations=("accept",)):
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            proposal["proposal_id"],
            expected_canon_revision=revision,
            operations=operations,
        )
        commit = self.service.accept_proposal(
            proposal["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=revision,
        )
        return grant, commit

    def materialization_commit(
        self,
        files: dict[str, str],
        *,
        bind_artifacts: bool = True,
        authorized_paths=None,
        existing_files: dict[str, str] | None = None,
        target_root: Path | None = None,
    ) -> str:
        target_root = target_root or self.root
        target_root.mkdir(parents=True, exist_ok=True)
        existing_files = dict(existing_files or {})
        for path, content in existing_files.items():
            target = target_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        manifest = []
        plan_files = []
        if bind_artifacts:
            for index, (path, content) in enumerate(sorted(files.items())):
                content_hash = hashlib.sha256(
                    content.encode("utf-8")
                ).hexdigest()
                manifest.append(
                    {
                        "source_id": f"source-materialize-{index}",
                        "path": path,
                        "content_hash": content_hash,
                        "source_role": "setting",
                    }
                )
                plan_files.append(
                    {
                        "path": path,
                        "new_hash": content_hash,
                        "expected_old_hash": (
                            hashlib.sha256(
                                existing_files[path].encode("utf-8")
                            ).hexdigest()
                            if path in existing_files
                            else None
                        ),
                    }
                )
        bundle = {
            "bundle_version": 1,
            "bundle_id": f"materialize-{len(files)}-{int(bind_artifacts)}",
            "branch_id": "main",
            "target_project_real_path": str(target_root),
            "entities": [],
            "events": [],
            "source_manifest": manifest,
            "materialization_plan": {"files": plan_files},
        }
        saved = self.service.save_initialization_bundle(bundle)
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            saved["proposal_id"],
            expected_canon_revision=revision,
            operations=("accept_initialization", "materialize"),
            target_project_real_path=target_root,
            authorized_paths=authorized_paths,
        )
        applied = self.service.apply_initialization_bundle(
            bundle,
            proposal_id=saved["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=revision,
        )
        return str(applied["commit_id"])

    def seed_crashed_materialization(
        self,
        commit_id: str,
        *,
        activated_paths: tuple[str, ...],
    ) -> dict[str, object]:
        """Persist the observable state left by an owner killed after swaps."""

        status = self.service.materialization_status(commit_id)
        run_id = str(status["run_id"])
        staging = Path(str(status["staging_path"]))
        rows = {
            str(row["relative_path"]): dict(row)
            for row in status["files"]
        }
        backup_root = (
            self.root
            / ".plot-rag"
            / "backups"
            / "materialize"
            / run_id
        )
        backup_root.mkdir(parents=True, exist_ok=True)
        for relative_path in activated_paths:
            row = rows[relative_path]
            target = self.root / relative_path
            staged = staging / relative_path
            if row["expected_old_hash"] is not None:
                backup = backup_root / relative_path
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                self.assertEqual(
                    row["expected_old_hash"],
                    hashlib.sha256(backup.read_bytes()).hexdigest(),
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged, target)
            self.assertEqual(
                row["proposed_new_hash"],
                hashlib.sha256(target.read_bytes()).hexdigest(),
            )

        with self.service.store.transaction() as connection:
            connection.execute(
                """
                UPDATE materialization_runs
                SET run_status='activating', updated_at=?
                WHERE run_id=?
                """,
                ("2026-07-17T00:00:00Z", run_id),
            )
            for relative_path in activated_paths:
                row = rows[relative_path]
                connection.execute(
                    """
                    UPDATE materialization_files
                    SET actual_hash=?, file_status='activated'
                    WHERE run_id=? AND relative_path=?
                    """,
                    (
                        row["proposed_new_hash"],
                        run_id,
                        relative_path,
                    ),
                )
            connection.execute(
                """
                INSERT INTO materialization_activation_claims(
                    run_id, owner_host, owner_pid, owner_token, claimed_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    socket.gethostname().strip().casefold() or "localhost",
                    2_147_483_647,
                    "dead-owner-token",
                    "2026-07-17T00:00:00Z",
                ),
            )
        return status

    def test_migrates_legacy_v2_with_backup_and_keeps_legacy_version(self):
        legacy_root = self.root / "legacy"
        db_path = legacy_root / ".plot-rag" / "state.sqlite3"
        db_path.parent.mkdir(parents=True)
        connection = sqlite3.connect(db_path)
        connection.executescript(
            """
            CREATE TABLE state_meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO state_meta VALUES('schema_version', '2', 'old');
            INSERT INTO state_meta VALUES('legacy_probe', 'preserved', 'old');
            """
        )
        connection.commit()
        connection.close()

        service = ContinuityService(legacy_root)
        status = service.schema_status()
        backup = Path(status["migration_backup"])
        self.assertTrue(backup.is_file())
        self.assertEqual(status["meta"]["schema_version"], "2")
        self.assertEqual(
            status["meta"]["continuity_schema_version"],
            str(SCHEMA_VERSION),
        )

        backup_connection = sqlite3.connect(backup)
        try:
            self.assertEqual(
                backup_connection.execute(
                    "SELECT value FROM state_meta WHERE key='legacy_probe'"
                ).fetchone()[0],
                "preserved",
            )
            self.assertIsNone(
                backup_connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='proposals'"
                ).fetchone()
            )
        finally:
            backup_connection.close()

    def test_stop_style_save_is_proposal_only_and_reject_is_replayable(self):
        actor = self.entity("character", "测试角色甲")
        city = self.entity("location", "测试城")
        proposal = self.proposal(
            [
                {
                    "event_type": "movement",
                    "actor_entity_id": actor,
                    "to_location_entity_id": city,
                    "action": "move",
                }
            ],
            artifact_id="chapter-next-design",
            stage="brainstorm",
            branch="alternative-a",
        )
        self.assertEqual(proposal["canon_status"], "proposed")
        self.assertEqual(self.service.get_canon_revisions(), {"head": 0, "active": 0})
        self.assertEqual(self.service.query_facts()["facts"], [])

        rejected = self.service.reject_proposal(
            proposal["proposal_id"],
            reason="discarded route",
            idempotency_key="reject-1",
        )
        repeated = self.service.reject_proposal(
            proposal["proposal_id"],
            reason="discarded route",
            idempotency_key="reject-1",
        )
        self.assertEqual(rejected["canon_status"], "rejected")
        self.assertEqual(repeated["proposal_id"], rejected["proposal_id"])
        self.assertEqual(self.service.replay()["event_count"], 0)

    def test_proposal_integer_fields_require_exact_json_integers(self):
        actor = self.entity("character", "主角")
        status = self.entity("status_effect", "灼烧")
        base_event = {
            "event_type": "state",
            "entity_id": actor,
            "field": "condition",
            "value": "ready",
        }

        def save_with(field, value, case_index):
            keyword_arguments = {
                "events": [base_event],
                "artifact_id": f"exact-int-{field}-{case_index}",
                "artifact_stage": "final",
                "branch_id": "main",
                "chapter_no": 1,
                "scene_index": 0,
                "artifact_revision": 1,
                "prepared_canon_revision": 0,
            }
            if field in {
                "chapter_no",
                "scene_index",
                "artifact_revision",
                "prepared_canon_revision",
            }:
                keyword_arguments[field] = value
            elif field == "stacks":
                keyword_arguments["events"] = [
                    {
                        "event_type": "status_effect",
                        "actor_entity_id": actor,
                        "status_entity_id": status,
                        "action": "apply",
                        "stacks": value,
                    }
                ]
            elif field == "due_chapter":
                keyword_arguments["events"] = [
                    {
                        "event_type": "open_loop",
                        "loop_id": f"loop-{case_index}",
                        "status": "open",
                        "due_chapter": value,
                    }
                ]
            return self.service.save_proposal(**keyword_arguments)

        invalid_values = (True, 1.0, 1.5, "1")
        for field in (
            "chapter_no",
            "scene_index",
            "artifact_revision",
            "prepared_canon_revision",
            "stacks",
            "due_chapter",
        ):
            for case_index, value in enumerate(invalid_values, start=1):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ContinuityError) as caught:
                        save_with(field, value, case_index)
                    self.assertEqual("INVALID_FIELD", caught.exception.code)
                    self.assertIn(
                        f"{field} must be an integer",
                        str(caught.exception),
                    )

        duplicate = save_with("artifact_revision", 1, 90)
        for value in invalid_values:
            with self.subTest(field="artifact_revision_duplicate", value=value):
                with self.assertRaises(ContinuityError) as caught:
                    self.service.save_proposal(
                        events=[base_event],
                        artifact_id=duplicate["artifact_id"],
                        artifact_stage="final",
                        branch_id="main",
                        chapter_no=1,
                        scene_index=0,
                        artifact_revision=value,
                    )
                self.assertEqual("INVALID_FIELD", caught.exception.code)

        for case_index, (field, value) in enumerate(
            (
                ("chapter_no", 2),
                ("scene_index", 2),
                ("artifact_revision", 2),
                ("prepared_canon_revision", 0),
                ("stacks", 2),
                ("due_chapter", 2),
            ),
            start=100,
        ):
            with self.subTest(field=field, value=value):
                accepted = save_with(field, value, case_index)
                if field in {
                    "chapter_no",
                    "scene_index",
                    "artifact_revision",
                    "prepared_canon_revision",
                }:
                    self.assertEqual(value, accepted[field])
                else:
                    self.assertEqual(value, accepted["events"][0][field])

        for field, keyword_arguments in (
            ("chapter_no", {"chapter_no": 1.0}),
            ("scene_index", {"chapter_no": 1, "scene_index": 0.0}),
        ):
            with self.subTest(query_field=field):
                with self.assertRaises(ContinuityError) as caught:
                    self.service.query_facts(**keyword_arguments)
                self.assertEqual("INVALID_FIELD", caught.exception.code)

        bundle = {
            "bundle_version": 1,
            "bundle_id": "exact-int-initialization",
            "branch_id": "main",
            "target_project_real_path": str(self.root),
            "entities": [],
            "events": [],
            "source_manifest": [],
            "materialization_plan": {"files": []},
            "meta": {"expected_canon_revision": 0.0},
        }
        with self.assertRaises(ContinuityError) as caught:
            self.service.save_initialization_bundle(bundle)
        self.assertEqual("INVALID_FIELD", caught.exception.code)

    def test_story_coordinate_ordinals_require_exact_json_integers(self):
        actor = self.entity("character", "主角")
        invalid_values = (True, 1.0, 1.5, "1")
        for case_index, value in enumerate(invalid_values, start=1):
            with self.subTest(value=value):
                with self.assertRaises(ContinuityError) as caught:
                    self.service.save_proposal(
                        events=[
                            {
                                "event_type": "state",
                                "entity_id": actor,
                                "field": "condition",
                                "value": "ready",
                                "story_coordinate": {
                                    "calendar_id": "project-main",
                                    "ordinal": value,
                                },
                            }
                        ],
                        artifact_id=f"coordinate-invalid-{case_index}",
                        artifact_stage="final",
                        prepared_canon_revision=0,
                    )
                self.assertEqual(
                    "POWER_STORY_COORDINATE_UNKNOWN",
                    caught.exception.code,
                )

        accepted = self.service.save_proposal(
            events=[
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "condition",
                    "value": "ready",
                    "story_coordinate": {
                        "calendar_id": "project-main",
                        "ordinal": 1,
                    },
                }
            ],
            artifact_id="coordinate-valid",
            artifact_stage="final",
            prepared_canon_revision=0,
        )
        self.assertEqual(
            1,
            accepted["events"][0]["story_coordinate"]["ordinal"],
        )

    def test_grant_integer_fields_require_exact_json_integers(self):
        actor = self.entity("character", "主角")
        proposal = self.proposal(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "condition",
                    "value": "ready",
                }
            ],
            artifact_id="grant-exact-integers",
        )
        revision_values = (False, 0.0, 0.5, "0")
        for value in revision_values:
            with self.subTest(field="issue.expected_canon_revision", value=value):
                with self.assertRaises(ContinuityError) as caught:
                    self.host.issue(
                        proposal["proposal_id"],
                        expected_canon_revision=value,
                    )
                self.assertEqual("INVALID_FIELD", caught.exception.code)

        for value in (True, 1.0, 1.5, "1"):
            with self.subTest(field="expires_in_seconds", value=value):
                with self.assertRaises(ContinuityError) as caught:
                    self.host.issue(
                        proposal["proposal_id"],
                        expected_canon_revision=0,
                        expires_in_seconds=value,
                    )
                self.assertEqual(
                    "INVALID_GRANT_EXPIRY",
                    caught.exception.code,
                )

        grant = self.host.issue(
            proposal["proposal_id"],
            expected_canon_revision=0,
            expires_in_seconds=300,
        )
        for value in revision_values:
            with self.subTest(field="accept.expected_canon_revision", value=value):
                with self.assertRaises(ContinuityError) as caught:
                    self.service.accept_proposal(
                        proposal["proposal_id"],
                        approval_id=grant["approval_id"],
                        expected_canon_revision=value,
                    )
                self.assertEqual("INVALID_FIELD", caught.exception.code)

        accepted = self.service.accept_proposal(
            proposal["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=0,
        )
        self.assertEqual(1, accepted["active_canon_revision"])

    def test_host_grant_hash_only_cas_and_network_retry(self):
        actor = self.entity("character", "主角")
        first = self.proposal(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "injury",
                    "value": "light",
                }
            ],
            artifact_id="chapter-1",
        )
        second = self.proposal(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "goal",
                    "value": "leave",
                }
            ],
            artifact_id="chapter-2",
            chapter=2,
        )
        grant_one = self.host.issue(
            first["proposal_id"], expected_canon_revision=0
        )
        grant_two = self.host.issue(
            second["proposal_id"], expected_canon_revision=0
        )

        with closing(sqlite3.connect(self.service.store.db_path)) as connection:
            dump = json.dumps(connection.execute(
                "SELECT * FROM approval_grants"
            ).fetchall())
            self.assertNotIn(grant_one["approval_id"], dump)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM approval_grants"
                ).fetchone()[0],
                2,
            )

        accepted = self.service.accept_proposal(
            first["proposal_id"],
            approval_id=grant_one["approval_id"],
            expected_canon_revision=0,
        )
        retry = self.service.accept_proposal(
            first["proposal_id"],
            approval_id=grant_one["approval_id"],
            expected_canon_revision=0,
        )
        self.assertEqual(accepted["commit_id"], retry["commit_id"])
        self.assertTrue(retry["idempotent_retry"])
        with self.assertRaises(ContinuityError) as caught:
            self.service.accept_proposal(
                second["proposal_id"],
                approval_id=grant_two["approval_id"],
                expected_canon_revision=0,
            )
        self.assertEqual(caught.exception.code, "CANON_REVISION_CONFLICT")
        with self.assertRaises(ContinuityError) as caught:
            self.host.issue(
                second["proposal_id"],
                expected_canon_revision=1,
            )
        self.assertEqual(
            caught.exception.code, "PREPARED_CANON_REVISION_STALE"
        )

        with self.assertRaises(ContinuityError) as caught:
            self.service.accept_proposal(
                first["proposal_id"],
                approval_id="forged-token",
                expected_canon_revision=1,
            )
        self.assertEqual(caught.exception.code, "APPROVAL_GRANT_NOT_FOUND")

    def test_expired_grant_fails_closed_without_consumption(self):
        actor = self.entity("character", "A")
        proposal = self.proposal(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "status",
                    "value": "ready",
                }
            ],
            artifact_id="expired-grant",
        )
        grant = self.host.issue(
            proposal["proposal_id"], expected_canon_revision=0
        )
        with self.service.store.transaction() as connection:
            connection.execute(
                "UPDATE approval_grants SET expires_at='2000-01-01T00:00:00Z'"
            )
        with self.assertRaises(ContinuityError) as caught:
            self.service.accept_proposal(
                proposal["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=0,
            )
        self.assertEqual(caught.exception.code, "APPROVAL_GRANT_EXPIRED")
        self.assertEqual(
            self.service.get_canon_revisions(), {"head": 0, "active": 0}
        )
        with self.service.store.read_connection() as connection:
            consumed = connection.execute(
                "SELECT consumed_at FROM approval_grants"
            ).fetchone()[0]
        self.assertIsNone(consumed)

    def test_initialization_grant_operations_cannot_cross_proposal_kinds(self):
        actor = self.entity("character", "A")
        story = self.proposal(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "status",
                    "value": "ready",
                }
            ],
            artifact_id="ordinary-story",
        )
        with self.assertRaises(ContinuityError) as caught:
            self.host.issue(
                story["proposal_id"],
                expected_canon_revision=0,
                operations=("accept_initialization",),
            )
        self.assertEqual(
            "APPROVAL_OPERATION_SCOPE_MISMATCH",
            caught.exception.code,
        )

        initialization = self.service.save_initialization_bundle(
            {
                "bundle_id": "init-operation-scope",
                "target_project_real_path": str(self.root),
                "source_manifest": [],
                "events": [],
            }
        )
        with self.assertRaises(ContinuityError) as caught:
            self.host.issue(
                initialization["proposal_id"],
                expected_canon_revision=0,
                operations=("accept",),
            )
        self.assertEqual(
            "APPROVAL_OPERATION_SCOPE_MISMATCH",
            caught.exception.code,
        )

    def test_branch_and_outline_are_isolated_from_current(self):
        actor = self.entity("character", "A")
        city = self.entity("location", "City")
        draft = self.proposal(
            [
                {
                    "event_type": "movement",
                    "actor_entity_id": actor,
                    "to_location_entity_id": city,
                    "action": "move",
                }
            ],
            artifact_id="draft-1",
            stage="draft",
            branch="what-if",
        )
        _, draft_commit = self.accept(draft)
        self.assertFalse(draft_commit["changes_authority"])
        self.assertEqual(
            self.service.get_canon_revisions(), {"head": 1, "active": 0}
        )
        self.assertEqual(self.service.query_facts()["facts"], [])
        branch = self.service.query_facts(
            branch_id="what-if", include_provisional=True
        )
        self.assertEqual(len(branch["facts"]), 1)
        self.assertTrue(branch["facts"][0]["provisional"])

        outline = self.proposal(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "location",
                    "value": city,
                    "scope": "current",
                }
            ],
            artifact_id="outline-1",
            stage="outline",
            chapter=2,
        )
        _, outline_commit = self.accept(outline)
        self.assertTrue(outline_commit["changes_authority"])
        self.assertEqual(
            self.service.query_facts(entity_id=actor)["facts"], []
        )
        with self.service.store.read_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM planned_facts"
                ).fetchone()[0],
                1,
            )

    def test_alias_and_context_pronoun_resolve_one_entity(self):
        actor = self.entity("character", "测试角色甲", aliases=("小云", "记录者"))
        self.assertEqual(
            self.service.resolve_mention("小云")["entity_id"], actor
        )
        self.assertEqual(
            self.service.resolve_mention(
                "他", context_entity_ids=(actor,)
            )["entity_id"],
            actor,
        )
        other = self.entity("character", "云")
        self.service.add_alias(other, "记录者")
        ambiguous = self.service.resolve_mention("记录者")
        self.assertEqual(ambiguous["status"], "AMBIGUOUS")
        self.assertEqual(len(ambiguous["candidates"]), 2)

    def test_unique_item_requires_atomic_transfer(self):
        owner_a = self.entity("character", "A")
        owner_b = self.entity("character", "B")
        item = self.entity("item", "唯一钥匙")
        acquired = self.proposal(
            [
                {
                    "event_type": "inventory",
                    "item_entity_id": item,
                    "to_owner_entity_id": owner_a,
                    "action": "acquire",
                    "unique": True,
                }
            ],
            artifact_id="item-ch1",
        )
        self.accept(acquired)

        double_owner = self.proposal(
            [
                {
                    "event_type": "inventory",
                    "item_entity_id": item,
                    "to_owner_entity_id": owner_b,
                    "action": "acquire",
                    "unique": True,
                }
            ],
            artifact_id="item-ch2-invalid",
            chapter=2,
        )
        revision = self.service.get_canon_revisions()["active"]
        bad_grant = self.host.issue(
            double_owner["proposal_id"],
            expected_canon_revision=revision,
        )
        with self.assertRaises(ContinuityError) as caught:
            self.service.accept_proposal(
                double_owner["proposal_id"],
                approval_id=bad_grant["approval_id"],
                expected_canon_revision=revision,
            )
        self.assertEqual(caught.exception.code, "UNIQUE_ITEM_DOUBLE_OWNER")

        transfer = self.proposal(
            [
                {
                    "event_type": "inventory",
                    "item_entity_id": item,
                    "from_owner_entity_id": owner_a,
                    "to_owner_entity_id": owner_b,
                    "action": "transfer",
                    "unique": True,
                }
            ],
            artifact_id="item-ch2-transfer",
            chapter=2,
        )
        self.accept(transfer)
        with self.service.store.read_connection() as connection:
            row = connection.execute(
                "SELECT owner_entity_id FROM inventory_state "
                "WHERE item_entity_id=?",
                (item,),
            ).fetchone()
            self.assertEqual(row["owner_entity_id"], owner_b)

    def test_conflicting_location_needs_explicit_movement_origin(self):
        actor = self.entity("character", "A")
        place_a = self.entity("location", "A地")
        place_b = self.entity("location", "B地")
        first = self.proposal(
            [
                {
                    "event_type": "movement",
                    "actor_entity_id": actor,
                    "to_location_entity_id": place_a,
                    "action": "arrive",
                }
            ],
            artifact_id="loc-1",
            chapter=1,
            scene=0,
        )
        self.accept(first)
        conflict = self.proposal(
            [
                {
                    "event_type": "movement",
                    "actor_entity_id": actor,
                    "to_location_entity_id": place_b,
                    "action": "arrive",
                }
            ],
            artifact_id="loc-2-bad",
            chapter=1,
            scene=0,
        )
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            conflict["proposal_id"], expected_canon_revision=revision
        )
        with self.assertRaises(ContinuityError) as caught:
            self.service.accept_proposal(
                conflict["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=revision,
            )
        self.assertEqual(caught.exception.code, "CONFLICTING_LOCATION")

        explained = self.proposal(
            [
                {
                    "event_type": "movement",
                    "actor_entity_id": actor,
                    "from_location_entity_id": place_a,
                    "to_location_entity_id": place_b,
                    "action": "move",
                }
            ],
            artifact_id="loc-2-good",
            chapter=1,
            scene=0,
        )
        self.accept(explained)
        current = self.service.query_facts(
            entity_id=actor, fact_type="location"
        )["facts"]
        self.assertEqual(current[0]["target_entity_id"], place_b)

    def test_flashback_does_not_override_current_timeline(self):
        actor = self.entity("character", "A")
        old_place = self.entity("location", "Old")
        current_place = self.entity("location", "Now")
        current = self.proposal(
            [
                {
                    "event_type": "movement",
                    "actor_entity_id": actor,
                    "to_location_entity_id": current_place,
                    "action": "arrive",
                }
            ],
            artifact_id="linear-location",
            chapter=10,
        )
        self.accept(current)
        flashback = self.proposal(
            [
                {
                    "event_type": "movement",
                    "actor_entity_id": actor,
                    "to_location_entity_id": old_place,
                    "action": "arrive",
                    "narrative_mode": "flashback",
                    "story_time": "ten years ago",
                }
            ],
            artifact_id="flashback-location",
            chapter=11,
        )
        self.accept(flashback)
        active = self.service.query_facts(
            entity_id=actor, fact_type="location"
        )["facts"]
        self.assertEqual(active[0]["target_entity_id"], current_place)
        at_chapter = self.service.query_facts(
            entity_id=actor,
            fact_type="location",
            chapter_no=11,
            include_historical=True,
        )["facts"]
        self.assertIn("historical", {fact["scope"] for fact in at_chapter})
        current_at_chapter = next(
            fact for fact in at_chapter if fact["scope"] == "current"
        )
        self.assertEqual(
            current_at_chapter["target_entity_id"], current_place
        )

    def test_revision_supersession_retraction_and_replay_hash(self):
        actor = self.entity("character", "A")
        revision_one = self.proposal(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "injury",
                    "value": "severe",
                }
            ],
            artifact_id="chapter-rewrite",
            revision=1,
        )
        self.accept(revision_one)
        revision_two = self.proposal(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "injury",
                    "value": "none",
                }
            ],
            artifact_id="chapter-rewrite",
            revision=2,
        )
        _, commit_two = self.accept(revision_two)
        facts = self.service.query_facts(
            entity_id=actor, fact_type="state"
        )["facts"]
        self.assertEqual([fact["value"] for fact in facts], ["none"])
        before = commit_two["projection_hash"]
        self.assertEqual(before, self.service.replay()["projection_hash"])

        active = self.service.get_canon_revisions()["active"]
        retract_grant = self.host.issue(
            revision_two["proposal_id"],
            expected_canon_revision=active,
            operations=("retract",),
        )
        retracted = self.service.retract_proposal(
            revision_two["proposal_id"],
            approval_id=retract_grant["approval_id"],
            expected_canon_revision=active,
            reason="rewrite withdrawn",
        )
        retry = self.service.retract_proposal(
            revision_two["proposal_id"],
            approval_id=retract_grant["approval_id"],
            expected_canon_revision=active,
            reason="rewrite withdrawn",
        )
        self.assertEqual(retracted["commit_id"], retry["commit_id"])
        restored = self.service.query_facts(
            entity_id=actor, fact_type="state"
        )["facts"]
        self.assertEqual([fact["value"] for fact in restored], ["severe"])

    def test_explicit_correction_supersession_and_causality_links(self):
        actor = self.entity("character", "A")
        original = self.proposal(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "knows_secret",
                    "value": False,
                }
            ],
            artifact_id="knowledge-original",
        )
        _, original_commit = self.accept(original)
        original_event = original_commit["events"][0]["event_id"]
        correction = self.proposal(
            [
                {
                    "event_type": "correction",
                    "supersedes": original_event,
                    "caused_by": original_event,
                    "replacement": {
                        "event_type": "state",
                        "entity_id": actor,
                        "field": "knows_secret",
                        "value": True,
                    },
                }
            ],
            artifact_id="knowledge-correction",
            chapter=2,
        )
        self.accept(correction)
        facts = self.service.query_facts(
            entity_id=actor, fact_type="state"
        )["facts"]
        self.assertEqual([fact["value"] for fact in facts], [True])
        with self.service.store.read_connection() as connection:
            link_types = {
                row[0]
                for row in connection.execute(
                    "SELECT link_type FROM event_links"
                )
            }
        self.assertIn("supersedes", link_types)
        self.assertIn("caused_by", link_types)

    def test_chapter_scene_query_and_timeless_merge(self):
        actor = self.entity("character", "A")
        place_a = self.entity("location", "A地")
        place_b = self.entity("location", "B地")
        rule = self.proposal(
            [
                {
                    "event_type": "world_rule",
                    "field": "magic_cost",
                    "value": "memory",
                    "scope": "timeless",
                }
            ],
            artifact_id="bootstrap-rules",
            stage="bootstrap",
            chapter=None,
            scene=None,
        )
        self.accept(rule)
        first = self.proposal(
            [
                {
                    "event_type": "movement",
                    "actor_entity_id": actor,
                    "to_location_entity_id": place_a,
                    "action": "arrive",
                }
            ],
            artifact_id="point-ch1",
            chapter=1,
            scene=0,
        )
        self.accept(first)
        second = self.proposal(
            [
                {
                    "event_type": "movement",
                    "actor_entity_id": actor,
                    "from_location_entity_id": place_a,
                    "to_location_entity_id": place_b,
                    "action": "move",
                }
            ],
            artifact_id="point-ch2",
            chapter=2,
            scene=1,
        )
        self.accept(second)
        at_one = self.service.query_facts(
            chapter_no=1, scene_index=0
        )["facts"]
        at_two = self.service.query_facts(
            chapter_no=2, scene_index=1
        )["facts"]
        location_one = next(
            fact for fact in at_one if fact["fact_type"] == "location"
        )
        location_two = next(
            fact for fact in at_two if fact["fact_type"] == "location"
        )
        self.assertEqual(location_one["target_entity_id"], place_a)
        self.assertEqual(location_two["target_entity_id"], place_b)
        self.assertIn("timeless", {fact["scope"] for fact in at_one})
        self.assertIn("timeless", {fact["scope"] for fact in at_two})

    def test_typed_relation_ability_belief_and_open_loop(self):
        actor = self.entity("character", "A")
        ally = self.entity("character", "B")
        ability = self.entity("ability", "Fire")
        proposal = self.proposal(
            [
                {
                    "event_type": "relation",
                    "source_entity_id": actor,
                    "target_entity_id": ally,
                    "dimension": "trust",
                    "value": 0.7,
                },
                {
                    "event_type": "ability",
                    "owner_entity_id": actor,
                    "ability_entity_id": ability,
                    "action": "gain",
                    "state": {"cost": "fatigue", "cooldown": 2},
                },
                {
                    "event_type": "belief",
                    "believer_entity_id": actor,
                    "proposition_key": "gate_is_safe",
                    "value": False,
                },
                {
                    "event_type": "open_loop",
                    "loop_id": "promise-1",
                    "owner_entity_id": actor,
                    "loop_type": "promise",
                    "status": "open",
                    "due_chapter": 5,
                },
            ],
            artifact_id="typed-events",
        )
        self.accept(proposal)
        relations = self.service.query_relations(actor)["facts"]
        self.assertEqual(relations[0]["field"], "trust")
        with self.service.store.read_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ability_state"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM belief_state"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT loop_status FROM open_loops"
                ).fetchone()[0],
                "open",
            )

    def test_initialization_bundle_apply_manifest_and_materialize_saga(self):
        content = "# 世界内核\n力量有代价。\n"
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        actor_id = "entity_init_actor"
        bundle = {
            "bundle_version": 1,
            "bundle_id": "init-fixture",
            "branch_id": "main",
            "target_project_real_path": str(self.root),
            "entities": [
                {
                    "entity_id": actor_id,
                    "entity_type": "character",
                    "canonical_name": "初始主角",
                }
            ],
            "events": [
                {
                    "event_type": "state",
                    "entity_id": actor_id,
                    "field": "status",
                    "value": "ready",
                    "scope": "current",
                }
            ],
            "world_rules": [
                {
                    "rule_id": "rule-cost",
                    "value": {"cost": "memory"},
                    "scope": "timeless",
                }
            ],
            "source_manifest": [
                {
                    "source_id": "source-world",
                    "path": "设定集/世界内核.md",
                    "content_hash": content_hash,
                    "source_role": "setting",
                }
            ],
            "materialization_plan": {
                "files": [
                    {
                        "path": "设定集/世界内核.md",
                        "new_hash": content_hash,
                    }
                ]
            },
        }
        saved = self.service.save_initialization_bundle(
            bundle, idempotency_key="init-propose"
        )
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            saved["proposal_id"],
            expected_canon_revision=revision,
            operations=("accept_initialization", "materialize"),
            target_project_real_path=self.root,
            authorized_paths=("设定集/世界内核.md",),
        )
        applied = self.service.apply_initialization_bundle(
            bundle,
            proposal_id=saved["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=revision,
        )
        self.assertIsNotNone(applied["materialization_run_id"])
        self.assertEqual(
            self.service.get_accepted_source_manifest(), []
        )
        pending = self.service.get_accepted_source_manifest(
            include_pending=True
        )
        self.assertEqual(pending[0]["status"], "pending")

        staged = self.service.stage_materialization(
            applied["commit_id"],
            target_root=self.root,
            files={"设定集/世界内核.md": content},
        )
        self.assertEqual(staged["status"], "staged")
        with self.assertRaises(ContinuityError) as caught:
            self.service.stage_materialization(
                applied["commit_id"],
                target_root=self.root / "other-target",
                files={"设定集/世界内核.md": content},
            )
        self.assertEqual(
            caught.exception.code, "MATERIALIZATION_TARGET_MISMATCH"
        )
        completed = self.service.activate_materialization(
            applied["commit_id"]
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            (self.root / "设定集" / "世界内核.md").read_text(encoding="utf-8"),
            content,
        )
        active_manifest = self.service.get_accepted_source_manifest()
        self.assertEqual(active_manifest[0]["content_hash"], content_hash)
        self.assertTrue(completed["completion_receipt"]["projection_hash"])

    def test_materialization_activation_has_one_exact_owner(self):
        content = {"race.md": "approved-new"}
        commit_id = self.materialization_commit(
            content,
            existing_files={"race.md": "approved-old"},
        )
        staged = self.service.stage_materialization(
            commit_id,
            target_root=self.root,
            files=content,
        )
        follower_service = ContinuityService(self.root)
        real_replace = os.replace
        first_replace_entered = threading.Event()
        release_first_replace = threading.Event()
        replace_lock = threading.Lock()
        replace_calls = 0
        owner_result: dict[str, object] = {}

        def blocking_first_replace(source, target):
            nonlocal replace_calls
            with replace_lock:
                replace_calls += 1
                call_no = replace_calls
            if call_no == 1:
                first_replace_entered.set()
                if not release_first_replace.wait(10):
                    raise RuntimeError("activation owner was not released")
            return real_replace(source, target)

        def activate_owner():
            try:
                owner_result["status"] = self.service.activate_materialization(
                    commit_id
                )["status"]
            except Exception as exc:
                owner_result["error"] = exc

        owner_thread = threading.Thread(target=activate_owner)
        with mock.patch(
            "scripts.continuity.service.os.replace",
            side_effect=blocking_first_replace,
        ):
            owner_thread.start()
            self.assertTrue(first_replace_entered.wait(10))
            try:
                follower = follower_service.activate_materialization(commit_id)
                self.assertEqual(follower["status"], "activating")
                self.assertEqual(replace_calls, 1)
            finally:
                release_first_replace.set()
                owner_thread.join(10)
        self.assertFalse(owner_thread.is_alive())
        self.assertNotIn("error", owner_result)
        self.assertEqual(owner_result.get("status"), "completed")

        completed = follower_service.activate_materialization(commit_id)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(replace_calls, 1)
        self.assertTrue(completed["completion_receipt"]["projection_hash"])
        self.assertEqual(
            hashlib.sha256((self.root / "race.md").read_bytes()).hexdigest(),
            staged["files"][0]["proposed_new_hash"],
        )
        self.assertTrue(
            all(
                item["file_status"] == "activated"
                for item in completed["files"]
            )
        )
        manifest = self.service.get_accepted_source_manifest()
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["status"], "active")
        with self.service.store.read_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM materialization_activation_claims
                    WHERE run_id=?
                    """,
                    (staged["run_id"],),
                ).fetchone()[0],
                0,
            )

    def test_materialization_owner_probe_detects_pid_reuse(self):
        host = socket.gethostname().strip().casefold() or "localhost"
        owner_token = json.dumps(
            {
                "birth": "linux-start:old-boot:10",
                "nonce": "fixture",
                "version": 1,
            }
        )
        with mock.patch.object(
            continuity_service_module,
            "_materialization_process_probe",
            return_value=("alive", "linux-start:new-boot:20"),
        ):
            state, reason = (
                continuity_service_module._materialization_owner_state(
                    host,
                    os.getpid(),
                    owner_token,
                )
            )
        self.assertEqual("dead", state)
        self.assertIn("reused", reason)

    def test_materialization_dead_owner_recovery_rolls_back_prior_replace(
        self,
    ):
        content = {
            "a-recovered.md": "approved-a-new",
            "b-fails.md": "approved-b-new",
        }
        old = {
            "a-recovered.md": "approved-a-old",
            "b-fails.md": "approved-b-old",
        }
        commit_id = self.materialization_commit(
            content,
            existing_files=old,
        )
        self.service.stage_materialization(
            commit_id,
            target_root=self.root,
            files=content,
        )
        self.seed_crashed_materialization(
            commit_id,
            activated_paths=("a-recovered.md",),
        )
        real_replace = os.replace
        failure_target = (self.root / "b-fails.md").resolve(strict=False)

        def fail_second_swap(source, target):
            if Path(target).resolve(strict=False) == failure_target:
                raise OSError("injected second-file failure")
            return real_replace(source, target)

        with mock.patch(
            "scripts.continuity.service.os.replace",
            side_effect=fail_second_swap,
        ):
            with self.assertRaises(ContinuityError) as caught:
                self.service.activate_materialization(commit_id)
        self.assertEqual(
            "MATERIALIZATION_ACTIVATION_FAILED",
            caught.exception.code,
        )
        self.assertEqual(
            old["a-recovered.md"],
            (self.root / "a-recovered.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            old["b-fails.md"],
            (self.root / "b-fails.md").read_text(encoding="utf-8"),
        )
        failed = self.service.materialization_status(commit_id)
        self.assertEqual("failed", failed["status"])
        statuses = {
            row["relative_path"]: row["file_status"]
            for row in failed["files"]
        }
        self.assertEqual("rolled_back", statuses["a-recovered.md"])
        self.assertEqual("staged", statuses["b-fails.md"])
        self.assertTrue(
            any(
                row["step"] == "activation_claim"
                and row["status"] == "recovered"
                for row in failed["journal"]
            )
        )

        completed = self.service.activate_materialization(commit_id)
        self.assertEqual("completed", completed["status"])
        for path, expected in content.items():
            self.assertEqual(
                expected,
                (self.root / path).read_text(encoding="utf-8"),
            )

    def test_materialization_dead_owner_recovery_rolls_back_prior_create(
        self,
    ):
        content = {
            "created-before-crash.md": "approved-created",
            "existing-after-crash.md": "approved-existing-new",
        }
        old = {"existing-after-crash.md": "approved-existing-old"}
        commit_id = self.materialization_commit(
            content,
            existing_files=old,
        )
        self.service.stage_materialization(
            commit_id,
            target_root=self.root,
            files=content,
        )
        self.seed_crashed_materialization(
            commit_id,
            activated_paths=("created-before-crash.md",),
        )
        real_replace = os.replace
        failure_target = (
            self.root / "existing-after-crash.md"
        ).resolve(strict=False)

        def fail_existing_swap(source, target):
            if Path(target).resolve(strict=False) == failure_target:
                raise OSError("injected existing-file failure")
            return real_replace(source, target)

        with mock.patch(
            "scripts.continuity.service.os.replace",
            side_effect=fail_existing_swap,
        ):
            with self.assertRaises(ContinuityError) as caught:
                self.service.activate_materialization(commit_id)
        self.assertEqual(
            "MATERIALIZATION_ACTIVATION_FAILED",
            caught.exception.code,
        )
        self.assertFalse((self.root / "created-before-crash.md").exists())
        self.assertEqual(
            old["existing-after-crash.md"],
            (self.root / "existing-after-crash.md").read_text(
                encoding="utf-8"
            ),
        )
        failed = self.service.materialization_status(commit_id)
        statuses = {
            row["relative_path"]: row["file_status"]
            for row in failed["files"]
        }
        self.assertEqual("rolled_back", statuses["created-before-crash.md"])

    def test_materialization_fresh_already_proposed_file_is_idempotent(self):
        content = {"fresh-idempotent.md": "approved-new"}
        commit_id = self.materialization_commit(
            content,
            existing_files={"fresh-idempotent.md": "approved-old"},
        )
        staged = self.service.stage_materialization(
            commit_id,
            target_root=self.root,
            files=content,
        )
        (self.root / "fresh-idempotent.md").write_text(
            content["fresh-idempotent.md"],
            encoding="utf-8",
        )

        completed = self.service.activate_materialization(commit_id)

        self.assertEqual("completed", completed["status"])
        backup = (
            self.root
            / ".plot-rag"
            / "backups"
            / "materialize"
            / str(staged["run_id"])
            / "fresh-idempotent.md"
        )
        self.assertFalse(backup.exists())

    def test_materialization_approved_noop_needs_no_recovery_backup(self):
        content = {"approved-noop.md": "same-bytes"}
        commit_id = self.materialization_commit(
            content,
            existing_files=content,
        )
        staged = self.service.stage_materialization(
            commit_id,
            target_root=self.root,
            files=content,
        )

        completed = self.service.activate_materialization(commit_id)

        self.assertEqual("completed", completed["status"])
        backup = (
            self.root
            / ".plot-rag"
            / "backups"
            / "materialize"
            / str(staged["run_id"])
            / "approved-noop.md"
        )
        self.assertFalse(backup.exists())

    def test_materialization_recovery_rejects_hardlinked_backup(self):
        content = {"hardlink-backup.md": "approved-new"}
        old = {"hardlink-backup.md": "approved-old"}
        commit_id = self.materialization_commit(
            content,
            existing_files=old,
        )
        staged = self.service.stage_materialization(
            commit_id,
            target_root=self.root,
            files=content,
        )
        self.seed_crashed_materialization(
            commit_id,
            activated_paths=("hardlink-backup.md",),
        )
        backup = (
            self.root
            / ".plot-rag"
            / "backups"
            / "materialize"
            / str(staged["run_id"])
            / "hardlink-backup.md"
        )
        decoy = self.root / "same-old-bytes.md"
        decoy.write_text(old["hardlink-backup.md"], encoding="utf-8")
        backup.unlink()
        try:
            os.link(decoy, backup)
        except OSError as exc:  # pragma: no cover - filesystem capability
            self.skipTest(f"hard links unavailable: {exc}")

        with self.assertRaises(ContinuityError) as caught:
            self.service.activate_materialization(commit_id)

        self.assertEqual("MATERIALIZATION_ROLLBACK_FAILED", caught.exception.code)
        failed = self.service.materialization_status(commit_id)
        self.assertEqual("failed", failed["status"])
        self.assertEqual(
            "rollback_failed",
            failed["files"][0]["file_status"],
        )
        self.assertEqual(
            content["hardlink-backup.md"],
            (self.root / "hardlink-backup.md").read_text(encoding="utf-8"),
        )

    def test_materialization_rejects_hardlinked_activation_swap(self):
        content = {"hardlink-swap.md": "approved-new"}
        old = {"hardlink-swap.md": "approved-old"}
        commit_id = self.materialization_commit(
            content,
            existing_files=old,
        )
        staged = self.service.stage_materialization(
            commit_id,
            target_root=self.root,
            files=content,
        )
        staging = Path(str(staged["staging_path"]))
        decoy = self.root / "same-new-bytes.md"
        decoy.write_text(content["hardlink-swap.md"], encoding="utf-8")
        original_assert = self.service._assert_materialization_activation_owner
        injected = False

        def inject_hardlink(run_id, owner):
            nonlocal injected
            original_assert(run_id, owner)
            if injected:
                return
            swaps = list(staging.rglob(".*.activation"))
            if not swaps:
                return
            swap = swaps[0]
            swap.unlink()
            try:
                os.link(decoy, swap)
            except OSError as exc:  # pragma: no cover - filesystem capability
                self.skipTest(f"hard links unavailable: {exc}")
            injected = True

        with mock.patch.object(
            self.service,
            "_assert_materialization_activation_owner",
            side_effect=inject_hardlink,
        ):
            with self.assertRaises(ContinuityError) as caught:
                self.service.activate_materialization(commit_id)

        self.assertTrue(injected)
        self.assertEqual("STAGING_HASH_MISMATCH", caught.exception.code)
        self.assertEqual(
            old["hardlink-swap.md"],
            (self.root / "hardlink-swap.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            content["hardlink-swap.md"],
            decoy.read_text(encoding="utf-8"),
        )

    def test_materialization_activation_failure_releases_claim_for_retry(self):
        content = {"retry.md": "approved-new"}
        commit_id = self.materialization_commit(
            content,
            existing_files={"retry.md": "approved-old"},
        )
        staged = self.service.stage_materialization(
            commit_id,
            target_root=self.root,
            files=content,
        )

        with mock.patch(
            "scripts.continuity.service.os.replace",
            side_effect=OSError("injected owner replace failure"),
        ):
            with self.assertRaises(ContinuityError) as caught:
                self.service.activate_materialization(commit_id)
        self.assertEqual(
            caught.exception.code,
            "MATERIALIZATION_ACTIVATION_FAILED",
        )
        failed = self.service.materialization_status(commit_id)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(
            (self.root / "retry.md").read_text(encoding="utf-8"),
            "approved-old",
        )
        with self.service.store.read_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM materialization_activation_claims
                    WHERE run_id=?
                    """,
                    (staged["run_id"],),
                ).fetchone()[0],
                0,
            )

        completed = self.service.activate_materialization(commit_id)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            (self.root / "retry.md").read_text(encoding="utf-8"),
            "approved-new",
        )
        with self.service.store.read_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM materialization_activation_claims
                    WHERE run_id=?
                    """,
                    (staged["run_id"],),
                ).fetchone()[0],
                0,
            )

    def test_materialization_activation_owner_mismatch_cannot_write_back(self):
        content = {"ownership.md": "approved-new"}
        commit_id = self.materialization_commit(
            content,
            existing_files={"ownership.md": "approved-old"},
        )
        staged = self.service.stage_materialization(
            commit_id,
            target_root=self.root,
            files=content,
        )
        real_replace = os.replace

        def transfer_claim_then_replace(source, target):
            with self.service.store.transaction() as connection:
                cursor = connection.execute(
                    """
                    UPDATE materialization_activation_claims
                    SET owner_token='owner-b'
                    WHERE run_id=?
                    """,
                    (staged["run_id"],),
                )
                self.assertEqual(cursor.rowcount, 1)
            return real_replace(source, target)

        with mock.patch(
            "scripts.continuity.service.os.replace",
            side_effect=transfer_claim_then_replace,
        ):
            with self.assertRaises(ContinuityError) as caught:
                self.service.activate_materialization(commit_id)
        self.assertEqual(
            caught.exception.code,
            "MATERIALIZATION_ACTIVATION_OWNERSHIP_LOST",
        )
        self.assertEqual(
            (self.root / "ownership.md").read_text(encoding="utf-8"),
            "approved-new",
        )
        with self.service.store.read_connection() as connection:
            run = connection.execute(
                """
                SELECT run_status, error, completion_receipt_json
                FROM materialization_runs
                WHERE run_id=?
                """,
                (staged["run_id"],),
            ).fetchone()
            file_row = connection.execute(
                """
                SELECT file_status
                FROM materialization_files
                WHERE run_id=? AND relative_path='ownership.md'
                """,
                (staged["run_id"],),
            ).fetchone()
            claim = connection.execute(
                """
                SELECT owner_token
                FROM materialization_activation_claims
                WHERE run_id=?
                """,
                (staged["run_id"],),
            ).fetchone()
            failed_journal_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM materialization_journal
                WHERE run_id=?
                  AND (
                      step_name='rollback'
                      OR (step_name='activation' AND step_status='failed')
                  )
                """,
                (staged["run_id"],),
            ).fetchone()[0]
        self.assertEqual(run["run_status"], "activating")
        self.assertEqual(run["error"], "")
        self.assertEqual(json.loads(run["completion_receipt_json"]), {})
        self.assertEqual(file_row["file_status"], "staged")
        self.assertEqual(claim["owner_token"], "owner-b")
        self.assertEqual(failed_journal_count, 0)

    def test_materialization_requires_bound_paths_and_hashes(self):
        content = {"blocked.md": "approved"}
        commit_id = self.materialization_commit(
            content,
            bind_artifacts=False,
            authorized_paths=(),
        )
        with self.assertRaises(ContinuityError) as caught:
            self.service.stage_materialization(
                commit_id,
                target_root=self.root,
                files=content,
            )
        self.assertEqual(
            caught.exception.code,
            "MATERIALIZATION_AUTHORIZATION_REQUIRED",
        )
        self.assertEqual(
            set(caught.exception.details["missing"]),
            {"authorized_paths", "target_old_new_hashes"},
        )

        path_only_commit = self.materialization_commit(
            content,
            bind_artifacts=False,
            authorized_paths=("blocked.md",),
        )
        with self.assertRaises(ContinuityError) as caught:
            self.service.stage_materialization(
                path_only_commit,
                target_root=self.root,
                files=content,
            )
        self.assertEqual(
            caught.exception.code,
            "MATERIALIZATION_AUTHORIZATION_REQUIRED",
        )
        self.assertEqual(
            caught.exception.details["missing"],
            ["target_old_new_hashes"],
        )
        self.assertFalse((self.root / "blocked.md").exists())

        approved = {"approved.md": "approved"}
        tampered_commit = self.materialization_commit(approved)
        staged = self.service.stage_materialization(
            tampered_commit,
            target_root=self.root,
            files=approved,
        )
        arbitrary_content = "arbitrary"
        arbitrary_hash = hashlib.sha256(
            arbitrary_content.encode("utf-8")
        ).hexdigest()
        staging_root = Path(staged["staging_path"])
        (staging_root / "arbitrary.md").write_text(
            arbitrary_content,
            encoding="utf-8",
        )
        with self.service.store.transaction() as connection:
            connection.execute(
                """
                UPDATE materialization_files
                SET relative_path='arbitrary.md',
                    expected_old_hash=NULL,
                    proposed_new_hash=?,
                    actual_hash=?,
                    file_status='staged'
                WHERE run_id=?
                """,
                (arbitrary_hash, arbitrary_hash, staged["run_id"]),
            )
        with self.assertRaises(ContinuityError) as caught:
            self.service.activate_materialization(tampered_commit)
        self.assertEqual(
            caught.exception.code,
            "MATERIALIZATION_PATH_NOT_AUTHORIZED",
        )
        self.assertFalse((self.root / "arbitrary.md").exists())

    def test_activation_rejects_symlink_parent_swapped_after_staging(self):
        content = {"link/escaped.md": "approved"}
        commit_id = self.materialization_commit(content)
        with tempfile.TemporaryDirectory() as outside_temporary:
            outside = Path(outside_temporary)
            run_id = self.service.materialization_status(commit_id)["run_id"]
            staging_link = (
                self.root / ".plot-rag" / "staging" / run_id
            )
            staging_link.parent.mkdir(parents=True, exist_ok=True)
            try:
                staging_link.symlink_to(
                    outside,
                    target_is_directory=True,
                )
            except OSError as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")
            with self.assertRaises(ContinuityError) as caught:
                self.service.stage_materialization(
                    commit_id,
                    target_root=self.root,
                    files=content,
                )
            self.assertEqual(caught.exception.code, "UNSAFE_STAGING_PATH")
            self.assertFalse((outside / "link" / "escaped.md").exists())
            staging_link.unlink()

            self.service.stage_materialization(
                commit_id,
                target_root=self.root,
                files=content,
            )
            link = self.root / "link"
            link.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ContinuityError) as caught:
                self.service.activate_materialization(commit_id)
            self.assertEqual(
                caught.exception.code,
                "UNSAFE_MATERIALIZATION_PATH",
            )
            self.assertFalse((outside / "escaped.md").exists())
            failed = self.service.materialization_status(commit_id)
            self.assertEqual(failed["status"], "failed")
            self.assertTrue(
                any(
                    item["step"] == "activation"
                    and item["status"] == "failed"
                    for item in failed["journal"]
                )
            )
            link.unlink()

            internal_content = {"internal-link/file.md": "approved"}
            internal_commit = self.materialization_commit(internal_content)
            self.service.stage_materialization(
                internal_commit,
                target_root=self.root,
                files=internal_content,
            )
            internal_target = self.root / "internal-target"
            internal_target.mkdir()
            (self.root / "internal-link").symlink_to(
                internal_target,
                target_is_directory=True,
            )
            with self.assertRaises(ContinuityError) as caught:
                self.service.activate_materialization(internal_commit)
            self.assertEqual(
                caught.exception.code,
                "UNSAFE_MATERIALIZATION_PATH",
            )
            self.assertFalse((internal_target / "file.md").exists())

            independent = self.root / "independent-target"
            root_content = {"root-escaped.md": "approved"}
            root_commit = self.materialization_commit(
                root_content,
                target_root=independent,
            )
            self.service.stage_materialization(
                root_commit,
                target_root=independent,
                files=root_content,
            )
            original_target = self.root / "independent-target-original"
            independent.rename(original_target)
            independent.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ContinuityError) as caught:
                self.service.activate_materialization(root_commit)
            self.assertEqual(
                caught.exception.code,
                "UNSAFE_MATERIALIZATION_PATH",
            )
            self.assertFalse((outside / "root-escaped.md").exists())

    def test_activation_rejects_internal_backup_reparse_point(self):
        content = {"existing.md": "approved-new"}
        commit_id = self.materialization_commit(
            content,
            existing_files={"existing.md": "approved-old"},
        )
        self.service.stage_materialization(
            commit_id,
            target_root=self.root,
            files=content,
        )
        backup_parent = self.root / ".plot-rag" / "backups"
        backup_parent.mkdir(parents=True, exist_ok=True)
        backup_sink = self.root / "backup-sink"
        backup_sink.mkdir()
        backup_link = backup_parent / "materialize"
        try:
            backup_link.symlink_to(
                backup_sink,
                target_is_directory=True,
            )
        except OSError as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")

        with self.assertRaises(ContinuityError) as caught:
            self.service.activate_materialization(commit_id)
        self.assertEqual(caught.exception.code, "UNSAFE_BACKUP_PATH")
        self.assertEqual(
            "approved-old",
            (self.root / "existing.md").read_text(encoding="utf-8"),
        )
        self.assertEqual([], list(backup_sink.rglob("*")))

    def test_activation_preflights_all_files_and_rolls_back_replace_failure(self):
        content = {
            "a-existing.md": "approved-a",
            "b-new/nested.md": "approved-b",
            "z-fail.md": "approved-z",
        }
        commit_id = self.materialization_commit(
            content,
            existing_files={"a-existing.md": "original-a"},
        )
        self.service.stage_materialization(
            commit_id,
            target_root=self.root,
            files=content,
        )

        conflict = self.root / "z-fail.md"
        conflict.write_text("concurrent-user-edit", encoding="utf-8")
        with self.assertRaises(ContinuityError) as caught:
            self.service.activate_materialization(commit_id)
        self.assertEqual(caught.exception.code, "TARGET_HASH_CONFLICT")
        self.assertEqual(
            (self.root / "a-existing.md").read_text(encoding="utf-8"),
            "original-a",
        )
        self.assertFalse((self.root / "b-new").exists())
        self.assertEqual(
            conflict.read_text(encoding="utf-8"),
            "concurrent-user-edit",
        )

        conflict.unlink()
        self.service.stage_materialization(
            commit_id,
            target_root=self.root,
            files=content,
        )
        real_replace = os.replace
        replace_calls = 0

        def fail_third_replace(source, target):
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 3:
                raise OSError("injected third replace failure")
            return real_replace(source, target)

        with mock.patch(
            "scripts.continuity.service.os.replace",
            side_effect=fail_third_replace,
        ):
            with self.assertRaises(ContinuityError) as caught:
                self.service.activate_materialization(commit_id)
        self.assertEqual(
            caught.exception.code,
            "MATERIALIZATION_ACTIVATION_FAILED",
        )
        self.assertEqual(
            (self.root / "a-existing.md").read_text(encoding="utf-8"),
            "original-a",
        )
        self.assertFalse((self.root / "b-new").exists())
        self.assertFalse((self.root / "z-fail.md").exists())
        failed = self.service.materialization_status(commit_id)
        self.assertEqual(failed["status"], "failed")
        file_statuses = {
            item["relative_path"]: item["file_status"]
            for item in failed["files"]
        }
        self.assertEqual(file_statuses["a-existing.md"], "rolled_back")
        self.assertEqual(file_statuses["b-new/nested.md"], "rolled_back")
        self.assertEqual(file_statuses["z-fail.md"], "staged")
        self.assertTrue(
            any(
                item["step"] == "rollback"
                and item["status"] == "completed"
                for item in failed["journal"]
            )
        )

    def test_rollback_rejects_parent_reparse_swap_before_deleting_target(self):
        content = {
            "a/new.md": "approved-a",
            "z-fail.md": "approved-z",
        }
        commit_id = self.materialization_commit(content)
        self.service.stage_materialization(
            commit_id,
            target_root=self.root,
            files=content,
        )
        real_replace = os.replace
        replace_calls = 0
        victim = self.root / "victim"
        victim.mkdir()
        victim_file = victim / "new.md"
        victim_file.write_text("approved-a", encoding="utf-8")
        displaced = self.root / "a-displaced"
        probe = self.root / "reparse-probe"
        try:
            probe.symlink_to(victim, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")
        else:
            probe.unlink()

        def swap_parent_then_fail(source, target):
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 1:
                result = real_replace(source, target)
                (self.root / "a").rename(displaced)
                (self.root / "a").symlink_to(
                    victim,
                    target_is_directory=True,
                )
                return result
            raise OSError("injected failure after parent reparse swap")

        try:
            with mock.patch(
                "scripts.continuity.service.os.replace",
                side_effect=swap_parent_then_fail,
            ):
                with self.assertRaises(ContinuityError):
                    self.service.activate_materialization(commit_id)
            self.assertTrue(victim_file.is_file())
            self.assertEqual(
                "approved-a",
                victim_file.read_text(encoding="utf-8"),
            )
            failed = self.service.materialization_status(commit_id)
            self.assertTrue(
                any(
                    item["relative_path"] == "a/new.md"
                    and item["file_status"] == "rollback_failed"
                    for item in failed["files"]
                )
            )
        finally:
            swapped = self.root / "a"
            if swapped.is_symlink():
                swapped.unlink()

    def test_initialization_source_drift_blocks_accept(self):
        source = self.root / "source.md"
        source.write_text("original", encoding="utf-8")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        bundle = {
            "bundle_id": "drift-fixture",
            "target_project_real_path": str(self.root),
            "source_manifest": [
                {
                    "source_id": "source-drift",
                    "path": "source.md",
                    "real_path": str(source),
                    "content_hash": source_hash,
                    "source_role": "setting",
                }
            ],
            "events": [],
        }
        saved = self.service.save_initialization_bundle(bundle)
        grant = self.host.issue(
            saved["proposal_id"],
            expected_canon_revision=0,
            operations=("accept_initialization",),
        )
        source.write_text("changed", encoding="utf-8")
        with self.assertRaises(ContinuityError) as caught:
            self.service.accept_proposal(
                saved["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=0,
            )
        self.assertEqual(caught.exception.code, "SOURCE_MANIFEST_DRIFT")
        self.assertEqual(
            self.service.get_canon_revisions(), {"head": 0, "active": 0}
        )

    def test_frozen_plot_init_proposal_adapts_and_materializes_directly(self):
        content = "# 世界内核\n\n真实规则。\n"
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        bundle = {
            "schema_version": 1,
            "meta": {"proposal_only": True},
            "world_model": {
                "rules": ["能力必有代价"],
                "mvw": {"locations_and_routes": ["测试城"]},
            },
            "actor_system": {
                "protagonist": {
                    "name": "测试角色甲",
                    "location": "测试城",
                    "external_goal": "保住退路",
                    "resources": ["青铜钥匙"],
                },
                "opponents": [],
                "third_parties": [],
            },
            "story_engine": {},
            "serialization_contract": {},
            "source_manifest": [],
            "provenance": {"claims": []},
            "artifact_manifest": [
                {
                    "artifact_id": "artifact-world",
                    "path": "设定集/世界内核.md",
                    "operation": "create",
                    "expected_old_hash": None,
                    "proposed_new_hash": content_hash,
                    "proposed_content": content,
                    "unified_diff": "volatile",
                    "materialized": False,
                }
            ],
        }
        package_hash = canonical_hash(
            bundle,
            extra_volatile_keys=(
                "real_path",
                "normalized_real_path",
                "unified_diff",
            ),
        )
        bundle["bundle_hash"] = package_hash
        frozen = {
            "schema_version": 1,
            "proposal_id": "initp-fixture",
            "package_hash": package_hash,
            "status": "PROPOSAL_FROZEN",
            "target_project_real_path": str(self.root),
            "source_manifest_hash": canonical_hash([]),
            "bundle": bundle,
            "apply_plan": {
                "requires_approval_grant": True,
                "authorized_operations_required": [
                    "accept_initialization",
                    "materialize",
                ],
                "artifacts": [
                    {
                        "artifact_id": "artifact-world",
                        "path": "设定集/世界内核.md",
                        "operation": "create",
                        "expected_old_hash": None,
                        "proposed_new_hash": content_hash,
                    }
                ],
                "executed": False,
            },
        }
        saved = self.service.save_initialization_bundle(frozen)
        self.assertGreater(len(saved["events"]), 3)
        grant = self.host.issue(
            saved["proposal_id"],
            expected_canon_revision=0,
            operations=("accept_initialization", "materialize"),
        )
        applied = self.service.apply_initialization_bundle(
            frozen,
            proposal_id=saved["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=0,
        )
        with self.assertRaises(ContinuityError) as caught:
            self.service.stage_materialization(
                applied["commit_id"],
                files={"设定集/世界内核.md": "tampered"},
            )
        self.assertEqual(
            caught.exception.code, "MATERIALIZATION_NEW_HASH_NOT_AUTHORIZED"
        )
        staged = self.service.stage_materialization(applied["commit_id"])
        self.assertEqual(staged["status"], "staged")
        completed = self.service.activate_materialization(
            applied["commit_id"]
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            (self.root / "设定集" / "世界内核.md").read_text(encoding="utf-8"),
            content,
        )
        self.assertTrue(
            any(
                fact["scope"] == "timeless"
                for fact in self.service.query_facts()["facts"]
            )
        )


if __name__ == "__main__":
    unittest.main()
