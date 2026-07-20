from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts.continuity.service import ContinuityService
from scripts.continuity.store import ContinuityStore
from scripts.extraction_jobs import (
    ExtractionJobConflict,
    ExtractionJobError,
    ExtractionJobQueue,
    ExtractionLeaseLost,
    ExtractionWorkResult,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ExtractionJobQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.continuity = ContinuityService(self.root)
        replay = self.continuity.replay()
        self.active_revision = int(replay["active_canon_revision"])
        self.projection_hash = str(replay["projection_hash"])
        self.queue = ExtractionJobQueue(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def spec(self, **overrides):
        base = {
            "receipt_id": "receipt-1",
            "request_id": "request-1",
            "assistant_text": "最终生成内容",
            "prompt_hash": digest("prompt"),
            "retrieved_context_digest": digest("context"),
            "prepared_canon_revision": self.active_revision,
            "active_projection_hash": self.projection_hash,
            "intent_contract_hash": digest("intent"),
            "event_seed_manifest_hash": digest("seed-manifest"),
            "event_experience_control_revision": 4,
            "event_seed_references": [
                {
                    "event_seed_id": "seed-1",
                    "event_seed_revision": 1,
                },
                {
                    "event_seed_id": "seed-2",
                    "event_seed_revision": 1,
                },
            ],
            "experience_contract_hashes": [
                digest("experience-a"),
                digest("experience-b"),
            ],
            "artifact_context": {
                "artifact_id": "chapter-8",
                "artifact_stage": "draft",
            },
            "branch_id": "main",
            "sequence_no": 8,
            "extract_provider": "siliconflow",
            "extract_base_url": "https://api.siliconflow.cn/v1",
            "extract_model": "deepseek-ai/DeepSeek-V3",
            "extract_schema_hash": digest("schema"),
            "extract_prompt_template_hash": digest("template"),
            "min_confidence": 0.82,
            "generation_params": {"temperature": 0, "max_tokens": 1024},
        }
        base.update(overrides)
        return base

    def enqueue(self, **overrides):
        spec = self.spec(**overrides)
        self.seed_turn(
            receipt_id=spec["receipt_id"],
            request_id=spec["request_id"],
            prompt_hash=spec["prompt_hash"],
        )
        return self.queue.enqueue(**spec)

    def seed_turn(
        self,
        *,
        receipt_id: str = "receipt-1",
        request_id: str = "request-1",
        prompt_hash: str | None = None,
        assistant_hash: str = "",
        status: str = "pending",
    ) -> None:
        with self.queue.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO turns(
                    receipt_id, request_id, prompt, prompt_hash,
                    assistant_hash, status, started_at
                ) VALUES(?, ?, '', ?, ?, ?, ?)
                ON CONFLICT(receipt_id) DO NOTHING
                """,
                (
                    receipt_id,
                    request_id,
                    prompt_hash or digest("prompt"),
                    assistant_hash,
                    status,
                    "2026-07-17T00:00:00.000000Z",
                ),
            )

    def claim(
        self,
        *,
        now: str = "2026-07-17T00:00:00Z",
        worker_id: str = "worker-a",
        lease_seconds: int = 30,
    ):
        claimed = self.queue.claim(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            now=now,
        )
        self.assertIsNotNone(claimed)
        return claimed

    def seed_proposal(
        self,
        proposal_id: str,
        *,
        job: dict | None = None,
        status: str = "proposed",
        validation_status: str = "valid",
        status_reason: str = "",
        branch_id: str | None = None,
        prepared_canon_revision: int | None = None,
        payload_overrides: dict | None = None,
        artifact_overrides: dict | None = None,
    ) -> None:
        now = "2026-07-17T00:00:00.000000Z"
        artifact_version_id = f"artifact-version-{proposal_id}"
        if job is None:
            job = self.queue.list_jobs(limit=1)[0]
        artifact = dict(job["artifact_context"])
        artifact.update(artifact_overrides or {})
        if branch_id is not None:
            artifact["branch_id"] = branch_id
        artifact_id = str(artifact["artifact_id"])
        artifact_stage = str(artifact["artifact_stage"])
        artifact_branch = str(artifact["branch_id"])
        chapter_no = artifact.get("chapter_no")
        scene_index = artifact.get("scene_index")
        artifact_revision = int(artifact["artifact_revision"])
        payload = self.queue.proposal_binding(job)
        payload.update(payload_overrides or {})
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        prepared_revision = (
            int(prepared_canon_revision)
            if prepared_canon_revision is not None
            else int(job["prepared_canon_revision"])
        )
        with self.queue.store.transaction() as connection:
            existing_artifact = connection.execute(
                """
                SELECT artifact_version_id
                FROM artifacts
                WHERE artifact_id=? AND branch_id=? AND artifact_revision=?
                """,
                (artifact_id, artifact_branch, artifact_revision),
            ).fetchone()
            if existing_artifact is not None:
                artifact_version_id = str(
                    existing_artifact["artifact_version_id"]
                )
            else:
                connection.execute(
                    """
                    INSERT INTO artifacts(
                        artifact_version_id, artifact_id, artifact_kind,
                        artifact_stage, canon_status, branch_id,
                        chapter_no, scene_index, artifact_revision,
                        source_role, content_hash, content_json, active,
                        created_at, updated_at
                    ) VALUES(?, ?, 'chapter', ?, ?, ?,
                             ?, ?, ?, 'draft', ?, '{}', 0, ?, ?)
                    """,
                    (
                        artifact_version_id,
                        artifact_id,
                        artifact_stage,
                        status,
                        artifact_branch,
                        chapter_no,
                        scene_index,
                        artifact_revision,
                        digest("artifact"),
                        now,
                        now,
                    ),
                )
            connection.execute(
                """
                INSERT INTO proposals(
                    proposal_id, artifact_version_id, artifact_id,
                    artifact_stage, canon_status, branch_id,
                    chapter_no, scene_index, artifact_revision,
                    prepared_canon_revision, source_role, proposal_kind,
                    payload_hash, payload_json, events_json,
                    validation_status, status_reason, accepted_commit_id,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         'draft', 'story_delta', ?, ?, '[]',
                         ?, ?, NULL, ?, ?)
                """,
                (
                    proposal_id,
                    artifact_version_id,
                    artifact_id,
                    artifact_stage,
                    status,
                    artifact_branch,
                    chapter_no,
                    scene_index,
                    artifact_revision,
                    prepared_revision,
                    digest(payload_json),
                    payload_json,
                    validation_status,
                    status_reason,
                    now,
                    now,
                ),
            )

    def accept_seeded_proposal(self, proposal_id: str) -> str:
        now = "2026-07-17T00:00:20.000000Z"
        commit_id = f"commit-{proposal_id}"
        grant_token_hash = digest(f"grant-{proposal_id}")
        with self.queue.store.transaction() as connection:
            proposal = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            self.assertIsNotNone(proposal)
            connection.execute(
                """
                INSERT INTO approval_grants(
                    token_hash, proposal_id, binding_hash, binding_json,
                    authorized_operations_json, expected_canon_revision,
                    issuer, channel, expires_at, consumed_request_hash,
                    accepted_commit_id, consumed_at, created_at
                ) VALUES(?, ?, ?, '{}', '["accept"]', 0,
                         'test-host', 'test', '2026-07-18T00:00:00Z', ?,
                         ?, ?, ?)
                """,
                (
                    grant_token_hash,
                    proposal_id,
                    digest(f"binding-{proposal_id}"),
                    digest(f"request-{proposal_id}"),
                    commit_id,
                    now,
                    now,
                ),
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
                ) VALUES(?, ?, 'accept', ?, ?, ?, ?, ?, ?, 0, 1, 0, 1, 1,
                         ?, ?, ?, '', '{}', ?)
                """,
                (
                    commit_id,
                    proposal_id,
                    str(proposal["artifact_id"]),
                    str(proposal["artifact_stage"]),
                    str(proposal["branch_id"]),
                    proposal["chapter_no"],
                    proposal["scene_index"],
                    int(proposal["artifact_revision"]),
                    digest(f"accepted-{proposal_id}"),
                    grant_token_hash,
                    str(proposal["payload_hash"]),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE proposals
                SET canon_status='accepted', validation_status='valid',
                    accepted_commit_id=?, updated_at=?
                WHERE proposal_id=?
                """,
                (commit_id, now, proposal_id),
            )
        return commit_id

    def test_enqueue_is_durable_idempotent_and_hash_bound(self) -> None:
        first = self.enqueue()
        second = self.enqueue(job_id="ignored-on-idempotent-reuse")

        self.assertEqual(first["job_id"], second["job_id"])
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual("queued", first["status"])
        self.assertGreaterEqual(first["enqueue_ms"], 0)
        self.assertEqual(
            digest("最终生成内容"),
            first["assistant_sha256"],
        )
        self.assertEqual(
            digest("https://api.siliconflow.cn/v1"),
            first["extract_endpoint_hash"],
        )
        self.assertRegex(first["job_binding_hash"], r"^[0-9a-f]{64}$")
        reopened = ExtractionJobQueue(self.root).inspect(first["job_id"])
        self.assertEqual(first["job_binding_hash"], reopened["job_binding_hash"])
        self.assertNotIn("assistant_text", reopened)
        self.assertNotIn("assistant_text", self.queue.list_jobs()[0])

        different = self.enqueue(
            assistant_text="修改后的最终内容",
        )
        self.assertNotEqual(first["job_id"], different["job_id"])
        self.assertEqual(2, len(self.queue.list_jobs()))

    def test_same_idempotency_key_with_changed_binding_conflicts(self) -> None:
        first = self.enqueue()
        with self.assertRaises(ExtractionJobConflict) as caught:
            self.queue.enqueue(
                **self.spec(
                    assistant_text=None,
                    assistant_sha256=first["assistant_sha256"],
                    extract_model="Qwen/Qwen3-32B",
                )
            )
        self.assertEqual(
            "EXTRACTION_JOB_IDEMPOTENCY_CONFLICT",
            caught.exception.code,
        )

    def test_expected_derived_binding_hashes_are_verified(self) -> None:
        with self.assertRaises(ExtractionJobConflict) as caught:
            self.queue.enqueue(
                **self.spec(extract_model_hash=digest("wrong"))
            )
        self.assertEqual(
            "EXTRACTION_BINDING_HASH_MISMATCH",
            caught.exception.code,
        )
        self.assertEqual([], self.queue.list_jobs())

    def test_new_hash_only_job_is_rejected_as_unrecoverable(self) -> None:
        self.seed_turn()
        with self.assertRaises(ExtractionJobError) as caught:
            self.queue.enqueue(
                **self.spec(
                    assistant_text=None,
                    assistant_sha256=digest("payload not supplied"),
                )
            )
        self.assertEqual(
            "EXTRACTION_JOB_PAYLOAD_REQUIRED",
            caught.exception.code,
        )

    def test_claimed_worker_can_read_payload_and_success_purges_it(self) -> None:
        queued = self.enqueue()
        self.claim()
        reopened = ExtractionJobQueue(self.root)
        self.assertEqual(
            "最终生成内容",
            reopened.read_assistant_text(
                queued["job_id"],
                worker_id="worker-a",
                expected_attempt_count=1,
                now="2026-07-17T00:00:01Z",
            ),
        )
        with self.assertRaises(ExtractionLeaseLost):
            reopened.read_assistant_text(
                queued["job_id"],
                worker_id="worker-b",
                expected_attempt_count=1,
                now="2026-07-17T00:00:01Z",
            )
        reopened.succeed(
            queued["job_id"],
            worker_id="worker-a",
            expected_attempt_count=1,
            validator_passed=True,
            result_kind="no_delta",
            now="2026-07-17T00:00:02Z",
        )
        with reopened.store.read_connection() as connection:
            payload = connection.execute(
                """
                SELECT 1 FROM extraction_job_payloads WHERE job_id=?
                """,
                (queued["job_id"],),
            ).fetchone()
        self.assertIsNone(payload)

    def test_claim_heartbeat_success_and_clear_barrier(self) -> None:
        queued = self.enqueue()
        self.assertEqual(
            "queued",
            self.queue.barrier_status(
                branch_id="main",
                sequence_no=8,
            )["code"],
        )
        claimed = self.claim()
        self.assertEqual("running", claimed["status"])
        self.assertEqual(1, claimed["attempt_count"])
        self.assertEqual(
            "running",
            self.queue.barrier_status(
                branch_id="main",
                sequence_no=8,
            )["code"],
        )

        heartbeat = self.queue.heartbeat(
            queued["job_id"],
            worker_id="worker-a",
            expected_attempt_count=1,
            lease_seconds=45,
            now="2026-07-17T00:00:10Z",
        )
        self.assertEqual(
            "2026-07-17T00:00:55.000000Z",
            heartbeat["lease_expires_at"],
        )
        succeeded = self.queue.succeed(
            queued["job_id"],
            worker_id="worker-a",
            expected_attempt_count=1,
            validator_passed=True,
            result_kind="no_delta",
            remote_status="no_delta",
            now="2026-07-17T00:00:20Z",
        )
        self.assertEqual("succeeded", succeeded["status"])
        barrier = self.queue.barrier_status(
            branch_id="main",
            sequence_no=8,
        )
        self.assertEqual("clear", barrier["code"])
        self.assertFalse(barrier["blocking"])

    def test_validator_attestation_is_required_before_success(self) -> None:
        queued = self.enqueue()
        self.claim()
        with self.assertRaises(ExtractionJobError) as caught:
            self.queue.succeed(
                queued["job_id"],
                worker_id="worker-a",
                expected_attempt_count=1,
                validator_passed=False,
                now="2026-07-17T00:00:10Z",
            )
        self.assertEqual(
            "EXTRACTION_VALIDATION_REQUIRED",
            caught.exception.code,
        )
        self.assertEqual("running", self.queue.inspect(queued["job_id"])["status"])

    def test_result_kind_is_explicit_and_shape_checked(self) -> None:
        queued = self.enqueue()
        self.claim()
        with self.assertRaises(ExtractionJobError) as missing:
            self.queue.succeed(
                queued["job_id"],
                worker_id="worker-a",
                expected_attempt_count=1,
                validator_passed=True,
                now="2026-07-17T00:00:10Z",
            )
        self.assertEqual(
            "EXTRACTION_RESULT_KIND_REQUIRED",
            missing.exception.code,
        )
        with self.assertRaises(ExtractionJobError) as proposal_without_id:
            self.queue.succeed(
                queued["job_id"],
                worker_id="worker-a",
                expected_attempt_count=1,
                validator_passed=True,
                result_kind="proposal",
                now="2026-07-17T00:00:10Z",
            )
        self.assertEqual(
            "EXTRACTION_RESULT_BINDING_INVALID",
            proposal_without_id.exception.code,
        )
        with self.assertRaises(ExtractionJobError) as no_delta_with_id:
            self.queue.succeed(
                queued["job_id"],
                worker_id="worker-a",
                expected_attempt_count=1,
                validator_passed=True,
                result_kind="no_delta",
                result_proposal_id="proposal-not-allowed",
                now="2026-07-17T00:00:10Z",
            )
        self.assertEqual(
            "EXTRACTION_RESULT_BINDING_INVALID",
            no_delta_with_id.exception.code,
        )
        succeeded = self.queue.succeed(
            queued["job_id"],
            worker_id="worker-a",
            expected_attempt_count=1,
            validator_passed=True,
            result_kind="no_delta",
            now="2026-07-17T00:00:10Z",
        )
        self.assertEqual("no_delta", succeeded["result_kind"])

    def test_receipt_binding_and_sequence_are_required(self) -> None:
        with self.assertRaises(ExtractionJobConflict) as missing:
            self.queue.enqueue(**self.spec())
        self.assertEqual(
            "EXTRACTION_RECEIPT_NOT_FOUND",
            missing.exception.code,
        )

        self.seed_turn()
        with self.assertRaises(ExtractionJobConflict) as mismatch:
            self.queue.enqueue(**self.spec(request_id="other-request"))
        self.assertEqual(
            "EXTRACTION_RECEIPT_BINDING_MISMATCH",
            mismatch.exception.code,
        )
        with self.assertRaises(ExtractionJobError) as sequence:
            self.queue.enqueue(**self.spec(sequence_no=None))
        self.assertEqual(
            "EXTRACTION_SEQUENCE_REQUIRED",
            sequence.exception.code,
        )

    def test_proposed_receipt_rejects_forged_async_shadow_marker(
        self,
    ) -> None:
        authoritative_id = "a" * 64
        prepared_artifact = {
            "artifact_id": "chapter-8",
            "artifact_stage": "draft",
            "branch_id": "main",
            "chapter_no": None,
            "scene_index": None,
            "artifact_revision": 1,
        }
        artifact_context = {
            **prepared_artifact,
            "artifact_revision": 2,
            "_plot_rag_v15": {
                "extraction_execution_mode": "async_shadow",
                "authoritative_proposal_id": authoritative_id,
                "authoritative_artifact_revision": 1,
                "intent_contract_hash": digest("intent"),
                "event_seed_manifest_hash": digest("seed-manifest"),
                "event_experience_control_revision": 4,
                "event_seed_references": [
                    {
                        "event_seed_id": "seed-1",
                        "event_seed_revision": 1,
                    },
                    {
                        "event_seed_id": "seed-2",
                        "event_seed_revision": 1,
                    },
                ],
            },
        }
        spec = self.spec(
            receipt_id="receipt-forged-shadow",
            request_id="request-forged-shadow",
            artifact_context=artifact_context,
        )
        self.seed_turn(
            receipt_id=spec["receipt_id"],
            request_id=spec["request_id"],
            prompt_hash=spec["prompt_hash"],
            assistant_hash=digest(spec["assistant_text"]),
            status="proposed",
        )
        lifecycle = {
            "intent_contract_hash": spec["intent_contract_hash"],
            "event_seed_manifest_hash": spec["event_seed_manifest_hash"],
            "event_experience_control_revision": spec[
                "event_experience_control_revision"
            ],
            "event_seed_references": spec["event_seed_references"],
            "experience_contract_hashes": spec[
                "experience_contract_hashes"
            ],
        }
        with self.queue.store.transaction() as connection:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(turns)")
            }
            additions = {
                "prepared_canon_revision": (
                    "INTEGER NOT NULL DEFAULT 0"
                ),
                "v1_context_json": "TEXT NOT NULL DEFAULT '{}'",
                "active_projection_hash": "TEXT NOT NULL DEFAULT ''",
                "retrieved_context_digest": "TEXT NOT NULL DEFAULT ''",
                "lifecycle_identity_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE turns ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                """
                UPDATE turns
                SET result_json=?, prepared_canon_revision=?,
                    v1_context_json=?, active_projection_hash=?,
                    retrieved_context_digest=?, lifecycle_identity_json=?
                WHERE receipt_id=?
                """,
                (
                    json.dumps({"proposal_id": authoritative_id}),
                    spec["prepared_canon_revision"],
                    json.dumps(prepared_artifact),
                    spec["active_projection_hash"],
                    spec["retrieved_context_digest"],
                    json.dumps(lifecycle),
                    spec["receipt_id"],
                ),
            )
        with self.assertRaises(ExtractionJobConflict) as forged:
            self.queue.enqueue(**spec)
        self.assertEqual(
            "EXTRACTION_RECEIPT_STATUS_CONFLICT",
            forged.exception.code,
        )

    def test_succeed_rejects_proposal_identity_and_validation_mismatches(
        self,
    ) -> None:
        cases = [
            (
                "wrong-branch",
                {"branch_id": "alternate"},
                "EXTRACTION_PROPOSAL_IDENTITY_MISMATCH",
            ),
            (
                "wrong-revision",
                {"prepared_canon_revision": self.active_revision + 1},
                "EXTRACTION_PROPOSAL_IDENTITY_MISMATCH",
            ),
            (
                "wrong-artifact-id",
                {
                    "artifact_overrides": {
                        "artifact_id": "chapter-other"
                    }
                },
                "EXTRACTION_PROPOSAL_IDENTITY_MISMATCH",
            ),
            (
                "wrong-artifact-stage",
                {
                    "artifact_overrides": {
                        "artifact_stage": "outline"
                    }
                },
                "EXTRACTION_PROPOSAL_IDENTITY_MISMATCH",
            ),
            (
                "wrong-artifact-revision",
                {
                    "artifact_overrides": {
                        "artifact_revision": 2
                    }
                },
                "EXTRACTION_PROPOSAL_IDENTITY_MISMATCH",
            ),
            (
                "quarantined",
                {"validation_status": "quarantined"},
                "EXTRACTION_PROPOSAL_VALIDATION_FAILED",
            ),
            (
                "wrong-binding",
                {
                    "payload_overrides": {
                        "job_binding_hash": digest("wrong-binding")
                    }
                },
                "EXTRACTION_PROPOSAL_BINDING_MISMATCH",
            ),
        ]
        for index, (label, proposal_options, expected_code) in enumerate(cases):
            with self.subTest(label=label):
                queued = self.enqueue(
                    receipt_id=f"receipt-{label}",
                    request_id=f"request-{label}",
                    assistant_text=f"assistant-{label}",
                    sequence_no=20 + index,
                )
                proposal_id = f"proposal-{label}"
                self.seed_proposal(
                    proposal_id,
                    job=queued,
                    **proposal_options,
                )
                claimed = self.queue.claim(
                    worker_id=f"worker-{label}",
                    branch_id="main",
                    now="2026-07-17T00:00:00Z",
                )
                self.assertEqual(queued["job_id"], claimed["job_id"])
                with self.assertRaises(ExtractionJobConflict) as caught:
                    self.queue.succeed(
                        queued["job_id"],
                        worker_id=f"worker-{label}",
                        expected_attempt_count=1,
                        validator_passed=True,
                        result_kind="proposal",
                        result_proposal_id=proposal_id,
                        now="2026-07-17T00:00:01Z",
                    )
                self.assertEqual(expected_code, caught.exception.code)
                self.assertEqual(
                    "running",
                    self.queue.inspect(queued["job_id"])["status"],
                )
                self.queue.cancel(
                    queued["job_id"],
                    expected_attempt_count=1,
                    reason="test cleanup",
                    now="2026-07-17T00:00:02Z",
                )

    def test_persisted_binding_corruption_does_not_transition_on_claim(
        self,
    ) -> None:
        queued = self.enqueue()
        with self.queue.store.transaction() as connection:
            connection.execute(
                """
                UPDATE extraction_jobs
                SET prompt_hash=?
                WHERE job_id=?
                """,
                (digest("tampered"), queued["job_id"]),
            )
        with self.assertRaises(ExtractionJobError) as caught:
            self.queue.claim(
                worker_id="worker-corrupt",
                now="2026-07-17T00:00:00Z",
            )
        self.assertEqual(
            "EXTRACTION_JOB_BINDING_CORRUPT",
            caught.exception.code,
        )
        with self.queue.store.read_connection() as connection:
            status = connection.execute(
                """
                SELECT job_status
                FROM extraction_jobs
                WHERE job_id=?
                """,
                (queued["job_id"],),
            ).fetchone()
        self.assertEqual("queued", str(status["job_status"]))

    def test_prepared_identity_stale_skips_remote_callback(self) -> None:
        queued = self.enqueue()
        with self.queue.store.transaction() as connection:
            self.queue.store.set_meta_int(
                connection,
                "active_canon_revision",
                self.active_revision + 1,
            )
        calls = []
        outcome = self.queue.run_once(
            worker_id="worker-stale",
            proposal_factory=lambda _job, _text: calls.append(True),
            now="2026-07-17T00:00:00Z",
        )
        self.assertEqual([], calls)
        self.assertEqual("failed", outcome["status"])
        self.assertEqual("failed", outcome["job"]["status"])
        self.assertIn("EXTRACTION_PREPARED_IDENTITY_STALE", outcome["error"])

    def test_run_once_is_proposal_only_and_restart_durable(self) -> None:
        queued = self.enqueue()
        observed = {}

        def proposal_factory(job, assistant_text):
            observed["job_id"] = job["job_id"]
            observed["assistant_text"] = assistant_text
            self.seed_proposal(
                "proposal-from-callback",
                job=job,
            )
            return ExtractionWorkResult(
                validator_passed=True,
                result_proposal_id="proposal-from-callback",
                result_kind="proposal",
                remote_status="validated",
            )

        restarted = ExtractionJobQueue(self.root)
        outcome = restarted.run_once(
            worker_id="restart-worker",
            proposal_factory=proposal_factory,
            lease_seconds=30,
            now="2026-07-17T00:00:00Z",
        )
        self.assertEqual("succeeded", outcome["status"])
        self.assertEqual(queued["job_id"], observed["job_id"])
        self.assertEqual("最终生成内容", observed["assistant_text"])
        self.assertEqual(
            "proposal-from-callback",
            outcome["job"]["result_proposal_id"],
        )
        self.assertEqual(
            "pending_review",
            restarted.barrier_status(
                branch_id="main",
                sequence_no=8,
            )["code"],
        )
        self.assertEqual(
            "idle",
            restarted.run_once(
                worker_id="restart-worker",
                proposal_factory=proposal_factory,
                now="2026-07-17T00:00:01Z",
            )["status"],
        )

    def test_run_once_failure_keeps_payload_for_explicit_retry(self) -> None:
        queued = self.enqueue()

        def broken_factory(_job, _assistant_text):
            raise RuntimeError("remote sk-supersecrettoken failed")

        outcome = self.queue.run_once(
            worker_id="worker-a",
            proposal_factory=broken_factory,
            now="2026-07-17T00:00:00Z",
        )
        self.assertEqual("failed", outcome["status"])
        self.assertNotIn("sk-supersecrettoken", outcome["error"])
        self.queue.retry(
            queued["job_id"],
            expected_attempt_count=1,
            now="2026-07-17T00:00:01Z",
        )
        success = self.queue.run_once(
            worker_id="worker-b",
            proposal_factory=lambda _job, text: {
                "validator_passed": bool(text),
                "result_kind": "no_delta",
                "remote_status": "no_delta",
            },
            now="2026-07-17T00:00:02Z",
        )
        self.assertEqual("succeeded", success["status"])

    def test_run_once_failure_redacts_common_credential_shapes(self) -> None:
        self.enqueue()
        secrets = (
            "sf-abcdefghijklmnop",
            "ak-abcdefghijklmnop",
            "Bearer abcdefghijklmnop",
            "Authorization: Bearer qrstuvwxyz123456",
            "password=fixture-password-123",
            'client_secret: "fixture-client-secret-123"',
        )

        def broken_factory(_job, _assistant_text):
            raise RuntimeError(" | ".join(secrets))

        outcome = self.queue.run_once(
            worker_id="worker-redaction",
            proposal_factory=broken_factory,
            now="2026-07-17T00:00:00Z",
        )
        self.assertEqual("failed", outcome["status"])
        self.assertIn("[REDACTED]", outcome["error"])
        for token in (
            "sf-abcdefghijklmnop",
            "ak-abcdefghijklmnop",
            "abcdefghijklmnop",
            "qrstuvwxyz123456",
            "fixture-password-123",
            "fixture-client-secret-123",
        ):
            self.assertNotIn(token, outcome["error"])

    def test_failure_redaction_covers_bare_environment_secret_and_direct_fail(
        self,
    ) -> None:
        queued = self.enqueue()
        claimed = self.claim()
        environment_secret = "TEST-ENV-SECRET-123456"
        raw_error = (
            "authorization: sf-TESTTOKEN123456 "
            "password=TESTSECRET123456 "
            f"provider returned {environment_secret}"
        )
        with mock.patch.dict(
            "os.environ",
            {"PLOT_RAG_LLM_API_KEY": environment_secret},
            clear=False,
        ):
            failed = self.queue.fail(
                queued["job_id"],
                worker_id="worker-a",
                expected_attempt_count=int(claimed["attempt_count"]),
                error=raw_error,
                remote_status=f"failed:{environment_secret}",
                now="2026-07-17T00:00:10Z",
            )
            inspected = self.queue.inspect(queued["job_id"])
            listed = self.queue.list_jobs(limit=1)[0]

        for payload in (failed, inspected, listed):
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertIn("[REDACTED]", serialized)
            self.assertNotIn("sf-TESTTOKEN123456", serialized)
            self.assertNotIn("TESTSECRET123456", serialized)
            self.assertNotIn(environment_secret, serialized)

    def test_expired_lease_recovery_fences_stale_worker(self) -> None:
        queued = self.enqueue()
        self.claim(lease_seconds=5)
        recovered = self.queue.recover_stale_running(
            now="2026-07-17T00:00:06Z"
        )
        self.assertEqual([queued["job_id"]], [job["job_id"] for job in recovered])
        self.assertEqual("queued", recovered[0]["status"])

        claimed_again = self.claim(
            now="2026-07-17T00:00:07Z",
            worker_id="worker-b",
        )
        self.assertEqual(2, claimed_again["attempt_count"])
        with self.assertRaises(ExtractionLeaseLost):
            self.queue.succeed(
                queued["job_id"],
                worker_id="worker-a",
                expected_attempt_count=1,
                validator_passed=True,
                result_kind="no_delta",
                now="2026-07-17T00:00:08Z",
            )
        final = self.queue.succeed(
            queued["job_id"],
            worker_id="worker-b",
            expected_attempt_count=2,
            validator_passed=True,
            result_kind="no_delta",
            now="2026-07-17T00:00:08Z",
        )
        self.assertEqual("succeeded", final["status"])

    def test_fail_retry_due_time_and_cancel_use_cas(self) -> None:
        queued = self.enqueue()
        self.claim()
        failed = self.queue.fail(
            queued["job_id"],
            worker_id="worker-a",
            expected_attempt_count=1,
            error="remote timeout",
            remote_status="timeout",
            now="2026-07-17T00:00:10Z",
        )
        self.assertEqual("failed", failed["status"])
        self.assertEqual(
            "failed",
            self.queue.barrier_status(
                branch_id="main",
                sequence_no=8,
            )["code"],
        )

        retried = self.queue.retry(
            queued["job_id"],
            expected_attempt_count=1,
            next_attempt_at="2026-07-17T00:05:00Z",
            now="2026-07-17T00:00:11Z",
        )
        self.assertEqual("queued", retried["status"])
        self.assertIsNone(
            self.queue.claim(
                worker_id="too-early",
                now="2026-07-17T00:04:59Z",
            )
        )
        claimed = self.queue.claim(
            worker_id="worker-b",
            now="2026-07-17T00:05:00Z",
        )
        self.assertEqual(2, claimed["attempt_count"])
        cancelled = self.queue.cancel(
            queued["job_id"],
            expected_attempt_count=2,
            reason="operator cancelled",
            now="2026-07-17T00:05:01Z",
        )
        self.assertEqual("cancelled", cancelled["status"])
        self.assertEqual(
            "cancelled",
            self.queue.barrier_status(
                branch_id="main",
                sequence_no=8,
            )["code"],
        )
        with self.assertRaises(ExtractionJobConflict):
            self.queue.cancel(
                queued["job_id"],
                expected_attempt_count=1,
                reason="stale request",
            )

    def test_pending_proposal_blocks_until_explicit_disposition(self) -> None:
        queued = self.enqueue()
        self.seed_proposal("proposal-1", job=queued)
        self.claim()
        self.queue.succeed(
            queued["job_id"],
            worker_id="worker-a",
            expected_attempt_count=1,
            validator_passed=True,
            result_proposal_id="proposal-1",
            result_kind="proposal",
            now="2026-07-17T00:00:10Z",
        )
        pending = self.queue.barrier_status(
            branch_id="main",
            sequence_no=8,
        )
        self.assertEqual("pending_review", pending["code"])
        self.assertTrue(pending["blocking"])
        self.assertEqual("proposal-1", pending["proposal"]["proposal_id"])

        self.accept_seeded_proposal("proposal-1")
        accepted = self.queue.barrier_status(
            branch_id="main",
            sequence_no=8,
        )
        self.assertEqual("accepted", accepted["code"])
        self.assertFalse(accepted["blocking"])

        with self.queue.store.transaction() as connection:
            connection.execute(
                """
                UPDATE proposals
                SET canon_status='rejected', accepted_commit_id=NULL
                WHERE proposal_id='proposal-1'
                """
            )
        rejected = self.queue.barrier_status(
            branch_id="main",
            sequence_no=8,
        )
        self.assertEqual("rejected", rejected["code"])
        self.assertTrue(rejected["blocking"])

        with self.queue.store.transaction() as connection:
            connection.execute(
                """
                UPDATE proposals
                SET canon_status='retracted'
                WHERE proposal_id='proposal-1'
                """
            )
        self.assertEqual(
            "retracted",
            self.queue.barrier_status(
                branch_id="main",
                sequence_no=8,
            )["code"],
        )

    def test_accepted_barrier_validates_commit_and_reverse_identity(self) -> None:
        queued = self.enqueue(
            receipt_id="receipt-accepted-integrity",
            request_id="request-accepted-integrity",
            assistant_text="accepted integrity",
            sequence_no=60,
            artifact_context={
                **self.spec()["artifact_context"],
                "artifact_id": "chapter-accepted-integrity",
            },
        )
        self.seed_proposal("proposal-accepted-integrity", job=queued)
        claimed = self.queue.claim(
            worker_id="worker-accepted-integrity",
            now="2026-07-17T00:00:00Z",
        )
        self.queue.succeed(
            queued["job_id"],
            worker_id="worker-accepted-integrity",
            expected_attempt_count=claimed["attempt_count"],
            validator_passed=True,
            result_kind="proposal",
            result_proposal_id="proposal-accepted-integrity",
            now="2026-07-17T00:00:01Z",
        )
        commit_id = self.accept_seeded_proposal(
            "proposal-accepted-integrity"
        )
        next_job = self.enqueue(
            receipt_id="receipt-after-accepted",
            request_id="request-after-accepted",
            assistant_text="no delta after accepted",
            sequence_no=61,
            artifact_context={
                **self.spec()["artifact_context"],
                "artifact_id": "chapter-after-accepted",
            },
        )
        next_claim = self.queue.claim(
            worker_id="worker-after-accepted",
            now="2026-07-17T00:00:02Z",
        )
        self.queue.succeed(
            next_job["job_id"],
            worker_id="worker-after-accepted",
            expected_attempt_count=next_claim["attempt_count"],
            validator_passed=True,
            result_kind="no_delta",
            now="2026-07-17T00:00:03Z",
        )
        accepted = self.queue.barrier_status(
            branch_id="main",
            sequence_no=61,
            include_prior=True,
        )
        self.assertEqual("accepted", accepted["code"])
        self.assertFalse(accepted["blocking"])

        corruptions = (
            (
                "invalid-proposal",
                """
                UPDATE proposals SET validation_status='invalid'
                WHERE proposal_id='proposal-accepted-integrity'
                """,
                "EXTRACTION_ACCEPTED_PROPOSAL_INVALID",
            ),
            (
                "missing-commit",
                f"""
                UPDATE proposals
                SET validation_status='valid',
                    accepted_commit_id='missing-commit'
                WHERE proposal_id='proposal-accepted-integrity'
                """,
                "EXTRACTION_ACCEPTED_COMMIT_NOT_FOUND",
            ),
            (
                "wrong-operation",
                f"""
                UPDATE proposals SET accepted_commit_id='{commit_id}'
                WHERE proposal_id='proposal-accepted-integrity';
                UPDATE canon_commits SET operation='retract'
                WHERE commit_id='{commit_id}'
                """,
                "EXTRACTION_ACCEPTED_COMMIT_MISMATCH",
            ),
            (
                "commit-artifact",
                f"""
                UPDATE canon_commits
                SET operation='accept', artifact_stage='outline'
                WHERE commit_id='{commit_id}'
                """,
                "EXTRACTION_ACCEPTED_COMMIT_IDENTITY_MISMATCH",
            ),
            (
                "reverse-binding",
                f"""
                UPDATE canon_commits SET artifact_stage='draft'
                WHERE commit_id='{commit_id}';
                UPDATE proposals
                SET payload_json=replace(
                    payload_json,
                    '"job_binding_hash":"',
                    '"job_binding_hash":"tampered-'
                )
                WHERE proposal_id='proposal-accepted-integrity'
                """,
                "EXTRACTION_PROPOSAL_BINDING_MISMATCH",
            ),
        )
        for label, script, expected_code in corruptions:
            with self.subTest(label=label):
                with self.queue.store.transaction() as connection:
                    connection.executescript(script)
                barrier = self.queue.barrier_status(
                    branch_id="main",
                    sequence_no=61,
                    include_prior=True,
                )
                self.assertEqual("failed", barrier["code"])
                self.assertTrue(barrier["blocking"])
                self.assertEqual(
                    expected_code,
                    barrier["proposal"]["barrier_error_code"],
                )

    def test_async_shadow_rejected_proposal_is_nonblocking_only_when_attested(
        self,
    ) -> None:
        authoritative_id = "proposal-authoritative"
        shadow_context = {
            **self.spec()["artifact_context"],
            "artifact_id": "chapter-shadow",
            "_plot_rag_v15": {
                "extraction_execution_mode": "async_shadow",
                "authoritative_proposal_id": authoritative_id,
            },
        }
        queued = self.enqueue(
            receipt_id="receipt-shadow",
            request_id="request-shadow",
            assistant_text="shadow result",
            sequence_no=62,
            artifact_context=shadow_context,
        )
        self.seed_proposal(
            "proposal-shadow",
            job=queued,
            status="rejected",
            status_reason="async_shadow_non_accepting",
            payload_overrides={
                "extraction_shadow": {
                    "mode": "async_shadow",
                    "authoritative_proposal_id": authoritative_id,
                    "acceptable": False,
                    "barrier_blocking": False,
                }
            },
        )
        claimed = self.queue.claim(
            worker_id="worker-shadow",
            now="2026-07-17T00:00:00Z",
        )
        succeeded = self.queue.succeed(
            queued["job_id"],
            worker_id="worker-shadow",
            expected_attempt_count=claimed["attempt_count"],
            validator_passed=True,
            result_kind="proposal",
            result_proposal_id="proposal-shadow",
            now="2026-07-17T00:00:01Z",
        )
        self.assertEqual("succeeded", succeeded["status"])
        self.assertEqual(
            "clear",
            self.queue.barrier_status(
                branch_id="main",
                sequence_no=62,
            )["code"],
        )

        invalid = self.enqueue(
            receipt_id="receipt-shadow-invalid",
            request_id="request-shadow-invalid",
            assistant_text="invalid shadow result",
            sequence_no=63,
            artifact_context={
                **shadow_context,
                "artifact_id": "chapter-shadow-invalid",
            },
        )
        self.seed_proposal(
            "proposal-shadow-invalid",
            job=invalid,
            status="rejected",
            status_reason="async_shadow_non_accepting",
        )
        invalid_claim = self.queue.claim(
            worker_id="worker-shadow-invalid",
            now="2026-07-17T00:00:02Z",
        )
        with self.assertRaises(ExtractionJobConflict) as caught:
            self.queue.succeed(
                invalid["job_id"],
                worker_id="worker-shadow-invalid",
                expected_attempt_count=invalid_claim["attempt_count"],
                validator_passed=True,
                result_kind="proposal",
                result_proposal_id="proposal-shadow-invalid",
                now="2026-07-17T00:00:03Z",
            )
        self.assertEqual(
            "EXTRACTION_PROPOSAL_SHADOW_BINDING_MISMATCH",
            caught.exception.code,
        )

    def test_missing_bound_proposal_is_rejected_without_state_change(self) -> None:
        queued = self.enqueue()
        self.claim()
        with self.assertRaises(ExtractionJobConflict) as caught:
            self.queue.succeed(
                queued["job_id"],
                worker_id="worker-a",
                expected_attempt_count=1,
                validator_passed=True,
                result_proposal_id="not-yet-visible",
                result_kind="proposal",
                now="2026-07-17T00:00:10Z",
            )
        self.assertEqual(
            "EXTRACTION_PROPOSAL_NOT_FOUND",
            caught.exception.code,
        )
        self.assertEqual(
            "running",
            self.queue.inspect(queued["job_id"])["status"],
        )
        with self.queue.store.read_connection() as connection:
            payload = connection.execute(
                """
                SELECT 1
                FROM extraction_job_payloads
                WHERE job_id=?
                """,
                (queued["job_id"],),
            ).fetchone()
        self.assertIsNotNone(payload)

    def test_barrier_is_branch_and_sequence_scoped(self) -> None:
        self.enqueue()
        self.enqueue(
            receipt_id="receipt-other-branch",
            request_id="request-other-branch",
            assistant_text="branch output",
            branch_id="alternate",
            sequence_no=1,
        )
        self.enqueue(
            receipt_id="receipt-sequence-9",
            request_id="request-sequence-9",
            assistant_text="next output",
            sequence_no=9,
        )
        self.assertEqual(
            "clear",
            self.queue.barrier_status(
                branch_id="alternate",
                sequence_no=8,
            )["code"],
        )
        prior = self.queue.barrier_status(
            branch_id="main",
            sequence_no=9,
            include_prior=True,
        )
        self.assertEqual("queued", prior["code"])
        self.assertEqual(3, len(self.queue.list_jobs()))

    def test_heartbeat_is_monotonic_and_never_shortens_a_lease(self) -> None:
        queued = self.enqueue()
        self.claim(lease_seconds=300)
        first = self.queue.heartbeat(
            queued["job_id"],
            worker_id="worker-a",
            expected_attempt_count=1,
            lease_seconds=60,
            now="2026-07-17T00:02:00Z",
        )
        self.assertEqual(
            "2026-07-17T00:05:00.000000Z",
            first["lease_expires_at"],
        )
        late = self.queue.heartbeat(
            queued["job_id"],
            worker_id="worker-a",
            expected_attempt_count=1,
            lease_seconds=60,
            now="2026-07-17T00:01:00Z",
        )
        self.assertEqual(first["lease_expires_at"], late["lease_expires_at"])
        self.assertEqual(first["heartbeat_at"], late["heartbeat_at"])

    def test_automatic_heartbeat_prevents_duplicate_recovery(self) -> None:
        self.enqueue()
        callback_started = threading.Event()
        callback_calls = []
        outcomes = []

        def proposal_factory(_job, _assistant_text):
            callback_calls.append(time.perf_counter())
            callback_started.set()
            time.sleep(1.4)
            return {
                "validator_passed": True,
                "result_kind": "no_delta",
                "remote_status": "no_delta",
            }

        worker = threading.Thread(
            target=lambda: outcomes.append(
                self.queue.run_once(
                    worker_id="heartbeat-worker",
                    proposal_factory=proposal_factory,
                    lease_seconds=1,
                    heartbeat_interval_seconds=0.15,
                )
            )
        )
        worker.start()
        self.assertTrue(callback_started.wait(timeout=2.0))
        time.sleep(1.1)
        self.assertEqual([], self.queue.recover_stale_running())
        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(1, len(callback_calls))
        self.assertEqual("succeeded", outcomes[0]["status"])

    def test_lock_wait_uses_post_lock_time_for_claim_and_completion(self) -> None:
        queued = self.enqueue()
        self.queue.store.ensure_schema()
        blocker = self.queue.store._connect()
        blocker.execute("BEGIN IMMEDIATE")
        claim_result = []
        claim_started = threading.Event()

        def delayed_claim() -> None:
            claim_started.set()
            claim_result.append(
                ExtractionJobQueue(self.root).claim(
                    worker_id="delayed-claim",
                    lease_seconds=1,
                )
            )

        claim_thread = threading.Thread(target=delayed_claim)
        claim_thread.start()
        self.assertTrue(claim_started.wait(timeout=1.0))
        time.sleep(1.2)
        unlock_time = datetime.now(timezone.utc)
        blocker.commit()
        blocker.close()
        claim_thread.join(timeout=5.0)
        self.assertFalse(claim_thread.is_alive())
        claimed = claim_result[0]
        expiry = datetime.fromisoformat(
            str(claimed["lease_expires_at"]).replace("Z", "+00:00")
        )
        self.assertGreater(expiry, unlock_time)

        completion_blocker = self.queue.store._connect()
        completion_blocker.execute("BEGIN IMMEDIATE")
        completion_errors = []
        completion_started = threading.Event()

        def delayed_completion() -> None:
            completion_started.set()
            try:
                ExtractionJobQueue(self.root).succeed(
                    queued["job_id"],
                    worker_id="delayed-claim",
                    expected_attempt_count=1,
                    validator_passed=True,
                    result_kind="no_delta",
                )
            except BaseException as exc:
                completion_errors.append(exc)

        completion_thread = threading.Thread(target=delayed_completion)
        completion_thread.start()
        self.assertTrue(completion_started.wait(timeout=1.0))
        time.sleep(1.2)
        completion_blocker.commit()
        completion_blocker.close()
        completion_thread.join(timeout=5.0)
        self.assertFalse(completion_thread.is_alive())
        self.assertEqual(1, len(completion_errors))
        self.assertIsInstance(completion_errors[0], ExtractionLeaseLost)

    def test_orphan_proposal_is_recoverably_adopted_by_next_epoch(self) -> None:
        queued = self.enqueue()
        self.claim(lease_seconds=1)
        self.seed_proposal("proposal-orphan", job=queued)
        self.queue.recover_stale_running(now="2026-07-17T00:00:02Z")
        calls = []
        adopted = self.queue.run_once(
            worker_id="worker-b",
            proposal_factory=lambda _job, _text: calls.append(True),
            now="2026-07-17T00:00:03Z",
        )
        self.assertEqual([], calls)
        self.assertEqual("succeeded", adopted["status"])
        self.assertTrue(adopted["adopted"])
        self.assertEqual("proposal-orphan", adopted["proposal_id"])
        self.assertEqual(2, adopted["job"]["attempt_count"])
        self.assertEqual(
            "proposal-orphan",
            adopted["job"]["result_proposal_id"],
        )

    def test_orphan_proposal_ambiguity_and_corruption_skip_callback(self) -> None:
        for index, mode in enumerate(("ambiguous", "corrupt"), start=1):
            with self.subTest(mode=mode):
                queued = self.enqueue(
                    receipt_id=f"receipt-orphan-{mode}",
                    request_id=f"request-orphan-{mode}",
                    assistant_text=f"assistant-orphan-{mode}",
                    sequence_no=40 + index,
                    artifact_context={
                        **self.spec()["artifact_context"],
                        "artifact_id": f"chapter-orphan-{mode}",
                    },
                )
                self.queue.claim(
                    worker_id=f"worker-a-{mode}",
                    lease_seconds=1,
                    now="2026-07-17T00:00:00Z",
                )
                if mode == "ambiguous":
                    self.seed_proposal("proposal-orphan-a", job=queued)
                    self.seed_proposal("proposal-orphan-b", job=queued)
                else:
                    self.seed_proposal(
                        "proposal-orphan-corrupt",
                        job=queued,
                        payload_overrides={
                            "job_binding_hash": digest("corrupt")
                        },
                    )
                self.queue.recover_stale_running(
                    now="2026-07-17T00:00:02Z"
                )
                calls = []
                outcome = self.queue.run_once(
                    worker_id=f"worker-b-{mode}",
                    proposal_factory=lambda _job, _text: calls.append(True),
                    now="2026-07-17T00:00:03Z",
                )
                self.assertEqual([], calls)
                self.assertEqual("failed", outcome["status"])
                expected = (
                    "EXTRACTION_ORPHAN_PROPOSAL_AMBIGUOUS"
                    if mode == "ambiguous"
                    else "EXTRACTION_ORPHAN_PROPOSAL_INVALID"
                )
                self.assertIn(expected, outcome["error"])

    def test_cancelled_and_rejected_barriers_require_explicit_resolution(
        self,
    ) -> None:
        queued_cancel = self.enqueue(
            receipt_id="receipt-queued-cancel",
            request_id="request-queued-cancel",
            assistant_text="queued cancel",
            sequence_no=29,
        )
        self.queue.cancel(
            queued_cancel["job_id"],
            expected_attempt_count=0,
            reason="cancel before claim",
            now="2026-07-17T00:00:00Z",
        )
        self.queue.resolve_barrier(
            queued_cancel["job_id"],
            expected_attempt_count=0,
            action="discard",
            reason="discard queued cancellation",
            now="2026-07-17T00:00:01Z",
        )
        self.assertEqual(
            "clear",
            self.queue.barrier_status(
                branch_id="main",
                sequence_no=29,
            )["code"],
        )

        cancelled_job = self.enqueue(
            receipt_id="receipt-cancelled",
            request_id="request-cancelled",
            assistant_text="cancelled",
            sequence_no=30,
        )
        self.queue.claim(
            worker_id="worker-cancelled",
            now="2026-07-17T00:00:00Z",
        )
        self.queue.cancel(
            cancelled_job["job_id"],
            expected_attempt_count=1,
            reason="operator cancelled",
            now="2026-07-17T00:00:01Z",
        )
        self.assertEqual(
            "cancelled",
            self.queue.barrier_status(
                branch_id="main",
                sequence_no=30,
            )["code"],
        )
        resolution = self.queue.resolve_barrier(
            cancelled_job["job_id"],
            expected_attempt_count=1,
            action="discard",
            reason="discard cancelled output",
            now="2026-07-17T00:00:02Z",
        )
        self.assertFalse(resolution["reused"])
        cleared = self.queue.barrier_status(
            branch_id="main",
            sequence_no=30,
        )
        self.assertEqual("clear", cleared["code"])
        self.assertEqual(1, cleared["resolved_job_count"])

        rejected_job = self.enqueue(
            receipt_id="receipt-rejected",
            request_id="request-rejected",
            assistant_text="rejected",
            sequence_no=31,
        )
        self.seed_proposal("proposal-rejected", job=rejected_job)
        self.queue.claim(
            worker_id="worker-rejected",
            now="2026-07-17T00:01:00Z",
        )
        self.queue.succeed(
            rejected_job["job_id"],
            worker_id="worker-rejected",
            expected_attempt_count=1,
            validator_passed=True,
            result_kind="proposal",
            result_proposal_id="proposal-rejected",
            now="2026-07-17T00:01:01Z",
        )
        with self.queue.store.transaction() as connection:
            connection.execute(
                """
                UPDATE proposals
                SET canon_status='rejected'
                WHERE proposal_id='proposal-rejected'
                """
            )
        self.assertEqual(
            "rejected",
            self.queue.barrier_status(
                branch_id="main",
                sequence_no=31,
            )["code"],
        )
        replacement = self.enqueue(
            receipt_id="receipt-replacement",
            request_id="request-replacement",
            assistant_text="replacement",
            sequence_no=31,
        )
        self.queue.resolve_barrier(
            rejected_job["job_id"],
            expected_attempt_count=1,
            action="supersede",
            replacement_job_id=replacement["job_id"],
            reason="rewrite rejected extraction",
            now="2026-07-17T00:01:02Z",
        )
        self.assertEqual(
            "queued",
            self.queue.barrier_status(
                branch_id="main",
                sequence_no=31,
            )["code"],
        )
        self.queue.claim(
            worker_id="worker-replacement",
            now="2026-07-17T00:01:03Z",
        )
        self.queue.succeed(
            replacement["job_id"],
            worker_id="worker-replacement",
            expected_attempt_count=1,
            validator_passed=True,
            result_kind="no_delta",
            now="2026-07-17T00:01:04Z",
        )
        final = self.queue.barrier_status(
            branch_id="main",
            sequence_no=31,
        )
        self.assertEqual("clear", final["code"])
        self.assertEqual(1, final["resolved_job_count"])

    def test_claim_is_single_writer_under_concurrency(self) -> None:
        self.enqueue()
        # Complete schema setup before the two independent connections race.
        self.queue.store.ensure_schema()
        results = []
        lock = threading.Lock()

        def worker(name: str) -> None:
            queue = ExtractionJobQueue(self.root)
            result = queue.claim(
                worker_id=name,
                now="2026-07-17T00:00:00Z",
            )
            with lock:
                results.append(result)

        threads = [
            threading.Thread(target=worker, args=("worker-1",)),
            threading.Thread(target=worker, args=("worker-2",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, sum(result is not None for result in results))
        self.assertEqual(
            "running",
            self.queue.list_jobs(status="running")[0]["status"],
        )

    def test_timestamp_parser_rejects_non_rfc3339_week_dates(self) -> None:
        self.enqueue()
        with self.assertRaises(ExtractionJobError) as caught:
            self.queue.claim(
                worker_id="worker-week-date",
                now="2026-W29-5T12:34:56Z",
            )
        self.assertEqual(
            "EXTRACTION_JOB_INVALID_TIMESTAMP",
            caught.exception.code,
        )
        self.assertEqual("queued", self.queue.list_jobs()[0]["status"])

    def test_direct_import_ignores_unrelated_top_level_scripts_module(
        self,
    ) -> None:
        scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
        code = "\n".join(
            (
                "import sys, types",
                "sys.modules['scripts'] = types.ModuleType('scripts')",
                f"sys.path.insert(0, {str(scripts_dir)!r})",
                "import extraction_jobs",
                "from continuity.store import ContinuityStore",
                (
                    "assert extraction_jobs.ContinuityStore is "
                    "ContinuityStore"
                ),
                (
                    "assert extraction_jobs.ExtractionJobConflict.__module__ "
                    "== 'extraction_jobs'"
                ),
            )
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            0,
            completed.returncode,
            msg=completed.stdout + completed.stderr,
        )

    def test_read_only_path_connection_avoids_unc_uri_authority(self) -> None:
        fake = mock.Mock()
        with mock.patch(
            "scripts.continuity.store.sqlite3.connect",
            return_value=fake,
        ) as connect:
            connection = ContinuityStore._connect_read_only_path(
                Path(r"\\TARGET\share\novel\.plot-rag\state.sqlite3"),
                timeout=7.0,
            )
        self.assertIs(fake, connection)
        args, kwargs = connect.call_args
        self.assertEqual(
            str(Path(r"\\TARGET\share\novel\.plot-rag\state.sqlite3")),
            args[0],
        )
        self.assertNotIn("uri", kwargs)
        self.assertEqual(7.0, kwargs["timeout"])
        self.assertIsNone(kwargs["isolation_level"])
        fake.execute.assert_called_once_with("PRAGMA query_only = ON")

        with self.queue.store.read_connection() as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute(
                    "CREATE TABLE forbidden_read_write(value TEXT)"
                )

    def test_read_assistant_text_samples_time_after_read_snapshot_begins(
        self,
    ) -> None:
        queued = self.enqueue()
        self.claim(lease_seconds=1)
        self.queue._clock = lambda: "2026-07-17T00:00:00.500000Z"
        original_read_connection = self.queue.store.read_connection

        @contextlib.contextmanager
        def delayed_read_connection():
            with original_read_connection() as connection:
                self.queue._clock = (
                    lambda: "2026-07-17T00:00:02.000000Z"
                )
                yield connection

        with (
            mock.patch.object(
                self.queue.store,
                "read_connection",
                delayed_read_connection,
            ),
            self.assertRaises(ExtractionLeaseLost) as caught,
        ):
            self.queue.read_assistant_text(
                queued["job_id"],
                worker_id="worker-a",
                expected_attempt_count=1,
            )
        self.assertEqual("EXTRACTION_JOB_LEASE_LOST", caught.exception.code)

    def test_live_heartbeat_thread_after_join_fences_worker_result(self) -> None:
        queued = self.enqueue()
        stop = threading.Event()

        class StuckThread:
            def join(self, timeout=None):
                return None

            def is_alive(self):
                return True

        with mock.patch.object(
            self.queue,
            "_start_heartbeat_thread",
            return_value=(stop, StuckThread(), []),
        ):
            outcome = self.queue.run_once(
                worker_id="worker-stuck-heartbeat",
                proposal_factory=lambda _job, _text: {
                    "validator_passed": True,
                    "result_kind": "no_delta",
                    "remote_status": "no_delta",
                },
                lease_seconds=30,
                heartbeat_interval_seconds=0.1,
            )
        self.assertTrue(stop.is_set())
        self.assertEqual("lease_lost", outcome["status"])
        self.assertIn(
            "EXTRACTION_JOB_HEARTBEAT_FAILED",
            outcome["error"],
        )
        self.assertEqual(
            "running",
            self.queue.inspect(queued["job_id"])["status"],
        )

    def test_booleans_are_rejected_as_integer_cas_values(self) -> None:
        self.enqueue()
        with self.assertRaises(ExtractionJobError) as caught:
            self.queue.claim(worker_id="worker", lease_seconds=True)
        self.assertEqual(
            "EXTRACTION_JOB_INVALID_INTEGER",
            caught.exception.code,
        )


if __name__ == "__main__":
    unittest.main()
