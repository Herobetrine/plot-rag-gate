from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from continuity import (  # noqa: E402
    ContinuityError,
    ContinuityService,
    HostApprovalAuthority,
)
from power_system.adapters import adapter_registry  # noqa: E402


class PowerAdapterRuntimeContractTests(unittest.TestCase):
    """Every declarative profile must pass through the same runtime gates."""

    @staticmethod
    def coordinate(profile: str, ordinal: int) -> dict[str, Any]:
        return {
            "calendar_id": f"runtime-contract:{profile}",
            "ordinal": ordinal,
            "label": f"{profile}运行时契约时点{ordinal}",
            "precision": "scene",
        }

    @staticmethod
    def register(
        service: ContinuityService,
        entity_type: str,
        name: str,
    ) -> str:
        return str(
            service.register_entity(entity_type, name)["entity_id"]
        )

    @staticmethod
    def save(
        service: ContinuityService,
        *,
        events: Sequence[Mapping[str, Any]],
        artifact_id: str,
        proposal_kind: str = "story_delta",
    ) -> dict[str, Any]:
        return service.save_proposal(
            events=[dict(event) for event in events],
            artifact_id=artifact_id,
            artifact_stage=(
                "bootstrap"
                if proposal_kind == "power_spec_change"
                else "final"
            ),
            branch_id="main",
            prepared_canon_revision=service.get_canon_revisions()[
                "active"
            ],
            proposal_kind=proposal_kind,
        )

    @staticmethod
    def accept(
        service: ContinuityService,
        host: HostApprovalAuthority,
        proposal: Mapping[str, Any],
        *,
        operation: str = "accept",
    ) -> dict[str, Any]:
        revision = int(service.get_canon_revisions()["active"])
        grant = host.issue(
            str(proposal["proposal_id"]),
            expected_canon_revision=revision,
            operations=(operation,),
        )
        return service.accept_proposal(
            str(proposal["proposal_id"]),
            approval_id=str(grant["approval_id"]),
            expected_canon_revision=revision,
        )

    def assert_accept_blocked(
        self,
        service: ContinuityService,
        host: HostApprovalAuthority,
        proposal: Mapping[str, Any],
        expected_code: str,
    ) -> None:
        revision = int(service.get_canon_revisions()["active"])
        grant = host.issue(
            str(proposal["proposal_id"]),
            expected_canon_revision=revision,
            operations=("accept",),
        )
        with self.assertRaises(ContinuityError) as caught:
            service.accept_proposal(
                str(proposal["proposal_id"]),
                approval_id=str(grant["approval_id"]),
                expected_canon_revision=revision,
            )
        self.assertEqual(expected_code, caught.exception.code)
        self.assertEqual(
            revision,
            service.get_canon_revisions()["active"],
        )

    @staticmethod
    def state_for(
        service: ContinuityService,
        actor: str,
    ) -> dict[str, Any]:
        return service.query_power_state(
            actor,
            include_inactive=True,
            include_history=True,
        )

    def exercise_profile(self, profile: str, target_profile: str) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / profile
            root.mkdir()
            service = ContinuityService(root)
            host = HostApprovalAuthority(
                service,
                issuer=f"adapter-runtime-contract:{profile}",
                channel="interactive_test",
            )
            prefix = f"{profile}:contract"
            actor = self.register(
                service,
                "character",
                f"{prefix}:actor",
            )
            observer = self.register(
                service,
                "character",
                f"{prefix}:observer",
            )
            system = self.register(
                service,
                "power_system",
                f"{prefix}:system",
            )
            target_system = self.register(
                service,
                "power_system",
                f"{prefix}:target-system",
            )
            track = self.register(
                service,
                "progression_track",
                f"{prefix}:track",
            )
            rank_one = self.register(
                service,
                "rank_node",
                f"{prefix}:rank-one",
            )
            rank_two = self.register(
                service,
                "rank_node",
                f"{prefix}:rank-two",
            )
            edge = self.register(
                service,
                "rank_edge",
                f"{prefix}:edge",
            )
            resource = self.register(
                service,
                "resource_pool",
                f"{prefix}:resource",
            )
            target_resource = self.register(
                service,
                "resource_pool",
                f"{prefix}:target-resource",
            )
            ability = self.register(
                service,
                "ability",
                f"{prefix}:ability",
            )
            status = self.register(
                service,
                "status_effect",
                f"{prefix}:status",
            )
            qualification = self.register(
                service,
                "qualification",
                f"{prefix}:qualification",
            )
            source_item = self.register(
                service,
                "item",
                f"{prefix}:source-item",
            )
            missing_conversion_rule = self.register(
                service,
                "conversion_rule",
                f"{prefix}:missing-conversion-rule",
            )
            binding_id = f"{prefix}:binding"

            specification = self.save(
                service,
                events=[
                    {
                        "event_type": "power_spec",
                        "action": "define",
                        "spec_type": "power_system",
                        "spec_entity_id": system,
                        "definition": {
                            "profile": profile,
                            "namespace": prefix,
                            "interaction_policy": "explicit_only",
                        },
                    },
                    {
                        "event_type": "power_spec",
                        "action": "define",
                        "spec_type": "power_system",
                        "spec_entity_id": target_system,
                        "definition": {
                            "profile": target_profile,
                            "namespace": f"{prefix}:target",
                            "interaction_policy": "explicit_only",
                        },
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
                            "definition": {
                                "track_entity_id": track,
                            },
                        }
                        for rank in (rank_one, rank_two)
                    ],
                    {
                        "event_type": "power_spec",
                        "action": "define",
                        "spec_type": "resource_definition",
                        "spec_entity_id": resource,
                        "definition": {
                            "system_entity_id": system,
                            "minimum_balance": 0,
                            "maximum_balance": 100,
                            "allow_debt": False,
                        },
                    },
                    {
                        "event_type": "power_spec",
                        "action": "define",
                        "spec_type": "resource_definition",
                        "spec_entity_id": target_resource,
                        "definition": {
                            "system_entity_id": target_system,
                            "minimum_balance": 0,
                            "maximum_balance": 100,
                            "allow_debt": False,
                        },
                    },
                    {
                        "event_type": "power_spec",
                        "action": "define",
                        "spec_type": "status_definition",
                        "spec_entity_id": status,
                        "definition": {
                            "system_entity_id": system,
                        },
                    },
                    {
                        "event_type": "power_spec",
                        "action": "define",
                        "spec_type": "qualification_definition",
                        "spec_entity_id": qualification,
                        "definition": {
                            "system_entity_id": system,
                            "quantity_mode": "stackable",
                        },
                    },
                    {
                        "event_type": "power_spec",
                        "action": "define",
                        "spec_type": "ability_definition",
                        "spec_entity_id": ability,
                        "definition": {
                            "system_entity_id": system,
                            "source_binding_id": binding_id,
                            "prerequisites": {
                                "qualification_entity_ids": [
                                    qualification
                                ]
                            },
                            "resource_costs": [
                                {
                                    "resource_entity_id": resource,
                                    "amount": 5,
                                }
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
                            "from_rank_entity_ids": [rank_one],
                            "to_rank_entity_id": rank_two,
                            "resource_costs": [
                                {
                                    "resource_entity_id": resource,
                                    "amount": 10,
                                }
                            ],
                        },
                    },
                ],
                artifact_id=f"{prefix}:specification",
                proposal_kind="power_spec_change",
            )
            self.accept(
                service,
                host,
                specification,
                operation="accept_power_spec",
            )

            illegal_progression = self.save(
                service,
                events=[
                    {
                        "event_type": "progression",
                        "actor_entity_id": actor,
                        "track_entity_id": track,
                        "action": "advance",
                        "to_rank_entity_id": rank_two,
                        "rank_edge_entity_id": edge,
                        "story_coordinate": self.coordinate(profile, 1),
                    }
                ],
                artifact_id=f"{prefix}:illegal-progression",
            )
            self.assert_accept_blocked(
                service,
                host,
                illegal_progression,
                "POWER_PREREQUISITE_UNMET",
            )

            bootstrap = self.save(
                service,
                events=[
                    {
                        "event_type": "resource",
                        "actor_entity_id": actor,
                        "resource_entity_id": resource,
                        "action": "initialize",
                        "amount": 20,
                        "story_coordinate": self.coordinate(profile, 2),
                    },
                    {
                        "event_type": "progression",
                        "actor_entity_id": actor,
                        "track_entity_id": track,
                        "action": "initialize",
                        "to_rank_entity_id": rank_one,
                        "story_coordinate": self.coordinate(profile, 2),
                    },
                    {
                        "event_type": "ability",
                        "owner_entity_id": actor,
                        "ability_entity_id": ability,
                        "action": "gain",
                        "state": {"level": 1},
                        "story_coordinate": self.coordinate(profile, 2),
                    },
                ],
                artifact_id=f"{prefix}:bootstrap",
            )
            self.accept(service, host, bootstrap)

            excessive_spend = self.save(
                service,
                events=[
                    {
                        "event_type": "resource",
                        "actor_entity_id": actor,
                        "resource_entity_id": resource,
                        "action": "spend",
                        "amount": 999,
                    }
                ],
                artifact_id=f"{prefix}:excessive-spend",
            )
            self.assert_accept_blocked(
                service,
                host,
                excessive_spend,
                "POWER_RESOURCE_INSUFFICIENT",
            )
            self.assertEqual(
                20,
                self.state_for(service, actor)["resources"][0]["balance"],
            )

            missing_prerequisites = self.save(
                service,
                events=[
                    {
                        "event_type": "ability",
                        "owner_entity_id": actor,
                        "ability_entity_id": ability,
                        "action": "use",
                        "story_coordinate": self.coordinate(profile, 3),
                    },
                    {
                        "event_type": "resource",
                        "actor_entity_id": actor,
                        "resource_entity_id": resource,
                        "action": "spend",
                        "amount": 5,
                    },
                ],
                artifact_id=f"{prefix}:missing-prerequisites",
            )
            self.assert_accept_blocked(
                service,
                host,
                missing_prerequisites,
                "POWER_PREREQUISITE_UNMET",
            )

            runtime_setup = self.save(
                service,
                events=[
                    {
                        "event_type": "power_binding",
                        "actor_entity_id": actor,
                        "binding_id": binding_id,
                        "source_entity_id": source_item,
                        "action": "bind",
                        "ability_entity_ids": [ability],
                        "slot_key": "primary",
                        "unique": True,
                        "story_coordinate": self.coordinate(profile, 4),
                    },
                    {
                        "event_type": "qualification",
                        "actor_entity_id": actor,
                        "qualification_entity_id": qualification,
                        "action": "grant",
                        "quantity": 1,
                        "story_coordinate": self.coordinate(profile, 4),
                    },
                    {
                        "event_type": "status_effect",
                        "actor_entity_id": actor,
                        "status_entity_id": status,
                        "action": "apply",
                        "stacks": 1,
                        "story_coordinate": self.coordinate(profile, 4),
                    },
                    {
                        "event_type": "power_observation",
                        "observer_entity_id": observer,
                        "subject_entity_id": actor,
                        "ability_entity_id": ability,
                        "action": "observe",
                        "knowledge_plane": "actor_belief",
                        "confidence": 0.8,
                        "observed_fields": {
                            "effect": f"{profile}:observed-effect"
                        },
                    },
                ],
                artifact_id=f"{prefix}:runtime-setup",
            )
            self.accept(service, host, runtime_setup)
            state = self.state_for(service, actor)
            self.assertTrue(state["bindings"][0]["active"])
            self.assertTrue(state["statuses"][0]["active"])
            self.assertTrue(state["qualifications"][0]["active"])
            self.assertEqual(
                {"actor_belief"},
                {
                    item["knowledge_plane"]
                    for item in state["observations"]
                },
            )

            legal_progression = self.save(
                service,
                events=[
                    {
                        "event_type": "progression",
                        "actor_entity_id": actor,
                        "track_entity_id": track,
                        "action": "advance",
                        "from_rank_entity_id": rank_one,
                        "to_rank_entity_id": rank_two,
                        "rank_edge_entity_id": edge,
                        "story_coordinate": self.coordinate(profile, 5),
                    },
                    {
                        "event_type": "resource",
                        "actor_entity_id": actor,
                        "resource_entity_id": resource,
                        "action": "spend",
                        "amount": 10,
                    },
                ],
                artifact_id=f"{prefix}:legal-progression",
            )
            self.accept(service, host, legal_progression)
            state = self.state_for(service, actor)
            self.assertEqual(
                rank_two,
                state["progression"][0]["rank_entity_id"],
            )
            self.assertEqual(10, state["resources"][0]["balance"])

            use = self.save(
                service,
                events=[
                    {
                        "event_type": "ability",
                        "owner_entity_id": actor,
                        "ability_entity_id": ability,
                        "action": "use",
                        "story_coordinate": self.coordinate(profile, 6),
                        "cooldown_until": self.coordinate(profile, 9),
                    },
                    {
                        "event_type": "resource",
                        "actor_entity_id": actor,
                        "resource_entity_id": resource,
                        "action": "spend",
                        "amount": 5,
                    },
                ],
                artifact_id=f"{prefix}:legal-use",
            )
            self.accept(service, host, use)
            state = self.state_for(service, actor)
            self.assertEqual(5, state["resources"][0]["balance"])
            self.assertTrue(state["abilities"][0]["acquired"])
            self.assertFalse(state["abilities"][0]["available"])

            cooldown_violation = self.save(
                service,
                events=[
                    {
                        "event_type": "ability",
                        "owner_entity_id": actor,
                        "ability_entity_id": ability,
                        "action": "use",
                        "story_coordinate": self.coordinate(profile, 7),
                    },
                    {
                        "event_type": "resource",
                        "actor_entity_id": actor,
                        "resource_entity_id": resource,
                        "action": "spend",
                        "amount": 5,
                    },
                ],
                artifact_id=f"{prefix}:cooldown-violation",
            )
            self.assert_accept_blocked(
                service,
                host,
                cooldown_violation,
                "POWER_COOLDOWN_ACTIVE",
            )
            executable_after_cooldown = service.explain_power_action(
                actor,
                ability_id=ability,
                action="use",
                story_coordinate=self.coordinate(profile, 9),
            )
            self.assertTrue(executable_after_cooldown["executable"])

            deactivate_sources = self.save(
                service,
                events=[
                    {
                        "event_type": "power_binding",
                        "actor_entity_id": actor,
                        "binding_id": binding_id,
                        "source_entity_id": source_item,
                        "action": "unbind",
                        "ability_entity_ids": [ability],
                        "story_coordinate": self.coordinate(profile, 9),
                    },
                    {
                        "event_type": "qualification",
                        "actor_entity_id": actor,
                        "qualification_entity_id": qualification,
                        "action": "consume",
                        "quantity": 1,
                        "story_coordinate": self.coordinate(profile, 9),
                    },
                    {
                        "event_type": "status_effect",
                        "actor_entity_id": actor,
                        "status_entity_id": status,
                        "action": "remove",
                        "stacks": 1,
                        "story_coordinate": self.coordinate(profile, 9),
                    },
                ],
                artifact_id=f"{prefix}:deactivate-sources",
            )
            self.accept(service, host, deactivate_sources)
            state = self.state_for(service, actor)
            self.assertFalse(state["bindings"][0]["active"])
            self.assertFalse(state["qualifications"][0]["active"])
            self.assertFalse(state["statuses"][0]["active"])
            invalidated = service.explain_power_action(
                actor,
                ability_id=ability,
                action="use",
                story_coordinate=self.coordinate(profile, 10),
            )
            self.assertFalse(invalidated["executable"])
            self.assertIn(
                "POWER_PREREQUISITE_UNMET",
                {
                    reason["code"]
                    for reason in invalidated["reasons"]
                },
            )

            unbridged_conversion = self.save(
                service,
                events=[
                    {
                        "event_type": "resource",
                        "actor_entity_id": actor,
                        "resource_entity_id": resource,
                        "action": "convert",
                        "amount": 1,
                        "target_resource_entity_id": target_resource,
                        "target_amount": 1,
                        "conversion_rule_entity_id": (
                            missing_conversion_rule
                        ),
                    }
                ],
                artifact_id=f"{prefix}:unbridged-conversion",
            )
            self.assert_accept_blocked(
                service,
                host,
                unbridged_conversion,
                "POWER_INTERACTION_UNKNOWN",
            )
            with service.store.read_connection() as connection:
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM bridge_rules"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM conversion_rules"
                    ).fetchone()[0],
                )

            projection_hash = service.projection_hash()
            first = service.replay()
            second = service.replay()
            self.assertEqual(
                projection_hash,
                first["projection_hash"],
            )
            self.assertEqual(
                projection_hash,
                second["projection_hash"],
            )

    def test_all_profiles_share_the_same_runtime_invariants(self) -> None:
        profiles = adapter_registry().profiles()
        self.assertEqual(12, len(profiles))
        for profile in profiles:
            target_profile = next(
                candidate
                for candidate in profiles
                if candidate != profile
            )
            with self.subTest(profile=profile):
                self.exercise_profile(profile, target_profile)


if __name__ == "__main__":
    unittest.main()
