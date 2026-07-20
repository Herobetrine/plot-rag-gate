from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from continuity import ContinuityError, ContinuityService  # noqa: E402
from continuity.validators import stable_hash  # noqa: E402
from plot_init import PlotInitError, PlotInitService  # noqa: E402
from v1_runtime import (  # noqa: E402
    accept_plot_proposal,
    apply_initialization_proposal,
    doctor_v1,
    issue_host_approval,
    prepare_initialization_apply,
    reject_plot_proposal,
    retract_plot_proposal,
    verify_initialization,
)
from tests.test_plot_init import complete_seed  # noqa: E402
from tests.test_power_initialization import cultivation_seed  # noqa: E402


class PowerInitializationRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.project = self.workspace / "novel"
        self.project.mkdir()
        (self.project / ".plot-rag").mkdir()
        (self.project / "正文").mkdir()
        (self.project / "正文" / "第一章.md").write_text(
            "测试角色甲在测试城清点当前状态。",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def configure(self, bundle_schema_version: str) -> None:
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
                "embedding": {
                    "enabled": True,
                    "base_url": "https://api.siliconflow.cn/v1",
                    "model": "fixture-embedding-v1",
                    "api_key_env": "PLOT_RAG_EMBED_API_KEY",
                    "api_key_required": True,
                },
                "rerank": {"enabled": False},
                "extract": {"enabled": False},
            },
            "initialization": {
                "schema_version": bundle_schema_version,
                "database_path": ".plot-rag/init.sqlite3",
                "proposal_only": True,
            },
        }
        (self.project / ".plot-rag" / "config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def freeze(
        self,
        *,
        bundle_schema_version: str = "plot-rag-init/v2",
        key_suffix: str = "",
        expected_canon_revision: int = 0,
    ) -> tuple[PlotInitService, dict]:
        self.configure(bundle_schema_version)
        initializer = PlotInitService(
            self.workspace,
            database_path=self.project / ".plot-rag" / "init.sqlite3",
        )
        seed = (
            cultivation_seed()
            if bundle_schema_version == "plot-rag-init/v2"
            else complete_seed()
        )
        suffix = f":{key_suffix}" if key_suffix else ""
        started = initializer.start(
            project_root=self.project,
            mode="new",
            interaction_profile="deep",
            seed=seed,
            bundle_schema_version=bundle_schema_version,
            expected_canon_revision=expected_canon_revision,
            idempotency_key=(
                f"runtime-start:{bundle_schema_version}{suffix}"
            ),
        )
        frozen = initializer.propose(
            started["session_id"],
            expected_session_revision=started["session_revision"],
            idempotency_key=(
                f"runtime-propose:{bundle_schema_version}{suffix}"
            ),
        )["proposal"]
        return initializer, frozen

    @staticmethod
    def fake_embedding(
        _service: object,
        inputs: list[str] | tuple[str, ...],
    ) -> tuple[list[list[float]], dict]:
        return (
            [[0.125, 0.5, 0.875] for _ in inputs],
            {"status": "ok"},
        )

    def grant_consumed_at(self, approval_id: str) -> str | None:
        service = ContinuityService(self.project)
        token_hash = stable_hash(
            approval_id,
            prefix="grant_token_",
        )
        with service.store.read_connection() as connection:
            row = connection.execute(
                """
                SELECT consumed_at
                FROM approval_grants
                WHERE token_hash=?
                """,
                (token_hash,),
            ).fetchone()
        self.assertIsNotNone(row)
        return row["consumed_at"]

    def accept_power_spec(
        self,
        frozen: dict,
    ) -> tuple[dict, dict, dict]:
        pending = prepare_initialization_apply(
            self.project,
            frozen["proposal_id"],
            workspace_root=self.workspace,
        )
        self.assertEqual(
            "POWER_SPEC_APPROVAL_REQUIRED",
            pending["status"],
        )
        grant = issue_host_approval(
            self.project,
            pending["power_spec_proposal_id"],
            expected_canon_revision=0,
            issuer="power-runtime-unittest",
            channel="interactive_test",
            workspace_root=self.workspace,
        )
        self.assertEqual(
            ["accept_power_spec"],
            grant["grant"]["operations"],
        )
        accepted = accept_plot_proposal(
            self.project,
            pending["power_spec_proposal_id"],
            approval_id=grant["grant"]["approval_id"],
            expected_canon_revision=0,
            workspace_root=self.workspace,
        )
        self.assertEqual("accepted", accepted["status"])
        self.assertEqual(
            "registered",
            accepted["initialization_rebase"]["status"],
        )
        return pending, grant, accepted

    def issue_initialization_grant(self, frozen: dict) -> dict:
        grant = issue_host_approval(
            self.project,
            frozen["proposal_id"],
            expected_canon_revision=1,
            issuer="power-runtime-unittest",
            channel="interactive_test",
            workspace_root=self.workspace,
        )
        self.assertEqual(
            {"accept_initialization", "materialize"},
            set(grant["grant"]["operations"]),
        )
        return grant

    def test_two_stage_apply_receipt_and_idempotent_retry(self) -> None:
        _initializer, frozen = self.freeze()
        pending = prepare_initialization_apply(
            self.project,
            frozen["proposal_id"],
            workspace_root=self.workspace,
        )
        spec_grant = issue_host_approval(
            self.project,
            pending["power_spec_proposal_id"],
            expected_canon_revision=0,
            issuer="power-runtime-unittest",
            channel="interactive_test",
            workspace_root=self.workspace,
        )
        early = apply_initialization_proposal(
            self.project,
            frozen["proposal_id"],
            approval_id=spec_grant["grant"]["approval_id"],
            expected_canon_revision=0,
            idempotency_key="runtime-early-apply",
            workspace_root=self.workspace,
        )
        self.assertEqual(
            "POWER_SPEC_APPROVAL_REQUIRED",
            early["status"],
        )
        self.assertFalse(early["approval_consumed"])
        self.assertIsNone(
            self.grant_consumed_at(spec_grant["grant"]["approval_id"])
        )
        self.assertEqual(
            {"head": 0, "active": 0},
            ContinuityService(self.project).get_canon_revisions(),
        )
        for item in frozen["bundle"]["artifact_manifest"]:
            candidate = self.project / item["path"]
            expected_old_hash = item.get("expected_old_hash")
            if expected_old_hash:
                self.assertTrue(candidate.is_file(), item["path"])
                self.assertEqual(
                    expected_old_hash,
                    hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    item["path"],
                )
            else:
                self.assertFalse(candidate.exists(), item["path"])

        with (
            patch.dict(
                os.environ,
                {"PLOT_RAG_EMBED_API_KEY": "TOKEN_TEST_ONLY"},
                clear=False,
            ),
            patch(
                "v1_runtime.state_rag._embedding_call",
                side_effect=self.fake_embedding,
            ),
        ):
            spec = accept_plot_proposal(
                self.project,
                pending["power_spec_proposal_id"],
                approval_id=spec_grant["grant"]["approval_id"],
                expected_canon_revision=0,
                workspace_root=self.workspace,
            )
            self.assertEqual(1, spec["commit"]["active_canon_revision"])
            self.assertEqual(
                "registered",
                spec["initialization_rebase"]["status"],
            )
            ready = prepare_initialization_apply(
                self.project,
                frozen["proposal_id"],
                workspace_root=self.workspace,
            )
            self.assertEqual("ready", ready["status"])
            self.assertEqual(1, ready["expected_canon_revision"])
            self.assertEqual(
                ready["canon_proposal_id"],
                spec["initialization_rebase"]["canon_proposal_id"],
            )
            init_grant = self.issue_initialization_grant(frozen)
            applied = apply_initialization_proposal(
                self.project,
                frozen["proposal_id"],
                approval_id=init_grant["grant"]["approval_id"],
                expected_canon_revision=1,
                idempotency_key="runtime-complete-apply",
                workspace_root=self.workspace,
            )
            repeated = apply_initialization_proposal(
                self.project,
                frozen["proposal_id"],
                approval_id=init_grant["grant"]["approval_id"],
                expected_canon_revision=1,
                idempotency_key="runtime-complete-apply",
                workspace_root=self.workspace,
            )

        self.assertEqual("completed", applied["status"])
        self.assertTrue(applied["bootstrap_ready"])
        self.assertEqual("verified", applied["verification"]["status"])
        self.assertEqual(
            {"head": 2, "active": 2},
            ContinuityService(self.project).get_canon_revisions(),
        )
        receipt = json.loads(
            (self.project / ".plot-rag" / "completion-receipt.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            "plot-rag-init/completion-v2",
            receipt["schema_version"],
        )
        self.assertEqual(1, receipt["base_canon_revision"])
        self.assertEqual(
            1,
            receipt["initialization_base_canon_revision"],
        )
        self.assertEqual(1, receipt["power_spec"]["active_canon_revision"])
        self.assertEqual(
            2,
            receipt["initialization"]["active_canon_revision"],
        )
        self.assertTrue(receipt["saga"]["two_grants_consumed"])
        self.assertTrue(receipt["saga"]["two_cas_commits_completed"])
        self.assertEqual(
            receipt["projection_hash"],
            receipt["replay_projection_hash"],
        )
        self.assertEqual(
            applied["commit"]["projection_hash"],
            receipt["commit_projection_hash"],
        )
        self.assertEqual(
            applied["commit"]["projection_hash"],
            receipt["initialization"]["projection_hash"],
        )
        self.assertEqual("verified", verify_initialization(
            self.project,
            applied["commit"]["commit_id"],
        )["status"])
        health = doctor_v1(self.project)
        self.assertTrue(health["bootstrap_ready"])
        self.assertEqual(
            "ready",
            health["components"]["bootstrap_readiness"]["status"],
        )
        self.assertEqual(
            applied["commit"]["commit_id"],
            repeated["commit"]["commit_id"],
        )
        self.assertTrue(repeated["commit"]["idempotent_retry"])
        service = ContinuityService(self.project)
        with service.store.read_connection() as connection:
            self.assertEqual(
                2,
                connection.execute(
                    "SELECT COUNT(*) FROM proposals"
                ).fetchone()[0],
            )
            self.assertEqual(
                2,
                connection.execute(
                    "SELECT COUNT(*) FROM canon_commits"
                ).fetchone()[0],
            )

    def test_completed_retry_reuses_receipt_after_unrelated_canon_commit(
        self,
    ) -> None:
        _initializer, frozen = self.freeze(
            key_suffix="receipt-after-later-canon"
        )
        with (
            patch.dict(
                os.environ,
                {"PLOT_RAG_EMBED_API_KEY": "TOKEN_TEST_ONLY"},
                clear=False,
            ),
            patch(
                "v1_runtime.state_rag._embedding_call",
                side_effect=self.fake_embedding,
            ),
        ):
            self.accept_power_spec(frozen)
            init_grant = self.issue_initialization_grant(frozen)
            applied = apply_initialization_proposal(
                self.project,
                frozen["proposal_id"],
                approval_id=init_grant["grant"]["approval_id"],
                expected_canon_revision=1,
                idempotency_key="runtime-later-canon-apply",
                workspace_root=self.workspace,
            )
            receipt_path = (
                self.project / ".plot-rag" / "completion-receipt.json"
            )
            receipt_before = receipt_path.read_bytes()

            service = ContinuityService(self.project)
            revision = service.get_canon_revisions()["active"]
            unrelated = service.save_proposal(
                events=[
                    {
                        "event_type": "world_rule",
                        "scope": "timeless",
                        "field": "unrelated_post_bootstrap_rule",
                        "value": {
                            "statement": (
                                "后续正典可独立增加世界规则。"
                            )
                        },
                    }
                ],
                artifact_id="unrelated-post-bootstrap-rule",
                artifact_stage="final",
                prepared_canon_revision=revision,
            )
            unrelated_grant = issue_host_approval(
                self.project,
                unrelated["proposal_id"],
                expected_canon_revision=revision,
                issuer="power-runtime-unittest",
                channel="interactive_test",
            )
            unrelated_commit = accept_plot_proposal(
                self.project,
                unrelated["proposal_id"],
                approval_id=unrelated_grant["grant"]["approval_id"],
                expected_canon_revision=revision,
            )
            with service.store.read_connection() as connection:
                counts_before_retry = (
                    connection.execute(
                        "SELECT COUNT(*) FROM proposals"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT COUNT(*) FROM canon_commits"
                    ).fetchone()[0],
                )

            repeated = apply_initialization_proposal(
                self.project,
                frozen["proposal_id"],
                approval_id=init_grant["grant"]["approval_id"],
                expected_canon_revision=1,
                idempotency_key="runtime-later-canon-apply",
                workspace_root=self.workspace,
            )
            with service.store.read_connection() as connection:
                counts_after_retry = (
                    connection.execute(
                        "SELECT COUNT(*) FROM proposals"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT COUNT(*) FROM canon_commits"
                    ).fetchone()[0],
                )

        self.assertEqual("completed", applied["status"])
        self.assertEqual("accepted", unrelated_commit["status"])
        self.assertEqual(3, unrelated_commit["commit"]["active_canon_revision"])
        self.assertEqual("completed", repeated["status"])
        self.assertTrue(repeated["commit"]["idempotent_retry"])
        self.assertEqual(
            applied["commit"]["commit_id"],
            repeated["commit"]["commit_id"],
        )
        self.assertEqual(counts_before_retry, counts_after_retry)
        self.assertEqual((3, 3), counts_after_retry)
        self.assertEqual(receipt_before, receipt_path.read_bytes())
        self.assertEqual(
            {"head": 3, "active": 3},
            ContinuityService(self.project).get_canon_revisions(),
        )

    def test_projection_degradation_recovers_with_same_grant_and_key(
        self,
    ) -> None:
        initializer, frozen = self.freeze()
        with (
            patch.dict(
                os.environ,
                {"PLOT_RAG_EMBED_API_KEY": "TOKEN_TEST_ONLY"},
                clear=False,
            ),
            patch(
                "v1_runtime.state_rag._embedding_call",
                side_effect=self.fake_embedding,
            ),
        ):
            self.accept_power_spec(frozen)
        init_grant = self.issue_initialization_grant(frozen)
        with patch.dict(
            os.environ,
            {"PLOT_RAG_EMBED_API_KEY": ""},
            clear=False,
        ):
            degraded = apply_initialization_proposal(
                self.project,
                frozen["proposal_id"],
                approval_id=init_grant["grant"]["approval_id"],
                expected_canon_revision=1,
                idempotency_key="runtime-recovery-apply",
                workspace_root=self.workspace,
            )
        self.assertEqual("degraded", degraded["status"])
        self.assertEqual("degraded", degraded["projections"]["status"])
        self.assertFalse(degraded["bootstrap_ready"])
        self.assertFalse(
            (self.project / ".plot-rag" / "completion-receipt.json").exists()
        )
        self.assertIsNotNone(
            self.grant_consumed_at(init_grant["grant"]["approval_id"])
        )
        self.assertEqual(
            {"head": 2, "active": 2},
            ContinuityService(self.project).get_canon_revisions(),
        )
        self.assertIsNotNone(
            initializer.find_active_session(project_root=self.project)
        )

        with (
            patch.dict(
                os.environ,
                {"PLOT_RAG_EMBED_API_KEY": "TOKEN_TEST_ONLY"},
                clear=False,
            ),
            patch(
                "v1_runtime.state_rag._embedding_call",
                side_effect=self.fake_embedding,
            ),
        ):
            recovered = apply_initialization_proposal(
                self.project,
                frozen["proposal_id"],
                approval_id=init_grant["grant"]["approval_id"],
                expected_canon_revision=1,
                idempotency_key="runtime-recovery-apply",
                workspace_root=self.workspace,
            )
        self.assertEqual("completed", recovered["status"])
        self.assertTrue(recovered["commit"]["idempotent_retry"])
        self.assertEqual(
            degraded["commit"]["commit_id"],
            recovered["commit"]["commit_id"],
        )
        self.assertEqual(
            {"head": 2, "active": 2},
            ContinuityService(self.project).get_canon_revisions(),
        )
        self.assertTrue(
            (self.project / ".plot-rag" / "completion-receipt.json").is_file()
        )
        self.assertIsNone(
            initializer.find_active_session(project_root=self.project)
        )

    def test_tampered_frozen_bundle_fails_before_grant_consumption(
        self,
    ) -> None:
        initializer, frozen = self.freeze()
        with (
            patch.dict(
                os.environ,
                {"PLOT_RAG_EMBED_API_KEY": "TOKEN_TEST_ONLY"},
                clear=False,
            ),
            patch(
                "v1_runtime.state_rag._embedding_call",
                side_effect=self.fake_embedding,
            ),
        ):
            self.accept_power_spec(frozen)
        init_grant = self.issue_initialization_grant(frozen)
        tampered = initializer.storage.load_proposal(
            frozen["proposal_id"]
        )
        tampered["bundle"]["artifact_manifest"][0][
            "proposed_content"
        ] += "\n篡改内容"
        with closing(
            sqlite3.connect(
                initializer.storage.database_path,
                timeout=30.0,
            )
        ) as connection:
            connection.execute(
                """
                UPDATE initialization_proposals
                SET proposal_json=?
                WHERE proposal_id=?
                """,
                (
                    json.dumps(
                        tampered,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    frozen["proposal_id"],
                ),
            )
            connection.commit()
        with self.assertRaises(PlotInitError) as caught:
            apply_initialization_proposal(
                self.project,
                frozen["proposal_id"],
                approval_id=init_grant["grant"]["approval_id"],
                expected_canon_revision=1,
                idempotency_key="runtime-tampered-apply",
                workspace_root=self.workspace,
            )
        self.assertEqual("PACKAGE_HASH_MISMATCH", caught.exception.code)
        self.assertIsNone(
            self.grant_consumed_at(init_grant["grant"]["approval_id"])
        )
        self.assertEqual(
            {"head": 1, "active": 1},
            ContinuityService(self.project).get_canon_revisions(),
        )

    def test_retracted_power_spec_blocks_old_initialization_grant(
        self,
    ) -> None:
        _initializer, frozen = self.freeze()
        with (
            patch.dict(
                os.environ,
                {"PLOT_RAG_EMBED_API_KEY": "TOKEN_TEST_ONLY"},
                clear=False,
            ),
            patch(
                "v1_runtime.state_rag._embedding_call",
                side_effect=self.fake_embedding,
            ),
        ):
            pending, _spec_grant, accepted = self.accept_power_spec(frozen)
            old_initialization_id = accepted["initialization_rebase"][
                "canon_proposal_id"
            ]
            init_grant = self.issue_initialization_grant(frozen)
            retract_grant = issue_host_approval(
                self.project,
                pending["power_spec_proposal_id"],
                expected_canon_revision=1,
                issuer="power-runtime-unittest",
                channel="interactive_test",
                operations=("retract",),
                workspace_root=self.workspace,
            )
            retract_plot_proposal(
                self.project,
                pending["power_spec_proposal_id"],
                approval_id=retract_grant["grant"]["approval_id"],
                expected_canon_revision=1,
                reason="replace the bootstrap specification",
            )
            with self.assertRaises(ContinuityError) as caught:
                apply_initialization_proposal(
                    self.project,
                    frozen["proposal_id"],
                    approval_id=init_grant["grant"]["approval_id"],
                    expected_canon_revision=1,
                    idempotency_key="runtime-retracted-spec-apply",
                    workspace_root=self.workspace,
                )
        self.assertEqual(
            "INITIALIZATION_POWER_SPEC_INVALIDATED",
            caught.exception.code,
        )
        self.assertEqual(
            pending["power_spec_proposal_id"],
            caught.exception.details["power_spec_proposal_id"],
        )
        self.assertEqual(
            "retracted",
            caught.exception.details["power_spec_canon_status"],
        )
        self.assertEqual(
            2,
            caught.exception.details["current_canon_revision"],
        )
        self.assertEqual(
            "freeze_new_initialization_proposal",
            caught.exception.details["required_action"],
        )
        self.assertEqual(
            "proposed",
            ContinuityService(self.project).inspect_proposal(
                old_initialization_id
            )["canon_status"],
        )
        self.assertIsNone(
            self.grant_consumed_at(init_grant["grant"]["approval_id"])
        )
        self.assertEqual(
            {"head": 2, "active": 2},
            ContinuityService(self.project).get_canon_revisions(),
        )

    def test_rejected_power_spec_allows_same_content_new_session(
        self,
    ) -> None:
        _initializer, first = self.freeze(key_suffix="rejected-first")
        first_pending = prepare_initialization_apply(
            self.project,
            first["proposal_id"],
            workspace_root=self.workspace,
        )
        old_grant = issue_host_approval(
            self.project,
            first_pending["power_spec_proposal_id"],
            expected_canon_revision=0,
            issuer="power-runtime-unittest",
            channel="interactive_test",
            workspace_root=self.workspace,
        )
        reject_plot_proposal(
            self.project,
            first_pending["power_spec_proposal_id"],
            reason="replace rejected bootstrap specification",
            idempotency_key="runtime-reject-first-power-spec",
        )
        with self.assertRaises(ContinuityError) as invalidated:
            prepare_initialization_apply(
                self.project,
                first["proposal_id"],
                workspace_root=self.workspace,
            )
        self.assertEqual(
            "INITIALIZATION_POWER_SPEC_INVALIDATED",
            invalidated.exception.code,
        )

        _second_initializer, second = self.freeze(
            key_suffix="rejected-second"
        )
        self.assertEqual(first["package_hash"], second["package_hash"])
        self.assertNotEqual(first["proposal_id"], second["proposal_id"])
        self.assertNotEqual(
            first["session_ref"]["session_id"],
            second["session_ref"]["session_id"],
        )
        second_pending = prepare_initialization_apply(
            self.project,
            second["proposal_id"],
            workspace_root=self.workspace,
        )
        self.assertEqual(
            "POWER_SPEC_APPROVAL_REQUIRED",
            second_pending["status"],
        )
        self.assertEqual(0, second_pending["expected_canon_revision"])
        self.assertNotEqual(
            first_pending["power_spec_proposal_id"],
            second_pending["power_spec_proposal_id"],
        )
        self.assertEqual(
            "proposed",
            second_pending["proposal"]["canon_status"],
        )

        with self.assertRaises(ContinuityError) as old_accept:
            accept_plot_proposal(
                self.project,
                first_pending["power_spec_proposal_id"],
                approval_id=old_grant["grant"]["approval_id"],
                expected_canon_revision=0,
                workspace_root=self.workspace,
            )
        self.assertEqual(
            "INVALID_PROPOSAL_TRANSITION",
            old_accept.exception.code,
        )
        self.assertIsNone(
            self.grant_consumed_at(old_grant["grant"]["approval_id"])
        )

        new_grant = issue_host_approval(
            self.project,
            second_pending["power_spec_proposal_id"],
            expected_canon_revision=0,
            issuer="power-runtime-unittest",
            channel="interactive_test",
            workspace_root=self.workspace,
        )
        with (
            patch.dict(
                os.environ,
                {"PLOT_RAG_EMBED_API_KEY": "TOKEN_TEST_ONLY"},
                clear=False,
            ),
            patch(
                "v1_runtime.state_rag._embedding_call",
                side_effect=self.fake_embedding,
            ),
        ):
            accepted = accept_plot_proposal(
                self.project,
                second_pending["power_spec_proposal_id"],
                approval_id=new_grant["grant"]["approval_id"],
                expected_canon_revision=0,
                workspace_root=self.workspace,
            )
        self.assertEqual("accepted", accepted["status"])
        self.assertEqual(
            "registered",
            accepted["initialization_rebase"]["status"],
        )
        self.assertNotEqual(
            first["proposal_id"],
            accepted["initialization_rebase"]["init_proposal_id"],
        )

    def test_same_content_new_saga_reuses_active_power_spec(
        self,
    ) -> None:
        _initializer, first = self.freeze(key_suffix="active-first")
        with (
            patch.dict(
                os.environ,
                {"PLOT_RAG_EMBED_API_KEY": "TOKEN_TEST_ONLY"},
                clear=False,
            ),
            patch(
                "v1_runtime.state_rag._embedding_call",
                side_effect=self.fake_embedding,
            ),
        ):
            first_pending, _first_grant, first_accepted = (
                self.accept_power_spec(first)
            )
            second_initializer, second = self.freeze(
                key_suffix="active-second",
                expected_canon_revision=1,
            )
            self.assertEqual(
                first["bundle"]["power_model"]["power_package_hash"],
                second["bundle"]["power_model"]["power_package_hash"],
            )
            self.assertNotEqual(
                first["proposal_id"],
                second["proposal_id"],
            )

            ready = prepare_initialization_apply(
                self.project,
                second["proposal_id"],
                workspace_root=self.workspace,
            )
            self.assertEqual("ready", ready["status"])
            self.assertEqual(1, ready["expected_canon_revision"])
            self.assertEqual(
                first_pending["power_spec_proposal_id"],
                ready["proposal"]["payload"]["power_spec_binding"][
                    "proposal_id"
                ],
            )
            self.assertEqual(
                first_accepted["commit"]["commit_id"],
                ready["proposal"]["payload"]["power_spec_binding"][
                    "commit_id"
                ],
            )
            self.assertEqual(
                1,
                len(
                    [
                        proposal
                        for proposal in ContinuityService(
                            self.project
                        ).list_proposals()
                        if proposal["proposal_kind"]
                        == "power_spec_change"
                    ]
                ),
            )

            init_grant = self.issue_initialization_grant(second)
            applied = apply_initialization_proposal(
                self.project,
                second["proposal_id"],
                approval_id=init_grant["grant"]["approval_id"],
                expected_canon_revision=1,
                idempotency_key="runtime-reused-spec-apply",
                workspace_root=self.workspace,
            )

        self.assertEqual("completed", applied["status"])
        self.assertEqual(2, applied["commit"]["active_canon_revision"])
        self.assertEqual(
            "COMPLETED",
            second_initializer.inspect(
                second["session_ref"]["session_id"],
                view="summary",
            )["status"],
        )
        self.assertEqual(
            1,
            len(
                [
                    proposal
                    for proposal in ContinuityService(
                        self.project
                    ).list_proposals()
                    if proposal["proposal_kind"] == "power_spec_change"
                ]
            ),
        )

    def test_reused_power_spec_rebases_after_unrelated_canon_commit(
        self,
    ) -> None:
        _first_initializer, first = self.freeze(
            key_suffix="active-gap-first"
        )
        with (
            patch.dict(
                os.environ,
                {"PLOT_RAG_EMBED_API_KEY": "TOKEN_TEST_ONLY"},
                clear=False,
            ),
            patch(
                "v1_runtime.state_rag._embedding_call",
                side_effect=self.fake_embedding,
            ),
        ):
            first_pending, _first_grant, first_accepted = (
                self.accept_power_spec(first)
            )
            service = ContinuityService(self.project)
            unrelated = service.save_proposal(
                events=[
                    {
                        "event_type": "world_rule",
                        "scope": "timeless",
                        "field": "unrelated_between_spec_and_initialization",
                        "value": {
                            "statement": (
                                "独立正典提交可位于力量定义与初始化之间。"
                            )
                        },
                    }
                ],
                artifact_id="unrelated-between-spec-and-init",
                artifact_stage="final",
                prepared_canon_revision=1,
            )
            unrelated_grant = issue_host_approval(
                self.project,
                unrelated["proposal_id"],
                expected_canon_revision=1,
                issuer="power-runtime-unittest",
                channel="interactive_test",
            )
            unrelated_commit = accept_plot_proposal(
                self.project,
                unrelated["proposal_id"],
                approval_id=unrelated_grant["grant"]["approval_id"],
                expected_canon_revision=1,
            )
            self.assertEqual(
                2,
                unrelated_commit["commit"]["active_canon_revision"],
            )

            second_initializer, second = self.freeze(
                key_suffix="active-gap-second",
                expected_canon_revision=2,
            )
            ready = prepare_initialization_apply(
                self.project,
                second["proposal_id"],
                workspace_root=self.workspace,
            )
            self.assertEqual("ready", ready["status"])
            self.assertEqual(2, ready["expected_canon_revision"])
            self.assertEqual(
                2,
                ready["proposal"]["prepared_canon_revision"],
            )
            binding = ready["proposal"]["payload"]["power_spec_binding"]
            accepted_power_package = first_pending["proposal"]["payload"][
                "lifecycle_package"
            ]
            requested_power_package = ready["proposal"]["payload"][
                "lifecycle_package"
            ]["power_spec_package"]
            self.assertNotEqual(
                accepted_power_package["package_hash"],
                requested_power_package["package_hash"],
            )
            self.assertEqual(
                accepted_power_package["power_package_hash"],
                requested_power_package["power_package_hash"],
            )
            self.assertEqual(
                first_pending["power_spec_proposal_id"],
                binding["proposal_id"],
            )
            self.assertEqual(
                first_accepted["commit"]["commit_id"],
                binding["commit_id"],
            )
            self.assertEqual(
                accepted_power_package["package_hash"],
                binding["package_hash"],
            )
            self.assertEqual(
                requested_power_package["package_hash"],
                binding["requested_package_hash"],
            )
            self.assertEqual(
                requested_power_package["power_package_hash"],
                binding["power_package_hash"],
            )
            self.assertEqual(1, binding["active_canon_revision"])
            self.assertTrue(binding["power_spec_reused"])
            self.assertFalse(
                binding["power_spec_grant_consumed_in_this_saga"]
            )

            repeated_ready = prepare_initialization_apply(
                self.project,
                second["proposal_id"],
                workspace_root=self.workspace,
            )
            self.assertEqual(
                ready["canon_proposal_id"],
                repeated_ready["canon_proposal_id"],
            )
            self.assertEqual(
                ready["proposal"]["prepared_canon_revision"],
                repeated_ready["proposal"]["prepared_canon_revision"],
            )
            self.assertEqual(
                1,
                len(
                    [
                        proposal
                        for proposal in service.list_proposals()
                        if proposal["proposal_kind"]
                        == "power_spec_change"
                    ]
                ),
            )

            init_grant = issue_host_approval(
                self.project,
                second["proposal_id"],
                expected_canon_revision=2,
                issuer="power-runtime-unittest",
                channel="interactive_test",
                workspace_root=self.workspace,
            )
            applied = apply_initialization_proposal(
                self.project,
                second["proposal_id"],
                approval_id=init_grant["grant"]["approval_id"],
                expected_canon_revision=2,
                idempotency_key="runtime-reused-spec-gap-apply",
                workspace_root=self.workspace,
            )

        self.assertEqual("completed", applied["status"])
        self.assertEqual(3, applied["commit"]["active_canon_revision"])
        receipt = json.loads(
            (self.project / ".plot-rag" / "completion-receipt.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(1, receipt["power_spec_canon_revision"])
        self.assertEqual(
            2,
            receipt["initialization_base_canon_revision"],
        )
        self.assertEqual(
            2,
            receipt["initialization_prepared_canon_revision"],
        )
        self.assertEqual(3, receipt["initialization_canon_revision"])
        self.assertEqual(
            accepted_power_package["package_hash"],
            receipt["power_spec"]["package_hash"],
        )
        self.assertEqual(
            requested_power_package["package_hash"],
            receipt["power_spec"]["requested_package_hash"],
        )
        self.assertTrue(receipt["saga"]["power_spec_reused"])
        self.assertFalse(
            receipt["saga"][
                "power_spec_grant_consumed_in_this_saga"
            ]
        )
        self.assertTrue(
            receipt["saga"]["initialization_grant_consumed"]
        )
        self.assertEqual(
            "cumulative_acceptance_chain",
            receipt["saga"]["two_grants_consumed_scope"],
        )
        verification = verify_initialization(
            self.project,
            applied["commit"]["commit_id"],
        )
        self.assertEqual("verified", verification["status"])
        self.assertTrue(
            verification["validations"]["power_spec_commit_active"]
        )
        self.assertTrue(
            verification["validations"][
                "initialization_prepared_after_power_spec"
            ]
        )
        self.assertTrue(
            verification["validations"][
                "initialization_commit_follows_prepared_revision"
            ]
        )
        self.assertEqual(
            "COMPLETED",
            second_initializer.inspect(
                second["session_ref"]["session_id"],
                view="summary",
            )["status"],
        )
        health = doctor_v1(self.project)
        self.assertTrue(health["bootstrap_ready"])
        self.assertEqual(
            "ready",
            health["components"]["bootstrap_readiness"]["status"],
        )

    def test_legacy_completion_v1_commit_projection_receipt_is_supported(
        self,
    ) -> None:
        _initializer, frozen = self.freeze(
            bundle_schema_version="plot-rag-init/v1"
        )
        ready = prepare_initialization_apply(
            self.project,
            frozen["proposal_id"],
            workspace_root=self.workspace,
        )
        self.assertEqual("ready", ready["status"])
        grant = issue_host_approval(
            self.project,
            frozen["proposal_id"],
            expected_canon_revision=0,
            issuer="power-runtime-unittest",
            channel="interactive_test",
            workspace_root=self.workspace,
        )
        with (
            patch.dict(
                os.environ,
                {"PLOT_RAG_EMBED_API_KEY": "TOKEN_TEST_ONLY"},
                clear=False,
            ),
            patch(
                "v1_runtime.state_rag._embedding_call",
                side_effect=self.fake_embedding,
            ),
        ):
            applied = apply_initialization_proposal(
                self.project,
                frozen["proposal_id"],
                approval_id=grant["grant"]["approval_id"],
                expected_canon_revision=0,
                idempotency_key="runtime-v1-legacy-apply",
                workspace_root=self.workspace,
            )
        self.assertEqual("completed", applied["status"])
        receipt_path = (
            self.project / ".plot-rag" / "completion-receipt.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertNotEqual(
            receipt["commit_projection_hash"],
            receipt["replay_projection_hash"],
        )
        legacy = dict(receipt)
        legacy["projection_hash"] = receipt["commit_projection_hash"]
        legacy.pop("commit_projection_hash")
        legacy.pop("replay_projection_hash")
        receipt_path.write_text(
            json.dumps(
                legacy,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        canonical = json.dumps(
            legacy,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        legacy_hash = hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()
        service = ContinuityService(self.project)
        with service.store.transaction() as connection:
            connection.execute(
                """
                UPDATE state_meta
                SET value=?
                WHERE key='bootstrap_ready_receipt_sha256'
                """,
                (legacy_hash,),
            )

        with (
            patch.dict(
                os.environ,
                {"PLOT_RAG_EMBED_API_KEY": "TOKEN_TEST_ONLY"},
                clear=False,
            ),
            patch(
                "v1_runtime.state_rag._embedding_call",
                side_effect=self.fake_embedding,
            ),
        ):
            repeated = apply_initialization_proposal(
                self.project,
                frozen["proposal_id"],
                approval_id=grant["grant"]["approval_id"],
                expected_canon_revision=0,
                idempotency_key="runtime-v1-legacy-apply",
                workspace_root=self.workspace,
            )
        self.assertEqual("completed", repeated["status"])
        self.assertTrue(repeated["commit"]["idempotent_retry"])
        self.assertEqual(
            applied["commit"]["commit_id"],
            repeated["commit"]["commit_id"],
        )
        self.assertEqual(
            legacy,
            json.loads(receipt_path.read_text(encoding="utf-8")),
        )
        verified = verify_initialization(
            self.project,
            applied["commit"]["commit_id"],
        )
        self.assertEqual("verified", verified["status"])
        self.assertTrue(verified["bootstrap_ready"])
        health = doctor_v1(self.project)
        self.assertTrue(health["bootstrap_ready"])
        self.assertEqual(
            "ready",
            health["components"]["bootstrap_readiness"]["status"],
        )


if __name__ == "__main__":
    unittest.main()
