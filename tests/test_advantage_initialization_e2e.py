from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping


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
from scripts.continuity.advantages import (  # noqa: E402
    ADVANTAGE_EVENT_TYPES,
    AdvantageProjectionState,
)
from scripts.plot_init import (  # noqa: E402
    ADVANTAGE_SCHEMA_VERSION,
    ADVANTAGE_SIDECAR_PATH,
    PlotInitError,
    PlotInitService,
    advantage_package_from_frozen_proposal,
    proposal_to_lifecycle_package,
    recompute_advantage_package_hash,
    verify_materialized_advantage_sidecar,
)
from tests.test_advantage_initialization import (  # noqa: E402
    minimal_dossier,
)
from tests.test_plot_init import complete_seed  # noqa: E402


def advantage_seed(*, stress_record_only: bool = False) -> dict[str, Any]:
    seed = complete_seed()
    seed.update(minimal_dossier())
    if stress_record_only:
        # The accepted runtime snapshot is authoritative.  This historical
        # ledger row must be recorded without re-applying its output.
        seed["advantage_ledger"][0]["output"]["演算点"] = 9
    return seed


def file_fingerprints(root: Path) -> dict[str, tuple[int, int, str]]:
    result: dict[str, tuple[int, int, str]] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        result[path.relative_to(root).as_posix()] = (
            int(stat.st_size),
            int(stat.st_mtime_ns),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return result


def advantage_artifact(bundle: Mapping[str, Any]) -> dict[str, Any]:
    matches = [
        dict(item)
        for item in bundle.get("artifact_manifest") or []
        if isinstance(item, Mapping)
        and str(item.get("path") or "") == ADVANTAGE_SIDECAR_PATH
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one Advantage sidecar artifact, got {len(matches)}"
        )
    return matches[0]


class AdvantageInitializationE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.project = self.workspace / "novel"
        self.project.mkdir()
        self.initializer = PlotInitService(self.workspace)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def freeze(
        self,
        *,
        stress_record_only: bool = False,
        key_prefix: str = "adv-init-e2e",
    ) -> dict[str, Any]:
        started = self.initializer.start(
            project_root=self.project,
            mode="new",
            target_profile="plot_ready",
            interaction_profile="deep",
            seed=advantage_seed(
                stress_record_only=stress_record_only,
            ),
            bundle_schema_version="plot-rag-init/v1",
            idempotency_key=f"{key_prefix}-start",
        )
        self.assertEqual("READY_TO_PROPOSE", started["status"])
        return self.initializer.propose(
            started["session_id"],
            expected_session_revision=started["session_revision"],
            idempotency_key=f"{key_prefix}-propose",
        )["proposal"]

    def plain_bundle(
        self,
        frozen: Mapping[str, Any],
        *,
        branch_id: str = "main",
    ) -> dict[str, Any]:
        bundle = copy.deepcopy(dict(frozen["bundle"]))
        bundle.pop("bundle_hash", None)
        bundle.pop("package_hash", None)
        bundle["target_project_real_path"] = str(self.project)
        bundle["branch_id"] = branch_id
        return bundle

    def test_dry_run_start_and_propose_bind_one_immutable_sidecar(
        self,
    ) -> None:
        before = file_fingerprints(self.project)
        dry_run = self.initializer.dry_run(
            project_root=self.project,
            mode="new",
            target_profile="plot_ready",
            interaction_profile="deep",
            seed=advantage_seed(),
            bundle_schema_version="plot-rag-init/v1",
        )

        self.assertEqual("READY_TO_PROPOSE", dry_run["status"])
        self.assertFalse(dry_run["persisted"])
        self.assertFalse(dry_run["database_touched"])
        self.assertFalse(self.initializer.database_path.exists())
        self.assertEqual(before, file_fingerprints(self.project))

        dry_bundle = dry_run["bundle"]
        dry_artifact = advantage_artifact(dry_bundle)
        dry_reference = dry_bundle["provenance"]["advantage_sidecars"]
        self.assertEqual(1, len(dry_reference))
        self.assertEqual("advantage_sidecar", dry_artifact["logical_owner"])
        self.assertEqual("create", dry_artifact["operation"])
        self.assertFalse(dry_artifact["materialized"])
        self.assertEqual(
            dry_artifact["advantage_package_hash"],
            dry_reference[0]["package_hash"],
        )
        self.assertEqual(
            dry_artifact["proposed_new_hash"],
            dry_reference[0]["content_hash"],
        )
        self.assertFalse((self.project / ADVANTAGE_SIDECAR_PATH).exists())

        started = self.initializer.start(
            project_root=self.project,
            mode="new",
            target_profile="plot_ready",
            interaction_profile="deep",
            seed=advantage_seed(),
            bundle_schema_version="plot-rag-init/v1",
            idempotency_key="adv-init-surfaces-start",
        )
        self.assertEqual("READY_TO_PROPOSE", started["status"])
        self.assertTrue(self.initializer.database_path.is_file())
        self.assertEqual(before, file_fingerprints(self.project))

        frozen = self.initializer.propose(
            started["session_id"],
            expected_session_revision=started["session_revision"],
            idempotency_key="adv-init-surfaces-propose",
        )["proposal"]
        package, reference = advantage_package_from_frozen_proposal(frozen)
        self.assertEqual("PROPOSAL_FROZEN", frozen["status"])
        self.assertEqual(
            frozen["apply_plan"]["advantage_sidecar"],
            reference,
        )
        self.assertEqual(
            frozen["bundle"]["provenance"]["advantage_sidecars"],
            [reference],
        )
        self.assertEqual(
            package["package_hash"],
            reference["package_hash"],
        )
        self.assertEqual(
            package["package_hash"],
            dry_reference[0]["package_hash"],
        )
        config_artifact = next(
            item
            for item in frozen["bundle"]["artifact_manifest"]
            if item["path"] == ".plot-rag/config.json"
        )
        proposed_config = json.loads(config_artifact["proposed_content"])
        self.assertEqual(
            {
                "schema_version": reference["schema_version"],
                "sidecar_path": reference["path"],
                "package_hash": reference["package_hash"],
                "content_hash": reference["content_hash"],
            },
            {
                key: proposed_config["advantage"][key]
                for key in (
                    "schema_version",
                    "sidecar_path",
                    "package_hash",
                    "content_hash",
                )
            },
        )
        self.assertEqual(before, file_fingerprints(self.project))

    def test_frozen_sidecar_reference_and_artifact_tamper_fail_closed(
        self,
    ) -> None:
        frozen = self.freeze(key_prefix="adv-init-tamper")

        reference_tamper = copy.deepcopy(frozen)
        reference_tamper["apply_plan"]["advantage_sidecar"][
            "content_hash"
        ] = "0" * 64
        with self.assertRaises(PlotInitError) as reference_error:
            proposal_to_lifecycle_package(reference_tamper)
        self.assertEqual(
            "ADVANTAGE_SIDECAR_REFERENCE_MISMATCH",
            reference_error.exception.code,
        )

        artifact_tamper = copy.deepcopy(frozen)
        artifact = next(
            item
            for item in artifact_tamper["bundle"]["artifact_manifest"]
            if item["path"] == ADVANTAGE_SIDECAR_PATH
        )
        artifact["proposed_content"] += " "
        with self.assertRaises(PlotInitError) as artifact_error:
            proposal_to_lifecycle_package(artifact_tamper)
        self.assertEqual(
            "PACKAGE_HASH_MISMATCH",
            artifact_error.exception.code,
        )

    def test_plain_initialization_rejects_invalid_advantage_sidecar_contract(
        self,
    ) -> None:
        frozen = self.freeze(key_prefix="adv-init-plain-invalid")
        plain = self.plain_bundle(frozen)
        artifact = next(
            item
            for item in plain["artifact_manifest"]
            if item["path"] == ADVANTAGE_SIDECAR_PATH
        )
        payload = json.loads(str(artifact["proposed_content"]))
        old_package_hash = str(payload["package_hash"])
        old_content_hash = str(artifact["proposed_new_hash"])
        payload["definitions"] = "corrupt"
        payload["package_hash"] = recompute_advantage_package_hash(payload)
        content = (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        def replace_reference_hashes(value: Any) -> Any:
            if isinstance(value, dict):
                for key, item in list(value.items()):
                    value[key] = replace_reference_hashes(item)
                return value
            if isinstance(value, list):
                for index, item in enumerate(value):
                    value[index] = replace_reference_hashes(item)
                return value
            if isinstance(value, str):
                return value.replace(
                    old_package_hash,
                    payload["package_hash"],
                ).replace(old_content_hash, content_hash)
            return value

        replace_reference_hashes(plain)
        artifact["proposed_content"] = content
        artifact["proposed_new_hash"] = content_hash
        artifact["advantage_package_hash"] = payload["package_hash"]
        config_artifact = next(
            item
            for item in plain["artifact_manifest"]
            if item["path"] == ".plot-rag/config.json"
        )
        config_artifact["proposed_new_hash"] = hashlib.sha256(
            str(config_artifact["proposed_content"]).encode("utf-8")
        ).hexdigest()
        plain["package_hash"] = "caller-self-reported"

        service = ContinuityService(self.project)
        with self.assertRaises(ContinuityError) as caught:
            service.save_initialization_bundle(plain)
        self.assertEqual(
            "ADVANTAGE_PACKAGE_SCHEMA_INVALID",
            caught.exception.code,
        )
        self.assertEqual(
            {"head": 0, "active": 0},
            service.get_canon_revisions(),
        )
        self.assertFalse((self.project / ADVANTAGE_SIDECAR_PATH).exists())

    def test_plain_initialization_materializes_typed_advantage_events(
        self,
    ) -> None:
        frozen = self.freeze(key_prefix="adv-init-plain-complete")
        expected_package, _reference = (
            advantage_package_from_frozen_proposal(frozen)
        )
        plain = self.plain_bundle(frozen)
        plain["package_hash"] = "caller-hash-is-not-authoritative"
        service = ContinuityService(self.project)
        host = HostApprovalAuthority(
            service,
            issuer="advantage-plain-initialization-e2e",
            channel="interactive_test",
        )
        saved = service.save_initialization_bundle(
            plain,
            idempotency_key="adv-init-plain-complete-save",
        )
        advantage_events = [
            event
            for event in saved["events"]
            if event["event_type"] in ADVANTAGE_EVENT_TYPES
        ]
        self.assertGreater(len(advantage_events), 0)
        self.assertTrue(
            all(event["branch_id"] == "main" for event in advantage_events)
        )
        self.assertEqual(
            expected_package["package_hash"],
            saved["payload"]["lifecycle_package"][
                "advantage_package_hash"
            ],
        )
        canonical_hash = saved["payload"]["package_hash"]
        second = self.plain_bundle(frozen)
        second["package_hash"] = "another-caller-hash"
        second_package, _raw = service._initialization_package(second)
        self.assertEqual(canonical_hash, second_package["package_hash"])

        grant = host.issue(
            saved["proposal_id"],
            expected_canon_revision=0,
            operations=("accept_initialization",),
        )
        accepted = service.accept_proposal(
            saved["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=0,
        )
        self.assertEqual(
            "materialized",
            accepted["advantage_sidecar_materialization"]["status"],
        )
        verified = verify_materialized_advantage_sidecar(
            expected_package,
            self.project,
        )
        self.assertEqual("verified", verified["status"])
        with service.store.read_connection() as connection:
            state = AdvantageProjectionState.from_connection(connection)
        self.assertEqual(
            {
                record["advantage_id"]
                for record in expected_package["definitions"]
            },
            set(state.definitions),
        )

    def test_plain_non_main_initialization_fails_closed_before_persistence(
        self,
    ) -> None:
        frozen = self.freeze(key_prefix="adv-init-plain-branch")
        service = ContinuityService(self.project)
        plain = self.plain_bundle(frozen, branch_id="what-if")

        with self.assertRaises(ContinuityError) as caught:
            service.save_initialization_bundle(
                plain,
                idempotency_key="adv-init-plain-branch-save",
            )
        self.assertEqual(
            "INITIALIZATION_BRANCH_UNSUPPORTED",
            caught.exception.code,
        )
        with service.store.read_connection() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM proposals"
                ).fetchone()[0],
            )
        self.assertEqual(
            {"head": 0, "active": 0},
            service.get_canon_revisions(),
        )
        self.assertFalse((self.project / ADVANTAGE_SIDECAR_PATH).exists())

    def test_adapter_emits_complete_ordered_advantage_event_chain(
        self,
    ) -> None:
        frozen = self.freeze(key_prefix="adv-init-adapter")
        package, reference = advantage_package_from_frozen_proposal(frozen)
        lifecycle = proposal_to_lifecycle_package(frozen)
        advantage_events = [
            event
            for event in lifecycle["events"]
            if event["event_type"] in ADVANTAGE_EVENT_TYPES
        ]

        self.assertEqual(reference, lifecycle["advantage_sidecar"])
        self.assertEqual(
            package["package_hash"],
            lifecycle["advantage_package_hash"],
        )
        self.assertEqual(
            len(advantage_events),
            lifecycle["advantage_info"]["event_count"],
        )
        self.assertEqual(
            len(advantage_events),
            len({event["event_id"] for event in advantage_events}),
        )
        self.assertTrue(
            all(
                event["schema_version"] == ADVANTAGE_SCHEMA_VERSION
                and event["evidence"]["advantage_package_hash"]
                == package["package_hash"]
                for event in advantage_events
            )
        )
        self.assertEqual(
            list(range(len(advantage_events))),
            sorted(
                int(event["story_coordinate"]["ordinal"])
                for event in advantage_events
            ),
        )

        definition_ids = {
            event["advantage_id"]
            for event in advantage_events
            if event["event_type"] == "advantage_spec"
            and event.get("spec_type") == "advantage_definition"
        }
        self.assertEqual(
            {
                record["advantage_id"]
                for record in package["definitions"]
            },
            definition_ids,
        )
        self.assertEqual(
            {record["anchor_id"] for record in package["anchors"]},
            {
                event["anchor_id"]
                for event in advantage_events
                if event["event_type"] == "advantage_anchor"
            },
        )
        self.assertEqual(
            {record["module_id"] for record in package["modules"]},
            {
                event["module_id"]
                for event in advantage_events
                if event["event_type"] == "advantage_module"
                and event.get("action") == "define"
            },
        )
        self.assertEqual(
            {record["slot_id"] for record in package["runtime_slots"]},
            {
                event["slot_id"]
                for event in advantage_events
                if event["event_type"] == "advantage_spec"
                and event.get("spec_type") == "runtime_slot"
            },
        )
        self.assertEqual(
            {
                record["narrative_contract_id"]
                for record in package["narrative_contracts"]
            },
            {
                event["narrative_contract_id"]
                for event in advantage_events
                if event["event_type"] == "advantage_spec"
                and event.get("spec_type") == "narrative_contract"
            },
        )

        record_only = [
            event
            for event in advantage_events
            if event["event_type"]
            in {"advantage_reward", "advantage_cost"}
            and event.get("record_only") is True
        ]
        self.assertEqual(
            {
                (
                    record["entry_id"],
                    record["entry_kind"],
                    json.dumps(
                        record["input"],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    json.dumps(
                        record["output"],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    json.dumps(
                        record["loss"],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
                for record in package["ledger_bootstrap"]
            },
            {
                (
                    event["entry_id"],
                    event["ledger_entry_kind"],
                    json.dumps(
                        event["input"],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    json.dumps(
                        event["output"],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    json.dumps(
                        event["loss"],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
                for event in record_only
            },
        )

        planned_module_ids = {
            record["module_id"]
            for record in package["modules"]
            if record["status"] != "canon"
        }
        runtime_module_events = [
            event
            for event in advantage_events
            if event["event_type"] == "advantage_module"
            and event.get("action") in {"unlock", "enable"}
        ]
        self.assertFalse(
            planned_module_ids.intersection(
                event["module_id"] for event in runtime_module_events
            )
        )

        def category(event: Mapping[str, Any]) -> int | None:
            event_type = str(event.get("event_type") or "")
            action = str(event.get("action") or "")
            spec_type = str(event.get("spec_type") or "")
            if event_type == "entity":
                return 0
            if event_type == "advantage_spec":
                if spec_type == "advantage_definition":
                    return 10
                return 40
            if event_type == "advantage_anchor":
                return 20
            if event_type == "advantage_module" and action == "define":
                return 30
            if event_type == "advantage_bind":
                return 50
            if event_type == "advantage_activate":
                return 60
            if event_type == "advantage_module":
                return 70
            if event_type in {"advantage_reward", "advantage_cost"}:
                return 80
            if event_type == "advantage_reveal":
                return 90
            if event_type == "advantage_contract":
                return 100
            return None

        ordered_categories = [
            value
            for event in lifecycle["events"]
            if (value := category(event)) is not None
        ]
        self.assertEqual(
            ordered_categories,
            sorted(ordered_categories),
        )
        last_advantage = max(
            index
            for index, event in enumerate(lifecycle["events"])
            if event["event_type"] in ADVANTAGE_EVENT_TYPES
        )
        first_other_runtime = min(
            index
            for index, event in enumerate(lifecycle["events"])
            if event["event_type"] not in ADVANTAGE_EVENT_TYPES
            and event["event_type"] != "entity"
        )
        self.assertLess(last_advantage, first_other_runtime)

    def test_accept_replay_matches_sidecar_and_never_reapplies_ledger(
        self,
    ) -> None:
        frozen = self.freeze(
            stress_record_only=True,
            key_prefix="adv-init-accept",
        )
        package, _reference = advantage_package_from_frozen_proposal(frozen)
        advantage_id = package["definitions"][0]["advantage_id"]
        expected_runtime = package["runtime_bootstrap"][0]

        service = ContinuityService(self.project)
        host = HostApprovalAuthority(
            service,
            issuer="advantage-initialization-e2e",
            channel="interactive_test",
        )
        saved = service.save_initialization_bundle(
            frozen,
            artifact_id=frozen["proposal_id"],
            idempotency_key="adv-init-accept-save",
        )
        grant = host.issue(
            saved["proposal_id"],
            expected_canon_revision=0,
            operations=("accept_initialization",),
        )
        commit = service.accept_proposal(
            saved["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=0,
        )

        self.assertEqual(1, commit["active_canon_revision"])
        self.assertTrue(
            commit["advantage_projection_hash"].startswith(
                "advantage_projection_"
            )
        )
        verified = verify_materialized_advantage_sidecar(
            frozen,
            self.project,
        )
        self.assertEqual("verified", verified["status"])
        self.assertTrue(
            (self.project / ".plot-rag" / "金手指").is_dir()
        )

        with service.store.read_connection() as connection:
            state = AdvantageProjectionState.from_connection(connection)

        self.assertEqual(
            {record["advantage_id"] for record in package["definitions"]},
            set(state.definitions),
        )
        for record in package["definitions"]:
            actual = state.definitions[record["advantage_id"]]
            self.assertEqual(record["title"], actual["title"])
            self.assertEqual(record["profiles"], actual["profiles_json"])
            self.assertEqual(record["status"], actual["advantage_status"])

        self.assertEqual(
            {record["anchor_id"] for record in package["anchors"]},
            set(state.anchors),
        )
        for record in package["anchors"]:
            actual = state.anchors[record["anchor_id"]]
            self.assertEqual(record["anchor_ref_id"], actual["anchor_ref_id"])
            self.assertEqual(
                record.get("owner_entity_id"),
                actual["owner_entity_id"],
            )
            self.assertEqual(
                record["binding_state"],
                actual["binding_state"],
            )
            self.assertEqual(record["status"], actual["authority_status"])

        canon_modules = [
            record
            for record in package["modules"]
            if record["status"] == "canon"
        ]
        planned_modules = [
            record
            for record in package["modules"]
            if record["status"] == "planned"
        ]
        self.assertEqual(
            {record["module_id"] for record in canon_modules},
            set(state.modules),
        )
        for record in canon_modules:
            actual = state.modules[record["module_id"]]
            self.assertEqual(record["status"], actual["authority_status"])
            self.assertEqual(
                record["module_status"],
                actual["module_status"],
            )

        materialized_package = json.loads(
            (self.project / ADVANTAGE_SIDECAR_PATH).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            {record["module_id"] for record in package["modules"]},
            {
                record["module_id"]
                for record in materialized_package["modules"]
            },
        )
        self.assertEqual(
            {record["knowledge_id"] for record in package["knowledge"]},
            {
                record["knowledge_id"]
                for record in materialized_package["knowledge"]
            },
        )
        planned_facts = service.query_facts(
            scope="planned",
            fact_type="advantage_module",
        )["facts"]
        self.assertEqual(
            {record["module_id"] for record in planned_modules},
            {
                str(fact["value"]["module_id"])
                for fact in planned_facts
            },
        )
        generation_contexts = service.query_special_item_context(
            advantage_id,
            visibility="generation",
        )["contexts"]
        self.assertEqual(1, len(generation_contexts))
        generation_module_ids = {
            str(record["module_id"])
            for record in generation_contexts[0]["modules"]
        }
        self.assertEqual(
            {record["module_id"] for record in canon_modules},
            generation_module_ids,
        )
        self.assertFalse(
            generation_module_ids.intersection(
                record["module_id"] for record in planned_modules
            )
        )

        runtime = state.runtime[(advantage_id, "main")]
        self.assertEqual(expected_runtime["stage"], runtime["stage"])
        self.assertEqual(
            bool(expected_runtime["enabled"]),
            bool(runtime["enabled"]),
        )
        self.assertEqual(
            expected_runtime.get("charges"),
            runtime["charges"],
        )
        self.assertEqual(
            expected_runtime.get("max_charges"),
            runtime["max_charges"],
        )
        self.assertEqual(
            expected_runtime["resources"],
            runtime["resources_json"],
        )
        self.assertEqual(
            sorted(expected_runtime["unlocked_modules"]),
            runtime["unlocked_modules_json"],
        )
        self.assertEqual(
            expected_runtime.get("runtime_metadata", {}),
            runtime["runtime_json"],
        )
        self.assertEqual(
            float(expected_runtime["pollution"]),
            float(runtime["pollution"]),
        )
        self.assertEqual(
            float(expected_runtime["exposure"]),
            float(runtime["exposure"]),
        )
        self.assertEqual(
            float(expected_runtime["debt"]),
            float(runtime["debt"]),
        )
        # The historical 演算点=9 ledger output is recorded, not applied.
        self.assertEqual(2, runtime["resources_json"]["演算点"])

        for record in package["ledger_bootstrap"]:
            actual = state.ledger[record["entry_id"]]
            self.assertEqual(record["entry_kind"], actual["entry_kind"])
            self.assertEqual(record["input"], actual["input_json"])
            self.assertEqual(record["output"], actual["output_json"])
            self.assertEqual(record["loss"], actual["loss_json"])

        canon_knowledge = [
            record
            for record in package["knowledge"]
            if record["status"] == "canon"
        ]
        planned_knowledge = [
            record
            for record in package["knowledge"]
            if record["status"] == "planned"
        ]
        self.assertEqual(
            {record["knowledge_id"] for record in canon_knowledge},
            set(state.knowledge),
        )
        planned_reveals = service.query_facts(
            scope="planned",
            fact_type="advantage_reveal",
        )["facts"]
        self.assertEqual(
            {record["knowledge_id"] for record in planned_knowledge},
            {
                str(fact["value"]["knowledge_id"])
                for fact in planned_reveals
            },
        )
        generation_claims = {
            json.dumps(
                record["claim"],
                ensure_ascii=False,
                sort_keys=True,
            )
            for record in generation_contexts[0]["knowledge"]
        }
        self.assertFalse(
            generation_claims.intersection(
                json.dumps(
                    record["claim"],
                    ensure_ascii=False,
                    sort_keys=True,
                )
                for record in planned_knowledge
            )
        )
        self.assertFalse(
            any(
                record["knowledge_plane"] == "author_plan"
                for record in generation_contexts[0]["knowledge"]
            )
        )
        self.assertEqual(
            {record["contract_id"] for record in package["contracts"]},
            set(state.contracts),
        )
        self.assertEqual(
            {
                record["narrative_contract_id"]
                for record in package["narrative_contracts"]
            },
            set(state.narrative_contracts),
        )

        accepted_hash = commit["advantage_projection_hash"]
        first_replay = service.replay()
        second_replay = service.replay()
        self.assertEqual(
            accepted_hash,
            first_replay["advantage_projection_hash"],
        )
        self.assertEqual(
            first_replay["advantage_projection_hash"],
            second_replay["advantage_projection_hash"],
        )

        sidecar = self.project / ADVANTAGE_SIDECAR_PATH
        sidecar.write_bytes(sidecar.read_bytes() + b" ")
        with self.assertRaises(PlotInitError) as materialized_tamper:
            verify_materialized_advantage_sidecar(frozen, self.project)
        self.assertEqual(
            "ADVANTAGE_SIDECAR_MATERIALIZED_HASH_MISMATCH",
            materialized_tamper.exception.code,
        )
        replay_after_sidecar_tamper = service.replay()
        self.assertEqual(
            accepted_hash,
            replay_after_sidecar_tamper["advantage_projection_hash"],
        )
        self.assertEqual(
            2,
            service.query_advantage_runtime(advantage_id)["runtime"][
                "resources_json"
            ]["演算点"],
        )

    def test_accept_retry_keeps_the_same_materialized_sidecar(
        self,
    ) -> None:
        frozen = self.freeze(key_prefix="adv-init-retry")
        service = ContinuityService(self.project)
        host = HostApprovalAuthority(
            service,
            issuer="advantage-initialization-retry-e2e",
            channel="interactive_test",
        )
        saved = service.save_initialization_bundle(
            frozen,
            artifact_id=frozen["proposal_id"],
            idempotency_key="adv-init-retry-save",
        )
        grant = host.issue(
            saved["proposal_id"],
            expected_canon_revision=0,
            operations=("accept_initialization",),
        )

        accepted = service.accept_proposal(
            saved["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=0,
        )
        self.assertEqual(
            "materialized",
            accepted["advantage_sidecar_materialization"]["status"],
        )
        sidecar = self.project / ADVANTAGE_SIDECAR_PATH
        first_fingerprint = file_fingerprints(sidecar.parent)[sidecar.name]
        first_bytes = sidecar.read_bytes()
        readable = self.project / ".plot-rag" / "金手指"
        readable_fingerprints = file_fingerprints(readable)

        retried = service.accept_proposal(
            saved["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=0,
        )
        self.assertEqual(accepted["commit_id"], retried["commit_id"])
        self.assertTrue(retried["idempotent_retry"])
        self.assertEqual(
            "already_materialized",
            retried["advantage_sidecar_materialization"]["status"],
        )
        self.assertEqual(
            accepted["advantage_sidecar_materialization"]["content_hash"],
            retried["advantage_sidecar_materialization"]["content_hash"],
        )
        self.assertEqual(first_bytes, sidecar.read_bytes())
        self.assertEqual(
            first_fingerprint,
            file_fingerprints(sidecar.parent)[sidecar.name],
        )
        self.assertEqual(
            readable_fingerprints,
            file_fingerprints(readable),
        )

    def test_accept_rejects_sidecar_target_drift_without_overwrite(
        self,
    ) -> None:
        frozen = self.freeze(key_prefix="adv-init-target-drift")
        service = ContinuityService(self.project)
        host = HostApprovalAuthority(
            service,
            issuer="advantage-initialization-target-drift-e2e",
            channel="interactive_test",
        )
        saved = service.save_initialization_bundle(
            frozen,
            artifact_id=frozen["proposal_id"],
            idempotency_key="adv-init-target-drift-save",
        )
        grant = host.issue(
            saved["proposal_id"],
            expected_canon_revision=0,
            operations=("accept_initialization",),
        )

        sidecar = self.project / ADVANTAGE_SIDECAR_PATH
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        occupied = b'{"foreign":"bytes"}\n'
        sidecar.write_bytes(occupied)

        with self.assertRaises(ContinuityError) as caught:
            service.accept_proposal(
                saved["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=0,
            )
        self.assertEqual("TARGET_HASH_CONFLICT", caught.exception.code)
        self.assertEqual(occupied, sidecar.read_bytes())
        self.assertEqual(
            {"head": 0, "active": 0},
            service.get_canon_revisions(),
        )

    def test_sidecar_directory_is_rejected_before_grant_or_accept_consumption(
        self,
    ) -> None:
        frozen = self.freeze(key_prefix="adv-init-target-directory")
        service = ContinuityService(self.project)
        host = HostApprovalAuthority(
            service,
            issuer="advantage-initialization-directory-e2e",
            channel="interactive_test",
        )
        saved = service.save_initialization_bundle(
            frozen,
            artifact_id=frozen["proposal_id"],
            idempotency_key="adv-init-target-directory-save",
        )
        sidecar = self.project / ADVANTAGE_SIDECAR_PATH
        sidecar.mkdir(parents=True)
        with self.assertRaises(ContinuityError) as issue_error:
            host.issue(
                saved["proposal_id"],
                expected_canon_revision=0,
                operations=("accept_initialization",),
            )
        self.assertEqual("TARGET_TYPE_CONFLICT", issue_error.exception.code)
        with service.store.read_connection() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM approval_grants"
                ).fetchone()[0],
            )
        self.assertEqual(
            {"head": 0, "active": 0},
            service.get_canon_revisions(),
        )

        sidecar.rmdir()
        grant = host.issue(
            saved["proposal_id"],
            expected_canon_revision=0,
            operations=("accept_initialization",),
        )
        sidecar.mkdir()
        with self.assertRaises(ContinuityError) as accept_error:
            service.accept_proposal(
                saved["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=0,
            )
        self.assertEqual("TARGET_TYPE_CONFLICT", accept_error.exception.code)
        with service.store.read_connection() as connection:
            grant_row = connection.execute(
                """
                SELECT consumed_at, accepted_commit_id
                FROM approval_grants
                WHERE proposal_id=?
                """,
                (saved["proposal_id"],),
            ).fetchone()
            self.assertIsNone(grant_row["consumed_at"])
            self.assertIsNone(grant_row["accepted_commit_id"])
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM canon_commits"
                ).fetchone()[0],
            )
        self.assertEqual(
            {"head": 0, "active": 0},
            service.get_canon_revisions(),
        )
        sidecar.rmdir()
        accepted = service.accept_proposal(
            saved["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=0,
        )
        self.assertEqual(1, accepted["active_canon_revision"])

    def test_sidecar_symlink_is_rejected_before_grant(self) -> None:
        frozen = self.freeze(key_prefix="adv-init-target-symlink")
        service = ContinuityService(self.project)
        host = HostApprovalAuthority(
            service,
            issuer="advantage-initialization-symlink-e2e",
            channel="interactive_test",
        )
        saved = service.save_initialization_bundle(
            frozen,
            artifact_id=frozen["proposal_id"],
            idempotency_key="adv-init-target-symlink-save",
        )
        sidecar = self.project / ADVANTAGE_SIDECAR_PATH
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        outside = self.workspace / "outside-advantage.json"
        outside.write_bytes(b"outside")
        try:
            sidecar.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        try:
            with self.assertRaises(ContinuityError) as caught:
                host.issue(
                    saved["proposal_id"],
                    expected_canon_revision=0,
                    operations=("accept_initialization",),
                )
            self.assertEqual(
                "UNSAFE_MATERIALIZATION_PATH",
                caught.exception.code,
            )
            self.assertEqual(b"outside", outside.read_bytes())
            with service.store.read_connection() as connection:
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM approval_grants"
                    ).fetchone()[0],
                )
        finally:
            sidecar.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
