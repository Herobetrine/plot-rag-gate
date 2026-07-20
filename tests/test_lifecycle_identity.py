from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from continuity.service import (  # noqa: E402
    ContinuityService,
    HostApprovalAuthority,
)
from continuity.validators import ContinuityError  # noqa: E402
from event_experience import EventExperienceService  # noqa: E402
from event_experience_runtime import ensure_locked_manifest  # noqa: E402
from extraction_jobs import ExtractionJobQueue  # noqa: E402
from v1_runtime import _ensure_turn_v1_columns  # noqa: E402


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class LifecycleIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "novel"
        self.root.mkdir()
        self.service = ContinuityService(self.root)
        self.host = HostApprovalAuthority(
            self.service,
            issuer="lifecycle-tests",
        )
        self.initial_projection_hash = str(
            self.service.replay()["projection_hash"]
        )
        self.prompt = "剧情推演：测试角色甲如何在封锁中保住退路"
        self.assistant = "测试角色甲借临时通行牌穿过封锁，并留下暴露余悸。"
        self.artifact = {
            "artifact_id": "chapter-001",
            "artifact_revision": 2,
            "branch_id": "main",
            "chapter_no": 1,
            "scene_index": 0,
            "event_seeds": [
                {
                    "dependency_order": 2,
                    "dramatic_function": "身份暴露余悸",
                },
                {
                    "dependency_order": 1,
                    "dramatic_function": "发现可行缝隙",
                },
            ],
        }
        gate = ensure_locked_manifest(
            self.root,
            prompt=self.prompt,
            artifact_context=self.artifact,
            intent_contract=self._intent_contract(),
            session_identity="host-session",
            turn_identity="turn-1",
        )
        self.manifest = dict(gate["manifest"])
        contracts = [
            dict(item) for item in self.manifest["contracts"]
        ]
        self.identity = {
            "intent_contract_hash": self.manifest[
                "source_intent_contract_hash"
            ],
            "event_seed_manifest_hash": self.manifest[
                "event_seed_manifest_hash"
            ],
            "experience_contract_hashes": [
                item["contract_hash"] for item in reversed(contracts)
            ]
            + [contracts[0]["contract_hash"]],
            "event_experience_control_revision": self.manifest[
                "control_revision"
            ],
            "event_seed_references": [
                {
                    "event_seed_id": item["event_seed_id"],
                    "event_seed_revision": item["event_seed_revision"],
                }
                for item in reversed(contracts)
            ],
        }
        self.normalized_identity = {
            **self.identity,
            "experience_contract_hashes": sorted(
                set(self.identity["experience_contract_hashes"])
            ),
            "event_seed_references": sorted(
                self.identity["event_seed_references"],
                key=lambda item: (
                    item["event_seed_id"],
                    item["event_seed_revision"],
                ),
            ),
        }

    def _activate_artifact_context(
        self,
        artifact: dict[str, Any],
        *,
        suffix: str,
    ) -> None:
        gate = ensure_locked_manifest(
            self.root,
            prompt=self.prompt,
            artifact_context=artifact,
            intent_contract=self._intent_contract(),
            session_identity=f"host-session-{suffix}",
            turn_identity=f"turn-{suffix}",
        )
        manifest = dict(gate["manifest"])
        contracts = [dict(item) for item in manifest["contracts"]]
        identity = {
            "intent_contract_hash": manifest[
                "source_intent_contract_hash"
            ],
            "event_seed_manifest_hash": manifest[
                "event_seed_manifest_hash"
            ],
            "experience_contract_hashes": [
                item["contract_hash"] for item in reversed(contracts)
            ]
            + [contracts[0]["contract_hash"]],
            "event_experience_control_revision": manifest[
                "control_revision"
            ],
            "event_seed_references": [
                {
                    "event_seed_id": item["event_seed_id"],
                    "event_seed_revision": item["event_seed_revision"],
                }
                for item in reversed(contracts)
            ],
        }
        self.artifact = copy.deepcopy(artifact)
        self.manifest = manifest
        self.identity = identity
        self.normalized_identity = {
            **identity,
            "experience_contract_hashes": sorted(
                set(identity["experience_contract_hashes"])
            ),
            "event_seed_references": sorted(
                identity["event_seed_references"],
                key=lambda item: (
                    item["event_seed_id"],
                    item["event_seed_revision"],
                ),
            ),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _intent_contract() -> dict[str, Any]:
        values = {
            "problem_to_solve": "让主角在封锁中找到现实退路",
            "expected_deliverable": "一个可执行、可核验的事件链",
            "reader_experience": "持续压迫、发现缝隙、短暂松弛与余悸",
            "protagonist_drive_conflict": "主角优先保命，对手持续压缩退路",
            "scope_endpoint": "推进到主角换取一次局部主动",
            "success_criteria": "形成不可逆状态变化并留下后续压力",
            "hard_constraints": "不改写 accepted 事实，不让主角舍己",
            "model_autonomy": "模型可决定场景实现与次级冲突",
        }
        return {
            "status": "EXECUTING",
            "grill_session_id": "grill-session",
            "revision": 3,
            "contract": {
                "schema_version": "plot-rag-intent/v1",
                "task_family": "plot",
                "fields": {
                    field: {"value": value, "source": "user_answer"}
                    for field, value in values.items()
                },
            },
        }

    def _insert_turn(
        self,
        suffix: str,
        *,
        identity: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        receipt_id = f"receipt-{suffix}"
        request_id = f"request-{suffix}"
        selected_identity = (
            self.identity if identity is None else identity
        )
        with self.service.store.transaction() as connection:
            _ensure_turn_v1_columns(connection)
            connection.execute(
                """
                INSERT INTO turns(
                    receipt_id, request_id, session_id, turn_id,
                    prompt, prompt_hash, assistant_hash, status,
                    retrieved_json, authority_json, craft_json,
                    remote_json, result_json, error, started_at,
                    prepared_canon_revision, active_projection_hash,
                    retrieved_context_digest, lifecycle_identity_json
                ) VALUES(
                    ?, ?, 'session', ?, ?, ?, '', 'pending',
                    '[]', '{}', '{}', '{}', '{}', '',
                    '2026-07-17T00:00:00Z', 0, ?, ?, ?
                )
                """,
                (
                    receipt_id,
                    request_id,
                    f"turn-{suffix}",
                    self.prompt,
                    digest(self.prompt),
                    self.initial_projection_hash,
                    digest(f"context-{suffix}"),
                    json.dumps(
                        selected_identity,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
        return {
            "receipt_id": receipt_id,
            "request_id": request_id,
            "context_digest": digest(f"context-{suffix}"),
        }

    def _payload(
        self,
        turn: dict[str, str],
        *,
        identity: dict[str, Any] | None = None,
        include_assistant_text: bool = True,
    ) -> dict[str, Any]:
        selected_identity = (
            self.identity if identity is None else identity
        )
        payload = {
            "receipt_id": turn["receipt_id"],
            "request_id": turn["request_id"],
            "prompt": self.prompt,
            "assistant_sha256": digest(self.assistant),
            "prompt_hash": digest(self.prompt),
            "retrieved_context_digest": turn["context_digest"],
            "prepared_canon_revision": 0,
            "active_projection_hash": self.initial_projection_hash,
            "artifact_context": copy.deepcopy(self.artifact),
            "lifecycle_identity": copy.deepcopy(selected_identity),
        }
        if include_assistant_text:
            payload["assistant_text"] = self.assistant
        return payload

    @staticmethod
    def _advantage_definition_event(
        *,
        advantage_id: str,
        experience_contract_id: str | None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "schema_version": "plot-rag-advantage/v1",
            "event_type": "advantage_spec",
            "action": "define",
            "spec_type": "advantage_definition",
            "advantage_id": advantage_id,
            "title": "体验合同绑定测试",
            "profiles": ["growth_relic"],
            "anchor_type": "body_or_vessel",
            "acquisition_mode": "inheritance",
            "uniqueness": "unique",
            "status": "canon",
            "evidence": {"quote": "体验合同绑定测试事件。"},
            "definition": {},
        }
        if experience_contract_id is not None:
            event["experience_contract_id"] = experience_contract_id
        return event

    def _save_advantage(
        self,
        suffix: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        turn = self._insert_turn(suffix)
        return self.service.save_proposal(
            events=[event],
            payload=self._payload(turn),
            artifact_id=self.artifact["artifact_id"],
            artifact_stage="brainstorm",
            branch_id="main",
            chapter_no=1,
            scene_index=0,
            artifact_revision=self.artifact["artifact_revision"],
            prepared_canon_revision=0,
        )

    def _save(
        self,
        suffix: str,
        *,
        payload_updates: dict[str, Any] | None = None,
        include_assistant_text: bool = True,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        turn = self._insert_turn(suffix)
        payload = self._payload(
            turn,
            include_assistant_text=include_assistant_text,
        )
        payload.update(payload_updates or {})
        proposal = self.service.save_proposal(
            events=[],
            payload=payload,
            artifact_id=self.artifact["artifact_id"],
            artifact_stage="brainstorm",
            branch_id="main",
            chapter_no=1,
            scene_index=0,
            prepared_canon_revision=0,
        )
        return turn, proposal

    def _grant(self, proposal: dict[str, Any]) -> dict[str, Any]:
        return self.host.issue(
            str(proposal["proposal_id"]),
            expected_canon_revision=0,
        )

    def _assert_unconsumed(self, grant: dict[str, Any]) -> None:
        with self.service.store.read_connection() as connection:
            row = connection.execute(
                """
                SELECT consumed_at, consumed_request_hash
                FROM approval_grants
                WHERE binding_hash=?
                """,
                (grant["binding_hash"],),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row["consumed_at"])
        self.assertIsNone(row["consumed_request_hash"])

    def _assert_error(self, expected_code: str):
        class ErrorContext:
            def __init__(
                self,
                outer: "LifecycleIdentityTests",
            ) -> None:
                self.outer = outer
                self.context = outer.assertRaises(ContinuityError)

            def __enter__(self):
                return self.context.__enter__()

            def __exit__(self, exc_type, exc, traceback):
                handled = self.context.__exit__(
                    exc_type,
                    exc,
                    traceback,
                )
                if handled:
                    self.outer.assertEqual(
                        expected_code,
                        self.context.exception.code,
                    )
                return handled

        return ErrorContext(self)

    def _enqueue_job(
        self,
        turn: dict[str, str],
        suffix: str,
    ) -> tuple[ExtractionJobQueue, dict[str, Any]]:
        queue = ExtractionJobQueue(self.root)
        job = queue.enqueue(
            receipt_id=turn["receipt_id"],
            request_id=turn["request_id"],
            assistant_text=self.assistant,
            prompt_hash=digest(self.prompt),
            retrieved_context_digest=turn["context_digest"],
            prepared_canon_revision=0,
            active_projection_hash=self.initial_projection_hash,
            intent_contract_hash=self.normalized_identity[
                "intent_contract_hash"
            ],
            event_seed_manifest_hash=self.normalized_identity[
                "event_seed_manifest_hash"
            ],
            event_experience_control_revision=self.normalized_identity[
                "event_experience_control_revision"
            ],
            event_seed_references=self.normalized_identity[
                "event_seed_references"
            ],
            experience_contract_hashes=self.normalized_identity[
                "experience_contract_hashes"
            ],
            artifact_context=copy.deepcopy(self.artifact),
            branch_id="main",
            sequence_no=int(suffix.rsplit("-", 1)[-1]),
            extract_provider="siliconflow",
            extract_base_url="https://api.siliconflow.cn/v1",
            extract_model="Qwen/Qwen3",
            extract_schema_hash=digest("schema"),
            extract_prompt_template_hash=digest("template"),
            min_confidence=0.8,
            generation_params={"temperature": 0},
        )
        return queue, job

    def _save_async(
        self,
        suffix: str,
        *,
        artifact_revision: int | None = None,
    ) -> tuple[
        dict[str, str],
        ExtractionJobQueue,
        dict[str, Any],
        dict[str, Any],
    ]:
        artifact = copy.deepcopy(self.artifact)
        if artifact_revision is not None:
            artifact["artifact_revision"] = artifact_revision
        if artifact != self.artifact:
            self._activate_artifact_context(artifact, suffix=suffix)
            artifact = copy.deepcopy(self.artifact)
        turn = self._insert_turn(suffix)
        queue, job = self._enqueue_job(turn, suffix)
        payload = self._payload(
            turn,
            include_assistant_text=False,
        )
        payload.update(queue.proposal_binding(job))
        proposal = self.service.save_proposal(
            events=[],
            payload=payload,
            artifact_id=artifact["artifact_id"],
            artifact_stage="brainstorm",
            branch_id="main",
            chapter_no=1,
            scene_index=0,
            artifact_revision=artifact["artifact_revision"],
            prepared_canon_revision=0,
        )
        return turn, queue, job, proposal

    def _finish_job(
        self,
        queue: ExtractionJobQueue,
        job: dict[str, Any],
        proposal: dict[str, Any],
        *,
        result_kind: str = "proposal",
    ) -> None:
        claimed = queue.claim(
            worker_id="worker",
            now="2026-07-17T00:00:00Z",
        )
        self.assertIsNotNone(claimed)
        queue.succeed(
            job["job_id"],
            worker_id="worker",
            expected_attempt_count=claimed["attempt_count"],
            validator_passed=True,
            result_kind=result_kind,
            result_proposal_id=(
                proposal["proposal_id"]
                if result_kind == "proposal"
                else None
            ),
            now="2026-07-17T00:00:01Z",
        )

    def test_full_identity_grant_and_accept_normalize_seed_order(self) -> None:
        _, proposal = self._save("success-1")
        self.assertEqual(
            self.normalized_identity,
            proposal["payload"]["lifecycle_identity"],
        )
        grant = self._grant(proposal)
        accepted = self.service.accept_proposal(
            proposal["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=0,
        )
        self.assertEqual("accept", accepted["operation"])
        self.assertEqual(
            proposal["proposal_id"],
            accepted["proposal_id"],
        )

    def test_lifecycle_bound_advantage_requires_event_contract_id(
        self,
    ) -> None:
        event = self._advantage_definition_event(
            advantage_id="advantage-missing-contract",
            experience_contract_id=None,
        )

        with self._assert_error("ADVANTAGE_EXPERIENCE_CONTRACT_REQUIRED"):
            self._save_advantage("advantage-missing-contract", event)

        with self.service.store.read_connection() as connection:
            proposal_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM proposals"
                ).fetchone()[0]
            )
        self.assertEqual(0, proposal_count)

    def test_lifecycle_bound_advantage_contract_must_belong_to_manifest(
        self,
    ) -> None:
        event = self._advantage_definition_event(
            advantage_id="advantage-foreign-contract",
            experience_contract_id="experience-contract-outside-manifest",
        )
        proposal = self._save_advantage(
            "advantage-foreign-contract",
            event,
        )

        with self._assert_error("ADVANTAGE_EXPERIENCE_CONTRACT_MISMATCH"):
            self._grant(proposal)

        with self.service.store.read_connection() as connection:
            grant_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM approval_grants"
                ).fetchone()[0]
            )
        self.assertEqual(0, grant_count)

    def test_lifecycle_bound_advantage_validates_full_contract_tuple(
        self,
    ) -> None:
        contract = dict(self.manifest["contracts"][0])
        event = self._advantage_definition_event(
            advantage_id="advantage-tuple-mismatch",
            experience_contract_id=str(contract["contract_id"]),
        )
        event.update(
            {
                "experience_contract_hash": "f" * 64,
                "causal_provenance": {
                    "event_seed_id": contract["event_seed_id"],
                    "event_seed_revision": contract[
                        "event_seed_revision"
                    ],
                },
            }
        )
        proposal = self._save_advantage(
            "advantage-tuple-mismatch",
            event,
        )
        with self._assert_error(
            "ADVANTAGE_EXPERIENCE_CONTRACT_MISMATCH"
        ):
            self._grant(proposal)

    def test_accept_revalidates_advantage_contract_membership(self) -> None:
        contract_id = str(self.manifest["contracts"][0]["contract_id"])
        event = self._advantage_definition_event(
            advantage_id="advantage-contract-drift",
            experience_contract_id=contract_id,
        )
        proposal = self._save_advantage(
            "advantage-contract-drift",
            event,
        )
        grant = self._grant(proposal)
        manifest = copy.deepcopy(self.manifest)
        manifest["contracts"][0]["contract_id"] = (
            "experience-contract-replaced-after-grant"
        )

        class FakeValidator:
            def validate_locked_manifest_in_transaction(
                self,
                *_args,
                **_kwargs,
            ):
                return manifest

        self.service._event_experience_service_instance = FakeValidator()
        with self._assert_error("ADVANTAGE_EXPERIENCE_CONTRACT_MISMATCH"):
            self.service.accept_proposal(
                proposal["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=0,
            )
        self._assert_unconsumed(grant)

    def test_valid_advantage_contract_binding_survives_accept_and_replay(
        self,
    ) -> None:
        contract_id = str(self.manifest["contracts"][0]["contract_id"])
        event = self._advantage_definition_event(
            advantage_id="advantage-valid-contract",
            experience_contract_id=contract_id,
        )
        proposal = self._save_advantage(
            "advantage-valid-contract",
            event,
        )
        grant = self._grant(proposal)
        accepted = self.service.accept_proposal(
            proposal["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=0,
        )

        self.assertEqual("accept", accepted["operation"])
        with self.service.store.read_connection() as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM continuity_events
                WHERE commit_id=?
                """,
                (accepted["commit_id"],),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(
            contract_id,
            json.loads(str(row["payload_json"]))[
                "experience_contract_id"
            ],
        )
        replayed = self.service.replay()
        self.assertRegex(
            replayed["advantage_projection_hash"],
            r"^advantage_projection_[0-9a-f]{64}$",
        )

    def test_payload_json_tamper_blocks_grant_and_accept(self) -> None:
        _, proposal = self._save("tamper-1")
        corrupted = dict(proposal["payload"])
        corrupted["tampered"] = True
        with self.service.store.transaction() as connection:
            connection.execute(
                "UPDATE proposals SET payload_json=? WHERE proposal_id=?",
                (
                    json.dumps(corrupted, ensure_ascii=False),
                    proposal["proposal_id"],
                ),
            )
        with self._assert_error("PROPOSAL_CONTENT_HASH_MISMATCH"):
            self._grant(proposal)

        _, second = self._save("tamper-2")
        grant = self._grant(second)
        corrupted = dict(second["payload"])
        corrupted["tampered_after_grant"] = True
        with self.service.store.transaction() as connection:
            connection.execute(
                "UPDATE proposals SET payload_json=? WHERE proposal_id=?",
                (
                    json.dumps(corrupted, ensure_ascii=False),
                    second["proposal_id"],
                ),
            )
        with self._assert_error("PROPOSAL_CONTENT_HASH_MISMATCH"):
            self.service.accept_proposal(
                second["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=0,
            )
        self._assert_unconsumed(grant)

    def test_projection_hash_drift_blocks_grant_and_accept(self) -> None:
        _, proposal = self._save("projection-1")
        grant = self._grant(proposal)
        with self.service.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO projection_runs(
                    run_id, projection_name, source_head_revision,
                    source_active_revision, run_status, projection_hash,
                    details_json, created_at, completed_at
                ) VALUES(
                    'projection-run-drift', 'continuity', 0, 0,
                    'completed', 'projection_drift', '{}',
                    '9999-01-01T00:00:00Z', '9999-01-01T00:00:00Z'
                )
                """
            )
        with self._assert_error(
            "PREPARED_PROJECTION_BINDING_MISMATCH"
        ):
            self._grant(proposal)
        with self._assert_error(
            "PREPARED_PROJECTION_BINDING_MISMATCH"
        ):
            self.service.accept_proposal(
                proposal["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=0,
            )
        self._assert_unconsumed(grant)

    def test_control_revision_drift_blocks_grant_and_accept(self) -> None:
        _, proposal = self._save("control-1")
        grant = self._grant(proposal)
        ensure_locked_manifest(
            self.root,
            prompt="剧情推演：另一条独立事件链",
            artifact_context={
                "artifact_id": "chapter-002",
                "artifact_revision": 1,
                "branch_id": "main",
                "chapter_no": 2,
                "scene_index": 0,
            },
            intent_contract=self._intent_contract(),
            session_identity="host-session-2",
            turn_identity="turn-2",
        )
        with self._assert_error("EVENT_EXPERIENCE_STALE_CONTROL"):
            self._grant(proposal)
        with self._assert_error("EVENT_EXPERIENCE_STALE_CONTROL"):
            self.service.accept_proposal(
                proposal["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=0,
            )
        self._assert_unconsumed(grant)

    def test_intent_hash_drift_is_compared_after_manifest_validation(
        self,
    ) -> None:
        _, proposal = self._save("intent-1")
        grant = self._grant(proposal)
        manifest = copy.deepcopy(self.manifest)
        manifest["source_intent_contract_hash"] = digest("other-intent")

        class FakeValidator:
            def validate_locked_manifest_in_transaction(
                self,
                *_args,
                **_kwargs,
            ):
                return manifest

        self.service._event_experience_service_instance = FakeValidator()
        with self._assert_error(
            "EVENT_EXPERIENCE_LIFECYCLE_BINDING_MISMATCH"
        ):
            self._grant(proposal)
        with self._assert_error(
            "EVENT_EXPERIENCE_LIFECYCLE_BINDING_MISMATCH"
        ):
            self.service.accept_proposal(
                proposal["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=0,
            )
        self._assert_unconsumed(grant)

    def test_contract_hash_drift_is_compared_after_manifest_validation(
        self,
    ) -> None:
        _, proposal = self._save("contract-1")
        grant = self._grant(proposal)
        manifest = copy.deepcopy(self.manifest)
        manifest["contracts"][0]["contract_hash"] = digest(
            "other-contract"
        )

        class FakeValidator:
            def validate_locked_manifest_in_transaction(
                self,
                *_args,
                **_kwargs,
            ):
                return manifest

        self.service._event_experience_service_instance = FakeValidator()
        with self._assert_error(
            "EVENT_EXPERIENCE_LIFECYCLE_BINDING_MISMATCH"
        ):
            self._grant(proposal)
        with self._assert_error(
            "EVENT_EXPERIENCE_LIFECYCLE_BINDING_MISMATCH"
        ):
            self.service.accept_proposal(
                proposal["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=0,
            )
        self._assert_unconsumed(grant)

    def test_extraction_job_missing_running_failed_and_no_delta_block(
        self,
    ) -> None:
        turn = self._insert_turn("job-missing-1")
        missing_payload = self._payload(turn)
        missing_payload.update(
            {
                "extraction_job_id": "missing-job",
                "job_binding_hash": digest("missing-job-binding"),
                "intent_contract_hash": self.normalized_identity[
                    "intent_contract_hash"
                ],
                "event_seed_manifest_hash": self.normalized_identity[
                    "event_seed_manifest_hash"
                ],
                "event_experience_control_revision": (
                    self.normalized_identity[
                        "event_experience_control_revision"
                    ]
                ),
                "event_seed_references": self.normalized_identity[
                    "event_seed_references"
                ],
                "experience_contract_hashes": (
                    self.normalized_identity[
                        "experience_contract_hashes"
                    ]
                ),
            }
        )
        missing = self.service.save_proposal(
            events=[],
            payload=missing_payload,
            artifact_id=self.artifact["artifact_id"],
            artifact_stage="brainstorm",
            branch_id="main",
            chapter_no=1,
            scene_index=0,
            prepared_canon_revision=0,
        )
        missing_grant = self._grant(missing)
        with self._assert_error(
            "EXTRACTION_PROPOSAL_JOB_NOT_FINALIZED"
        ):
            self.service.accept_proposal(
                missing["proposal_id"],
                approval_id=missing_grant["approval_id"],
                expected_canon_revision=0,
            )
        self._assert_unconsumed(missing_grant)

        for index, status in enumerate(
            ("running", "failed", "no_delta"),
            start=2,
        ):
            with self.subTest(status=status):
                _, queue, job, proposal = self._save_async(
                    f"job-{status}-{index}",
                    artifact_revision=index,
                )
                if status == "running":
                    queue.claim(
                        worker_id=f"worker-{index}",
                        now="2026-07-17T00:00:00Z",
                    )
                elif status == "failed":
                    with queue.store.transaction() as connection:
                        connection.execute(
                            """
                            UPDATE extraction_jobs
                            SET job_status='failed', remote_status='failed',
                                error='fixture failure',
                                completed_at='2026-07-17T00:00:01Z',
                                updated_at='2026-07-17T00:00:01Z'
                            WHERE job_id=?
                            """,
                            (job["job_id"],),
                        )
                else:
                    self._finish_job(
                        queue,
                        job,
                        proposal,
                        result_kind="no_delta",
                    )
                grant = self._grant(proposal)
                with self._assert_error(
                    "EXTRACTION_PROPOSAL_JOB_NOT_FINALIZED"
                ):
                    self.service.accept_proposal(
                        proposal["proposal_id"],
                        approval_id=grant["approval_id"],
                        expected_canon_revision=0,
                    )
                self._assert_unconsumed(grant)

    def test_extraction_job_result_and_rehashed_binding_mismatch_block(
        self,
    ) -> None:
        _, queue, job, proposal = self._save_async("job-other-5")
        self._finish_job(queue, job, proposal)
        other = self.service.save_proposal(
            events=[],
            payload={"legacy": True},
            artifact_id="other-artifact",
            artifact_stage="brainstorm",
            branch_id="main",
            prepared_canon_revision=0,
        )
        with queue.store.transaction() as connection:
            connection.execute(
                """
                UPDATE extraction_jobs
                SET result_proposal_id=?
                WHERE job_id=?
                """,
                (other["proposal_id"], job["job_id"]),
            )
        grant = self._grant(proposal)
        with self._assert_error("EXTRACTION_PROPOSAL_BINDING_MISMATCH"):
            self.service.accept_proposal(
                proposal["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=0,
            )
        self._assert_unconsumed(grant)

        _, queue, job, proposal = self._save_async(
            "job-rehash-6",
            artifact_revision=3,
        )
        self._finish_job(queue, job, proposal)
        with queue.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM extraction_jobs WHERE job_id=?",
                (job["job_id"],),
            ).fetchone()
            changed = dict(row)
            changed["prompt_hash"] = digest("changed-prompt")
            changed_hash = ExtractionJobQueue._hash_binding(changed)[
                "job_binding_hash"
            ]
            connection.execute(
                """
                UPDATE extraction_jobs
                SET prompt_hash=?, job_binding_hash=?
                WHERE job_id=?
                """,
                (
                    changed["prompt_hash"],
                    changed_hash,
                    job["job_id"],
                ),
            )
        grant = self._grant(proposal)
        with self._assert_error("EXTRACTION_PROPOSAL_BINDING_MISMATCH"):
            self.service.accept_proposal(
                proposal["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=0,
            )
        self._assert_unconsumed(grant)

    def test_successful_extraction_job_remains_accept_only(self) -> None:
        _, queue, job, proposal = self._save_async("job-success-7")
        self._finish_job(queue, job, proposal)
        grant = self._grant(proposal)
        accepted = self.service.accept_proposal(
            proposal["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=0,
        )
        self.assertEqual(proposal["proposal_id"], accepted["proposal_id"])

    def test_legacy_story_accept_and_retract_remain_compatible(self) -> None:
        legacy = self.service.save_proposal(
            events=[],
            payload={"legacy": True},
            artifact_id="legacy-story",
            artifact_stage="brainstorm",
            branch_id="main",
            prepared_canon_revision=0,
        )
        grant = self._grant(legacy)
        accepted = self.service.accept_proposal(
            legacy["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=0,
        )
        self.assertEqual("accept", accepted["operation"])
        retract_grant = self.host.issue(
            legacy["proposal_id"],
            expected_canon_revision=0,
            operations=("retract",),
        )
        retracted = self.service.retract_proposal(
            legacy["proposal_id"],
            approval_id=retract_grant["approval_id"],
            expected_canon_revision=0,
            reason="compatibility test",
        )
        self.assertEqual("retract", retracted["operation"])


if __name__ == "__main__":
    unittest.main()
