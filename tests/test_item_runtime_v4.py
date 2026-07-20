from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.continuity import (
    ContinuityError,
    ContinuityService,
    HostApprovalAuthority,
)


class ItemRuntimeV4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        runtime = Path(self.temp_dir.name) / ".plot-rag"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "config.json").write_text(
            json.dumps(
                {
                    "items": {
                        "strict_runtime_validation": True,
                        "power_binding_bridge": True,
                    }
                }
            ),
            encoding="utf-8",
        )
        self.service = ContinuityService(self.temp_dir.name)
        self.host = HostApprovalAuthority(
            self.service,
            issuer="item-v4-unittest-host",
            channel="interactive_test",
        )
        self.initial_projection_hash = self.service.projection_hash()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def entity(self, entity_type: str, name: str) -> str:
        return self.service.register_entity(entity_type, name)["entity_id"]

    @staticmethod
    def coordinate(ordinal: int) -> dict[str, object]:
        return {"calendar_id": "item-test-calendar", "ordinal": ordinal}

    def item_event(
        self,
        event_type: str,
        *,
        ordinal: int,
        quote: str,
        **fields: object,
    ) -> dict[str, object]:
        return {
            "schema_version": "plot-rag-delta/v4",
            "event_type": event_type,
            "story_coordinate": self.coordinate(ordinal),
            "knowledge_plane": "objective",
            "evidence": {"quote": quote},
            **fields,
        }

    def proposal(
        self,
        events: list[dict[str, object]],
        *,
        artifact_id: str,
        chapter: int | None = 1,
        stage: str = "final",
        proposal_kind: str = "story_delta",
    ) -> dict[str, object]:
        return self.service.save_proposal(
            events=events,
            artifact_id=artifact_id,
            artifact_stage=stage,
            proposal_kind=proposal_kind,
            branch_id="main",
            chapter_no=chapter,
            scene_index=0 if chapter is not None else None,
        )

    def accept(
        self,
        proposal: dict[str, object],
        *,
        operation: str = "accept",
    ) -> dict[str, object]:
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            str(proposal["proposal_id"]),
            expected_canon_revision=revision,
            operations=(operation,),
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
        chapter: int | None = 1,
        stage: str = "final",
        proposal_kind: str = "story_delta",
        operation: str = "accept",
    ) -> tuple[dict[str, object], dict[str, object]]:
        proposal = self.proposal(
            events,
            artifact_id=artifact_id,
            chapter=chapter,
            stage=stage,
            proposal_kind=proposal_kind,
        )
        return proposal, self.accept(proposal, operation=operation)

    def basic_instance_events(
        self,
        *,
        owner: str,
        carrier: str | None = None,
        definition_id: str = "definition_blade",
        function_id: str = "function_cut",
        binding_id: str = "binding_cut",
        instance_id: str = "instance_blade",
        max_durability: float = 10,
        max_energy: float = 5,
        charges: float = 2,
        durability_cost: float = 1,
        energy_cost: float = 2,
        cooldown: int | None = None,
    ) -> list[dict[str, object]]:
        carrier = carrier or owner
        function_definition: dict[str, object] = {
            "item_definition_id": definition_id,
            "effect_owner": "inline",
            "inline_effects": [{"kind": "test_effect"}],
            "charges": charges,
            "durability_cost": durability_cost,
            "costs": [{"kind": "energy", "amount": energy_cost}],
        }
        if cooldown is not None:
            function_definition["cooldown"] = cooldown
        return [
            self.item_event(
                "item_spec",
                ordinal=1,
                quote="定义测试物品。",
                action="define",
                spec_type="item_definition",
                spec_id=definition_id,
                definition={
                    "item_kind": "weapon",
                    "stack_policy": "non_stackable",
                    "uniqueness_policy": "unique_definition",
                    "max_durability": max_durability,
                    "max_energy": max_energy,
                },
            ),
            self.item_event(
                "item_spec",
                ordinal=1,
                quote="定义测试物品功能。",
                action="define",
                spec_type="function_definition",
                spec_id=function_id,
                definition=function_definition,
            ),
            self.item_event(
                "item_spec",
                ordinal=1,
                quote="功能绑定到测试物品。",
                action="define",
                spec_type="function_binding",
                spec_id=binding_id,
                definition={
                    "item_definition_id": definition_id,
                    "function_id": function_id,
                },
            ),
            self.item_event(
                "item_instance",
                ordinal=2,
                quote="测试物品实例出现。",
                action="instantiate",
                subject_type="item_instance",
                subject_id=instance_id,
                item_instance_id=instance_id,
                item_definition_id=definition_id,
                attributes={},
            ),
            self.item_event(
                "item_custody",
                ordinal=2,
                quote="所有权与实际携带关系被明确记录。",
                action="acquire",
                subject_type="item_instance",
                subject_id=instance_id,
                item_instance_id=instance_id,
                to_legal_owner_entity_id=owner,
                to_custodian_entity_id=carrier,
                to_carrier_entity_id=carrier,
            ),
        ]

    def test_v4_contract_rejects_computed_state_subject_conflicts_and_bad_endpoint(
        self,
    ) -> None:
        with self.assertRaises(ContinuityError) as computed:
            self.proposal(
                [
                    self.item_event(
                        "item_runtime",
                        ordinal=1,
                        quote="模型试图直接填写结果状态。",
                        action="damage",
                        subject_type="item_instance",
                        subject_id="instance_missing",
                        item_instance_id="instance_missing",
                        delta={"durability": 1},
                        before={"durability": 10},
                    )
                ],
                artifact_id="item-computed-state-forbidden",
            )
        self.assertEqual(
            "ITEM_COMPUTED_STATE_FORBIDDEN",
            computed.exception.code,
        )

        with self.assertRaises(ContinuityError) as subject:
            self.proposal(
                [
                    self.item_event(
                        "item_instance",
                        ordinal=1,
                        quote="类型化寻址发生冲突。",
                        action="instantiate",
                        subject_type="item_instance",
                        subject_id="instance_a",
                        stack_id="stack_a",
                        item_definition_id="definition_missing",
                        attributes={},
                    )
                ],
                artifact_id="item-subject-conflict",
            )
        self.assertEqual("ITEM_SUBJECT_MISMATCH", subject.exception.code)

        wrong_item_entity = self.entity("character", "并非物品的实体")
        proposal = self.proposal(
            [
                self.item_event(
                    "item_spec",
                    ordinal=1,
                    quote="错误类型的实体被声明为物品。",
                    action="define",
                    spec_type="item_definition",
                    spec_id="definition_bad_endpoint",
                    definition={
                        "item_entity_id": wrong_item_entity,
                        "item_kind": "weapon",
                        "stack_policy": "non_stackable",
                        "uniqueness_policy": "ordinary",
                    },
                )
            ],
            artifact_id="item-endpoint-type-mismatch",
        )
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            str(proposal["proposal_id"]),
            expected_canon_revision=revision,
        )
        with self.assertRaises(ContinuityError) as endpoint:
            self.service.accept_proposal(
                str(proposal["proposal_id"]),
                approval_id=str(grant["approval_id"]),
                expected_canon_revision=revision,
            )
        self.assertEqual("ITEM_ENTITY_TYPE_MISMATCH", endpoint.exception.code)
        self.assertEqual(
            revision,
            self.service.get_canon_revisions()["active"],
        )

    def test_definition_function_binding_instance_use_observation_and_queries(
        self,
    ) -> None:
        owner = self.entity("character", "法器所有者")
        carrier = self.entity("character", "法器携带者")
        events = self.basic_instance_events(
            owner=owner,
            carrier=carrier,
            cooldown=2,
        )
        events.extend(
            [
                self.item_event(
                    "item_runtime",
                    ordinal=3,
                    quote="携带者将法器装备在右手。",
                    action="equip",
                    subject_type="item_instance",
                    subject_id="instance_blade",
                    item_instance_id="instance_blade",
                    actor_entity_id=carrier,
                    slot_key="right_hand",
                    delta={},
                ),
                self.item_event(
                    "item_use",
                    ordinal=4,
                    quote="携带者发动了法器功能。",
                    action="use",
                    subject_type="item_instance",
                    subject_id="instance_blade",
                    item_instance_id="instance_blade",
                    actor_entity_id=carrier,
                    function_id="function_cut",
                    delta={},
                ),
                self.item_event(
                    "item_observation",
                    ordinal=4,
                    quote="所有者观察到法器出现轻微磨损。",
                    action="observe",
                    subject_type="item_instance",
                    subject_id="instance_blade",
                    item_instance_id="instance_blade",
                    observer_entity_id=owner,
                    function_id="function_cut",
                    knowledge_plane="actor_belief",
                    observation={"durability": "slightly_worn"},
                ),
            ]
        )
        _, commit = self.accept_events(
            events,
            artifact_id="item-full-runtime-chain",
        )

        self.assertEqual(
            self.initial_projection_hash,
            commit["projection_hash"],
        )
        self.assertTrue(
            str(commit["item_projection_hash"]).startswith(
                "item_projection_"
            )
        )
        self.assertEqual(1, commit["item_projection_schema_version"])

        definition = self.service.query_item_definition("definition_blade")
        self.assertEqual(1, len(definition["functions"]))
        self.assertEqual(1, len(definition["bindings"]))
        self.assertEqual(1, len(definition["instances"]))

        instance = self.service.query_item_instance("instance_blade")
        self.assertEqual(
            owner,
            instance["custody"]["legal_owner_entity_id"],
        )
        self.assertEqual(
            carrier,
            instance["custody"]["carrier_entity_id"],
        )
        self.assertEqual(9.0, instance["runtime"]["durability"])
        self.assertEqual(3.0, instance["runtime"]["energy"])
        self.assertEqual(
            carrier,
            instance["runtime"]["equipped_by_entity_id"],
        )
        self.assertEqual(
            1.0,
            instance["function_runtime"][0]["remaining_charges"],
        )
        self.assertEqual(
            self.coordinate(6),
            instance["function_runtime"][0]["cooldown_until"],
        )

        function = self.service.query_item_function(
            "function_cut",
            item_instance_id="instance_blade",
        )
        self.assertEqual(1, len(function["bindings"]))
        self.assertEqual(1, len(function["runtime"]))
        self.assertEqual(
            1,
            len(
                self.service.query_item_history(
                    item_instance_id="instance_blade"
                )["history"]
            ),
        )
        self.assertEqual(
            1,
            len(
                self.service.query_item_observations(
                    item_instance_id="instance_blade",
                    observer_entity_id=owner,
                    knowledge_plane="actor_belief",
                )["observations"]
            ),
        )

        owner_inventory = self.service.query_actor_inventory(owner)
        carrier_inventory = self.service.query_actor_inventory(carrier)
        self.assertEqual(
            ["instance_blade"],
            [item["subject_id"] for item in owner_inventory["owned"]],
        )
        self.assertEqual([], owner_inventory["carried"])
        self.assertEqual(
            ["instance_blade"],
            [item["subject_id"] for item in carrier_inventory["carried"]],
        )
        self.assertEqual([], carrier_inventory["owned"])

        first = self.service.replay()
        second = self.service.replay()
        self.assertEqual(first["projection_hash"], second["projection_hash"])
        self.assertEqual(
            first["item_projection_hash"],
            second["item_projection_hash"],
        )
        self.assertEqual(
            commit["item_projection_hash"],
            second["item_projection_hash"],
        )

    def test_observation_generation_visibility_is_observer_scoped(
        self,
    ) -> None:
        owner = self.entity("character", "观察者甲")
        other = self.entity("character", "观察者乙")
        events = self.basic_instance_events(owner=owner)
        observations = (
            ("objective", owner, "objective"),
            ("public_narrative", owner, "public"),
            ("reader_disclosed", owner, "reader"),
            ("author_plan", owner, "author"),
            ("actor_belief", owner, "belief-owner"),
            ("actor_belief", other, "belief-other"),
        )
        for offset, (plane, observer, marker) in enumerate(
            observations,
            start=3,
        ):
            events.append(
                self.item_event(
                    "item_observation",
                    ordinal=offset,
                    quote=f"记录物品观察：{marker}。",
                    action="observe",
                    subject_type="item_instance",
                    subject_id="instance_blade",
                    item_instance_id="instance_blade",
                    observer_entity_id=observer,
                    knowledge_plane=plane,
                    observation={"marker": marker},
                )
            )
        self.accept_events(
            events,
            artifact_id="item-observation-visibility",
        )

        def markers(**kwargs: object) -> set[str]:
            response = self.service.query_item_observations(
                item_instance_id="instance_blade",
                **kwargs,
            )
            return {
                str(row["observation"]["observation"]["marker"])
                for row in response["observations"]
            }

        self.assertEqual(
            {"objective", "public", "reader"},
            markers(),
        )
        self.assertEqual(
            {"objective", "public", "reader", "belief-owner"},
            markers(observer_entity_id=owner),
        )
        self.assertEqual(
            {"objective", "public", "reader", "belief-other"},
            markers(observer_entity_id=other),
        )
        self.assertEqual(
            set(),
            markers(knowledge_plane="actor_belief"),
        )
        self.assertEqual(
            {
                "objective",
                "public",
                "reader",
                "author",
                "belief-owner",
                "belief-other",
            },
            markers(visibility="inspection"),
        )
        self.assertEqual(
            {
                "objective",
                "public",
                "reader",
                "author",
                "belief-owner",
            },
            markers(
                visibility="inspection",
                observer_entity_id=owner,
            ),
        )
        self.assertEqual(
            {"author"},
            markers(
                visibility="raw",
                knowledge_plane="author_plan",
            ),
        )
        with self.assertRaises(ContinuityError) as caught:
            self.service.query_item_observations(
                item_instance_id="instance_blade",
                visibility="unsupported",
            )
        self.assertEqual(
            "ITEM_VISIBILITY_MODE_INVALID",
            caught.exception.code,
        )

    def test_failed_use_is_atomic_and_does_not_create_history(self) -> None:
        actor = self.entity("character", "一次性法器使用者")
        events = self.basic_instance_events(
            owner=actor,
            charges=1,
            durability_cost=2,
            energy_cost=1,
        )
        events.append(
            self.item_event(
                "item_use",
                ordinal=3,
                quote="使用者发动了一次法器功能。",
                action="use",
                subject_type="item_instance",
                subject_id="instance_blade",
                item_instance_id="instance_blade",
                actor_entity_id=actor,
                function_id="function_cut",
                delta={},
            )
        )
        self.accept_events(events, artifact_id="item-use-once")
        before = self.service.query_item_instance("instance_blade")
        history_before = self.service.query_item_history(
            item_instance_id="instance_blade"
        )
        revision = self.service.get_canon_revisions()["active"]

        proposal = self.proposal(
            [
                self.item_event(
                    "item_use",
                    ordinal=4,
                    quote="使用者试图再次发动已经耗尽次数的法器。",
                    action="use",
                    subject_type="item_instance",
                    subject_id="instance_blade",
                    item_instance_id="instance_blade",
                    actor_entity_id=actor,
                    function_id="function_cut",
                    delta={},
                )
            ],
            artifact_id="item-use-overdraw",
            chapter=2,
        )
        grant = self.host.issue(
            str(proposal["proposal_id"]),
            expected_canon_revision=revision,
        )
        with self.assertRaises(ContinuityError) as caught:
            self.service.accept_proposal(
                str(proposal["proposal_id"]),
                approval_id=str(grant["approval_id"]),
                expected_canon_revision=revision,
            )
        self.assertEqual("ITEM_INSUFFICIENT_CHARGES", caught.exception.code)
        self.assertEqual(
            revision,
            self.service.get_canon_revisions()["active"],
        )
        after = self.service.query_item_instance("instance_blade")
        history_after = self.service.query_item_history(
            item_instance_id="instance_blade"
        )
        self.assertEqual(
            before["item_projection_hash"],
            after["item_projection_hash"],
        )
        self.assertEqual(before["runtime"], after["runtime"])
        self.assertEqual(
            before["function_runtime"],
            after["function_runtime"],
        )
        self.assertEqual(
            history_before["history"],
            history_after["history"],
        )

    def test_stack_split_merge_preserves_quantity_batch_and_custody(self) -> None:
        actor = self.entity("character", "材料持有人")
        definition_events = [
            self.item_event(
                "item_spec",
                ordinal=1,
                quote="定义同质可堆叠材料。",
                action="define",
                spec_type="item_definition",
                spec_id="definition_material",
                definition={
                    "item_kind": "material",
                    "stack_policy": "homogeneous",
                    "uniqueness_policy": "ordinary",
                    "unit_bulk": 0.5,
                },
            ),
            self.item_event(
                "item_instance",
                ordinal=2,
                quote="十份同批材料形成一个堆叠。",
                action="instantiate",
                subject_type="item_stack",
                subject_id="stack_a",
                stack_id="stack_a",
                item_definition_id="definition_material",
                quantity=10,
                batch={"lot": "same-lot"},
                attributes={},
            ),
            self.item_event(
                "item_custody",
                ordinal=2,
                quote="材料堆叠归持有人保管。",
                action="acquire",
                subject_type="item_stack",
                subject_id="stack_a",
                stack_id="stack_a",
                quantity=10,
                to_legal_owner_entity_id=actor,
                to_custodian_entity_id=actor,
                to_carrier_entity_id=actor,
            ),
        ]
        self.accept_events(
            definition_events,
            artifact_id="item-stack-seed",
        )
        self.accept_events(
            [
                self.item_event(
                    "item_instance",
                    ordinal=3,
                    quote="从主堆叠拆出四份同批材料。",
                    action="split",
                    subject_type="item_stack",
                    subject_id="stack_a",
                    stack_id="stack_a",
                    source_stack_id="stack_a",
                    target_stack_id="stack_b",
                    quantity=4,
                    target_batch={"lot": "same-lot"},
                )
            ],
            artifact_id="item-stack-split",
            chapter=2,
        )
        stack_a = self.service.query_item_custody(
            subject_type="item_stack",
            subject_id="stack_a",
        )
        stack_b = self.service.query_item_custody(
            subject_type="item_stack",
            subject_id="stack_b",
        )
        self.assertEqual(6.0, stack_a["stack"]["quantity"])
        self.assertEqual(6.0, stack_a["custody"]["quantity"])
        self.assertEqual(4.0, stack_b["stack"]["quantity"])
        self.assertEqual(4.0, stack_b["custody"]["quantity"])
        self.assertEqual(
            stack_a["stack"]["batch"],
            stack_b["stack"]["batch"],
        )
        self.assertEqual(
            stack_a["custody"]["carrier_entity_id"],
            stack_b["custody"]["carrier_entity_id"],
        )

        self.accept_events(
            [
                self.item_event(
                    "item_instance",
                    ordinal=4,
                    quote="四份材料重新并回主堆叠。",
                    action="merge",
                    subject_type="item_stack",
                    subject_id="stack_b",
                    stack_id="stack_b",
                    source_stack_id="stack_b",
                    target_stack_id="stack_a",
                    quantity=4,
                )
            ],
            artifact_id="item-stack-merge",
            chapter=3,
        )
        definition = self.service.query_item_definition(
            "definition_material"
        )
        stacks = {
            row["stack_id"]: row for row in definition["stacks"]
        }
        self.assertEqual(10.0, stacks["stack_a"]["quantity"])
        self.assertEqual("active", stacks["stack_a"]["stack_status"])
        self.assertEqual(0.0, stacks["stack_b"]["quantity"])
        self.assertEqual("merged", stacks["stack_b"]["stack_status"])
        inventory = self.service.query_actor_inventory(actor)
        self.assertEqual(
            [("stack_a", 10.0)],
            [
                (item["subject_id"], item["custody"]["quantity"])
                for item in inventory["carried"]
            ],
        )

    def test_item_correction_replay_and_retraction_restore_original_hash(
        self,
    ) -> None:
        actor = self.entity("character", "受损法器持有人")
        seed = self.basic_instance_events(
            owner=actor,
            charges=0,
            durability_cost=0,
            energy_cost=0,
        )
        self.accept_events(seed, artifact_id="item-correction-seed")
        original_proposal, original_commit = self.accept_events(
            [
                self.item_event(
                    "item_runtime",
                    ordinal=3,
                    quote="法器受到四点耐久损伤。",
                    action="damage",
                    subject_type="item_instance",
                    subject_id="instance_blade",
                    item_instance_id="instance_blade",
                    delta={"durability": 4},
                )
            ],
            artifact_id="item-original-damage",
            chapter=2,
        )
        del original_proposal
        original_event_id = str(
            original_commit["events"][0]["event_id"]
        )
        original_hash = str(original_commit["item_projection_hash"])
        self.assertEqual(
            6.0,
            self.service.query_item_runtime("instance_blade")["runtime"][
                "durability"
            ],
        )

        replacement = self.item_event(
            "item_runtime",
            ordinal=3,
            quote="核对后确认法器实际只受到一点损伤。",
            action="damage",
            subject_type="item_instance",
            subject_id="instance_blade",
            item_instance_id="instance_blade",
            delta={"durability": 1},
        )
        correction_proposal, correction_commit = self.accept_events(
            [
                self.item_event(
                    "item_correction",
                    ordinal=4,
                    quote="原损伤记录被更正。",
                    action="correct",
                    target_event_id=original_event_id,
                    replacement=replacement,
                )
            ],
            artifact_id="item-damage-correction",
            chapter=3,
        )
        corrected_hash = str(correction_commit["item_projection_hash"])
        self.assertNotEqual(original_hash, corrected_hash)
        self.assertEqual(
            9.0,
            self.service.query_item_runtime("instance_blade")["runtime"][
                "durability"
            ],
        )
        first_replay = self.service.replay()
        second_replay = self.service.replay()
        self.assertEqual(
            corrected_hash,
            first_replay["item_projection_hash"],
        )
        self.assertEqual(
            first_replay["item_projection_hash"],
            second_replay["item_projection_hash"],
        )

        active = self.service.get_canon_revisions()["active"]
        retract_grant = self.host.issue(
            str(correction_proposal["proposal_id"]),
            expected_canon_revision=active,
            operations=("retract",),
        )
        retracted = self.service.retract_proposal(
            str(correction_proposal["proposal_id"]),
            approval_id=str(retract_grant["approval_id"]),
            expected_canon_revision=active,
            reason="correction withdrawn",
        )
        self.assertEqual(original_hash, retracted["item_projection_hash"])
        restored = self.service.query_item_runtime("instance_blade")
        self.assertEqual(6.0, restored["runtime"]["durability"])
        self.assertEqual(original_hash, restored["item_projection_hash"])
        self.assertEqual(
            self.initial_projection_hash,
            self.service.projection_hash(),
        )

    def test_destroy_clears_custody_equipment_and_instance_binding(self) -> None:
        actor = self.entity("character", "法器持有人")
        definition_id = "definition_destroyable"
        function_id = "function_destroyable"
        instance_id = "instance_destroyable"
        events = [
            self.item_event(
                "item_spec",
                ordinal=1,
                quote="定义可销毁法器。",
                action="define",
                spec_type="item_definition",
                spec_id=definition_id,
                definition={
                    "item_kind": "weapon",
                    "stack_policy": "non_stackable",
                    "uniqueness_policy": "ordinary",
                    "max_durability": 5,
                },
            ),
            self.item_event(
                "item_spec",
                ordinal=1,
                quote="定义法器功能。",
                action="define",
                spec_type="function_definition",
                spec_id=function_id,
                definition={
                    "item_definition_id": definition_id,
                    "effect_owner": "inline",
                    "inline_effects": [{"kind": "test_effect"}],
                    "charges": 2,
                },
            ),
            self.item_event(
                "item_instance",
                ordinal=2,
                quote="法器实例出现。",
                action="instantiate",
                subject_type="item_instance",
                subject_id=instance_id,
                item_instance_id=instance_id,
                item_definition_id=definition_id,
                attributes={},
            ),
            self.item_event(
                "item_spec",
                ordinal=2,
                quote="功能只绑定到这个法器实例。",
                action="define",
                spec_type="function_binding",
                spec_id="binding_instance_only",
                definition={
                    "item_instance_id": instance_id,
                    "function_id": function_id,
                },
            ),
            self.item_event(
                "item_custody",
                ordinal=2,
                quote="持有人携带法器。",
                action="acquire",
                subject_type="item_instance",
                subject_id=instance_id,
                item_instance_id=instance_id,
                to_legal_owner_entity_id=actor,
                to_custodian_entity_id=actor,
                to_carrier_entity_id=actor,
            ),
            self.item_event(
                "item_runtime",
                ordinal=3,
                quote="持有人装备法器。",
                action="equip",
                subject_type="item_instance",
                subject_id=instance_id,
                item_instance_id=instance_id,
                actor_entity_id=actor,
                slot_key="main_hand",
                delta={},
            ),
            self.item_event(
                "item_runtime",
                ordinal=4,
                quote="法器被彻底销毁。",
                action="destroy",
                subject_type="item_instance",
                subject_id=instance_id,
                item_instance_id=instance_id,
                delta={},
            ),
        ]
        self.accept_events(events, artifact_id="item-destroy-cleanup")
        instance = self.service.query_item_instance(instance_id)
        self.assertEqual("destroyed", instance["instance"]["instance_status"])
        self.assertIsNone(instance["custody"])
        self.assertTrue(instance["runtime"]["destroyed"])
        self.assertIsNone(instance["runtime"]["equipped_by_entity_id"])
        self.assertEqual(
            "suppressed",
            instance["function_runtime"][0]["unlock_state"],
        )
        function = self.service.query_item_function(
            function_id,
            item_instance_id=instance_id,
        )
        self.assertEqual(
            "deprecated",
            function["bindings"][0]["binding_status"],
        )
        inventory = self.service.query_actor_inventory(actor)
        self.assertEqual([], inventory["owned"])
        self.assertEqual([], inventory["carried"])
        self.assertEqual([], inventory["equipped"])

    def test_container_cycle_and_capacity_fail_without_revision(self) -> None:
        actor = self.entity("character", "容器持有人")
        seed = [
            self.item_event(
                "item_spec",
                ordinal=1,
                quote="定义容量为二的容器。",
                action="define",
                spec_type="item_definition",
                spec_id="definition_container",
                definition={
                    "item_kind": "container",
                    "stack_policy": "non_stackable",
                    "uniqueness_policy": "ordinary",
                    "capacity": 2,
                    "unit_bulk": 1,
                },
            ),
            self.item_event(
                "item_spec",
                ordinal=1,
                quote="定义单位体积为一的材料。",
                action="define",
                spec_type="item_definition",
                spec_id="definition_bulk",
                definition={
                    "item_kind": "material",
                    "stack_policy": "homogeneous",
                    "uniqueness_policy": "ordinary",
                    "unit_bulk": 1,
                },
            ),
        ]
        for instance_id in ("container_a", "container_b"):
            seed.append(
                self.item_event(
                    "item_instance",
                    ordinal=2,
                    quote=f"容器实例 {instance_id} 出现。",
                    action="instantiate",
                    subject_type="item_instance",
                    subject_id=instance_id,
                    item_instance_id=instance_id,
                    item_definition_id="definition_container",
                    attributes={},
                )
            )
        seed.extend(
            [
                self.item_event(
                    "item_instance",
                    ordinal=2,
                    quote="两份材料形成一个堆叠。",
                    action="instantiate",
                    subject_type="item_stack",
                    subject_id="stack_bulk",
                    stack_id="stack_bulk",
                    item_definition_id="definition_bulk",
                    quantity=2,
                    batch={"lot": "bulk"},
                    attributes={},
                ),
                self.item_event(
                    "item_custody",
                    ordinal=2,
                    quote="容器甲由持有人携带。",
                    action="acquire",
                    subject_type="item_instance",
                    subject_id="container_a",
                    item_instance_id="container_a",
                    to_legal_owner_entity_id=actor,
                    to_custodian_entity_id=actor,
                    to_carrier_entity_id=actor,
                ),
                self.item_event(
                    "item_custody",
                    ordinal=2,
                    quote="容器乙被放入容器甲。",
                    action="acquire",
                    subject_type="item_instance",
                    subject_id="container_b",
                    item_instance_id="container_b",
                    to_container_instance_id="container_a",
                ),
            ]
        )
        self.accept_events(seed, artifact_id="item-container-seed")

        revision = self.service.get_canon_revisions()["active"]
        cycle = self.proposal(
            [
                self.item_event(
                    "item_custody",
                    ordinal=3,
                    quote="有人试图把容器甲放进容器乙。",
                    action="handover",
                    subject_type="item_instance",
                    subject_id="container_a",
                    item_instance_id="container_a",
                    from_carrier_entity_id=actor,
                    to_container_instance_id="container_b",
                )
            ],
            artifact_id="item-container-cycle",
            chapter=2,
        )
        grant = self.host.issue(
            str(cycle["proposal_id"]),
            expected_canon_revision=revision,
        )
        with self.assertRaises(ContinuityError) as cycle_error:
            self.service.accept_proposal(
                str(cycle["proposal_id"]),
                approval_id=str(grant["approval_id"]),
                expected_canon_revision=revision,
            )
        self.assertEqual("ITEM_CONTAINER_CYCLE", cycle_error.exception.code)
        self.assertEqual(
            revision,
            self.service.get_canon_revisions()["active"],
        )

        capacity = self.proposal(
            [
                self.item_event(
                    "item_custody",
                    ordinal=3,
                    quote="两份材料试图装入已经占用一格的容器甲。",
                    action="acquire",
                    subject_type="item_stack",
                    subject_id="stack_bulk",
                    stack_id="stack_bulk",
                    quantity=2,
                    to_container_instance_id="container_a",
                )
            ],
            artifact_id="item-container-capacity",
            chapter=2,
        )
        capacity_grant = self.host.issue(
            str(capacity["proposal_id"]),
            expected_canon_revision=revision,
        )
        with self.assertRaises(ContinuityError) as capacity_error:
            self.service.accept_proposal(
                str(capacity["proposal_id"]),
                approval_id=str(capacity_grant["approval_id"]),
                expected_canon_revision=revision,
            )
        self.assertEqual(
            "ITEM_CONTAINER_CAPACITY_EXCEEDED",
            capacity_error.exception.code,
        )
        self.assertEqual(
            revision,
            self.service.get_canon_revisions()["active"],
        )
        container_a = self.service.query_item_custody(
            subject_type="item_instance",
            subject_id="container_a",
        )
        container_b = self.service.query_item_custody(
            subject_type="item_instance",
            subject_id="container_b",
        )
        self.assertEqual(
            actor,
            container_a["custody"]["carrier_entity_id"],
        )
        self.assertEqual(
            "container_a",
            container_b["custody"]["container_instance_id"],
        )

    def test_ability_bridge_reuses_power_definition_and_requires_binding(
        self,
    ) -> None:
        actor = self.entity("character", "法杖使用者")
        item_entity = self.entity("item", "桥接法杖")
        power_system = self.entity("power_system", "桥接法术体系")
        ability = self.entity("ability", "桥接火花")
        power_proposal, _ = self.accept_events(
            [
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "power_system",
                    "spec_entity_id": power_system,
                    "definition": {"profile": "magic"},
                },
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "ability_definition",
                    "spec_entity_id": ability,
                    "definition": {
                        "system_entity_id": power_system,
                        "requirements": [],
                    },
                },
            ],
            artifact_id="item-bridge-power-spec",
            chapter=None,
            stage="bootstrap",
            proposal_kind="power_spec_change",
            operation="accept_power_spec",
        )
        del power_proposal
        self.accept_events(
            [
                self.item_event(
                    "item_spec",
                    ordinal=1,
                    quote="定义引用既有能力的桥接法杖。",
                    action="define",
                    spec_type="item_definition",
                    spec_id="definition_bridge_wand",
                    definition={
                        "item_entity_id": item_entity,
                        "item_kind": "weapon",
                        "stack_policy": "non_stackable",
                        "uniqueness_policy": "unique_definition",
                    },
                ),
                self.item_event(
                    "item_spec",
                    ordinal=1,
                    quote="法杖功能桥接到既有能力定义。",
                    action="define",
                    spec_type="function_definition",
                    spec_id="function_bridge",
                    definition={
                        "item_definition_id": "definition_bridge_wand",
                        "effect_owner": "ability_bridge",
                        "granted_ability_ids": [ability],
                        "inline_effects": [],
                    },
                ),
                self.item_event(
                    "item_spec",
                    ordinal=1,
                    quote="桥接功能绑定到法杖定义。",
                    action="define",
                    spec_type="function_binding",
                    spec_id="binding_bridge",
                    definition={
                        "item_definition_id": "definition_bridge_wand",
                        "function_id": "function_bridge",
                    },
                ),
                self.item_event(
                    "item_instance",
                    ordinal=2,
                    quote="桥接法杖实例出现。",
                    action="instantiate",
                    subject_type="item_instance",
                    subject_id="instance_bridge_wand",
                    item_instance_id="instance_bridge_wand",
                    item_definition_id="definition_bridge_wand",
                    attributes={},
                ),
                self.item_event(
                    "item_custody",
                    ordinal=2,
                    quote="使用者持有桥接法杖。",
                    action="acquire",
                    subject_type="item_instance",
                    subject_id="instance_bridge_wand",
                    item_instance_id="instance_bridge_wand",
                    to_legal_owner_entity_id=actor,
                    to_custodian_entity_id=actor,
                    to_carrier_entity_id=actor,
                ),
            ],
            artifact_id="item-bridge-seed",
        )

        revision = self.service.get_canon_revisions()["active"]
        without_binding = self.proposal(
            [
                self.item_event(
                    "item_use",
                    ordinal=3,
                    quote="使用者在桥接关系生效前尝试发动法杖。",
                    action="use",
                    subject_type="item_instance",
                    subject_id="instance_bridge_wand",
                    item_instance_id="instance_bridge_wand",
                    actor_entity_id=actor,
                    function_id="function_bridge",
                    delta={},
                )
            ],
            artifact_id="item-bridge-use-before-binding",
            chapter=2,
        )
        grant = self.host.issue(
            str(without_binding["proposal_id"]),
            expected_canon_revision=revision,
        )
        with self.assertRaises(ContinuityError) as inactive:
            self.service.accept_proposal(
                str(without_binding["proposal_id"]),
                approval_id=str(grant["approval_id"]),
                expected_canon_revision=revision,
            )
        self.assertEqual(
            "ITEM_ABILITY_BRIDGE_INACTIVE",
            inactive.exception.code,
        )

        self.accept_events(
            [
                {
                    "event_type": "power_binding",
                    "actor_entity_id": actor,
                    "binding_id": "accepted-item-bridge",
                    "source_entity_id": item_entity,
                    "action": "equip",
                    "ability_entity_ids": [ability],
                    "slot_key": "main_hand",
                    "unique": True,
                }
            ],
            artifact_id="item-bridge-power-binding",
            chapter=2,
        )
        self.accept_events(
            [
                self.item_event(
                    "item_use",
                    ordinal=4,
                    quote="桥接关系生效后，使用者成功发动法杖。",
                    action="use",
                    subject_type="item_instance",
                    subject_id="instance_bridge_wand",
                    item_instance_id="instance_bridge_wand",
                    actor_entity_id=actor,
                    function_id="function_bridge",
                    delta={},
                )
            ],
            artifact_id="item-bridge-use-after-binding",
            chapter=3,
        )
        function = self.service.query_item_function(
            "function_bridge",
            item_instance_id="instance_bridge_wand",
        )
        self.assertEqual(
            [ability],
            function["function"]["definition"]["granted_ability_ids"],
        )
        self.assertEqual(
            1,
            len(
                self.service.query_item_history(
                    item_instance_id="instance_bridge_wand"
                )["history"]
            ),
        )
        with self.service.store.read_connection() as connection:
            count = connection.execute(
                """
                SELECT COUNT(*) FROM ability_definitions
                WHERE ability_entity_id=?
                """,
                (ability,),
            ).fetchone()[0]
        self.assertEqual(1, count)


if __name__ == "__main__":
    unittest.main()
