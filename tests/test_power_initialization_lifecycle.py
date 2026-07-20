from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from scripts.continuity import (  # noqa: E402
    ContinuityError,
    ContinuityService,
    HostApprovalAuthority,
)
from scripts.plot_init import (  # noqa: E402
    PlotInitService,
    proposal_to_lifecycle_package,
)
from tests.test_power_initialization import cultivation_seed  # noqa: E402


class PowerInitializationLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.project = self.workspace / "novel"
        self.project.mkdir()
        initializer = PlotInitService(self.workspace)
        started = initializer.start(
            project_root=self.project,
            mode="new",
            interaction_profile="deep",
            seed=cultivation_seed(),
            bundle_schema_version="plot-rag-init/v2",
            idempotency_key="power-lifecycle-start",
        )
        self.frozen = initializer.propose(
            started["session_id"],
            expected_session_revision=started["session_revision"],
            idempotency_key="power-lifecycle-propose",
        )["proposal"]
        self.lifecycle = proposal_to_lifecycle_package(self.frozen)
        self.power_spec_package = dict(
            self.lifecycle["power_spec_package"]
        )
        self.service = ContinuityService(self.project)
        self.host = HostApprovalAuthority(
            self.service,
            issuer="power-initialization-unittest",
            channel="interactive_test",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def register_power_entities(self) -> None:
        for entity in self.power_spec_package["entities"]:
            self.service.register_entity(
                str(entity["entity_type"]),
                str(entity["canonical_name"]),
                entity_id=str(entity["entity_id"]),
                aliases=tuple(entity.get("aliases") or ()),
                attributes=dict(entity.get("attributes") or {}),
            )

    def save_and_accept_power_spec(self) -> tuple[dict, dict]:
        self.register_power_entities()
        package = self.power_spec_package
        proposal = self.service.save_proposal(
            events=package["events"],
            payload={
                "lifecycle_package": package,
                "package_hash": package["package_hash"],
                "power_package_hash": package["power_package_hash"],
                "parent_initialization_proposal_id": (
                    package["parent_initialization_proposal_id"]
                ),
            },
            artifact_id=package["proposal_id"],
            artifact_kind="power_spec",
            artifact_stage="bootstrap",
            branch_id="main",
            chapter_no=None,
            scene_index=None,
            prepared_canon_revision=0,
            source_role="setting",
            proposal_kind="power_spec_change",
            idempotency_key="power-lifecycle-save-spec",
        )
        grant = self.host.issue(
            proposal["proposal_id"],
            expected_canon_revision=0,
            operations=("accept_power_spec",),
        )
        commit = self.service.accept_proposal(
            proposal["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=0,
        )
        return proposal, commit

    def binding_for(
        self,
        proposal: dict,
        commit: dict,
        **overrides: object,
    ) -> dict:
        binding = {
            "proposal_id": proposal["proposal_id"],
            "commit_id": commit["commit_id"],
            "package_hash": self.power_spec_package["package_hash"],
            "power_package_hash": self.power_spec_package[
                "power_package_hash"
            ],
            "projection_hash": commit["projection_hash"],
            "active_canon_revision": commit["active_canon_revision"],
        }
        binding.update(overrides)
        return binding

    def issue_initialization_grant(self, proposal: dict, revision: int) -> dict:
        return self.host.issue(
            proposal["proposal_id"],
            expected_canon_revision=revision,
            operations=("accept_initialization",),
        )

    def test_v2_initialization_requires_accepted_power_spec_binding(
        self,
    ) -> None:
        proposal = self.service.save_initialization_bundle(
            self.frozen,
            artifact_id=self.frozen["proposal_id"],
            idempotency_key="power-lifecycle-save-init-without-spec",
        )
        grant = self.issue_initialization_grant(proposal, 0)
        with self.assertRaises(ContinuityError) as caught:
            self.service.accept_proposal(
                proposal["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=0,
            )
        self.assertEqual(
            caught.exception.code,
            "POWER_SPEC_ACCEPTANCE_REQUIRED",
        )
        self.assertEqual(
            caught.exception.details[
                "parent_initialization_proposal_id"
            ],
            self.lifecycle["proposal_id"],
        )
        self.assertEqual(
            self.service.get_canon_revisions(),
            {"head": 0, "active": 0},
        )

    def test_v2_initialization_rebases_and_accepts_after_power_spec(
        self,
    ) -> None:
        stale_initialization = self.service.save_initialization_bundle(
            self.frozen,
            artifact_id=self.frozen["proposal_id"],
            idempotency_key="power-lifecycle-save-init-before-spec-r0",
        )
        self.assertEqual(
            stale_initialization["prepared_canon_revision"],
            0,
        )
        spec_proposal, spec_commit = self.save_and_accept_power_spec()
        binding = self.binding_for(spec_proposal, spec_commit)
        initialization = self.service.save_initialization_bundle(
            self.frozen,
            artifact_id=self.frozen["proposal_id"],
            prepared_canon_revision=1,
            power_spec_binding=binding,
            idempotency_key="power-lifecycle-save-init-after-spec-r1",
        )
        self.assertEqual(initialization["prepared_canon_revision"], 1)
        self.assertEqual(
            initialization["artifact_revision"],
            stale_initialization["artifact_revision"] + 1,
        )
        self.assertEqual(
            initialization["payload"]["package_hash"],
            stale_initialization["payload"]["package_hash"],
        )
        self.assertEqual(
            initialization["payload"]["power_spec_binding"],
            binding,
        )
        grant = self.issue_initialization_grant(initialization, 1)
        commit = self.service.accept_proposal(
            initialization["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=1,
        )
        self.assertEqual(commit["active_canon_revision"], 2)
        self.assertEqual(
            self.service.get_canon_revisions(),
            {"head": 2, "active": 2},
        )
        with self.service.store.read_connection() as connection:
            self.assertGreater(
                connection.execute(
                    "SELECT COUNT(*) FROM power_system_specs"
                ).fetchone()[0],
                0,
            )
            self.assertGreater(
                connection.execute(
                    "SELECT COUNT(*) FROM actor_progression_state"
                ).fetchone()[0],
                0,
            )
            self.assertGreater(
                connection.execute(
                    "SELECT COUNT(*) FROM actor_ability_state"
                ).fetchone()[0],
                0,
            )

    def test_v2_initialization_rejects_hash_mismatched_binding(self) -> None:
        spec_proposal, spec_commit = self.save_and_accept_power_spec()
        binding = self.binding_for(
            spec_proposal,
            spec_commit,
            package_hash="sha256-tampered-package",
        )
        initialization = self.service.save_initialization_bundle(
            self.frozen,
            artifact_id=self.frozen["proposal_id"],
            prepared_canon_revision=1,
            power_spec_binding=binding,
            idempotency_key="power-lifecycle-save-init-bad-hash",
        )
        grant = self.issue_initialization_grant(initialization, 1)
        with self.assertRaises(ContinuityError) as caught:
            self.service.accept_proposal(
                initialization["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=1,
            )
        self.assertEqual(
            caught.exception.code,
            "POWER_SPEC_BINDING_MISMATCH",
        )
        self.assertEqual(
            caught.exception.details["expected_package_hash"],
            self.power_spec_package["package_hash"],
        )
        self.assertEqual(
            caught.exception.details["actual_package_hash"],
            "sha256-tampered-package",
        )

    def test_v2_initialization_rejects_retracted_power_spec(self) -> None:
        spec_proposal, spec_commit = self.save_and_accept_power_spec()
        retract_grant = self.host.issue(
            spec_proposal["proposal_id"],
            expected_canon_revision=1,
            operations=("retract",),
        )
        self.service.retract_proposal(
            spec_proposal["proposal_id"],
            approval_id=retract_grant["approval_id"],
            expected_canon_revision=1,
            reason="test retraction",
        )
        initialization = self.service.save_initialization_bundle(
            self.frozen,
            artifact_id=self.frozen["proposal_id"],
            prepared_canon_revision=2,
            power_spec_binding=self.binding_for(
                spec_proposal,
                spec_commit,
                active_canon_revision=2,
            ),
            idempotency_key="power-lifecycle-save-init-retracted-spec",
        )
        grant = self.issue_initialization_grant(initialization, 2)
        with self.assertRaises(ContinuityError) as caught:
            self.service.accept_proposal(
                initialization["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=2,
            )
        self.assertEqual(
            caught.exception.code,
            "POWER_SPEC_COMMIT_INACTIVE",
        )
        self.assertEqual(
            caught.exception.details["power_spec_commit_id"],
            spec_commit["commit_id"],
        )

    def test_v2_initialization_rejects_missing_definition_projection(
        self,
    ) -> None:
        spec_proposal, spec_commit = self.save_and_accept_power_spec()
        with self.service.store.transaction() as connection:
            connection.execute("DELETE FROM ability_definitions")
        initialization = self.service.save_initialization_bundle(
            self.frozen,
            artifact_id=self.frozen["proposal_id"],
            prepared_canon_revision=1,
            power_spec_binding=self.binding_for(
                spec_proposal,
                spec_commit,
            ),
            idempotency_key="power-lifecycle-save-init-missing-projection",
        )
        grant = self.issue_initialization_grant(initialization, 1)
        with self.assertRaises(ContinuityError) as caught:
            self.service.accept_proposal(
                initialization["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=1,
            )
        self.assertEqual(
            caught.exception.code,
            "POWER_SPEC_PROJECTION_MISSING",
        )
        definitions = caught.exception.details["definitions"]
        self.assertTrue(
            any(
                item["spec_type"] == "ability_definition"
                for item in definitions
            )
        )

    def test_v2_status_effect_requires_active_status_definition(
        self,
    ) -> None:
        spec_proposal, spec_commit = self.save_and_accept_power_spec()
        status_entity_id = self.service.register_entity(
            "status_effect",
            "临时灼烧",
        )["entity_id"]
        actor_entity_id = next(
            str(event["actor_entity_id"])
            for event in self.lifecycle["events"]
            if event.get("event_type") == "progression"
        )
        modified_lifecycle = copy.deepcopy(self.lifecycle)
        modified_lifecycle["events"].append(
            {
                "event_type": "status_effect",
                "scope": "current",
                "actor_entity_id": actor_entity_id,
                "status_entity_id": status_entity_id,
                "action": "apply",
                "stacks": 1,
                "state": {},
            }
        )
        initialization = self.service.save_initialization_bundle(
            modified_lifecycle,
            artifact_id="status-effect-initialization",
            prepared_canon_revision=1,
            power_spec_binding=self.binding_for(
                spec_proposal,
                spec_commit,
            ),
            idempotency_key="power-lifecycle-save-init-status-effect",
        )
        grant = self.issue_initialization_grant(initialization, 1)
        with self.assertRaises(ContinuityError) as caught:
            self.service.accept_proposal(
                initialization["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=1,
            )
        self.assertEqual(
            caught.exception.code,
            "POWER_RUNTIME_DEFINITION_MISSING",
        )
        self.assertEqual(
            caught.exception.details["definitions"],
            [
                {
                    "spec_type": "status_definition",
                    "spec_entity_id": status_entity_id,
                    "actual_status": None,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
