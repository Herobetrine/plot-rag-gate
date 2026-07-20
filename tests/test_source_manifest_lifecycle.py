from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.continuity import (
    ContinuityError,
    ContinuityService,
    HostApprovalAuthority,
)
from scripts.continuity import source_manifest as source_manifest_module
from scripts.v1_runtime import doctor_v1, refresh_longform_index


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SourceManifestLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.service = ContinuityService(self.root)
        self.host = HostApprovalAuthority(
            self.service,
            issuer="source-manifest-test-host",
            channel="interactive_test",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write(self, relative: str, text: str) -> dict[str, object]:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return {
            "source_path": relative,
            "content_hash": _sha256(path),
            "source_role": "setting",
            "metadata": {
                "artifact_stage": "setting",
                "indexable": True,
                "file_size": path.stat().st_size,
            },
        }

    def _seed_active_manifest(
        self,
        sources: list[dict[str, object]],
    ) -> str:
        proposal = self.service.save_proposal(
            events=[],
            artifact_id="bootstrap-source-carrier",
            artifact_kind="initialization",
            artifact_stage="bootstrap",
            branch_id="main",
            proposal_kind="story_delta",
        )
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            proposal["proposal_id"],
            expected_canon_revision=revision,
            operations=("accept",),
        )
        commit = self.service.accept_proposal(
            proposal["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=revision,
        )
        with self.service.store.transaction() as connection:
            for index, source in enumerate(sources):
                connection.execute(
                    """
                    INSERT INTO accepted_source_manifest(
                        manifest_entry_id, commit_id, source_id, source_path,
                        content_hash, source_role, manifest_status,
                        metadata_json, activated_at, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        f"seed-manifest-{index}",
                        commit["commit_id"],
                        f"seed-source-{index}",
                        source["source_path"],
                        source["content_hash"],
                        source["source_role"],
                        json.dumps(
                            source["metadata"],
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "2026-07-20T00:00:00+00:00",
                        "2026-07-20T00:00:00+00:00",
                    ),
                )
        return str(commit["commit_id"])

    def _plan(
        self,
        *,
        retain_ids: list[str],
        deactivate_ids: list[str],
        upserts: list[dict[str, object]],
        target_sources: list[dict[str, object]],
    ) -> dict[str, object]:
        revision = self.service.get_canon_revisions()["active"]
        return {
            "schema_version": "plot-rag-source-manifest-migration-plan/v1",
            "generated_at": "2026-07-20T00:00:00+00:00",
            "project_root": str(self.root),
            "expected_canon_revision": revision,
            "head_canon_revision": self.service.get_canon_revisions()["head"],
            "retire_commits": [],
            "baseline": {},
            "operations": {
                "deactivate_entry_ids": deactivate_ids,
                "retain_entry_ids": retain_ids,
                "upserts": upserts,
            },
            "target": {
                "active_rows": len(target_sources),
                "unique_paths": len(target_sources),
                "sources": target_sources,
            },
        }

    def _accept_manifest(
        self,
        plan: dict[str, object],
        *,
        key: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        revision = self.service.get_canon_revisions()["active"]
        proposed = self.service.propose_source_manifest_change(
            plan,
            expected_canon_revision=revision,
            idempotency_key=key,
        )
        proposal = proposed["proposal"]
        grant = self.host.issue(
            proposal["proposal_id"],
            expected_canon_revision=revision,
            operations=("accept_source_manifest",),
        )
        commit = self.service.accept_proposal(
            proposal["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=revision,
        )
        return proposal, commit

    def _retract(self, proposal_id: str, *, reason: str) -> dict[str, object]:
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            proposal_id,
            expected_canon_revision=revision,
            operations=("retract",),
        )
        return self.service.retract_proposal(
            proposal_id,
            approval_id=grant["approval_id"],
            expected_canon_revision=revision,
            reason=reason,
        )

    def test_full_lifecycle_second_migration_and_retract_restore(self) -> None:
        alpha = self._write("setting/alpha.md", "alpha-v1")
        beta = self._write("setting/beta.md", "beta-v1")
        self._seed_active_manifest([alpha, beta])
        original = self.service.get_current_source_manifest_snapshot()["entries"]
        self.assertEqual(2, len(original))

        beta = self._write("setting/beta.md", "beta-v2")
        gamma = self._write("setting/gamma.md", "gamma-v1")
        plan = self._plan(
            retain_ids=["seed-manifest-0"],
            deactivate_ids=["seed-manifest-1"],
            upserts=[beta, gamma],
            target_sources=[alpha, beta, gamma],
        )
        first, first_commit = self._accept_manifest(
            plan,
            key="manifest-first",
        )
        self.assertTrue(first_commit["changes_authority"])
        self.assertEqual(
            {"head": 2, "active": 2},
            self.service.get_canon_revisions(),
        )
        status = self.service.source_manifest_status()
        self.assertEqual((3, 3, 0), (
            status["active_rows"],
            status["unique_active_paths"],
            status["duplicate_active_rows"],
        ))
        self.assertEqual(4, status["history_rows"])

        current = self.service.get_current_source_manifest_snapshot()["entries"]
        second_plan = self._plan(
            retain_ids=sorted(item["manifest_entry_id"] for item in current),
            deactivate_ids=[],
            upserts=[],
            target_sources=[
                {
                    "source_path": item["path"],
                    "content_hash": item["content_hash"],
                    "source_role": item["source_role"],
                    "metadata": item["metadata"],
                }
                for item in current
            ],
        )
        second, _ = self._accept_manifest(
            second_plan,
            key="manifest-second",
        )
        self.assertEqual(
            3,
            len(self.service.get_current_source_manifest_snapshot()["entries"]),
        )

        with self.assertRaises(ContinuityError) as caught:
            self._retract(
                first["proposal_id"],
                reason="older migration is not current",
            )
        self.assertEqual(
            "SOURCE_MANIFEST_RETRACT_NOT_LATEST",
            caught.exception.code,
        )

        self._retract(second["proposal_id"], reason="restore first migration")
        self.assertEqual(
            first["proposal_id"],
            self.service.source_manifest_status()[
                "active_manifest_proposal_id"
            ],
        )
        self.assertEqual(
            3,
            len(self.service.get_current_source_manifest_snapshot()["entries"]),
        )

        self._write("setting/beta.md", "beta-v1")
        self._retract(first["proposal_id"], reason="restore bootstrap manifest")
        restored = self.service.source_manifest_status()
        self.assertEqual((2, 2, 0), (
            restored["active_rows"],
            restored["unique_active_paths"],
            restored["duplicate_active_rows"],
        ))
        self.assertEqual(
            {"setting/alpha.md", "setting/beta.md"},
            {
                item["path"]
                for item in self.service.get_current_source_manifest_snapshot()[
                    "entries"
                ]
            },
        )

    def test_source_manifest_requires_dedicated_authority_envelope(self) -> None:
        alpha = self._write("setting/alpha.md", "alpha-v1")
        self._seed_active_manifest([alpha])
        alpha_v2 = self._write("setting/alpha.md", "alpha-v2")
        plan = self._plan(
            retain_ids=[],
            deactivate_ids=["seed-manifest-0"],
            upserts=[alpha_v2],
            target_sources=[alpha_v2],
        )
        migration = self.service.preview_source_manifest_change(
            plan,
            expected_canon_revision=1,
        )["migration"]
        cases = (
            {
                "artifact_id": "forged-source-manifest",
                "artifact_kind": "source_manifest",
                "artifact_stage": "bootstrap",
                "branch_id": "main",
            },
            {
                "artifact_id": "plot_rag_source_manifest",
                "artifact_kind": "source_manifest",
                "artifact_stage": "draft",
                "branch_id": "main",
            },
            {
                "artifact_id": "plot_rag_source_manifest",
                "artifact_kind": "story",
                "artifact_stage": "bootstrap",
                "branch_id": "main",
            },
            {
                "artifact_id": "plot_rag_source_manifest",
                "artifact_kind": "source_manifest",
                "artifact_stage": "bootstrap",
                "branch_id": "branch-forged",
            },
        )
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                forged = self.service.save_proposal(
                    events=[],
                    payload={"source_manifest_change": migration},
                    prepared_canon_revision=1,
                    source_role="setting",
                    proposal_kind="source_manifest_change",
                    idempotency_key=f"forged-envelope-{index}",
                    **case,
                )
                with self.assertRaises(ContinuityError) as caught:
                    self.host.issue(
                        forged["proposal_id"],
                        expected_canon_revision=1,
                        operations=("accept_source_manifest",),
                    )
                self.assertEqual(
                    "SOURCE_MANIFEST_PROPOSAL_ENVELOPE_INVALID",
                    caught.exception.code,
                )
        self.assertEqual(
            {"head": 1, "active": 1},
            self.service.get_canon_revisions(),
        )
        self.assertEqual(
            {"setting/alpha.md"},
            {
                item["path"]
                for item in self.service.get_current_source_manifest_snapshot()[
                    "entries"
                ]
            },
        )

    def test_freeze_rejects_result_forgery_and_metadata_drift(self) -> None:
        alpha = self._write("setting/alpha.md", "alpha-v1")
        beta = self._write("setting/beta.md", "beta-v1")
        self._seed_active_manifest([alpha, beta])

        drifted_alpha = {
            **alpha,
            "metadata": {
                **dict(alpha["metadata"]),
                "indexable": False,
            },
        }
        bad_retain_plan = self._plan(
            retain_ids=["seed-manifest-0", "seed-manifest-1"],
            deactivate_ids=[],
            upserts=[],
            target_sources=[drifted_alpha, beta],
        )
        with self.assertRaises(ContinuityError) as caught:
            self.service.preview_source_manifest_change(
                bad_retain_plan,
                expected_canon_revision=1,
            )
        self.assertEqual(
            "SOURCE_MANIFEST_RETAIN_TARGET_MISMATCH",
            caught.exception.code,
        )

        beta_v2 = self._write("setting/beta.md", "beta-v2")
        plan = self._plan(
            retain_ids=["seed-manifest-0"],
            deactivate_ids=["seed-manifest-1"],
            upserts=[beta_v2],
            target_sources=[alpha, beta_v2],
        )
        preview = self.service.preview_source_manifest_change(
            plan,
            expected_canon_revision=1,
        )
        migration = json.loads(
            json.dumps(preview["migration"], ensure_ascii=False)
        )
        migration["result_manifest"][0][
            "manifest_entry_id"
        ] = migration["result_manifest"][1]["manifest_entry_id"]
        migration["target_manifest_hash"] = (
            source_manifest_module._target_manifest_hash(
                migration["result_manifest"]
            )
        )
        forged = self.service.save_proposal(
            events=[],
            payload={"source_manifest_change": migration},
            artifact_id="plot_rag_source_manifest",
            artifact_kind="source_manifest",
            artifact_stage="bootstrap",
            branch_id="main",
            prepared_canon_revision=1,
            source_role="setting",
            proposal_kind="source_manifest_change",
        )
        with self.assertRaises(ContinuityError) as caught:
            self.host.issue(
                forged["proposal_id"],
                expected_canon_revision=1,
                operations=("accept_source_manifest",),
            )
        self.assertIn(
            caught.exception.code,
            {
                "SOURCE_MANIFEST_FROZEN_PLAN_MISMATCH",
                "SOURCE_MANIFEST_OPERATION_ID_CONFLICT",
                "SOURCE_MANIFEST_RESULT_OPERATION_MISMATCH",
            },
        )

    def test_frozen_plan_blocks_forged_base_ids_hashes_and_actions(self) -> None:
        alpha = self._write("setting/alpha.md", "alpha-v1")
        self._seed_active_manifest([alpha])
        alpha_v2 = self._write("setting/alpha.md", "alpha-v2")
        plan = self._plan(
            retain_ids=[],
            deactivate_ids=["seed-manifest-0"],
            upserts=[alpha_v2],
            target_sources=[alpha_v2],
        )
        preview = self.service.preview_source_manifest_change(
            plan,
            expected_canon_revision=1,
        )
        migration = json.loads(
            json.dumps(preview["migration"], ensure_ascii=False)
        )
        migration["plan_hash"] = "source_manifest_plan_" + ("f" * 64)
        migration["base_manifest"] = []
        upsert = migration["operations"]["upsert"][0]
        upsert["source_id"] = "source_forged"
        upsert["manifest_entry_id"] = "manifest_entry_forged"
        upsert["action"] = "enroll"
        upsert["metadata"]["manifest_plan_hash"] = migration["plan_hash"]
        upsert["metadata"]["manifest_action"] = upsert["action"]
        result = migration["result_manifest"][0]
        result["source_id"] = upsert["source_id"]
        result["manifest_entry_id"] = upsert["manifest_entry_id"]
        result["metadata"] = dict(upsert["metadata"])
        migration["target_manifest_hash"] = (
            source_manifest_module._target_manifest_hash(
                migration["result_manifest"]
            )
        )
        forged = self.service.save_proposal(
            events=[],
            payload={"source_manifest_change": migration},
            artifact_id="plot_rag_source_manifest",
            artifact_kind="source_manifest",
            artifact_stage="bootstrap",
            branch_id="main",
            prepared_canon_revision=1,
            source_role="setting",
            proposal_kind="source_manifest_change",
        )
        with self.assertRaises(ContinuityError) as caught:
            self.host.issue(
                forged["proposal_id"],
                expected_canon_revision=1,
                operations=("accept_source_manifest",),
            )
        self.assertEqual(
            "SOURCE_MANIFEST_FROZEN_PLAN_MISMATCH",
            caught.exception.code,
        )
        self.assertEqual(
            {"setting/alpha.md"},
            {
                item["path"]
                for item in self.service.get_current_source_manifest_snapshot()[
                    "entries"
                ]
            },
        )

    def test_plan_requires_exact_json_integers(self) -> None:
        alpha = self._write("setting/alpha.md", "alpha-v1")
        self._seed_active_manifest([alpha])
        base = self._plan(
            retain_ids=["seed-manifest-0"],
            deactivate_ids=[],
            upserts=[],
            target_sources=[alpha],
        )
        invalid_values = (True, 1.0, "1")
        for value in invalid_values:
            with self.subTest(field="expected_canon_revision", value=value):
                plan = json.loads(json.dumps(base))
                plan["expected_canon_revision"] = value
                with self.assertRaises(ContinuityError) as caught:
                    self.service.preview_source_manifest_change(
                        plan,
                        expected_canon_revision=1,
                    )
                self.assertEqual("INVALID_FIELD", caught.exception.code)

        for field in ("active_rows", "unique_paths"):
            for value in invalid_values:
                with self.subTest(field=field, value=value):
                    plan = json.loads(json.dumps(base))
                    plan["target"][field] = value
                    with self.assertRaises(ContinuityError) as caught:
                        self.service.preview_source_manifest_change(
                            plan,
                            expected_canon_revision=1,
                        )
                    self.assertEqual("INVALID_FIELD", caught.exception.code)

    def test_retract_requires_restored_base_bytes_at_grant_and_consume(
        self,
    ) -> None:
        config_dir = self.root / ".plot-rag"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text(
            json.dumps(
                {
                    "config_version": 3,
                    "enabled": True,
                    "authority_sources": [
                        {
                            "glob": "setting/**/*.md",
                            "role": "setting",
                            "scope_policy": "timeless_candidate",
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
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        alpha_v1 = self._write("setting/alpha.md", "alpha-v1")
        self._seed_active_manifest([alpha_v1])
        alpha_v2 = self._write("setting/alpha.md", "alpha-v2")
        plan = self._plan(
            retain_ids=[],
            deactivate_ids=["seed-manifest-0"],
            upserts=[alpha_v2],
            target_sources=[alpha_v2],
        )
        proposal, _ = self._accept_manifest(
            plan,
            key="manifest-retract-bytes",
        )
        accepted_revision = self.service.get_canon_revisions()
        with self.service.store.read_connection() as connection:
            grants_before = int(
                connection.execute(
                    "SELECT COUNT(*) FROM approval_grants"
                ).fetchone()[0]
            )
            commits_before = int(
                connection.execute(
                    "SELECT COUNT(*) FROM canon_commits"
                ).fetchone()[0]
            )

        with self.assertRaises(ContinuityError) as caught:
            self.host.issue(
                proposal["proposal_id"],
                expected_canon_revision=accepted_revision["active"],
                operations=("retract",),
            )
        self.assertEqual(
            "SOURCE_MANIFEST_RETRACT_BASE_HASH_MISMATCH",
            caught.exception.code,
        )
        self.assertEqual(
            accepted_revision,
            self.service.get_canon_revisions(),
        )
        with self.service.store.read_connection() as connection:
            self.assertEqual(
                grants_before,
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM approval_grants"
                    ).fetchone()[0]
                ),
            )
            self.assertEqual(
                commits_before,
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM canon_commits"
                    ).fetchone()[0]
                ),
            )
        self.assertEqual(
            "accepted",
            self.service.inspect_proposal(proposal["proposal_id"])[
                "canon_status"
            ],
        )

        alpha_v1 = self._write("setting/alpha.md", "alpha-v1")
        grant = self.host.issue(
            proposal["proposal_id"],
            expected_canon_revision=accepted_revision["active"],
            operations=("retract",),
        )
        with self.service.store.read_connection() as connection:
            self.assertEqual(
                grants_before + 1,
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM approval_grants"
                    ).fetchone()[0]
                ),
            )
            self.assertEqual(
                commits_before,
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM canon_commits"
                    ).fetchone()[0]
                ),
            )
        self._write("setting/alpha.md", "alpha-v2")
        with self.assertRaises(ContinuityError) as caught:
            self.service.retract_proposal(
                proposal["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=accepted_revision["active"],
                reason="consume must recheck bytes",
            )
        self.assertEqual(
            "SOURCE_MANIFEST_RETRACT_BASE_HASH_MISMATCH",
            caught.exception.code,
        )
        self.assertEqual(
            accepted_revision,
            self.service.get_canon_revisions(),
        )
        with self.service.store.read_connection() as connection:
            grant_row = connection.execute(
                """
                SELECT consumed_request_hash
                FROM approval_grants
                WHERE proposal_id=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (proposal["proposal_id"],),
            ).fetchone()
            self.assertIsNotNone(grant_row)
            self.assertIsNone(grant_row["consumed_request_hash"])
            self.assertEqual(
                commits_before,
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM canon_commits"
                    ).fetchone()[0]
                ),
            )

        alpha_v1 = self._write("setting/alpha.md", "alpha-v1")
        retracted = self.service.retract_proposal(
            proposal["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=accepted_revision["active"],
            reason="base bytes restored",
        )
        self.assertEqual("retract", retracted["operation"])
        with self.service.store.read_connection() as connection:
            self.assertEqual(
                commits_before + 1,
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM canon_commits"
                    ).fetchone()[0]
                ),
            )
        current = self.service.get_current_source_manifest_snapshot()["entries"]
        self.assertEqual(1, len(current))
        self.assertEqual(alpha_v1["content_hash"], current[0]["content_hash"])

        doctor = doctor_v1(self.root)
        self.assertEqual(
            "ok",
            doctor["components"]["source_manifest"]["status"],
        )
        refreshed = refresh_longform_index(self.root)
        self.assertEqual(
            0,
            refreshed["refresh"]["manifest_hash_mismatches"],
        )
        self.assertEqual(1, refreshed["schema"]["file_count"])

    def test_replay_rejects_tampered_manifest_row(self) -> None:
        alpha = self._write("setting/alpha.md", "alpha-v1")
        self._seed_active_manifest([alpha])
        alpha_v2 = self._write("setting/alpha.md", "alpha-v2")
        plan = self._plan(
            retain_ids=[],
            deactivate_ids=["seed-manifest-0"],
            upserts=[alpha_v2],
            target_sources=[alpha_v2],
        )
        self._accept_manifest(plan, key="manifest-tamper")
        entry = self.service.source_manifest_status()["active"][0]
        with self.service.store.transaction() as connection:
            connection.execute(
                """
                UPDATE accepted_source_manifest
                SET content_hash=?
                WHERE manifest_entry_id=?
                """,
                ("0" * 64, entry["manifest_entry_id"]),
            )
        with self.assertRaises(ContinuityError) as caught:
            self.service.replay()
        self.assertEqual(
            "SOURCE_MANIFEST_REPLAY_ENTRY_MISMATCH",
            caught.exception.code,
        )


if __name__ == "__main__":
    unittest.main()
