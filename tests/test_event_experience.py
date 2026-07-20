from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from event_experience import (
    CONTROL_TABLES,
    EVENT_EXPERIENCE_HASH_PROTOCOL,
    EVENT_EXPERIENCE_SCHEMA_VERSION,
    EventExperienceError,
    EventExperienceService,
    canonical_hash,
    canonical_json,
)
from continuity.schema import SCHEMA_VERSION as CONTINUITY_SCHEMA_VERSION
from continuity.store import ContinuityStore


class EventExperienceServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "novel"
        self.root.mkdir()
        self.database = self.root / ".plot-rag" / "state.sqlite3"
        self.service = EventExperienceService(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def seed(
        self,
        *,
        seed_id: str = "seed-1",
        chain: str = "chain-1",
        order: int = 1,
        branch: str = "main",
        artifact: str = "chapter-1",
        artifact_revision: int = 1,
        dramatic_function: str = "逼迫主角作出不可逆选择",
    ) -> dict:
        return {
            "event_seed_id": seed_id,
            "event_seed_revision": 1,
            "parent_chain_id": chain,
            "dependency_order": order,
            "dramatic_function": dramatic_function,
            "causal_role": "升级主动对手反应",
            "intended_state_change": "主角失去旧退路并获得新目标",
            "event_boundary": "从收到最后通牒到作出选择",
            "narrative_event_id": f"event-{seed_id}",
            "artifact_id": artifact,
            "artifact_revision": artifact_revision,
            "branch_id": branch,
            "chapter_no": 1,
            "scene_index": order,
        }

    def arc(
        self,
        seed_ids: list[str],
        *,
        arc_id: str = "arc-1",
        chain: str = "chain-1",
        branch: str = "main",
        artifact: str = "chapter-1",
        artifact_revision: int = 1,
    ) -> dict:
        return {
            "arc_id": arc_id,
            "arc_revision": 1,
            "parent_chain_id": chain,
            "entry_reader_state": "担忧主角处境",
            "target_reader_state": "压迫中看到突破希望",
            "overall_peak": "主角以代价换来局部主动",
            "release_rhythm": "持续加压后短促释放",
            "aftertaste": "希望与身份暴露余悸并存",
            "event_seed_ids": seed_ids,
            "branch_id": branch,
            "artifact_id": artifact,
            "artifact_revision": artifact_revision,
        }

    def contract(
        self,
        *,
        seed_id: str = "seed-1",
        seed_revision: int = 1,
        contract_id: str = "contract-1",
        contract_revision: int = 1,
        source_intent_id: str = "intent-1",
        source_intent_revision: int = 1,
        source_intent_hash: str | None = None,
        primary_emotion: str = "紧张",
        artifact_revision: int | None = None,
    ) -> dict:
        result = {
            "contract_id": contract_id,
            "contract_revision": contract_revision,
            "event_seed_id": seed_id,
            "event_seed_revision": seed_revision,
            "source_intent_contract_id": source_intent_id,
            "source_intent_contract_revision": source_intent_revision,
            "source_intent_contract_hash": (
                source_intent_hash
                or hashlib.sha256(b"intent-1").hexdigest()
            ),
            "entry_reader_state": "担忧",
            "target_reader_state": "压迫中产生希望",
            "primary_emotion": primary_emotion,
            "ordered_secondary_emotions": ["不安", "期待"],
            "emotional_turn": "从被动压迫转向有限主动",
            "intensity": {"entry": 35, "peak": 82, "exit": 55},
            "emotion_curve": ["期待", "压迫", "惊讶", "短促释放", "余悸"],
            "mechanisms": ["信息差", "选择代价", "局部反击"],
            "reader_knowledge_position": "与视角人物同步",
            "viewpoint_character_state": "谨慎评估退路，不作舍己选择",
            "payoff_or_reveal": "兑现局部脱困并暴露更大代价",
            "aftertaste": "短暂希望后留下身份暴露余悸",
            "anti_experiences": ["滑稽化", "无代价开挂"],
            "success_signals": ["选择改变后续前提", "结尾保留持续压力"],
            "open_loop_links": ["loop-identity"],
            "derivation": {
                "source": "locked_intent_contract",
                "confidence": 0.94,
                "user_confirmed": True,
                "delegated_choice": False,
            },
            "field_provenance": {
                field: {
                    "source": "test locked intent/artifact context",
                    "source_intent_contract_hash": (
                        source_intent_hash
                        or hashlib.sha256(b"intent-1").hexdigest()
                    ),
                }
                for field in (
                    "entry_reader_state",
                    "target_reader_state",
                    "primary_emotion",
                    "emotional_turn",
                    "intensity",
                    "emotion_curve",
                    "mechanisms",
                    "reader_knowledge_position",
                    "viewpoint_character_state",
                    "payoff_or_reveal",
                    "aftertaste",
                    "anti_experiences",
                    "success_signals",
                )
            },
        }
        if artifact_revision is not None:
            result["artifact_revision"] = artifact_revision
        return result

    def create_seed(self, payload: dict | None = None, *, key: str = "seed") -> dict:
        result = self.service.create_seed(
            payload or self.seed(),
            expected_control_revision=self.service.get_control_revision(),
            idempotency_key=key,
        )
        return result["seed"]

    def create_locked(
        self,
        *,
        seed_payload: dict | None = None,
        contract_payload: dict | None = None,
        key: str = "locked",
    ) -> tuple[dict, dict]:
        seed = self.create_seed(seed_payload, key=f"{key}-seed")
        result = self.service.propose_and_lock_contract(
            contract_payload or self.contract(seed_id=seed["event_seed_id"]),
            expected_control_revision=self.service.get_control_revision(),
            idempotency_key=f"{key}-contract",
        )
        return seed, result["contract"]

    def assert_error(self, code: str):
        class Context:
            def __init__(self, outer: unittest.TestCase) -> None:
                self.outer = outer
                self.caught = None

            def __enter__(self):
                self.caught = self.outer.assertRaises(EventExperienceError)
                return self.caught.__enter__()

            def __exit__(self, exc_type, exc, traceback):
                handled = self.caught.__exit__(exc_type, exc, traceback)
                if handled:
                    self.outer.assertEqual(code, self.caught.exception.code)
                return handled

        return Context(self)

    def rows(self, table: str) -> list[sqlite3.Row]:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(f'SELECT * FROM "{table}"').fetchall()
        finally:
            connection.close()

    def test_hash_normalization_is_stable_and_rejects_key_collision(self) -> None:
        left = {"line": "a\r\nb", "nested": {"b": 2, "a": 1}}
        right = {"line": "a\nb", "nested": {"a": 1, "b": 2}}
        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(canonical_hash(left), canonical_hash(right))
        self.assertNotEqual(
            canonical_hash({"text": "e\u0301"}),
            canonical_hash({"text": "é"}),
        )
        with self.assert_error("EVENT_EXPERIENCE_NORMALIZED_KEY_COLLISION"):
            canonical_hash({"line\r\n": 1, "line\n": 2})
        with self.assert_error("EVENT_EXPERIENCE_NONFINITE_NUMBER"):
            canonical_hash({"bad": float("nan")})
        with self.assert_error("EVENT_EXPERIENCE_INVALID_UNICODE_SCALAR"):
            canonical_hash({"bad": "\ud800"})

    def test_hash_protocol_fixture_is_cross_runtime_deterministic(self) -> None:
        payload = {
            "line": "a\r\nb",
            "nested": {"b": 2, "a": 1},
            "unicode": "a\u0898\u0323",
        }
        self.assertEqual(
            "plot-rag-event-experience-hash/codepoint-json-v1",
            EVENT_EXPERIENCE_HASH_PROTOCOL,
        )
        self.assertEqual(
            '{"line":"a\\nb","nested":{"a":1,"b":2},"unicode":"ạ࢘"}',
            canonical_json(payload),
        )
        self.assertEqual(
            "e2a48e1301815402501ba26587513e32"
            "f823578e67a0798453ae88b6e39665dd",
            canonical_hash(payload),
        )

    def test_schema_creation_reopen_and_boundary_evidence(self) -> None:
        self.assertEqual(0, self.service.get_control_revision())
        reopened = EventExperienceService(self.database)
        self.assertEqual(0, reopened.get_control_revision())
        report = reopened.storage_boundary_report()
        self.assertTrue(report["boundary_ok"])
        self.assertEqual(
            EVENT_EXPERIENCE_HASH_PROTOCOL,
            report["hash_protocol"],
        )
        self.assertEqual(sorted(CONTROL_TABLES), report["control_tables"])
        self.assertEqual([], report["control_triggers"])
        self.assertEqual([], report["foreign_key_violations"])
        self.assertIn(
            "event_experience_contracts",
            report["control_row_counts"],
        )

    def test_empty_legacy_control_database_adopts_hash_protocol(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                DELETE FROM event_experience_meta
                WHERE key='hash_protocol'
                """
            )
            connection.commit()

        reopened = EventExperienceService(self.database)
        self.assertEqual(0, reopened.get_control_revision())
        self.assertEqual(
            EVENT_EXPERIENCE_HASH_PROTOCOL,
            reopened.storage_boundary_report()["hash_protocol"],
        )

    def test_populated_unversioned_control_database_fails_closed(self) -> None:
        self.create_seed()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                DELETE FROM event_experience_meta
                WHERE key='hash_protocol'
                """
            )
            connection.commit()

        with self.assert_error("EVENT_EXPERIENCE_HASH_PROTOCOL_MISSING"):
            EventExperienceService(self.database)

        with closing(sqlite3.connect(self.database)) as connection:
            protocol = connection.execute(
                """
                SELECT value FROM event_experience_meta
                WHERE key='hash_protocol'
                """
            ).fetchone()
            seeds = int(
                connection.execute(
                    "SELECT COUNT(*) FROM event_seeds"
                ).fetchone()[0]
            )
        self.assertIsNone(protocol)
        self.assertEqual(1, seeds)

    def test_unknown_hash_protocol_fails_closed(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                UPDATE event_experience_meta
                SET value='plot-rag-event-experience-hash/future'
                WHERE key='hash_protocol'
                """
            )
            connection.commit()
        with self.assert_error("EVENT_EXPERIENCE_HASH_PROTOCOL_UNSUPPORTED"):
            EventExperienceService(self.database)

    def test_iso_z_timestamp_is_stable_across_supported_python_versions(
        self,
    ) -> None:
        result = self.service.retire_expired_questions(
            expected_control_revision=0,
            idempotency_key="iso-z",
            observed_at="2026-07-17T00:00:00Z",
        )
        self.assertEqual(
            "2026-07-17T00:00:00+00:00",
            result["observed_at"],
        )
        with self.assert_error("EVENT_EXPERIENCE_TIMESTAMP_INVALID"):
            self.service.retire_expired_questions(
                expected_control_revision=0,
                idempotency_key="iso-invalid",
                observed_at="not-a-timestamp",
            )

    def test_shared_database_path_uses_host_case_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = (
                Path(temporary)
                / ".PLOT-RAG"
                / "state.sqlite3"
            )
            service = EventExperienceService(database)
            self.assertEqual(0, service.get_control_revision())
            with closing(sqlite3.connect(database)) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type='table'
                        """
                    )
                }
        if os.name == "nt":
            self.assertIn("state_meta", tables)
        else:
            self.assertNotIn("state_meta", tables)
        self.assertIn("event_experience_meta", tables)

    def test_project_control_bootstrap_keeps_shared_state_continuity_valid(
        self,
    ) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            continuity_version = connection.execute(
                """
                SELECT value FROM state_meta
                WHERE key='continuity_schema_version'
                """
            ).fetchone()
            self.assertIsNotNone(continuity_version)
            self.assertEqual(
                str(CONTINUITY_SCHEMA_VERSION),
                str(continuity_version[0]),
            )
        self.assertIsNone(ContinuityStore(self.root).ensure_schema())

    def test_seed_crud_supersession_and_idempotency(self) -> None:
        created = self.service.create_seed(
            self.seed(),
            expected_control_revision=0,
            idempotency_key="seed-create",
        )
        retry = self.service.create_seed(
            self.seed(),
            expected_control_revision=created["control_revision"],
            idempotency_key="seed-create",
        )
        self.assertEqual(created, retry)
        self.assertEqual(1, self.service.get_control_revision())
        original_hash = created["seed"]["seed_hash"]

        replacement = self.seed(dramatic_function="迫使主角换一条退路")
        replacement["artifact_revision"] = 2
        superseded = self.service.supersede_seed(
            "seed-1",
            replacement,
            expected_control_revision=1,
            idempotency_key="seed-supersede",
            reason="accepted correction",
        )
        self.assertEqual(2, superseded["seed"]["event_seed_revision"])
        self.assertEqual(1, superseded["seed"]["supersedes_seed_revision"])
        old = self.service.get_seed("seed-1", 1)
        self.assertEqual("retired", old["status"])
        self.assertEqual(original_hash, old["seed_hash"])
        self.assertEqual(2, self.service.get_seed("seed-1")["event_seed_revision"])

        retired = self.service.retire_seed(
            "seed-1",
            2,
            expected_control_revision=2,
            idempotency_key="seed-retire",
            reason="story proposal rejected",
        )
        self.assertEqual("retired", retired["seed"]["status"])

    def test_seed_rejects_order_conflict_and_identity_changing_supersession(self) -> None:
        self.create_seed()
        with self.assert_error("EVENT_EXPERIENCE_DEPENDENCY_ORDER_CONFLICT"):
            self.service.create_seed(
                self.seed(seed_id="seed-2"),
                expected_control_revision=1,
                idempotency_key="seed-order-conflict",
            )
        replacement = self.seed(chain="other-chain")
        with self.assert_error("EVENT_EXPERIENCE_SEED_IDENTITY_MISMATCH"):
            self.service.supersede_seed(
                "seed-1",
                replacement,
                expected_control_revision=1,
                idempotency_key="seed-bad-chain",
                reason="invalid correction",
            )
        self.assertEqual("seeded", self.service.get_seed("seed-1")["status"])

    def test_stale_control_rolls_back_without_consuming_key(self) -> None:
        self.create_seed()
        with self.assert_error("EVENT_EXPERIENCE_STALE_CONTROL"):
            self.service.create_seed(
                self.seed(
                    seed_id="seed-2",
                    chain="chain-2",
                    artifact="chapter-2",
                ),
                expected_control_revision=0,
                idempotency_key="stale-key",
            )
        self.assertEqual(1, self.service.get_control_revision())
        self.assertEqual(1, len(self.rows("event_seeds")))
        result = self.service.create_seed(
            self.seed(
                seed_id="seed-2",
                chain="chain-2",
                artifact="chapter-2",
            ),
            expected_control_revision=1,
            idempotency_key="stale-key",
        )
        self.assertEqual(2, result["control_revision"])

    def test_concurrent_cas_has_one_winner_and_same_key_writes_once(self) -> None:
        barrier = threading.Barrier(2)

        def create_distinct(index: int):
            service = EventExperienceService(self.database)
            barrier.wait()
            try:
                return service.create_seed(
                    self.seed(
                        seed_id=f"seed-{index}",
                        chain=f"chain-{index}",
                        artifact=f"chapter-{index}",
                    ),
                    expected_control_revision=0,
                    idempotency_key=f"cas-{index}",
                )
            except EventExperienceError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create_distinct, (1, 2)))
        self.assertEqual(
            1,
            sum(isinstance(result, dict) for result in results),
        )
        self.assertIn("EVENT_EXPERIENCE_STALE_CONTROL", results)

        second_root = Path(self.temporary.name) / "same-key.sqlite3"
        service_a = EventExperienceService(second_root)
        service_b = EventExperienceService(second_root)
        same_barrier = threading.Barrier(2)

        def create_same(service: EventExperienceService):
            same_barrier.wait()
            return service.create_seed(
                self.seed(),
                expected_control_revision=0,
                idempotency_key="same-key",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            same_results = list(pool.map(create_same, (service_a, service_b)))
        self.assertEqual(same_results[0], same_results[1])
        self.assertEqual(1, service_a.get_control_revision())

    def test_arc_crud_binds_exact_seed_revision_and_hash(self) -> None:
        seed = self.create_seed()
        created = self.service.create_arc(
            self.arc([seed["event_seed_id"]]),
            expected_control_revision=1,
            idempotency_key="arc-create",
        )
        arc = created["arc"]
        self.assertEqual(
            seed["seed_hash"],
            arc["event_seed_bindings"][0]["seed_hash"],
        )
        locked = self.service.lock_arc(
            arc["arc_id"],
            arc["arc_revision"],
            expected_control_revision=2,
            idempotency_key="arc-lock",
            expected_arc_hash=arc["arc_hash"],
        )
        self.assertEqual("locked", locked["arc"]["status"])
        replacement = self.arc([seed["event_seed_id"]])
        replacement["overall_peak"] = "更晚、更强的峰值"
        replacement["artifact_revision"] = 1
        superseded = self.service.supersede_arc(
            arc["arc_id"],
            replacement,
            expected_control_revision=3,
            idempotency_key="arc-supersede",
            reason="accepted arc correction",
            lock_replacement=True,
        )
        self.assertEqual(2, superseded["arc"]["arc_revision"])
        self.assertEqual("locked", superseded["arc"]["status"])
        self.assertEqual("retired", self.service.get_arc("arc-1", 1)["status"])
        retired = self.service.retire_arc(
            "arc-1",
            2,
            expected_control_revision=4,
            idempotency_key="arc-retire",
            reason="chain abandoned",
        )
        self.assertEqual("retired", retired["arc"]["status"])

    def test_arc_cannot_lock_after_seed_supersession(self) -> None:
        self.create_seed()
        arc = self.service.create_arc(
            self.arc(["seed-1"]),
            expected_control_revision=1,
            idempotency_key="arc-stale-create",
        )["arc"]
        replacement = self.seed(dramatic_function="修正后的事件职责")
        replacement["artifact_revision"] = 2
        self.service.supersede_seed(
            "seed-1",
            replacement,
            expected_control_revision=2,
            idempotency_key="seed-correction",
            reason="accepted correction",
        )
        with self.assert_error("EVENT_EXPERIENCE_ARC_BINDING_STALE"):
            self.service.lock_arc(
                arc["arc_id"],
                1,
                expected_control_revision=3,
                idempotency_key="arc-stale-lock",
            )

    def test_only_one_active_arc_per_chain_branch_artifact(self) -> None:
        self.create_seed()
        self.service.create_arc(
            self.arc(["seed-1"], arc_id="arc-a"),
            expected_control_revision=1,
            idempotency_key="arc-a",
        )
        with self.assert_error("EVENT_EXPERIENCE_ARC_ACTIVE"):
            self.service.create_arc(
                self.arc(["seed-1"], arc_id="arc-b"),
                expected_control_revision=2,
                idempotency_key="arc-b",
            )

    def test_contract_context_is_bound_to_seed_and_revision_starts_at_one(self) -> None:
        self.create_seed()
        bad = self.contract()
        bad["branch_id"] = "other"
        with self.assert_error(
            "EVENT_EXPERIENCE_CONTRACT_SEED_CONTEXT_MISMATCH"
        ):
            self.service.propose_contract(
                bad,
                expected_control_revision=1,
                idempotency_key="bad-context",
            )
        bad_revision = self.contract(contract_revision=7)
        with self.assert_error("EVENT_EXPERIENCE_CONTRACT_REVISION"):
            self.service.propose_contract(
                bad_revision,
                expected_control_revision=1,
                idempotency_key="bad-contract-revision",
            )
        self.assertEqual(1, self.service.get_control_revision())

    def test_atomic_contract_lock_and_manifest_are_stable(self) -> None:
        seed, contract = self.create_locked()
        self.assertEqual("locked", contract["status"])
        self.assertEqual(
            contract["contract_hash"],
            self.service.get_seed(seed["event_seed_id"])[
                "experience_contract_hash"
            ],
        )
        refs = [("seed-1", 1)]
        first = self.service.locked_manifest(refs)
        self.assertTrue(first["ready"])
        self.assertEqual("chain-1", first["parent_chain_id"])
        self.assertEqual("main", first["branch_id"])
        self.assertEqual("chapter-1", first["artifact_id"])
        self.assertEqual("intent-1", first["source_intent_contract_id"])

        self.service.create_seed(
            self.seed(
                seed_id="seed-other",
                chain="chain-other",
                artifact="chapter-other",
            ),
            expected_control_revision=2,
            idempotency_key="unrelated-seed",
        )
        second = EventExperienceService(self.database).locked_manifest(refs)
        self.assertNotEqual(
            first["control_revision"], second["control_revision"]
        )
        self.assertEqual(
            first["event_seed_manifest_hash"],
            second["event_seed_manifest_hash"],
        )
        validated = self.service.validate_locked_manifest(
            refs,
            expected_event_seed_manifest_hash=second[
                "event_seed_manifest_hash"
            ],
            expected_control_revision=second["control_revision"],
        )
        self.assertEqual(second, validated)

    def test_transaction_manifest_validator_matches_outer_snapshot(self) -> None:
        self.create_locked()
        references = [("seed-1", 1)]
        expected = self.service.locked_manifest(references)
        connection = sqlite3.connect(self.database, isolation_level=None)
        original_row_factory = connection.row_factory
        try:
            connection.execute("BEGIN IMMEDIATE")
            validated = self.service.validate_locked_manifest_in_transaction(
                connection,
                references,
                expected_event_seed_manifest_hash=expected[
                    "event_seed_manifest_hash"
                ],
                expected_control_revision=expected["control_revision"],
            )
            self.assertEqual(
                canonical_json(expected),
                canonical_json(validated),
            )
            self.assertTrue(connection.in_transaction)
            self.assertIs(original_row_factory, connection.row_factory)
            connection.rollback()
        finally:
            connection.close()

        connection = sqlite3.connect(self.database)
        try:
            with self.assert_error(
                "EVENT_EXPERIENCE_TRANSACTION_REQUIRED"
            ):
                self.service.validate_locked_manifest_in_transaction(
                    connection,
                    references,
                    expected_event_seed_manifest_hash=expected[
                        "event_seed_manifest_hash"
                    ],
                    expected_control_revision=expected["control_revision"],
                )
        finally:
            connection.close()

    def test_strict_contract_and_manifest_hash_boundaries(self) -> None:
        self.create_seed()
        wrong_schema = self.contract()
        wrong_schema["schema_version"] = "plot-rag-event-experience/v999"
        with self.assert_error("EVENT_EXPERIENCE_SCHEMA_VERSION"):
            self.service.propose_contract(
                wrong_schema,
                expected_control_revision=1,
                idempotency_key="wrong-schema",
            )

        missing_provenance = self.contract()
        missing_provenance["field_provenance"] = {}
        proposed = self.service.propose_contract(
            missing_provenance,
            expected_control_revision=1,
            idempotency_key="missing-provenance",
        )
        with self.assert_error("EVENT_EXPERIENCE_PROVENANCE_INCOMPLETE"):
            self.service.lock_contract(
                proposed["contract"]["contract_id"],
                expected_control_revision=2,
                idempotency_key="missing-provenance-lock",
            )

        self.service.create_seed(
            self.seed(seed_id="seed-2", order=2),
            expected_control_revision=2,
            idempotency_key="strict-seed-2",
        )
        complete = self.contract(
            seed_id="seed-2",
            contract_id="contract-complete",
        )
        locked = self.service.propose_and_lock_contract(
            complete,
            expected_control_revision=3,
            idempotency_key="complete-contract",
        )
        manifest = self.service.locked_manifest([("seed-2", 1)])
        with self.assert_error("EVENT_EXPERIENCE_SHA256_REQUIRED"):
            self.service.validate_locked_manifest(
                [("seed-2", 1)],
                expected_event_seed_manifest_hash=manifest[
                    "event_seed_manifest_hash"
                ].upper(),
                expected_control_revision=manifest["control_revision"],
            )
        self.assertEqual(
            hashlib.sha256(b"intent-1").hexdigest(),
            locked["contract"]["source_intent_contract_hash"],
        )

    def test_missing_contract_has_clear_zero_remote_blocking_state(self) -> None:
        self.create_seed()
        with self.assert_error("EVENT_EXPERIENCE_CONTRACT_REQUIRED") as caught:
            self.service.locked_manifest([("seed-1", 1)])
        self.assertEqual(
            "AWAITING_EVENT_EXPERIENCE",
            caught.exception.details["blocking_state"],
        )
        self.assertIsNone(caught.exception.details["active_contract"])

        proposed = self.service.propose_contract(
            self.contract(),
            expected_control_revision=1,
            idempotency_key="proposed-only",
        )
        with self.assert_error("EVENT_EXPERIENCE_CONTRACT_REQUIRED") as caught:
            self.service.locked_manifest([("seed-1", 1)])
        self.assertEqual(
            proposed["contract"]["contract_id"],
            caught.exception.details["active_contract"]["contract_id"],
        )
        self.assertEqual(
            "proposed",
            caught.exception.details["active_contract"]["status"],
        )

    def test_manifest_rejects_cross_chain_branch_or_artifact(self) -> None:
        self.create_locked(key="one")
        second_seed = self.seed(
            seed_id="seed-2",
            chain="chain-2",
            artifact="chapter-2",
        )
        second_contract = self.contract(
            seed_id="seed-2",
            contract_id="contract-2",
        )
        self.create_locked(
            seed_payload=second_seed,
            contract_payload=second_contract,
            key="two",
        )
        with self.assert_error(
            "EVENT_EXPERIENCE_MANIFEST_CONTEXT_MISMATCH"
        ):
            self.service.locked_manifest(
                [("seed-1", 1), ("seed-2", 1)]
            )

    def test_contract_supersession_is_append_only(self) -> None:
        _, contract = self.create_locked()
        replacement = self.contract(
            contract_id="contract-2",
            contract_revision=2,
            primary_emotion="敬畏",
        )
        replacement["supersedes_contract_id"] = contract["contract_id"]
        result = self.service.supersede_contract(
            contract["contract_id"],
            replacement,
            expected_control_revision=2,
            idempotency_key="contract-supersede",
            reason="accepted experience correction",
            lock_replacement=True,
        )
        self.assertEqual("retired", self.service.get_contract("contract-1")["status"])
        self.assertEqual("locked", result["contract"]["status"])
        self.assertEqual("contract-1", result["contract"]["supersedes_contract_id"])
        active = self.service.active_contract_for_seed("seed-1", 1)
        self.assertEqual("contract-2", active["contract_id"])
        with self.assert_error("EVENT_EXPERIENCE_CONTRACT_ACTIVE"):
            self.service.propose_contract(
                self.contract(contract_id="contract-3"),
                expected_control_revision=3,
                idempotency_key="contract-history-bypass",
            )
        self.service.retire_contract(
            "contract-2",
            expected_control_revision=3,
            idempotency_key="contract-retire-active",
            reason="candidate abandoned",
        )
        with self.assert_error(
            "EVENT_EXPERIENCE_CONTRACT_SUPERSESSION_REQUIRED"
        ):
            self.service.propose_contract(
                self.contract(contract_id="contract-3"),
                expected_control_revision=4,
                idempotency_key="contract-history-bypass-after-retire",
            )

    def test_seed_status_transitions_require_locked_contract(self) -> None:
        seed, contract = self.create_locked()
        expanded = self.service.advance_seed_status(
            seed["event_seed_id"],
            1,
            "expanded",
            expected_control_revision=2,
            idempotency_key="seed-expanded",
            expected_contract_hash=contract["contract_hash"],
        )
        self.assertEqual("expanded", expanded["seed"]["status"])
        generated = self.service.advance_seed_status(
            seed["event_seed_id"],
            1,
            "generated",
            expected_control_revision=3,
            idempotency_key="seed-generated",
            expected_contract_hash=contract["contract_hash"],
        )
        self.assertEqual("generated", generated["seed"]["status"])

    def test_review_requires_verbatim_evidence_and_preserves_crlf(self) -> None:
        _, contract = self.create_locked()
        assistant = "第一行\r\n主角没有舍己，而是先留退路。\r\n尾声"
        quote = "主角没有舍己，而是先留退路。"
        start = assistant.index(quote)
        review = {
            "review_id": "review-1",
            "review_revision": 1,
            "proposal_id": "proposal-1",
            "receipt_id": "receipt-1",
            "assistant_sha256": hashlib.sha256(
                assistant.encode("utf-8")
            ).hexdigest(),
            "contract_id": contract["contract_id"],
            "contract_hash": contract["contract_hash"],
            "artifact_revision": 1,
            "observed_entry": "担忧",
            "observed_peak": "紧张",
            "observed_exit": "希望与余悸",
            "supporting_quotes": [quote],
            "supporting_quote_offsets": [[start, start + len(quote)]],
            "drift": "无明显偏差",
            "severity": "none",
            "recommendation": "保留",
        }
        recorded = self.service.record_review(
            review,
            expected_control_revision=2,
            idempotency_key="review-record",
            assistant_text=assistant,
        )
        self.assertEqual(
            quote, recorded["review"]["supporting_quotes"][0]
        )
        self.assertEqual(recorded["review"], self.service.get_review("review-1"))

        replacement = dict(review)
        replacement.update(
            {
                "review_id": "review-2",
                "review_revision": 2,
                "supersedes_review_id": "review-1",
                "drift": "峰值略晚",
                "recommendation": "前移一次对手反应",
            }
        )
        superseded = self.service.record_review(
            replacement,
            expected_control_revision=3,
            idempotency_key="review-supersede",
            assistant_text=assistant,
        )
        self.assertEqual("superseded", self.service.get_review("review-1")["status"])
        self.assertEqual("recorded", superseded["review"]["status"])
        self.assertEqual(2, len(self.service.list_reviews(contract_id="contract-1")))
        retired = self.service.retire_review(
            "review-2",
            expected_control_revision=4,
            idempotency_key="review-retire",
            reason="artifact retired",
        )
        self.assertEqual("retired", retired["review"]["status"])

    def test_review_rejects_zero_evidence_uppercase_hash_and_unlocked_contract(self) -> None:
        self.create_seed()
        proposed = self.service.propose_contract(
            self.contract(),
            expected_control_revision=1,
            idempotency_key="review-proposed-contract",
        )["contract"]
        assistant = "逐字证据"
        base = {
            "review_id": "review-bad",
            "review_revision": 1,
            "proposal_id": "proposal-1",
            "receipt_id": "receipt-1",
            "assistant_sha256": hashlib.sha256(
                assistant.encode("utf-8")
            ).hexdigest(),
            "contract_id": proposed["contract_id"],
            "contract_hash": proposed["contract_hash"],
            "artifact_revision": 1,
            "observed_entry": "担忧",
            "observed_peak": "紧张",
            "observed_exit": "余悸",
            "supporting_quotes": [assistant],
            "supporting_quote_offsets": [[0, len(assistant)]],
            "drift": "无",
            "severity": "none",
            "recommendation": "保留",
        }
        with self.assert_error(
            "EVENT_EXPERIENCE_REVIEW_CONTRACT_NOT_LOCKED"
        ):
            self.service.record_review(
                base,
                expected_control_revision=2,
                idempotency_key="review-unlocked",
                assistant_text=assistant,
            )
        zero = dict(base)
        zero["supporting_quotes"] = []
        zero["supporting_quote_offsets"] = []
        with self.assert_error("EVENT_EXPERIENCE_FIELD_REQUIRED"):
            self.service.record_review(
                zero,
                expected_control_revision=2,
                idempotency_key="review-zero",
                assistant_text=assistant,
            )
        uppercase = dict(base)
        uppercase["assistant_sha256"] = base["assistant_sha256"].upper()
        with self.assert_error("EVENT_EXPERIENCE_SHA256_REQUIRED"):
            self.service.record_review(
                uppercase,
                expected_control_revision=2,
                idempotency_key="review-uppercase",
                assistant_text=assistant,
            )

    def open_question(
        self,
        *,
        seed_id: str = "seed-1",
        key: str = "question-open",
        ttl_seconds: int = 60,
    ) -> tuple[dict, dict]:
        manifest = self.service.seed_manifest([(seed_id, 1)])
        result = self.service.open_question(
            event_seed_manifest_hash=manifest["event_seed_manifest_hash"],
            seed_references=[(seed_id, 1)],
            question="这条事件结束时，主要体验更偏向哪一条？",
            options=[
                {
                    "option_id": "A",
                    "label": "压迫中看到突破口",
                    "value": {"primary_emotion": "希望"},
                },
                {
                    "option_id": "B",
                    "label": "痛快后意识到代价",
                    "value": {"primary_emotion": "余悸"},
                },
            ],
            recommended_option_id="B",
            rationale="既要局部兑现，也要保留持续压力。",
            expected_control_revision=self.service.get_control_revision(),
            idempotency_key=key,
            ttl_seconds=ttl_seconds,
        )
        return manifest, result

    def test_question_is_bound_to_real_seed_and_has_suppression_flags(self) -> None:
        self.create_seed()
        manifest, opened = self.open_question()
        question = opened["question"]
        self.assertEqual("event_experience", question["phase"])
        self.assertTrue(question["suppress_plot_receipt"])
        self.assertTrue(question["suppress_remote_retrieval"])
        self.assertTrue(question["suppress_stop_proposal"])
        self.assertEqual(
            manifest["event_seed_manifest_hash"],
            question["event_seed_manifest_hash"],
        )
        with self.assert_error(
            "EVENT_EXPERIENCE_QUESTION_MANIFEST_MISMATCH"
        ):
            self.service.open_question(
                event_seed_manifest_hash="0" * 64,
                seed_references=[("seed-1", 1)],
                question="另一个问题？",
                options=[
                    {"option_id": "A", "label": "一", "value": {}},
                    {"option_id": "B", "label": "二", "value": {}},
                ],
                recommended_option_id="A",
                rationale="测试",
                expected_control_revision=2,
                idempotency_key="fake-manifest",
            )

    def test_question_rejects_casefold_option_collision(self) -> None:
        self.create_seed()
        manifest = self.service.seed_manifest([("seed-1", 1)])
        with self.assert_error(
            "EVENT_EXPERIENCE_QUESTION_OPTION_DUPLICATE"
        ):
            self.service.open_question(
                event_seed_manifest_hash=manifest[
                    "event_seed_manifest_hash"
                ],
                seed_references=[("seed-1", 1)],
                question="选择？",
                options=[
                    {"option_id": "A", "label": "一", "value": {}},
                    {"option_id": "a", "label": "二", "value": {}},
                ],
                recommended_option_id="A",
                rationale="测试",
                expected_control_revision=1,
                idempotency_key="casefold-options",
            )

    def test_invalid_answer_repeats_once_then_waits_stably(self) -> None:
        self.create_seed()
        manifest, _ = self.open_question()
        revision = self.service.get_control_revision()
        first = self.service.answer_question(
            manifest["event_seed_manifest_hash"],
            "继续",
            expected_control_revision=revision,
            idempotency_key="answer-invalid-1",
        )
        self.assertEqual("repeat", first["action"])
        self.assertEqual(revision, first["control_revision"])
        second = self.service.answer_question(
            manifest["event_seed_manifest_hash"],
            "下一步",
            expected_control_revision=revision,
            idempotency_key="answer-invalid-2",
        )
        self.assertEqual("awaiting_explicit_choice", second["action"])
        self.assertEqual("AWAITING_EVENT_EXPERIENCE", second["question"]["status"])
        updated_at = second["question"]["updated_at"]
        third = self.service.answer_question(
            manifest["event_seed_manifest_hash"],
            "开始吧",
            expected_control_revision=revision,
            idempotency_key="answer-invalid-3",
        )
        self.assertEqual("awaiting_explicit_choice", third["action"])
        self.assertEqual(updated_at, third["question"]["updated_at"])
        selected = self.service.answer_question(
            manifest["event_seed_manifest_hash"],
            "按推荐答案",
            expected_control_revision=revision,
            idempotency_key="answer-select",
        )
        self.assertEqual("B", selected["selected_option"]["option_id"])
        self.assertEqual(revision + 1, selected["control_revision"])
        same = self.service.answer_question(
            manifest["event_seed_manifest_hash"],
            "B",
            expected_control_revision=selected["control_revision"],
            idempotency_key="answer-same",
        )
        self.assertEqual("already_answered", same["reason"])
        with self.assert_error(
            "EVENT_EXPERIENCE_QUESTION_ALREADY_ANSWERED"
        ):
            self.service.answer_question(
                manifest["event_seed_manifest_hash"],
                "A",
                expected_control_revision=selected["control_revision"],
                idempotency_key="answer-conflict",
            )

    def test_question_cancel_retires_bound_seed_and_arc_candidates(self) -> None:
        self.create_seed()
        self.service.create_arc(
            self.arc(["seed-1"]),
            expected_control_revision=1,
            idempotency_key="cancel-arc",
        )
        manifest, _ = self.open_question(key="cancel-open")
        cancelled = self.service.answer_question(
            manifest["event_seed_manifest_hash"],
            "取消本轮",
            expected_control_revision=3,
            idempotency_key="cancel-answer",
        )
        self.assertEqual("cancelled", cancelled["action"])
        self.assertEqual(1, cancelled["retired_seed_count"])
        self.assertEqual(1, cancelled["retired_arc_count"])
        self.assertEqual("retired", self.service.get_seed("seed-1", 1)["status"])
        self.assertEqual("retired", self.service.get_arc("arc-1", 1)["status"])

    def test_ttl_sweep_is_idempotent_and_noop_does_not_advance_revision(self) -> None:
        self.create_seed()
        manifest, _ = self.open_question()
        before = self.service.get_control_revision()
        now = datetime.now(timezone.utc)
        no_op = self.service.retire_expired_questions(
            expected_control_revision=before,
            idempotency_key="ttl-noop",
            observed_at=(now + timedelta(seconds=30)).isoformat(),
        )
        self.assertEqual(0, no_op["retired_count"])
        self.assertEqual(before, no_op["control_revision"])
        retry = self.service.retire_expired_questions(
            expected_control_revision=before,
            idempotency_key="ttl-noop",
            observed_at=(now + timedelta(seconds=30)).isoformat(),
        )
        self.assertEqual(no_op, retry)

        expired = self.service.retire_expired_questions(
            expected_control_revision=before,
            idempotency_key="ttl-expire",
            observed_at=(now + timedelta(minutes=2)).isoformat(),
        )
        self.assertEqual(1, expired["retired_count"])
        self.assertEqual(1, expired["retired_seed_count"])
        self.assertEqual(before + 1, expired["control_revision"])
        self.assertEqual(
            "RETIRED",
            self.service.get_question(
                manifest["event_seed_manifest_hash"]
            )["status"],
        )

    def test_default_time_noop_retry_uses_stable_idempotency_request(self) -> None:
        first = self.service.retire_expired_questions(
            expected_control_revision=0,
            idempotency_key="ttl-default",
        )
        second = self.service.retire_expired_questions(
            expected_control_revision=0,
            idempotency_key="ttl-default",
        )
        self.assertEqual(first, second)
        self.assertEqual(0, self.service.get_control_revision())

    def test_late_answer_auto_expires_and_retires_candidates(self) -> None:
        self.create_seed()
        manifest, _ = self.open_question()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                """
                UPDATE event_experience_questions
                SET expires_at=?
                WHERE event_seed_manifest_hash=?
                """,
                (
                    (
                        datetime.now(timezone.utc)
                        - timedelta(seconds=1)
                    ).isoformat(),
                    manifest["event_seed_manifest_hash"],
                ),
            )
            connection.commit()
        finally:
            connection.close()
        result = self.service.answer_question(
            manifest["event_seed_manifest_hash"],
            "A",
            expected_control_revision=2,
            idempotency_key="late-answer",
        )
        self.assertEqual("expired", result["action"])
        self.assertEqual(1, result["retired_seed_count"])
        self.assertEqual(3, result["control_revision"])

    def test_control_operations_do_not_modify_foreign_canon_tables(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "CREATE TABLE canon_events(id TEXT PRIMARY KEY, payload TEXT)"
            )
            connection.execute(
                "INSERT INTO canon_events(id, payload) VALUES('canon-1', 'sentinel')"
            )
            connection.commit()
        finally:
            connection.close()
        self.create_locked()
        report = self.service.storage_boundary_report()
        self.assertIn("canon_events", report["foreign_tables"])
        connection = sqlite3.connect(self.database)
        try:
            rows = connection.execute(
                "SELECT id, payload FROM canon_events"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([("canon-1", "sentinel")], rows)
        self.assertTrue(report["boundary_ok"])


if __name__ == "__main__":
    unittest.main()
