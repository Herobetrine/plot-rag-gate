from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scripts.continuity import (
    ContinuityError,
    ContinuityService,
    HostApprovalAuthority,
)
from scripts.continuity.replay import MAX_CORRECTION_DEPTH


class ReplayIntegrityM0Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        runtime = self.root / ".plot-rag"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "config.json").write_text(
            json.dumps(
                {
                    "items": {
                        "strict_runtime_validation": True,
                        "power_binding_bridge": True,
                    },
                    "advantage": {
                        "enabled": False,
                        "shadow": True,
                        "strict_runtime_validation": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        self.service = ContinuityService(self.root)
        self.host = HostApprovalAuthority(
            self.service,
            issuer="replay-integrity-m0",
            channel="interactive_test",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def entity(self, entity_type: str, name: str) -> str:
        return self.service.register_entity(entity_type, name)["entity_id"]

    @staticmethod
    def item_event(
        event_type: str,
        *,
        ordinal: int,
        quote: str,
        **fields: object,
    ) -> dict[str, object]:
        return {
            "schema_version": "plot-rag-delta/v4",
            "event_type": event_type,
            "story_coordinate": {
                "calendar_id": "replay-integrity-calendar",
                "ordinal": ordinal,
            },
            "knowledge_plane": "objective",
            "evidence": {"quote": quote},
            **fields,
        }

    def save(
        self,
        events: list[dict[str, object]],
        *,
        artifact_id: str,
        branch_id: str = "main",
        stage: str = "final",
        chapter_no: int = 1,
        artifact_revision: int | None = None,
    ) -> dict[str, object]:
        return self.service.save_proposal(
            events=events,
            artifact_id=artifact_id,
            artifact_stage=stage,
            branch_id=branch_id,
            chapter_no=chapter_no,
            scene_index=0,
            artifact_revision=artifact_revision,
        )

    def accept(
        self,
        proposal: dict[str, object],
        *,
        operations: tuple[str, ...] = ("accept",),
    ) -> dict[str, object]:
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            str(proposal["proposal_id"]),
            expected_canon_revision=revision,
            operations=operations,
        )
        return self.service.accept_proposal(
            str(proposal["proposal_id"]),
            approval_id=str(grant["approval_id"]),
            expected_canon_revision=revision,
        )

    def accept_events(
        self,
        events: list[dict[str, object]],
        *,
        artifact_id: str,
        branch_id: str = "main",
        stage: str = "final",
        chapter_no: int = 1,
        artifact_revision: int | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        proposal = self.save(
            events,
            artifact_id=artifact_id,
            branch_id=branch_id,
            stage=stage,
            chapter_no=chapter_no,
            artifact_revision=artifact_revision,
        )
        return proposal, self.accept(proposal)

    def seed_item(self, owner: str) -> dict[str, object]:
        _proposal, commit = self.accept_events(
            [
                self.item_event(
                    "item_spec",
                    ordinal=1,
                    quote="定义一件可损伤的测试法器。",
                    action="define",
                    spec_type="item_definition",
                    spec_id="integrity_item_definition",
                    definition={
                        "item_kind": "artifact",
                        "stack_policy": "non_stackable",
                        "uniqueness_policy": "unique_definition",
                        "max_durability": 10,
                        "max_energy": 0,
                    },
                ),
                self.item_event(
                    "item_instance",
                    ordinal=1,
                    quote="法器实例进入世界。",
                    action="instantiate",
                    subject_type="item_instance",
                    subject_id="integrity_item_instance",
                    item_instance_id="integrity_item_instance",
                    item_definition_id="integrity_item_definition",
                    attributes={},
                ),
                self.item_event(
                    "item_custody",
                    ordinal=1,
                    quote="角色取得法器。",
                    action="acquire",
                    subject_type="item_instance",
                    subject_id="integrity_item_instance",
                    item_instance_id="integrity_item_instance",
                    to_legal_owner_entity_id=owner,
                    to_custodian_entity_id=owner,
                    to_carrier_entity_id=owner,
                ),
            ],
            artifact_id="integrity-item-seed",
        )
        return commit

    def test_proposal_and_nested_replacement_branch_must_match(self) -> None:
        actor = self.entity("character", "分支校验角色")
        with self.assertRaises(ContinuityError) as direct:
            self.save(
                [
                    {
                        "event_type": "state",
                        "entity_id": actor,
                        "field": "mood",
                        "value": "calm",
                        "branch_id": "other",
                    }
                ],
                artifact_id="branch-mismatch-direct",
            )
        self.assertEqual(
            "PROPOSAL_EVENT_BRANCH_MISMATCH",
            direct.exception.code,
        )

        with self.assertRaises(ContinuityError) as nested:
            self.save(
                [
                    {
                        "event_type": "correction",
                        "supersedes": ["placeholder-event"],
                        "replacement": {
                            "event_type": "state",
                            "entity_id": actor,
                            "field": "mood",
                            "value": "alert",
                            "branch_id": "other",
                        },
                    }
                ],
                artifact_id="branch-mismatch-nested",
            )
        self.assertEqual(
            "PROPOSAL_EVENT_BRANCH_MISMATCH",
            nested.exception.code,
        )

    def test_correction_cycle_and_depth_fail_with_controlled_errors(self) -> None:
        cyclic: dict[str, object] = {
            "event_type": "correction",
            "supersedes": ["placeholder-event"],
        }
        cyclic["replacement"] = cyclic
        with self.assertRaises(ContinuityError) as cycle:
            self.save([cyclic], artifact_id="correction-cycle")
        self.assertEqual("CORRECTION_REPLACEMENT_CYCLE", cycle.exception.code)

        nested: dict[str, object] = {
            "event_type": "state",
            "entity_id": "placeholder-entity",
            "field": "depth",
            "value": 0,
        }
        for _index in range(MAX_CORRECTION_DEPTH + 1):
            nested = {
                "event_type": "correction",
                "supersedes": ["placeholder-event"],
                "replacement": nested,
            }
        with self.assertRaises(ContinuityError) as depth:
            self.save([nested], artifact_id="correction-depth")
        self.assertEqual(
            "CORRECTION_REPLACEMENT_DEPTH_EXCEEDED",
            depth.exception.code,
        )

    def test_cross_branch_supersede_and_retract_are_rejected(self) -> None:
        actor = self.entity("character", "跨分支角色")
        _original, original_commit = self.accept_events(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "status",
                    "value": "canon",
                }
            ],
            artifact_id="cross-branch-original",
        )
        target = str(original_commit["events"][0]["event_id"])

        for event in (
            {
                "event_type": "correction",
                "supersedes": [target],
                "replacement": {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "status",
                    "value": "alternate",
                },
            },
            {
                "event_type": "retraction",
                "retracts": [target],
            },
        ):
            proposal = self.save(
                [event],
                artifact_id=f"cross-branch-{event['event_type']}",
                branch_id="alternate",
            )
            with self.assertRaises(ContinuityError) as caught:
                self.accept(proposal)
            self.assertEqual(
                "EVENT_LINK_BRANCH_MISMATCH",
                caught.exception.code,
            )

        facts = self.service.query_facts(
            entity_id=actor,
            fact_type="state",
        )["facts"]
        self.assertEqual(["canon"], [fact["value"] for fact in facts])

    def test_alternative_branch_inactive_set_is_local_and_retractable(self) -> None:
        actor = self.entity("character", "备选分支角色")
        self.accept_events(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "canon_status",
                    "value": "stable",
                }
            ],
            artifact_id="branch-local-canon",
        )
        _branch_original, branch_original_commit = self.accept_events(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "discarded_branch_field",
                    "value": "old",
                }
            ],
            artifact_id="branch-local-original",
            branch_id="alternate",
        )
        target = str(branch_original_commit["events"][0]["event_id"])
        branch_correction, _branch_correction_commit = self.accept_events(
            [
                {
                    "event_type": "correction",
                    "supersedes": [target],
                    "replacement": {
                        "event_type": "state",
                        "entity_id": actor,
                        "field": "replacement_branch_field",
                        "value": "new",
                    },
                }
            ],
            artifact_id="branch-local-correction",
            branch_id="alternate",
            chapter_no=2,
        )
        provisional = self.service.query_facts(
            entity_id=actor,
            branch_id="alternate",
            include_provisional=True,
        )["facts"]
        provisional_fields = {
            fact["field"] for fact in provisional if fact["provisional"]
        }
        self.assertEqual(
            {"replacement_branch_field"},
            provisional_fields,
        )
        self.assertEqual(
            ["stable"],
            [
                fact["value"]
                for fact in self.service.query_facts(
                    entity_id=actor,
                    fact_type="state",
                )["facts"]
            ],
        )

        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            str(branch_correction["proposal_id"]),
            expected_canon_revision=revision,
            operations=("retract",),
        )
        self.service.retract_proposal(
            str(branch_correction["proposal_id"]),
            approval_id=str(grant["approval_id"]),
            expected_canon_revision=revision,
            reason="alternate correction withdrawn",
        )
        restored = self.service.query_facts(
            entity_id=actor,
            branch_id="alternate",
            include_provisional=True,
        )["facts"]
        restored_fields = {
            fact["field"] for fact in restored if fact["provisional"]
        }
        self.assertEqual({"discarded_branch_field"}, restored_fields)

    def test_nested_generic_correction_replays_item_leaf_and_retracts(self) -> None:
        owner = self.entity("character", "法器持有人")
        self.seed_item(owner)
        _original, original_commit = self.accept_events(
            [
                self.item_event(
                    "item_runtime",
                    ordinal=2,
                    quote="法器最初被记录为损失四点耐久。",
                    action="damage",
                    subject_type="item_instance",
                    subject_id="integrity_item_instance",
                    item_instance_id="integrity_item_instance",
                    delta={"durability": 4},
                )
            ],
            artifact_id="generic-item-correction-original",
            chapter_no=2,
        )
        target = str(original_commit["events"][0]["event_id"])
        corrected_leaf = self.item_event(
            "item_runtime",
            ordinal=3,
            quote="核验后只损失一点耐久。",
            action="damage",
            subject_type="item_instance",
            subject_id="integrity_item_instance",
            item_instance_id="integrity_item_instance",
            delta={"durability": 1},
        )
        correction, correction_commit = self.accept_events(
            [
                {
                    "event_type": "correction",
                    "supersedes": [target],
                    "replacement": {
                        "event_type": "correction",
                        "supersedes": [target],
                        "replacement": corrected_leaf,
                    },
                }
            ],
            artifact_id="generic-item-correction",
            chapter_no=3,
        )
        self.assertEqual(
            9.0,
            self.service.query_item_runtime("integrity_item_instance")[
                "runtime"
            ]["durability"],
        )
        self.assertEqual(
            correction_commit["item_projection_hash"],
            self.service.replay()["item_projection_hash"],
        )

        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            str(correction["proposal_id"]),
            expected_canon_revision=revision,
            operations=("retract",),
        )
        self.service.retract_proposal(
            str(correction["proposal_id"]),
            approval_id=str(grant["approval_id"]),
            expected_canon_revision=revision,
            reason="nested correction withdrawn",
        )
        self.assertEqual(
            6.0,
            self.service.query_item_runtime("integrity_item_instance")[
                "runtime"
            ]["durability"],
        )

    def test_nested_correction_rejects_unrepresented_inner_links(self) -> None:
        actor = self.entity("character", "内层链接角色")
        _original, original_commit = self.accept_events(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "nested_link_guard",
                    "value": "old",
                }
            ],
            artifact_id="nested-link-original",
        )
        target = str(original_commit["events"][0]["event_id"])
        with self.assertRaises(ContinuityError) as caught:
            self.save(
                [
                    {
                        "event_type": "correction",
                        "supersedes": [target],
                        "replacement": {
                            "event_type": "correction",
                            "supersedes": ["unknown-inner-event"],
                            "replacement": {
                                "event_type": "state",
                                "entity_id": actor,
                                "field": "nested_link_guard",
                                "value": "new",
                            },
                        },
                    }
                ],
                artifact_id="nested-link-mismatch",
                chapter_no=2,
            )
        self.assertEqual(
            "CORRECTION_NESTED_LINK_MISMATCH",
            caught.exception.code,
        )

    def test_generic_correction_can_define_entity_leaf(self) -> None:
        actor = self.entity("character", "实体叶校验角色")
        _original, original_commit = self.accept_events(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "entity_leaf_guard",
                    "value": "placeholder",
                }
            ],
            artifact_id="entity-leaf-original",
        )
        target = str(original_commit["events"][0]["event_id"])
        entity_id = "entity_nested_location"
        _proposal, commit = self.accept_events(
            [
                {
                    "event_type": "correction",
                    "supersedes": [target],
                    "replacement": {
                        "event_type": "entity",
                        "entity_id": entity_id,
                        "entity_type": "location",
                        "canonical_name": "内层新地点",
                        "attributes": {"kind": "test"},
                    },
                }
            ],
            artifact_id="entity-leaf-correction",
            chapter_no=2,
        )
        with self.service.store.read_connection() as connection:
            entity_row = connection.execute(
                """
                SELECT entity_type, canonical_name
                FROM entities
                WHERE entity_id=?
                """,
                (entity_id,),
            ).fetchone()
        self.assertIsNotNone(entity_row)
        self.assertEqual("location", str(entity_row["entity_type"]))
        self.assertEqual("内层新地点", str(entity_row["canonical_name"]))
        facts = self.service.query_facts(
            entity_id=entity_id,
            fact_type="entity",
        )["facts"]
        self.assertEqual(["内层新地点"], [fact["value"]["canonical_name"] for fact in facts])
        self.assertEqual(
            commit["projection_hash"],
            self.service.replay()["projection_hash"],
        )

    def test_item_replay_ignores_accepted_alternative_branch_events(self) -> None:
        owner = self.entity("character", "分支法器持有人")
        seed_commit = self.seed_item(owner)
        _proposal, alternate_commit = self.accept_events(
            [
                self.item_event(
                    "item_runtime",
                    ordinal=2,
                    quote="备选分支中的法器损伤。",
                    action="damage",
                    subject_type="item_instance",
                    subject_id="integrity_item_instance",
                    item_instance_id="integrity_item_instance",
                    delta={"durability": 7},
                )
            ],
            artifact_id="alternate-item-damage",
            branch_id="alternate",
            chapter_no=2,
        )
        self.assertEqual(
            10.0,
            self.service.query_item_runtime("integrity_item_instance")[
                "runtime"
            ]["durability"],
        )
        self.assertEqual(
            seed_commit["item_projection_hash"],
            alternate_commit["item_projection_hash"],
        )
        self.assertEqual(
            alternate_commit["item_projection_hash"],
            self.service.replay()["item_projection_hash"],
        )

    def test_automatic_supersession_retargets_event_restored_by_retraction(
        self,
    ) -> None:
        actor = self.entity("character", "恢复后重定向角色")
        _original, original_commit = self.accept_events(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "auto_supersede_status",
                    "value": "original",
                }
            ],
            artifact_id="auto-supersede-artifact",
            artifact_revision=1,
        )
        original_event_id = str(original_commit["events"][0]["event_id"])

        correction, _correction_commit = self.accept_events(
            [
                {
                    "event_type": "correction",
                    "supersedes": [original_event_id],
                    "replacement": {
                        "event_type": "state",
                        "entity_id": actor,
                        "field": "auto_supersede_status",
                        "value": "temporary",
                    },
                }
            ],
            artifact_id="auto-supersede-artifact",
            artifact_revision=2,
            chapter_no=2,
        )
        self.assertEqual(
            ["temporary"],
            [
                fact["value"]
                for fact in self.service.query_facts(
                    entity_id=actor,
                    fact_type="state",
                )["facts"]
            ],
        )

        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            str(correction["proposal_id"]),
            expected_canon_revision=revision,
            operations=("retract",),
        )
        self.service.retract_proposal(
            str(correction["proposal_id"]),
            approval_id=str(grant["approval_id"]),
            expected_canon_revision=revision,
            reason="restore original before next artifact revision",
        )
        self.assertEqual(
            ["original"],
            [
                fact["value"]
                for fact in self.service.query_facts(
                    entity_id=actor,
                    fact_type="state",
                )["facts"]
            ],
        )

        _next, next_commit = self.accept_events(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "auto_supersede_status",
                    "value": "next",
                }
            ],
            artifact_id="auto-supersede-artifact",
            artifact_revision=3,
            chapter_no=3,
        )
        next_commit_id = str(next_commit["commit_id"])
        with closing(sqlite3.connect(self.service.store.db_path)) as connection:
            auto_link = connection.execute(
                """
                SELECT 1
                FROM event_links
                WHERE source_commit_id=?
                  AND source_event_id IS NULL
                  AND target_event_id=?
                  AND link_type='supersedes'
                """,
                (next_commit_id, original_event_id),
            ).fetchone()
        self.assertIsNotNone(auto_link)
        self.assertEqual(
            ["next"],
            [
                fact["value"]
                for fact in self.service.query_facts(
                    entity_id=actor,
                    fact_type="state",
                )["facts"]
            ],
        )
        self.assertEqual(
            next_commit["projection_hash"],
            self.service.replay()["projection_hash"],
        )

    def test_accepted_payload_and_event_row_tampering_fail_closed(self) -> None:
        actor = self.entity("character", "完整性角色")
        proposal, commit = self.accept_events(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "injury",
                    "value": "none",
                }
            ],
            artifact_id="accepted-payload-integrity",
        )
        original_hash = str(commit["projection_hash"])
        event_id = str(commit["events"][0]["event_id"])
        with closing(sqlite3.connect(self.service.store.db_path)) as connection:
            connection.execute(
                """
                UPDATE continuity_events
                SET payload_json=?
                WHERE event_id=?
                """,
                (
                    json.dumps(
                        {
                            "event_type": "state",
                            "entity_id": actor,
                            "field": "injury",
                            "value": "fatal",
                            "scope": "current",
                            "branch_id": "main",
                            "chapter_no": 1,
                            "scene_index": 0,
                            "story_time": None,
                            "narrative_mode": "linear",
                        }
                    ),
                    event_id,
                ),
            )
            connection.commit()
        with self.assertRaises(ContinuityError) as event_tamper:
            self.service.replay()
        self.assertEqual(
            "ACCEPTED_EVENT_PAYLOAD_MISMATCH",
            event_tamper.exception.code,
        )
        event_tamper.exception.__traceback__ = None
        self.assertEqual(
            ["none"],
            [
                fact["value"]
                for fact in self.service.query_facts(
                    entity_id=actor,
                    fact_type="state",
                )["facts"]
            ],
        )
        self.assertEqual(original_hash, self.service.projection_hash())

        with closing(sqlite3.connect(self.service.store.db_path)) as connection:
            connection.execute(
                """
                UPDATE continuity_events
                SET payload_json=(SELECT events_json FROM proposals
                                  WHERE proposal_id=?)
                WHERE event_id=?
                """,
                (str(proposal["proposal_id"]), event_id),
            )
            connection.commit()
        # Restore the row precisely; events_json is an array, so extract item 0.
        with closing(sqlite3.connect(self.service.store.db_path)) as connection:
            events_json = connection.execute(
                "SELECT events_json FROM proposals WHERE proposal_id=?",
                (str(proposal["proposal_id"]),),
            ).fetchone()[0]
            connection.execute(
                "UPDATE continuity_events SET payload_json=? WHERE event_id=?",
                (json.dumps(json.loads(events_json)[0]), event_id),
            )
            connection.execute(
                "UPDATE proposals SET events_json='[]' WHERE proposal_id=?",
                (str(proposal["proposal_id"]),),
            )
            connection.commit()
        with self.assertRaises(ContinuityError) as proposal_tamper:
            self.service.replay()
        self.assertEqual(
            "ACCEPTED_PAYLOAD_HASH_MISMATCH",
            proposal_tamper.exception.code,
        )
        proposal_tamper.exception.__traceback__ = None

    def test_json_type_tampering_fails_closed_for_payload_and_evidence(
        self,
    ) -> None:
        actor = self.entity("character", "JSON 类型完整性角色")
        proposal, commit = self.accept_events(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "json_type_guard",
                    "value": False,
                    "evidence": {
                        "quote": "布尔值证据。",
                        "verified": False,
                    },
                }
            ],
            artifact_id="json-type-integrity",
        )
        event_id = str(commit["events"][0]["event_id"])
        with closing(sqlite3.connect(self.service.store.db_path)) as connection:
            original_payload = json.loads(
                connection.execute(
                    """
                    SELECT payload_json
                    FROM continuity_events
                    WHERE event_id=?
                    """,
                    (event_id,),
                ).fetchone()[0]
            )
            tampered_payload = dict(original_payload)
            tampered_payload["value"] = 0
            connection.execute(
                "UPDATE continuity_events SET payload_json=? WHERE event_id=?",
                (json.dumps(tampered_payload), event_id),
            )
            connection.commit()
        with self.assertRaises(ContinuityError) as payload_tamper:
            self.service.replay()
        self.assertEqual(
            "ACCEPTED_EVENT_PAYLOAD_MISMATCH",
            payload_tamper.exception.code,
        )

        with closing(sqlite3.connect(self.service.store.db_path)) as connection:
            frozen_event = json.loads(
                connection.execute(
                    "SELECT events_json FROM proposals WHERE proposal_id=?",
                    (str(proposal["proposal_id"]),),
                ).fetchone()[0]
            )[0]
            connection.execute(
                "UPDATE continuity_events SET payload_json=? WHERE event_id=?",
                (json.dumps(frozen_event), event_id),
            )
            evidence = json.loads(
                connection.execute(
                    """
                    SELECT evidence_json
                    FROM continuity_events
                    WHERE event_id=?
                    """,
                    (event_id,),
                ).fetchone()[0]
            )
            evidence["verified"] = 0
            connection.execute(
                "UPDATE continuity_events SET evidence_json=? WHERE event_id=?",
                (json.dumps(evidence), event_id),
            )
            connection.commit()
        with self.assertRaises(ContinuityError) as evidence_tamper:
            self.service.replay()
        self.assertEqual(
            "ACCEPTED_EVENT_EVIDENCE_MISMATCH",
            evidence_tamper.exception.code,
        )

    def test_deterministic_event_id_tampering_fails_closed(self) -> None:
        actor = self.entity("character", "事件身份完整性角色")
        _proposal, commit = self.accept_events(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "event_id_guard",
                    "value": "stable",
                }
            ],
            artifact_id="event-id-integrity",
        )
        original_hash = self.service.projection_hash()
        original_event_id = str(commit["events"][0]["event_id"])
        with closing(sqlite3.connect(self.service.store.db_path)) as connection:
            connection.execute(
                """
                UPDATE continuity_events
                SET event_id='tampered_event_id'
                WHERE event_id=?
                """,
                (original_event_id,),
            )
            connection.commit()
        with self.assertRaises(ContinuityError) as event_id_tamper:
            self.service.replay()
        self.assertEqual(
            "ACCEPTED_EVENT_ID_MISMATCH",
            event_id_tamper.exception.code,
        )
        self.assertEqual(original_hash, self.service.projection_hash())

    def test_proposal_status_and_orphan_commit_tampering_fail_closed(self) -> None:
        actor = self.entity("character", "提案反向完整性角色")
        proposal, commit = self.accept_events(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "reverse_guard",
                    "value": "accepted",
                }
            ],
            artifact_id="reverse-ledger-integrity",
        )
        original_hash = self.service.projection_hash()
        with closing(sqlite3.connect(self.service.store.db_path)) as connection:
            connection.execute(
                "UPDATE proposals SET canon_status='retracted' WHERE proposal_id=?",
                (str(proposal["proposal_id"]),),
            )
            connection.commit()
        with self.assertRaises(ContinuityError) as status_tamper:
            self.service.replay()
        self.assertEqual(
            "ACCEPTED_PROPOSAL_STATUS_MISMATCH",
            status_tamper.exception.code,
        )
        self.assertEqual(original_hash, self.service.projection_hash())

        event_id = str(commit["events"][0]["event_id"])
        with closing(sqlite3.connect(self.service.store.db_path)) as connection:
            connection.execute(
                "DELETE FROM event_links WHERE source_commit_id=?",
                (str(commit["commit_id"]),),
            )
            connection.execute(
                "DELETE FROM continuity_events WHERE event_id=?",
                (event_id,),
            )
            connection.execute(
                "DELETE FROM canon_commits WHERE commit_id=?",
                (str(commit["commit_id"]),),
            )
            connection.commit()
        with self.assertRaises(ContinuityError) as orphan_tamper:
            self.service.replay()
        self.assertEqual(
            "ACCEPTED_PROPOSAL_COMMIT_MISSING",
            orphan_tamper.exception.code,
        )
        self.assertEqual(original_hash, self.service.projection_hash())

    def test_state_meta_revision_tampering_fails_closed(self) -> None:
        actor = self.entity("character", "版本元数据角色")
        _proposal, _commit = self.accept_events(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "revision_guard",
                    "value": "stable",
                }
            ],
            artifact_id="revision-meta-integrity",
        )
        original_hash = self.service.projection_hash()
        with closing(sqlite3.connect(self.service.store.db_path)) as connection:
            connection.execute(
                """
                UPDATE state_meta
                SET value='999'
                WHERE key='head_canon_revision'
                """
            )
            connection.commit()
        with self.assertRaises(ContinuityError) as meta_tamper:
            self.service.replay()
        self.assertEqual(
            "ACCEPTED_REVISION_META_MISMATCH",
            meta_tamper.exception.code,
        )
        self.assertEqual(original_hash, self.service.projection_hash())

    def test_revision_ordinal_and_authority_types_fail_closed(self) -> None:
        actor = self.entity("character", "数值类型完整性角色")
        _proposal, commit = self.accept_events(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "numeric_type_guard",
                    "value": "stable",
                }
            ],
            artifact_id="numeric-type-integrity",
        )
        commit_id = str(commit["commit_id"])
        event_id = str(commit["events"][0]["event_id"])

        with closing(sqlite3.connect(self.service.store.db_path)) as connection:
            connection.execute(
                """
                UPDATE canon_commits
                SET head_revision_after=1.5
                WHERE commit_id=?
                """,
                (commit_id,),
            )
            connection.commit()
        with self.assertRaises(ContinuityError) as revision_tamper:
            self.service.replay()
        self.assertEqual(
            "ACCEPTED_COMMIT_NUMERIC_CORRUPT",
            revision_tamper.exception.code,
        )

        with closing(sqlite3.connect(self.service.store.db_path)) as connection:
            connection.execute(
                """
                UPDATE canon_commits
                SET head_revision_after=1
                WHERE commit_id=?
                """,
                (commit_id,),
            )
            connection.execute(
                """
                UPDATE continuity_events
                SET event_ordinal=0.5
                WHERE event_id=?
                """,
                (event_id,),
            )
            connection.commit()
        with self.assertRaises(ContinuityError) as ordinal_tamper:
            self.service.replay()
        self.assertEqual(
            "ACCEPTED_EVENT_NUMERIC_CORRUPT",
            ordinal_tamper.exception.code,
        )

        with closing(sqlite3.connect(self.service.store.db_path)) as connection:
            connection.execute(
                """
                UPDATE continuity_events
                SET event_ordinal=0
                WHERE event_id=?
                """,
                (event_id,),
            )
            connection.execute(
                """
                UPDATE canon_commits
                SET changes_authority=2
                WHERE commit_id=?
                """,
                (commit_id,),
            )
            connection.commit()
        with self.assertRaises(ContinuityError) as authority_tamper:
            self.service.replay()
        self.assertEqual(
            "ACCEPTED_COMMIT_AUTHORITY_MISMATCH",
            authority_tamper.exception.code,
        )

        with closing(sqlite3.connect(self.service.store.db_path)) as connection:
            connection.execute(
                """
                UPDATE canon_commits
                SET changes_authority=1, artifact_revision=0
                WHERE commit_id=?
                """,
                (commit_id,),
            )
            connection.commit()
        with self.assertRaises(ContinuityError) as artifact_range:
            self.service.replay()
        self.assertEqual(
            "ACCEPTED_COMMIT_NUMERIC_CORRUPT",
            artifact_range.exception.code,
        )

        with closing(sqlite3.connect(self.service.store.db_path)) as connection:
            connection.execute(
                """
                UPDATE canon_commits
                SET artifact_revision=1
                WHERE commit_id=?
                """,
                (commit_id,),
            )
            connection.execute(
                """
                UPDATE continuity_events
                SET event_ordinal=-1
                WHERE event_id=?
                """,
                (event_id,),
            )
            connection.commit()
        with self.assertRaises(ContinuityError) as ordinal_range:
            self.service.replay()
        self.assertEqual(
            "ACCEPTED_EVENT_NUMERIC_CORRUPT",
            ordinal_range.exception.code,
        )

    def test_missing_explicit_link_fails_before_projection_rebuild(self) -> None:
        actor = self.entity("character", "链接完整性角色")
        _original, original_commit = self.accept_events(
            [
                {
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "knowledge",
                    "value": False,
                }
            ],
            artifact_id="link-integrity-original",
        )
        target = str(original_commit["events"][0]["event_id"])
        _correction, correction_commit = self.accept_events(
            [
                {
                    "event_type": "correction",
                    "supersedes": [target],
                    "replacement": {
                        "event_type": "state",
                        "entity_id": actor,
                        "field": "knowledge",
                        "value": True,
                    },
                }
            ],
            artifact_id="link-integrity-correction",
            chapter_no=2,
        )
        source = str(correction_commit["events"][0]["event_id"])
        with closing(sqlite3.connect(self.service.store.db_path)) as connection:
            connection.execute(
                """
                DELETE FROM event_links
                WHERE source_event_id=? AND target_event_id=?
                  AND link_type='supersedes'
                """,
                (source, target),
            )
            connection.commit()
        with self.assertRaises(ContinuityError) as caught:
            self.service.replay()
        self.assertEqual("ACCEPTED_LINK_SET_INCOMPLETE", caught.exception.code)
        caught.exception.__traceback__ = None
        self.assertEqual(
            [True],
            [
                fact["value"]
                for fact in self.service.query_facts(
                    entity_id=actor,
                    fact_type="state",
                )["facts"]
            ],
        )


if __name__ == "__main__":
    unittest.main()
