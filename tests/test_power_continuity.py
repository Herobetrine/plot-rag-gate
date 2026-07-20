from __future__ import annotations

import hashlib
import json
import os
import shutil
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
from scripts.continuity.store import ContinuityStore, StoreError


class PowerContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.service = ContinuityService(self.root)
        self.host = HostApprovalAuthority(
            self.service,
            issuer="power-unittest-host",
            channel="interactive_test",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def entity(self, entity_type: str, name: str) -> str:
        return self.service.register_entity(entity_type, name)["entity_id"]

    def proposal(
        self,
        events,
        *,
        artifact_id: str,
        chapter: int | None = 1,
        stage: str = "final",
        proposal_kind: str = "story_delta",
        revision: int | None = None,
    ):
        return self.service.save_proposal(
            events=events,
            artifact_id=artifact_id,
            artifact_stage=stage,
            branch_id="main",
            chapter_no=chapter,
            scene_index=0 if chapter is not None else None,
            proposal_kind=proposal_kind,
            artifact_revision=revision,
        )

    def accept(self, proposal, *, operation: str = "accept"):
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            proposal["proposal_id"],
            expected_canon_revision=revision,
            operations=(operation,),
        )
        return self.service.accept_proposal(
            proposal["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=revision,
        )

    @staticmethod
    def coordinate(ordinal: int) -> dict[str, object]:
        return {
            "calendar_id": "project-main",
            "ordinal": ordinal,
            "label": f"T{ordinal}",
            "precision": "tick",
        }

    def test_ability_ownership_runtime_and_cross_commit_loss(self) -> None:
        actor = self.entity("character", "主角")
        ability = self.entity("ability", "焚风")
        gain = self.proposal(
            [
                {
                    "event_type": "ability",
                    "owner_entity_id": actor,
                    "ability_entity_id": ability,
                    "action": "gain",
                    "state": {
                        "level": 3,
                        "cost": "灵力",
                        "limits": ["需结印"],
                        "action": "lose",
                    },
                }
            ],
            artifact_id="ability-gain",
            chapter=1,
        )
        self.accept(gain)
        use = self.proposal(
            [
                {
                    "event_type": "ability",
                    "owner_entity_id": actor,
                    "ability_entity_id": ability,
                    "action": "use",
                    "story_coordinate": self.coordinate(10),
                    "state": {"effect": "点燃目标"},
                }
            ],
            artifact_id="ability-use",
            chapter=2,
        )
        self.accept(use)

        queried = self.service.query_power_state(
            actor, ability_entity_id=ability
        )
        self.assertEqual(len(queried["abilities"]), 1)
        projected = queried["abilities"][0]
        self.assertTrue(projected["acquired"])
        self.assertEqual(projected["ownership"]["level"], 3)
        self.assertEqual(projected["ownership"]["cost"], "灵力")
        self.assertEqual(projected["ownership"]["limits"], ["需结印"])
        self.assertNotIn("action", projected["ownership"].get("state", {}))
        self.assertEqual(projected["runtime"]["use_count"], 1)
        self.assertEqual(
            [row["action"] for row in queried["ability_history"]],
            ["gain", "use"],
        )

        lose = self.proposal(
            [
                {
                    "event_type": "ability",
                    "owner_entity_id": actor,
                    "ability_entity_id": ability,
                    "action": "lose",
                    "story_coordinate": self.coordinate(20),
                }
            ],
            artifact_id="ability-lose",
            chapter=3,
        )
        self.accept(lose)
        with self.service.store.read_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ability_state"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT acquired FROM actor_ability_state"
                ).fetchone()[0],
                0,
            )

        invalid = self.proposal(
            [
                {
                    "event_type": "ability",
                    "owner_entity_id": actor,
                    "ability_entity_id": ability,
                    "action": "use",
                    "story_coordinate": self.coordinate(21),
                }
            ],
            artifact_id="ability-use-after-loss",
            chapter=4,
        )
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            invalid["proposal_id"],
            expected_canon_revision=revision,
        )
        with self.assertRaises(ContinuityError) as caught:
            self.service.accept_proposal(
                invalid["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=revision,
            )
        self.assertEqual(caught.exception.code, "POWER_ABILITY_NOT_ACQUIRED")
        self.assertEqual(
            self.service.get_canon_revisions()["active"], revision
        )

    def test_ability_use_count_is_runtime_owned(self) -> None:
        actor = self.entity("character", "施法者")
        ability = self.entity("ability", "连发术")
        for injected in (0, False, 1.0, "1"):
            with self.subTest(injected=repr(injected)):
                with self.assertRaises(ContinuityError) as caught:
                    self.proposal(
                        [
                            {
                                "event_type": "ability",
                                "owner_entity_id": actor,
                                "ability_entity_id": ability,
                                "action": "gain",
                                "state": {"use_count": injected},
                            }
                        ],
                        artifact_id=f"use-count-injection-{injected!r}",
                    )
                self.assertEqual("INVALID_FIELD", caught.exception.code)

        gain = self.proposal(
            [
                {
                    "event_type": "ability",
                    "owner_entity_id": actor,
                    "ability_entity_id": ability,
                    "action": "gain",
                }
            ],
            artifact_id="runtime-use-count-gain",
        )
        self.accept(gain)
        for ordinal in (10, 20):
            use = self.proposal(
                [
                    {
                        "event_type": "ability",
                        "owner_entity_id": actor,
                        "ability_entity_id": ability,
                        "action": "use",
                        "story_coordinate": self.coordinate(ordinal),
                    }
                ],
                artifact_id=f"runtime-use-count-{ordinal}",
            )
            self.accept(use)

        before = self.service.query_power_state(
            actor,
            ability_entity_id=ability,
        )
        self.assertEqual(2, before["abilities"][0]["runtime"]["use_count"])
        self.service.replay()
        after = self.service.query_power_state(
            actor,
            ability_entity_id=ability,
        )
        self.assertEqual(2, after["abilities"][0]["runtime"]["use_count"])

    def test_power_prerequisite_story_coordinate_requires_exact_integer(
        self,
    ) -> None:
        actor = self.entity("character", "守门人")
        ability = self.entity("ability", "月门")
        for invalid_ordinal in (False, 1.0, 1.5, "1"):
            with self.subTest(
                source="definition",
                ordinal=repr(invalid_ordinal),
            ):
                with self.assertRaises(ContinuityError) as caught:
                    self.proposal(
                        [
                            {
                                "event_type": "power_spec",
                                "action": "define",
                                "spec_type": "ability_definition",
                                "spec_entity_id": ability,
                                "definition": {
                                    "prerequisites": {
                                        "minimum_story_coordinate": {
                                            "calendar_id": "project-main",
                                            "ordinal": invalid_ordinal,
                                        }
                                    }
                                },
                            }
                        ],
                        artifact_id=(
                            f"invalid-definition-coordinate-"
                            f"{invalid_ordinal!r}"
                        ),
                        proposal_kind="power_spec_change",
                    )
                self.assertEqual(
                    "POWER_STORY_COORDINATE_UNKNOWN",
                    caught.exception.code,
                )
            with self.subTest(
                source="event",
                ordinal=repr(invalid_ordinal),
            ):
                with self.assertRaises(ContinuityError) as caught:
                    self.proposal(
                        [
                            {
                                "event_type": "ability",
                                "owner_entity_id": actor,
                                "ability_entity_id": ability,
                                "action": "use",
                                "story_coordinate": self.coordinate(10),
                                "prerequisites": {
                                    "minimum_story_coordinate": {
                                        "calendar_id": "project-main",
                                        "ordinal": invalid_ordinal,
                                    }
                                },
                            }
                        ],
                        artifact_id=(
                            f"invalid-event-coordinate-"
                            f"{invalid_ordinal!r}"
                        ),
                    )
                self.assertEqual(
                    "POWER_STORY_COORDINATE_UNKNOWN",
                    caught.exception.code,
                )

        specification = self.proposal(
            [
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "ability_definition",
                    "spec_entity_id": ability,
                    "definition": {
                        "prerequisites": {
                            "minimum_story_coordinate": self.coordinate(10)
                        }
                    },
                }
            ],
            artifact_id="valid-minimum-story-coordinate",
            proposal_kind="power_spec_change",
        )
        self.accept(specification, operation="accept_power_spec")
        gain = self.proposal(
            [
                {
                    "event_type": "ability",
                    "owner_entity_id": actor,
                    "ability_entity_id": ability,
                    "action": "gain",
                }
            ],
            artifact_id="minimum-coordinate-gain",
        )
        self.accept(gain)
        before = self.service.explain_power_action(
            actor,
            ability_id=ability,
            story_coordinate=self.coordinate(9),
        )
        at_gate = self.service.explain_power_action(
            actor,
            ability_id=ability,
            story_coordinate=self.coordinate(10),
        )
        self.assertIn(
            "POWER_CONTEXT_CONDITION_UNMET",
            {reason["code"] for reason in before["reasons"]},
        )
        self.assertTrue(at_gate["executable"])

    def test_lose_before_gain_and_breakthrough_do_not_create_ownership(self) -> None:
        actor = self.entity("character", "主角")
        ability = self.entity("ability", "未学秘法")
        for index, action in enumerate(("lose", "breakthrough"), start=1):
            with self.subTest(action=action):
                proposal = self.proposal(
                    [
                        {
                            "event_type": "ability",
                            "owner_entity_id": actor,
                            "ability_entity_id": ability,
                            "action": action,
                        }
                    ],
                    artifact_id=f"ability-invalid-{index}",
                    chapter=index,
                )
                revision = self.service.get_canon_revisions()["active"]
                grant = self.host.issue(
                    proposal["proposal_id"],
                    expected_canon_revision=revision,
                )
                with self.assertRaises(ContinuityError) as caught:
                    self.service.accept_proposal(
                        proposal["proposal_id"],
                        approval_id=grant["approval_id"],
                        expected_canon_revision=revision,
                    )
                self.assertEqual(
                    caught.exception.code, "POWER_ABILITY_NOT_ACQUIRED"
                )

    def test_power_endpoint_entity_types_fail_closed(self) -> None:
        actor = self.entity("character", "主角")
        location = self.entity("location", "城门")
        ability = self.entity("ability", "火球")
        invalid_events = (
            {
                "event_type": "ability",
                "owner_entity_id": location,
                "ability_entity_id": ability,
                "action": "gain",
            },
            {
                "event_type": "ability",
                "owner_entity_id": actor,
                "ability_entity_id": actor,
                "action": "gain",
            },
        )
        for index, event in enumerate(invalid_events, start=1):
            proposal = self.proposal(
                [event],
                artifact_id=f"endpoint-invalid-{index}",
                chapter=index,
            )
            revision = self.service.get_canon_revisions()["active"]
            grant = self.host.issue(
                proposal["proposal_id"],
                expected_canon_revision=revision,
            )
            with self.assertRaises(ContinuityError) as caught:
                self.service.accept_proposal(
                    proposal["proposal_id"],
                    approval_id=grant["approval_id"],
                    expected_canon_revision=revision,
                )
            self.assertEqual(
                caught.exception.code, "POWER_ENTITY_TYPE_MISMATCH"
            )

    def test_power_spec_requires_isolated_proposal_and_dedicated_grant(self) -> None:
        system = self.entity("power_system", "修仙")
        track = self.entity("progression_track", "修为")
        rank_a = self.entity("rank_node", "练气")
        rank_b = self.entity("rank_node", "筑基")
        edge = self.entity("rank_edge", "练气至筑基")
        events = [
            {
                "event_type": "power_spec",
                "action": "define",
                "spec_type": "power_system",
                "spec_entity_id": system,
                "definition": {"profile": "cultivation"},
            },
            {
                "event_type": "power_spec",
                "action": "define",
                "spec_type": "progression_track",
                "spec_entity_id": track,
                "definition": {
                    "system_entity_id": system,
                    "track_kind": "ordered_rank",
                },
            },
            {
                "event_type": "power_spec",
                "action": "define",
                "spec_type": "rank_node",
                "spec_entity_id": rank_a,
                "definition": {"track_entity_id": track},
            },
            {
                "event_type": "power_spec",
                "action": "define",
                "spec_type": "rank_node",
                "spec_entity_id": rank_b,
                "definition": {"track_entity_id": track},
            },
            {
                "event_type": "power_spec",
                "action": "define",
                "spec_type": "rank_edge",
                "spec_entity_id": edge,
                "definition": {
                    "track_entity_id": track,
                    "from_rank_entity_ids": [rank_a],
                    "to_rank_entity_id": rank_b,
                },
            },
        ]
        with self.assertRaises(ContinuityError) as mixed:
            self.proposal(
                [
                    *events,
                    {
                        "event_type": "time",
                        "field": "day",
                        "value": 1,
                    },
                ],
                artifact_id="mixed-spec",
                chapter=None,
                stage="bootstrap",
                proposal_kind="power_spec_change",
            )
        self.assertEqual(
            mixed.exception.code, "POWER_SPEC_PROPOSAL_REQUIRED"
        )

        proposal = self.proposal(
            events,
            artifact_id="cultivation-spec",
            chapter=None,
            stage="bootstrap",
            proposal_kind="power_spec_change",
        )
        with self.assertRaises(ContinuityError) as wrong_grant:
            self.host.issue(
                proposal["proposal_id"],
                expected_canon_revision=0,
                operations=("accept",),
            )
        self.assertEqual(
            wrong_grant.exception.code,
            "APPROVAL_OPERATION_SCOPE_MISMATCH",
        )
        self.accept(proposal, operation="accept_power_spec")
        systems = self.service.list_power_systems()
        self.assertEqual(systems["systems"][0]["system_entity_id"], system)
        self.assertEqual(
            systems["systems"][0]["tracks"][0]["track_entity_id"], track
        )
        with self.service.store.read_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM rank_edges"
                ).fetchone()[0],
                1,
            )

    def test_progression_resource_cost_and_cooldown_story_time(self) -> None:
        actor = self.entity("character", "主角")
        system = self.entity("power_system", "法术")
        track = self.entity("progression_track", "法师等级")
        rank_a = self.entity("rank_node", "一环")
        rank_b = self.entity("rank_node", "二环")
        edge = self.entity("rank_edge", "一环至二环")
        mana = self.entity("resource_pool", "法力")
        ability = self.entity("ability", "火球术")
        spec = self.proposal(
            [
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "power_system",
                    "spec_entity_id": system,
                    "definition": {"profile": "magic"},
                },
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "progression_track",
                    "spec_entity_id": track,
                    "definition": {
                        "system_entity_id": system,
                        "track_kind": "ordered_rank",
                    },
                },
                *[
                    {
                        "event_type": "power_spec",
                        "action": "define",
                        "spec_type": "rank_node",
                        "spec_entity_id": rank,
                        "definition": {"track_entity_id": track},
                    }
                    for rank in (rank_a, rank_b)
                ],
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "resource_definition",
                    "spec_entity_id": mana,
                    "definition": {
                        "system_entity_id": system,
                        "maximum_balance": 100,
                    },
                },
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "ability_definition",
                    "spec_entity_id": ability,
                    "definition": {
                        "system_entity_id": system,
                        "resource_costs": [
                            {"resource_entity_id": mana, "amount": 5}
                        ],
                    },
                },
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "rank_edge",
                    "spec_entity_id": edge,
                    "definition": {
                        "track_entity_id": track,
                        "from_rank_entity_ids": [rank_a],
                        "to_rank_entity_id": rank_b,
                        "resource_costs": [
                            {"resource_entity_id": mana, "amount": 10}
                        ],
                    },
                },
            ],
            artifact_id="magic-spec",
            chapter=None,
            stage="bootstrap",
            proposal_kind="power_spec_change",
        )
        self.accept(spec, operation="accept_power_spec")
        bootstrap = self.proposal(
            [
                {
                    "event_type": "progression",
                    "actor_entity_id": actor,
                    "track_entity_id": track,
                    "action": "initialize",
                    "to_rank_entity_id": rank_a,
                },
                {
                    "event_type": "resource",
                    "actor_entity_id": actor,
                    "resource_entity_id": mana,
                    "action": "initialize",
                    "amount": 30,
                },
                {
                    "event_type": "ability",
                    "owner_entity_id": actor,
                    "ability_entity_id": ability,
                    "action": "gain",
                },
            ],
            artifact_id="magic-bootstrap",
            chapter=1,
        )
        self.accept(bootstrap)

        missing_cost = self.proposal(
            [
                {
                    "event_type": "progression",
                    "actor_entity_id": actor,
                    "track_entity_id": track,
                    "action": "advance",
                    "from_rank_entity_id": rank_a,
                    "to_rank_entity_id": rank_b,
                    "rank_edge_entity_id": edge,
                }
            ],
            artifact_id="advance-no-cost",
            chapter=2,
        )
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            missing_cost["proposal_id"],
            expected_canon_revision=revision,
        )
        with self.assertRaises(ContinuityError) as caught:
            self.service.accept_proposal(
                missing_cost["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=revision,
            )
        self.assertEqual(caught.exception.code, "POWER_COST_NOT_APPLIED")

        advance = self.proposal(
            [
                {
                    "event_type": "progression",
                    "actor_entity_id": actor,
                    "track_entity_id": track,
                    "action": "advance",
                    "from_rank_entity_id": rank_a,
                    "to_rank_entity_id": rank_b,
                    "rank_edge_entity_id": edge,
                },
                {
                    "event_type": "resource",
                    "actor_entity_id": actor,
                    "resource_entity_id": mana,
                    "action": "spend",
                    "amount": 10,
                },
            ],
            artifact_id="advance-with-cost",
            chapter=2,
        )
        self.accept(advance)
        path = self.service.query_progression_path(
            actor,
            track_entity_id=track,
            target_rank_entity_id=rank_b,
        )
        self.assertEqual(
            path["tracks"][0]["current_rank_entity_id"], rank_b
        )

        use = self.proposal(
            [
                {
                    "event_type": "ability",
                    "owner_entity_id": actor,
                    "ability_entity_id": ability,
                    "action": "use",
                    "story_coordinate": self.coordinate(100),
                    "cooldown_until": self.coordinate(110),
                },
                {
                    "event_type": "resource",
                    "actor_entity_id": actor,
                    "resource_entity_id": mana,
                    "action": "spend",
                    "amount": 5,
                },
            ],
            artifact_id="cast-fireball",
            chapter=3,
        )
        self.accept(use)
        blocked = self.proposal(
            [
                {
                    "event_type": "ability",
                    "owner_entity_id": actor,
                    "ability_entity_id": ability,
                    "action": "use",
                    "story_coordinate": self.coordinate(105),
                },
                {
                    "event_type": "resource",
                    "actor_entity_id": actor,
                    "resource_entity_id": mana,
                    "action": "spend",
                    "amount": 5,
                },
            ],
            artifact_id="cast-during-cooldown",
            chapter=4,
        )
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            blocked["proposal_id"],
            expected_canon_revision=revision,
        )
        with self.assertRaises(ContinuityError) as cooldown:
            self.service.accept_proposal(
                blocked["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=revision,
            )
        self.assertEqual(cooldown.exception.code, "POWER_COOLDOWN_ACTIVE")
        explanation = self.service.explain_power_action(
            actor,
            ability_id=ability,
            story_coordinate=self.coordinate(105),
        )
        self.assertFalse(explanation["executable"])
        self.assertIn(
            "POWER_COOLDOWN_ACTIVE",
            {reason["code"] for reason in explanation["reasons"]},
        )

    def test_resource_conversion_arbitrage_and_source_rules(self) -> None:
        system = self.entity("power_system", "系统")
        resource_a = self.entity("resource_pool", "A")
        resource_b = self.entity("resource_pool", "B")
        rule_ab = self.entity("conversion_rule", "A-B")
        rule_ba = self.entity("conversion_rule", "B-A")
        proposal = self.proposal(
            [
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "conversion_rule",
                    "spec_entity_id": rule_ab,
                    "definition": {
                        "source_system_entity_id": system,
                        "target_system_entity_id": system,
                        "source_resource_entity_id": resource_a,
                        "target_resource_entity_id": resource_b,
                        "ratio": 2,
                    },
                },
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "conversion_rule",
                    "spec_entity_id": rule_ba,
                    "definition": {
                        "source_system_entity_id": system,
                        "target_system_entity_id": system,
                        "source_resource_entity_id": resource_b,
                        "target_resource_entity_id": resource_a,
                        "ratio": 1,
                    },
                },
            ],
            artifact_id="arbitrage-spec",
            chapter=None,
            stage="bootstrap",
            proposal_kind="power_spec_change",
        )
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            proposal["proposal_id"],
            expected_canon_revision=revision,
            operations=("accept_power_spec",),
        )
        with self.assertRaises(ContinuityError) as caught:
            self.service.accept_proposal(
                proposal["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=revision,
            )
        self.assertEqual(
            caught.exception.code, "POWER_CONVERSION_ARBITRAGE"
        )

        actor = self.entity("character", "主角")
        no_source = self.proposal(
            [
                {
                    "event_type": "resource",
                    "actor_entity_id": actor,
                    "resource_entity_id": resource_a,
                    "action": "gain",
                    "amount": 1,
                }
            ],
            artifact_id="resource-no-source",
            chapter=1,
        )
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            no_source["proposal_id"],
            expected_canon_revision=revision,
        )
        with self.assertRaises(ContinuityError) as source:
            self.service.accept_proposal(
                no_source["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=revision,
            )
        self.assertEqual(
            source.exception.code, "POWER_RESOURCE_SOURCE_REQUIRED"
        )

    def test_status_binding_qualification_and_observation_projections(self) -> None:
        actor = self.entity("character", "主角")
        source = self.entity("item", "法杖")
        ability = self.entity("ability", "法杖施法")
        status = self.entity("status_effect", "专注")
        qualification = self.entity("qualification", "二环法术位")
        definition = self.proposal(
            [
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "qualification_definition",
                    "spec_entity_id": qualification,
                    "definition": {
                        "qualification_kind": "spell_slot",
                        "consumable": True,
                    },
                }
            ],
            artifact_id="qualification-definition",
            chapter=None,
            stage="bootstrap",
            proposal_kind="power_spec_change",
        )
        self.accept(definition, operation="accept_power_spec")
        proposal = self.proposal(
            [
                {
                    "event_type": "status_effect",
                    "actor_entity_id": actor,
                    "status_entity_id": status,
                    "action": "apply",
                    "stacks": 1,
                    "story_coordinate": self.coordinate(1),
                    "expires_coordinate": self.coordinate(5),
                },
                {
                    "event_type": "power_binding",
                    "actor_entity_id": actor,
                    "binding_id": "main-wand",
                    "source_entity_id": source,
                    "action": "equip",
                    "ability_entity_ids": [ability],
                    "slot_key": "main-hand",
                    "unique": True,
                },
                {
                    "event_type": "qualification",
                    "actor_entity_id": actor,
                    "qualification_entity_id": qualification,
                    "action": "grant",
                    "quantity": 2,
                    "story_coordinate": self.coordinate(1),
                    "expires_coordinate": self.coordinate(10),
                },
                {
                    "event_type": "power_observation",
                    "observer_entity_id": actor,
                    "subject_entity_id": actor,
                    "ability_entity_id": ability,
                    "action": "observe",
                    "knowledge_plane": "actor_belief",
                    "observed_fields": {"effect": "发出火光"},
                    "confidence": 0.8,
                },
                {
                    "event_type": "power_observation",
                    "observer_entity_id": actor,
                    "subject_entity_id": actor,
                    "ability_entity_id": ability,
                    "action": "confirm",
                    "knowledge_plane": "reader_disclosed",
                    "observed_fields": {"effect": "发出火光"},
                    "confidence": 1,
                },
            ],
            artifact_id="typed-power-runtime",
            chapter=1,
        )
        self.accept(proposal)
        state = self.service.query_power_state(actor)
        self.assertEqual(state["statuses"][0]["status_entity_id"], status)
        self.assertTrue(state["statuses"][0]["active"])
        self.assertEqual(state["bindings"][0]["binding_id"], "main-wand")
        self.assertTrue(state["bindings"][0]["active"])
        self.assertEqual(
            state["qualifications"][0]["qualification_entity_id"],
            qualification,
        )
        self.assertEqual(state["qualifications"][0]["quantity"], 2)
        self.assertEqual(
            {item["knowledge_plane"] for item in state["observations"]},
            {"actor_belief", "reader_disclosed"},
        )

    def test_qualification_definition_accept_retract_and_replay(self) -> None:
        actor = self.entity("character", "主角")
        qualification = self.entity("qualification", "秘境通行资格")
        definition = self.proposal(
            [
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "qualification_definition",
                    "spec_entity_id": qualification,
                    "definition": {
                        "qualification_kind": "access",
                        "consumable": False,
                    },
                }
            ],
            artifact_id="qualification-definition-lifecycle",
            chapter=None,
            stage="bootstrap",
            proposal_kind="power_spec_change",
        )
        self.accept(definition, operation="accept_power_spec")
        with self.service.store.read_connection() as connection:
            row = connection.execute(
                """
                SELECT definition_status, definition_json
                FROM qualification_definitions
                WHERE qualification_entity_id=?
                """,
                (qualification,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(str(row["definition_status"]), "active")
        self.assertEqual(
            json.loads(str(row["definition_json"]))["qualification_kind"],
            "access",
        )
        accepted_hash = self.service.projection_hash()
        self.assertEqual(
            accepted_hash,
            self.service.replay()["projection_hash"],
        )

        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            definition["proposal_id"],
            expected_canon_revision=revision,
            operations=("retract",),
        )
        self.service.retract_proposal(
            definition["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=revision,
            reason="资格规则撤回",
        )
        with self.service.store.read_connection() as connection:
            self.assertIsNone(
                connection.execute(
                    """
                    SELECT 1
                    FROM qualification_definitions
                    WHERE qualification_entity_id=?
                    """,
                    (qualification,),
                ).fetchone()
            )
        first = self.service.replay()
        second = self.service.replay()
        self.assertEqual(first["projection_hash"], second["projection_hash"])

        runtime = self.proposal(
            [
                {
                    "event_type": "qualification",
                    "actor_entity_id": actor,
                    "qualification_entity_id": qualification,
                    "action": "grant",
                    "quantity": 1,
                }
            ],
            artifact_id="qualification-after-definition-retraction",
            chapter=1,
        )
        revision = self.service.get_canon_revisions()["active"]
        runtime_grant = self.host.issue(
            runtime["proposal_id"],
            expected_canon_revision=revision,
        )
        with self.assertRaises(ContinuityError) as caught:
            self.service.accept_proposal(
                runtime["proposal_id"],
                approval_id=runtime_grant["approval_id"],
                expected_canon_revision=revision,
            )
        self.assertEqual(
            caught.exception.code,
            "POWER_RUNTIME_DEFINITION_MISSING",
        )

    def test_power_spec_supersession_replays_latest_definition(self) -> None:
        system = self.entity("power_system", "混合体系")
        first = self.proposal(
            [
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "power_system",
                    "spec_entity_id": system,
                    "definition": {"profile": "magic", "version": 1},
                }
            ],
            artifact_id="versioned-power-spec",
            chapter=None,
            stage="bootstrap",
            proposal_kind="power_spec_change",
            revision=1,
        )
        self.accept(first, operation="accept_power_spec")
        second = self.proposal(
            [
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "power_system",
                    "spec_entity_id": system,
                    "definition": {"profile": "hybrid", "version": 2},
                }
            ],
            artifact_id="versioned-power-spec",
            chapter=None,
            stage="bootstrap",
            proposal_kind="power_spec_change",
            revision=2,
        )
        self.accept(second, operation="accept_power_spec")
        systems = self.service.list_power_systems()["systems"]
        self.assertEqual(systems[0]["definition"]["profile"], "hybrid")
        self.assertEqual(systems[0]["definition"]["version"], 2)
        before = self.service.projection_hash()
        self.assertEqual(before, self.service.replay()["projection_hash"])

    def test_retraction_restores_ability_and_replay_hash_is_stable(self) -> None:
        actor = self.entity("character", "主角")
        ability = self.entity("ability", "剑意")
        gain = self.proposal(
            [
                {
                    "event_type": "ability",
                    "owner_entity_id": actor,
                    "ability_entity_id": ability,
                    "action": "gain",
                    "state": {"level": 1},
                }
            ],
            artifact_id="retract-gain",
            chapter=1,
        )
        self.accept(gain)
        lose = self.proposal(
            [
                {
                    "event_type": "ability",
                    "owner_entity_id": actor,
                    "ability_entity_id": ability,
                    "action": "lose",
                }
            ],
            artifact_id="retract-lose",
            chapter=2,
        )
        self.accept(lose)
        self.assertEqual(
            self.service.query_power_state(
                actor, include_inactive=True
            )["abilities"][0]["acquired"],
            False,
        )

        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            lose["proposal_id"],
            expected_canon_revision=revision,
            operations=("retract",),
        )
        self.service.retract_proposal(
            lose["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=revision,
            reason="恢复旧稿能力",
        )
        restored = self.service.query_power_state(
            actor, ability_entity_id=ability
        )
        self.assertTrue(restored["abilities"][0]["acquired"])
        self.assertEqual(restored["abilities"][0]["ownership"]["level"], 1)
        first = self.service.replay()
        second = self.service.replay()
        self.assertEqual(first["projection_hash"], second["projection_hash"])

    def test_reverse_ability_query_and_condition_comparison(self) -> None:
        actor_a = self.entity("character", "甲")
        actor_b = self.entity("character", "乙")
        ability = self.entity("ability", "共通技能")
        self.accept(
            self.proposal(
                [
                    {
                        "event_type": "ability",
                        "owner_entity_id": actor_a,
                        "ability_entity_id": ability,
                        "action": "gain",
                    },
                    {
                        "event_type": "ability",
                        "owner_entity_id": actor_b,
                        "ability_entity_id": ability,
                        "action": "gain",
                    },
                ],
                artifact_id="reverse-ability",
                chapter=1,
            )
        )
        reverse = self.service.query_power_state(
            ability_entity_id=ability
        )
        self.assertEqual(
            {item["owner_entity_id"] for item in reverse["abilities"]},
            {actor_a, actor_b},
        )
        compared = self.service.compare_power_conditions(actor_a, actor_b)
        self.assertEqual(compared["status"], "conditional_only")
        self.assertIsNone(compared["winner"])
        self.assertTrue(
            compared["claim_id"].startswith("comparison_claim_")
        )
        self.assertEqual(compared["claim_type"], "comparison_claim")
        self.assertEqual(compared["derivation"], "query_time")
        self.assertFalse(compared["persisted"])
        self.assertEqual(compared["knowledge_plane"], "objective")
        self.assertEqual(compared["conditions"], {})
        self.assertEqual(
            set(compared["source_event_ids"]),
            {
                compared["left"]["abilities"][0]["source_event_id"],
                compared["right"]["abilities"][0]["source_event_id"],
            },
        )
        self.assertGreater(compared["confidence"], 0)
        repeated = self.service.compare_power_conditions(actor_a, actor_b)
        self.assertEqual(compared["claim_id"], repeated["claim_id"])
        conditioned = self.service.compare_power_conditions(
            actor_a,
            actor_b,
            conditions={"distance": "近身"},
            knowledge_plane="reader_disclosed",
        )
        self.assertNotEqual(compared["claim_id"], conditioned["claim_id"])
        self.assertEqual(conditioned["conditions"], {"distance": "近身"})
        self.assertEqual(
            conditioned["knowledge_plane"],
            "reader_disclosed",
        )

    def test_v4_orphan_ability_projection_import_has_provenance(self) -> None:
        actor = self.entity("character", "旧角色")
        ability = self.entity("ability", "旧能力")
        self.service.schema_status()
        db_path = self.root / ".plot-rag" / "state.sqlite3"
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                """
                UPDATE state_meta
                SET value='4'
                WHERE key='continuity_schema_version'
                """
            )
            connection.execute(
                """
                INSERT INTO ability_state(
                    ability_key, owner_entity_id, ability_entity_id,
                    state_json, source_event_id, updated_order
                ) VALUES('legacy-ability', ?, ?, ?, 'missing-event', 7)
                """,
                (
                    actor,
                    ability,
                    json.dumps(
                        {
                            "level": 2,
                            "cost": "旧代价",
                            "action": "use",
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        migrated = ContinuityService(self.root)
        status = migrated.schema_status()
        self.assertEqual(
            status["meta"]["continuity_schema_version"],
            str(SCHEMA_VERSION),
        )
        self.assertTrue(Path(status["migration_backup"]).is_file())
        migrated.replay()
        state = migrated.query_power_state(
            actor, ability_entity_id=ability, include_inactive=True
        )
        self.assertEqual(state["abilities"][0]["ownership"]["level"], 2)
        with migrated.store.read_connection() as read:
            provenance = read.execute(
                "SELECT provenance_json FROM legacy_power_imports"
            ).fetchone()
            self.assertIsNotNone(provenance)
            self.assertEqual(
                json.loads(provenance[0])["kind"],
                "legacy_projection_import",
            )

    def test_concurrent_v4_orphan_migration_is_serialized_across_stores(
        self,
    ) -> None:
        actor = self.entity("character", "并发旧角色")
        ability = self.entity("ability", "并发旧能力")
        self.service.schema_status()
        db_path = self.root / ".plot-rag" / "state.sqlite3"
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                """
                UPDATE state_meta
                SET value='4'
                WHERE key='continuity_schema_version'
                """
            )
            connection.execute(
                """
                INSERT INTO ability_state(
                    ability_key, owner_entity_id, ability_entity_id,
                    state_json, source_event_id, updated_order
                ) VALUES('concurrent-legacy-ability', ?, ?, ?,
                         'missing-concurrent-event', 9)
                """,
                (
                    actor,
                    ability,
                    json.dumps(
                        {"level": 3, "action": "use"},
                        ensure_ascii=False,
                    ),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        begin_barrier = threading.Barrier(2)

        class BarrierConnection:
            def __init__(self, raw: sqlite3.Connection) -> None:
                self.raw = raw

            def execute(self, sql: str, parameters=()):
                if " ".join(sql.split()).upper() == "BEGIN IMMEDIATE":
                    begin_barrier.wait(timeout=10)
                return self.raw.execute(sql, parameters)

            def __getattr__(self, name: str):
                return getattr(self.raw, name)

        class BarrierStore(ContinuityStore):
            def _connect(self):
                return BarrierConnection(super()._connect())

        stores = [BarrierStore(self.root), BarrierStore(self.root)]
        backups: list[Path | None] = []
        errors: list[BaseException] = []

        def migrate(store: ContinuityStore) -> None:
            try:
                backups.append(store.ensure_schema())
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [
            threading.Thread(target=migrate, args=(store,), daemon=True)
            for store in stores
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual(2, len(backups))
        self.assertEqual(1, sum(path is not None for path in backups))
        read = sqlite3.connect(db_path)
        try:
            self.assertEqual(
                str(SCHEMA_VERSION),
                read.execute(
                    """
                    SELECT value FROM state_meta
                    WHERE key='continuity_schema_version'
                    """
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                read.execute(
                    """
                    SELECT COUNT(*) FROM canon_commits
                    WHERE operation='accept'
                      AND artifact_stage='bootstrap'
                    """
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                read.execute(
                    "SELECT COUNT(*) FROM legacy_power_imports"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                read.execute(
                    """
                    SELECT CAST(value AS INTEGER) FROM state_meta
                    WHERE key='head_canon_revision'
                    """
                ).fetchone()[0],
            )
        finally:
            read.close()

    @unittest.skipIf(
        os.name == "nt",
        "Windows prevents replacing an open SQLite database",
    )
    def test_migration_rejects_database_path_replacement_before_backup(
        self,
    ) -> None:
        self.service.schema_status()
        database = self.root / ".plot-rag" / "state.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                """
                UPDATE state_meta
                SET value='4'
                WHERE key='continuity_schema_version'
                """
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        replacement = self.root / "replacement-state.sqlite3"
        displaced = self.root / "displaced-state.sqlite3"
        shutil.copyfile(database, replacement)

        class ReplacingStore(ContinuityStore):
            def _backup_existing_database(self, from_version: int, **kwargs):
                os.replace(self.db_path, displaced)
                os.replace(replacement, self.db_path)
                return super()._backup_existing_database(
                    from_version,
                    **kwargs,
                )

        with self.assertRaisesRegex(
            StoreError,
            "STATE_DATABASE_PATH_CHANGED",
        ):
            ReplacingStore(self.root).ensure_schema()

        for path in (database, displaced):
            with closing(
                sqlite3.connect(
                    path.as_uri() + "?mode=ro&immutable=1",
                    uri=True,
                )
            ) as connection:
                version = connection.execute(
                    """
                    SELECT value FROM state_meta
                    WHERE key='continuity_schema_version'
                    """
                ).fetchone()[0]
            self.assertEqual("4", version)

    def test_late_created_v4_database_is_backed_up_after_locking(self) -> None:
        root = self.root / "late-created-project"
        root.mkdir()
        database = root / ".plot-rag" / "state.sqlite3"

        class LateCreatingStore(ContinuityStore):
            seeded = False

            def _connect(self):
                if not type(self).seeded:
                    type(self).seeded = True
                    ContinuityStore(root).ensure_schema()
                    connection = sqlite3.connect(database)
                    try:
                        connection.execute(
                            """
                            UPDATE state_meta
                            SET value='4'
                            WHERE key='continuity_schema_version'
                            """
                        )
                        connection.execute(
                            """
                            INSERT INTO ability_state(
                                ability_key, owner_entity_id,
                                ability_entity_id, state_json,
                                source_event_id, updated_order
                            ) VALUES(
                                'late-created-ability',
                                'late-owner',
                                'late-ability',
                                '{"level":4,"action":"use"}',
                                'missing-late-event',
                                12
                            )
                            """
                        )
                        connection.commit()
                    finally:
                        connection.close()
                return super()._connect()

        backup = LateCreatingStore(root).ensure_schema()

        self.assertIsNotNone(backup)
        self.assertTrue(backup.is_file())
        self.assertEqual(
            1,
            len(list((root / ".plot-rag" / "backups").glob("*.bak"))),
        )
        read = sqlite3.connect(database)
        try:
            provenance = json.loads(
                read.execute(
                    """
                    SELECT provenance_json
                    FROM legacy_power_imports
                    WHERE owner_entity_id='late-owner'
                      AND ability_entity_id='late-ability'
                    """
                ).fetchone()[0]
            )
        finally:
            read.close()
        self.assertTrue(provenance["source_db_hash"])

    def test_existing_database_without_version_metadata_fails_closed(self) -> None:
        self.service.schema_status()
        database = self.root / ".plot-rag" / "state.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                """
                DELETE FROM state_meta
                WHERE key IN ('schema_version', 'continuity_schema_version')
                """
            )
            connection.execute(
                """
                INSERT INTO ability_state(
                    ability_key, owner_entity_id, ability_entity_id,
                    state_json, source_event_id, updated_order
                ) VALUES(
                    'versionless-ability',
                    'versionless-owner',
                    'versionless-definition',
                    '{"level":7}',
                    'missing-versionless-event',
                    21
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

        first_store = ContinuityStore(self.root)
        with self.assertRaisesRegex(
            StoreError,
            "STATE_SCHEMA_VERSION_MISSING.*backup=",
        ):
            first_store.ensure_schema()
        first_backup = first_store.last_backup_path
        self.assertIsNotNone(first_backup)

        with self.assertRaisesRegex(
            StoreError,
            "STATE_SCHEMA_VERSION_MISSING.*backup=",
        ):
            first_store.ensure_schema()
        restarted_store = ContinuityStore(self.root)
        with self.assertRaisesRegex(
            StoreError,
            "STATE_SCHEMA_VERSION_MISSING.*backup=",
        ):
            restarted_store.ensure_schema()

        self.assertEqual(
            1,
            len(list((self.root / ".plot-rag" / "backups").glob("*.bak"))),
        )
        self.assertEqual(first_backup, first_store.last_backup_path)
        self.assertEqual(first_backup, restarted_store.last_backup_path)
        read = sqlite3.connect(database)
        try:
            self.assertEqual(
                1,
                read.execute(
                    """
                    SELECT COUNT(*) FROM ability_state
                    WHERE ability_key='versionless-ability'
                    """
                ).fetchone()[0],
            )
            self.assertIsNone(
                read.execute(
                    """
                    SELECT value FROM state_meta
                    WHERE key='continuity_schema_version'
                    """
                ).fetchone()
            )
        finally:
            read.close()

    def test_versionless_backup_is_replaced_after_source_changes(self) -> None:
        self.service.schema_status()
        database = self.root / ".plot-rag" / "state.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                """
                DELETE FROM state_meta
                WHERE key IN ('schema_version', 'continuity_schema_version')
                """
            )
            connection.commit()
        finally:
            connection.close()

        first_store = ContinuityStore(self.root)
        with self.assertRaisesRegex(
            StoreError,
            "STATE_SCHEMA_VERSION_MISSING",
        ):
            first_store.ensure_schema()
        first_backup = first_store.last_backup_path
        self.assertIsNotNone(first_backup)

        connection = sqlite3.connect(database)
        try:
            connection.execute(
                """
                INSERT INTO ability_state(
                    ability_key, owner_entity_id, ability_entity_id,
                    state_json, source_event_id, updated_order
                ) VALUES(
                    'post-backup-ability',
                    'post-backup-owner',
                    'post-backup-definition',
                    '{"level":8}',
                    'post-backup-event',
                    22
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

        changed_store = ContinuityStore(self.root)
        with self.assertRaisesRegex(
            StoreError,
            "STATE_SCHEMA_VERSION_MISSING",
        ):
            changed_store.ensure_schema()
        self.assertIsNotNone(changed_store.last_backup_path)
        self.assertNotEqual(first_backup, changed_store.last_backup_path)
        self.assertEqual(
            2,
            len(list((self.root / ".plot-rag" / "backups").glob("*.bak"))),
        )

    def test_versionless_backup_reuses_stable_post_checkpoint_layout(
        self,
    ) -> None:
        self.service.schema_status()
        database = self.root / ".plot-rag" / "state.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                """
                DELETE FROM state_meta
                WHERE key IN ('schema_version', 'continuity_schema_version')
                """
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(
            StoreError,
            "STATE_SCHEMA_VERSION_MISSING",
        ):
            ContinuityStore(self.root).ensure_schema()

        checkpoint = sqlite3.connect(database)
        try:
            checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            checkpoint.close()

        with self.assertRaisesRegex(
            StoreError,
            "STATE_SCHEMA_VERSION_MISSING",
        ):
            ContinuityStore(self.root).ensure_schema()
        after_checkpoint = len(
            list((self.root / ".plot-rag" / "backups").glob("*.bak"))
        )
        self.assertIn(after_checkpoint, {1, 2})

        with self.assertRaisesRegex(
            StoreError,
            "STATE_SCHEMA_VERSION_MISSING",
        ):
            ContinuityStore(self.root).ensure_schema()
        self.assertEqual(
            after_checkpoint,
            len(list((self.root / ".plot-rag" / "backups").glob("*.bak"))),
        )

    def test_corrupt_schema_version_preserves_store_error_contract(self) -> None:
        self.service.schema_status()
        db_path = self.root / ".plot-rag" / "state.sqlite3"
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                """
                UPDATE state_meta
                SET value='not-an-integer'
                WHERE key='continuity_schema_version'
                """
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(StoreError) as raised:
            ContinuityStore(self.root).ensure_schema()

        self.assertIn("STATE_SCHEMA_UNREADABLE", str(raised.exception))

    def test_unknown_schema_versions_fail_closed_without_store_mutation(
        self,
    ) -> None:
        self.service.schema_status()
        db_path = self.root / ".plot-rag" / "state.sqlite3"

        for key, value, expected_code in (
            ("schema_version", "-1", "STATE_SCHEMA_UNREADABLE"),
            ("schema_version", "1", "STATE_LEGACY_SCHEMA_UNSUPPORTED"),
            ("schema_version", "999", "STATE_LEGACY_SCHEMA_TOO_NEW"),
            (
                "continuity_schema_version",
                "-1",
                "STATE_SCHEMA_UNREADABLE",
            ),
            (
                "continuity_schema_version",
                "999",
                "STATE_SCHEMA_TOO_NEW",
            ),
        ):
            with self.subTest(key=key, value=value):
                with closing(sqlite3.connect(db_path)) as connection:
                    connection.execute(
                        "UPDATE state_meta SET value=? WHERE key=?",
                        (value, key),
                    )
                    connection.commit()
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                before = {
                    path.name: path.read_bytes()
                    for path in db_path.parent.iterdir()
                    if path.is_file()
                }
                backup_dir = db_path.parent / "backups"
                backups_before = (
                    sorted(path.name for path in backup_dir.iterdir())
                    if backup_dir.is_dir()
                    else []
                )

                with self.assertRaisesRegex(StoreError, expected_code):
                    ContinuityStore(self.root).ensure_schema()

                after = {
                    path.name: path.read_bytes()
                    for path in db_path.parent.iterdir()
                    if path.is_file()
                }
                backups_after = (
                    sorted(path.name for path in backup_dir.iterdir())
                    if backup_dir.is_dir()
                    else []
                )
                self.assertEqual(before, after)
                self.assertEqual(backups_before, backups_after)

                with closing(sqlite3.connect(db_path)) as connection:
                    connection.execute(
                        "UPDATE state_meta SET value=? WHERE key=?",
                        (
                            "2"
                            if key == "schema_version"
                            else "5",
                            key,
                        ),
                    )
                    connection.commit()

    def test_existing_continuity_schema_requires_legacy_version_key(
        self,
    ) -> None:
        self.service.schema_status()
        db_path = self.root / ".plot-rag" / "state.sqlite3"
        with closing(sqlite3.connect(db_path)) as connection:
            connection.execute(
                "DELETE FROM state_meta WHERE key='schema_version'"
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = db_path.read_bytes()
        backup_dir = db_path.parent / "backups"
        backups_before = (
            sorted(path.name for path in backup_dir.iterdir())
            if backup_dir.is_dir()
            else []
        )

        with self.assertRaisesRegex(
            StoreError,
            "STATE_SCHEMA_VERSION_MISSING",
        ):
            ContinuityStore(self.root).ensure_schema()

        self.assertEqual(before, db_path.read_bytes())
        backups_after = (
            sorted(path.name for path in backup_dir.iterdir())
            if backup_dir.is_dir()
            else []
        )
        self.assertEqual(backups_before, backups_after)

    def test_failed_schema_backup_connection_removes_reserved_file(self) -> None:
        self.service.schema_status()
        store = ContinuityStore(self.root)
        backup_dir = self.root / ".plot-rag" / "backups"

        with (
            mock.patch(
                "scripts.continuity.store.sqlite3.connect",
                side_effect=sqlite3.OperationalError(
                    "fixture connection failure"
                ),
            ),
            self.assertRaises(sqlite3.OperationalError),
        ):
            store._backup_existing_database(5)

        self.assertEqual([], list(backup_dir.glob("*.bak")))

    def test_backup_publish_reuses_complete_concurrent_winner(self) -> None:
        self.service.schema_status()
        database = self.root / ".plot-rag" / "state.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                """
                UPDATE state_meta
                SET value='4'
                WHERE key='continuity_schema_version'
                """
            )

        real_link = os.link
        raced_paths: list[Path] = []

        def publish_winner(source: str | bytes, destination: str | bytes) -> None:
            destination_path = Path(destination)
            shutil.copyfile(source, destination_path)
            raced_paths.append(destination_path)
            real_link(source, destination)

        with mock.patch(
            "scripts.continuity.store.os.link",
            side_effect=publish_winner,
        ):
            backup = ContinuityStore(self.root).ensure_schema()

        self.assertEqual(raced_paths, [backup])
        self.assertTrue(backup.is_file())
        with closing(
            sqlite3.connect(
                backup.as_uri() + "?mode=ro&immutable=1",
                uri=True,
            )
        ) as connection:
            self.assertEqual(
                "4",
                connection.execute(
                    """
                    SELECT value FROM state_meta
                    WHERE key='continuity_schema_version'
                    """
                ).fetchone()[0],
            )
        self.assertEqual(
            [],
            list((self.root / ".plot-rag" / "backups").glob("*.tmp.*")),
        )

    def test_backup_rejects_preexisting_valid_but_unrelated_database(
        self,
    ) -> None:
        self.service.schema_status()
        database = self.root / ".plot-rag" / "state.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                """
                UPDATE state_meta
                SET value='4'
                WHERE key='continuity_schema_version'
                """
            )

        fixed_fingerprint = "a" * 64

        class FixedFingerprintStore(ContinuityStore):
            def _database_source_fingerprint(self) -> str:
                return fixed_fingerprint

        backup = (
            self.root
            / ".plot-rag"
            / "backups"
            / (
                "state.sqlite3.schema-v4."
                f"source-{fixed_fingerprint[:32]}.bak"
            )
        )
        backup.parent.mkdir(parents=True)
        with closing(sqlite3.connect(backup)) as connection, connection:
            connection.execute(
                "CREATE TABLE wrong_backup_marker(value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO wrong_backup_marker(value) VALUES('preserve me')"
            )

        with self.assertRaisesRegex(StoreError, "STATE_BACKUP_CONFLICT"):
            FixedFingerprintStore(self.root).ensure_schema()

        with closing(
            sqlite3.connect(
                backup.as_uri() + "?mode=ro&immutable=1",
                uri=True,
            )
        ) as connection:
            self.assertEqual(
                "preserve me",
                connection.execute(
                    "SELECT value FROM wrong_backup_marker"
                ).fetchone()[0],
            )
            self.assertIsNone(
                connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='state_meta'
                    """
                ).fetchone()
            )
        with closing(
            sqlite3.connect(
                database.as_uri() + "?mode=ro&immutable=1",
                uri=True,
            )
        ) as connection:
            self.assertEqual(
                "4",
                connection.execute(
                    """
                    SELECT value FROM state_meta
                    WHERE key='continuity_schema_version'
                    """
                ).fetchone()[0],
            )

    def test_backup_publish_preserves_invalid_concurrent_destination(self) -> None:
        self.service.schema_status()
        database = self.root / ".plot-rag" / "state.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                """
                UPDATE state_meta
                SET value='4'
                WHERE key='continuity_schema_version'
                """
            )

        real_link = os.link
        raced_paths: list[Path] = []
        competitor_bytes = b"concurrent backup owner"

        def publish_competitor(
            source: str | bytes,
            destination: str | bytes,
        ) -> None:
            destination_path = Path(destination)
            destination_path.write_bytes(competitor_bytes)
            raced_paths.append(destination_path)
            real_link(source, destination)

        with mock.patch(
            "scripts.continuity.store.os.link",
            side_effect=publish_competitor,
        ):
            with self.assertRaisesRegex(StoreError, "STATE_BACKUP_CONFLICT"):
                ContinuityStore(self.root).ensure_schema()

        self.assertEqual(1, len(raced_paths))
        self.assertEqual(competitor_bytes, raced_paths[0].read_bytes())
        self.assertEqual(
            [],
            list((self.root / ".plot-rag" / "backups").glob("*.tmp.*")),
        )
        with closing(
            sqlite3.connect(
                database.as_uri() + "?mode=ro&immutable=1",
                uri=True,
            )
        ) as connection:
            self.assertEqual(
                "4",
                connection.execute(
                    """
                    SELECT value FROM state_meta
                    WHERE key='continuity_schema_version'
                    """
                ).fetchone()[0],
            )

    def test_cancelled_schema_backup_closes_connections_and_cleans_temp(
        self,
    ) -> None:
        self.service.schema_status()
        store = ContinuityStore(self.root)
        backup_dir = self.root / ".plot-rag" / "backups"

        class InterruptingSource:
            def __init__(self) -> None:
                self.closed = False

            def execute(self, _statement):
                return self

            def backup(self, _destination) -> None:
                raise KeyboardInterrupt("fixture cancellation")

            def close(self) -> None:
                self.closed = True

        class BackupDestination:
            def __init__(self) -> None:
                self.closed = False

            def commit(self) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        source = InterruptingSource()
        destination = BackupDestination()
        with (
            mock.patch(
                "scripts.continuity.store.sqlite3.connect",
                side_effect=(source, destination),
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            store._backup_existing_database(5)

        self.assertTrue(source.closed)
        self.assertTrue(destination.closed)
        self.assertEqual([], list(backup_dir.iterdir()))

    def test_backup_rejects_live_database_hardlink_as_concurrent_winner(
        self,
    ) -> None:
        self.service.schema_status()
        database = self.root / ".plot-rag" / "state.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                """
                UPDATE state_meta SET value='4'
                WHERE key='continuity_schema_version'
                """
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

        class FixedFingerprintStore(ContinuityStore):
            def _database_source_fingerprint(self) -> str:
                return "a" * 64

        backup = (
            database.parent
            / "backups"
            / f"state.sqlite3.schema-v4.source-{'a' * 32}.bak"
        )
        backup.parent.mkdir(parents=True)
        os.link(database, backup)

        with self.assertRaisesRegex(StoreError, "STATE_BACKUP_CONFLICT"):
            FixedFingerprintStore(self.root).ensure_schema()

        self.assertTrue(
            os.path.samestat(os.stat(database), os.stat(backup))
        )
        with closing(sqlite3.connect(database)) as connection:
            version = connection.execute(
                """
                SELECT value FROM state_meta
                WHERE key='continuity_schema_version'
                """
            ).fetchone()[0]
        self.assertEqual("4", version)

    def test_migration_rejects_backup_content_change_after_publication(
        self,
    ) -> None:
        self.service.schema_status()
        database = self.root / ".plot-rag" / "state.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                """
                UPDATE state_meta SET value='4'
                WHERE key='continuity_schema_version'
                """
            )
        attacker = self.root / "attacker.sqlite3"
        with closing(sqlite3.connect(attacker)) as connection, connection:
            connection.execute(
                "CREATE TABLE attacker_marker(value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO attacker_marker(value) VALUES('not-source')"
            )

        class ReplacingBackupStore(ContinuityStore):
            def _backup_existing_database(self, from_version: int, **kwargs):
                backup = super()._backup_existing_database(
                    from_version,
                    **kwargs,
                )
                backup.write_bytes(attacker.read_bytes())
                return backup

        with self.assertRaisesRegex(StoreError, "STATE_BACKUP_PATH_CHANGED"):
            ReplacingBackupStore(self.root).ensure_schema()

        with closing(sqlite3.connect(database)) as connection:
            version = connection.execute(
                """
                SELECT value FROM state_meta
                WHERE key='continuity_schema_version'
                """
            ).fetchone()[0]
        self.assertEqual("4", version)
        self.assertEqual(
            [],
            list((database.parent / "backups").glob("*.bak")),
        )

    @unittest.skipIf(
        os.name == "nt",
        "Windows prevents replacing an open retained backup",
    )
    def test_post_commit_backup_replacement_publishes_recovery_copy(
        self,
    ) -> None:
        self.service.schema_status()
        database = self.root / ".plot-rag" / "state.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                """
                UPDATE state_meta
                SET value='4'
                WHERE key='continuity_schema_version'
                """
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

        attacker = self.root / "attacker-backup.sqlite3"
        with closing(sqlite3.connect(attacker)) as connection, connection:
            connection.execute(
                "CREATE TABLE attacker_marker(value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO attacker_marker(value) VALUES('not-source')"
            )

        class ReplacingAfterCommitStore(ContinuityStore):
            verify_calls = 0
            invalid_public_path: Path | None = None

            def _verify_held_backup_identity(self, path: Path) -> str:
                type(self).verify_calls += 1
                if type(self).verify_calls == 3:
                    type(self).invalid_public_path = path
                    os.replace(attacker, path)
                return super()._verify_held_backup_identity(path)

        store = ReplacingAfterCommitStore(self.root)
        with self.assertRaises(StoreError) as raised:
            store.ensure_schema()

        self.assertIn("STATE_BACKUP_PATH_CHANGED", str(raised.exception))
        recovery = store.last_backup_path
        invalid_public = ReplacingAfterCommitStore.invalid_public_path
        self.assertIsNotNone(recovery)
        self.assertIsNotNone(invalid_public)
        assert recovery is not None
        assert invalid_public is not None
        self.assertNotEqual(invalid_public, recovery)
        self.assertIn(f"recovery_backup={recovery}", str(raised.exception))
        self.assertTrue(recovery.is_file())

        with closing(sqlite3.connect(recovery)) as connection:
            backup_version = connection.execute(
                """
                SELECT value FROM state_meta
                WHERE key='continuity_schema_version'
                """
            ).fetchone()[0]
        with closing(sqlite3.connect(database)) as connection:
            live_version = connection.execute(
                """
                SELECT value FROM state_meta
                WHERE key='continuity_schema_version'
                """
            ).fetchone()[0]
        with closing(sqlite3.connect(invalid_public)) as connection:
            attacker_value = connection.execute(
                "SELECT value FROM attacker_marker"
            ).fetchone()[0]

        self.assertEqual("4", backup_version)
        self.assertEqual(str(SCHEMA_VERSION), live_version)
        self.assertEqual("not-source", attacker_value)
        self.assertEqual(
            [],
            list(
                recovery.parent.glob(
                    "state.sqlite3.schema-v*.bak.tmp.*"
                )
            ),
        )

    @unittest.skipIf(
        os.name == "nt",
        "POSIX in-place backup corruption regression",
    )
    def test_post_commit_backup_rewrite_preserves_private_recovery(
        self,
    ) -> None:
        self.service.schema_status()
        database = self.root / ".plot-rag" / "state.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                """
                UPDATE state_meta
                SET value='4'
                WHERE key='continuity_schema_version'
                """
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

        class RewritingAfterCommitStore(ContinuityStore):
            verify_calls = 0
            invalid_public_path: Path | None = None
            expected_backup_hash = ""

            def _verify_held_backup_identity(self, path: Path) -> str:
                type(self).verify_calls += 1
                if type(self).verify_calls == 3:
                    held = self._held_backup_identity
                    assert held is not None
                    type(self).invalid_public_path = path
                    type(self).expected_backup_hash = held.sha256
                    with closing(
                        sqlite3.connect(path)
                    ) as connection, connection:
                        connection.execute(
                            "CREATE TABLE attacker_marker("
                            "value TEXT NOT NULL)"
                        )
                        connection.execute(
                            "INSERT INTO attacker_marker(value) "
                            "VALUES('in-place-corruption')"
                        )
                return super()._verify_held_backup_identity(path)

        store = RewritingAfterCommitStore(self.root)
        with self.assertRaises(StoreError) as raised:
            store.ensure_schema()

        recovery = store.last_backup_path
        invalid_public = RewritingAfterCommitStore.invalid_public_path
        self.assertIsNotNone(recovery)
        self.assertIsNotNone(invalid_public)
        assert recovery is not None
        assert invalid_public is not None
        self.assertNotEqual(invalid_public, recovery)
        self.assertIn(f"recovery_backup={recovery}", str(raised.exception))
        self.assertEqual(
            RewritingAfterCommitStore.expected_backup_hash,
            hashlib.sha256(recovery.read_bytes()).hexdigest(),
        )

        with closing(sqlite3.connect(recovery)) as connection:
            backup_version = connection.execute(
                """
                SELECT value FROM state_meta
                WHERE key='continuity_schema_version'
                """
            ).fetchone()[0]
            recovery_marker = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='attacker_marker'
                """
            ).fetchone()
        with closing(sqlite3.connect(database)) as connection:
            live_version = connection.execute(
                """
                SELECT value FROM state_meta
                WHERE key='continuity_schema_version'
                """
            ).fetchone()[0]
        with closing(sqlite3.connect(invalid_public)) as connection:
            attacker_value = connection.execute(
                "SELECT value FROM attacker_marker"
            ).fetchone()[0]

        self.assertEqual("4", backup_version)
        self.assertIsNone(recovery_marker)
        self.assertEqual(str(SCHEMA_VERSION), live_version)
        self.assertEqual("in-place-corruption", attacker_value)

    def test_v4_breakthrough_as_legacy_gain_remains_replayable(self) -> None:
        actor = self.entity("character", "旧修士")
        ability = self.entity("ability", "旧版突破")
        accepted = self.accept(
            self.proposal(
                [
                    {
                        "event_type": "ability",
                        "owner_entity_id": actor,
                        "ability_entity_id": ability,
                        "action": "gain",
                        "state": {"level": 4},
                    }
                ],
                artifact_id="legacy-breakthrough-fixture",
                chapter=1,
            )
        )
        event_id = accepted["events"][0]["event_id"]
        db_path = self.root / ".plot-rag" / "state.sqlite3"
        connection = sqlite3.connect(db_path)
        try:
            payload = json.loads(
                connection.execute(
                    """
                    SELECT payload_json FROM continuity_events
                    WHERE event_id=?
                    """,
                    (event_id,),
                ).fetchone()[0]
            )
            payload["action"] = "breakthrough"
            connection.execute(
                """
                UPDATE continuity_events
                SET payload_json=?
                WHERE event_id=?
                """,
                (
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    event_id,
                ),
            )
            connection.execute(
                """
                UPDATE state_meta SET value='4'
                WHERE key='continuity_schema_version'
                """
            )
            connection.commit()
        finally:
            connection.close()

        migrated = ContinuityService(self.root)
        migrated.schema_status()
        migrated.replay()
        state = migrated.query_power_state(
            actor, ability_entity_id=ability
        )
        self.assertTrue(state["abilities"][0]["acquired"])
        self.assertEqual(state["abilities"][0]["ownership"]["level"], 4)


if __name__ == "__main__":
    unittest.main()
