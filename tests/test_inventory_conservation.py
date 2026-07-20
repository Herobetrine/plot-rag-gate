from __future__ import annotations

import tempfile
import unittest

from scripts.continuity import (
    ContinuityError,
    ContinuityService,
    HostApprovalAuthority,
)


class InventoryConservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = ContinuityService(self.temp_dir.name)
        self.host = HostApprovalAuthority(
            self.service,
            issuer="inventory-unittest-host",
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
        chapter: int = 1,
    ):
        return self.service.save_proposal(
            events=events,
            artifact_id=artifact_id,
            artifact_stage="final",
            branch_id="main",
            chapter_no=chapter,
            scene_index=0,
        )

    def accept(self, proposal):
        revision = self.service.get_canon_revisions()["active"]
        grant = self.host.issue(
            proposal["proposal_id"],
            expected_canon_revision=revision,
        )
        return self.service.accept_proposal(
            proposal["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=revision,
        )

    def balances(self, item_entity_id: str) -> dict[str, float]:
        with self.service.store.read_connection() as connection:
            return {
                str(row["owner_entity_id"]): float(row["quantity"])
                for row in connection.execute(
                    """
                    SELECT owner_entity_id, quantity
                    FROM inventory_state
                    WHERE item_entity_id=? AND is_unique=0
                    ORDER BY owner_entity_id
                    """,
                    (item_entity_id,),
                )
            }

    def test_actions_preserve_balances_and_replay_is_stable(self):
        owner_a = self.entity("character", "甲")
        owner_b = self.entity("character", "乙")
        item = self.entity("item", "灵石")
        actions = [
            (
                "inventory-acquire",
                {
                    "event_type": "inventory",
                    "item_entity_id": item,
                    "to_owner_entity_id": owner_a,
                    "action": "acquire",
                    "quantity": 10,
                },
            ),
            (
                "inventory-transfer",
                {
                    "event_type": "inventory",
                    "item_entity_id": item,
                    "from_owner_entity_id": owner_a,
                    "to_owner_entity_id": owner_b,
                    "action": "transfer",
                    "quantity": 3,
                },
            ),
            (
                "inventory-consume",
                {
                    "event_type": "inventory",
                    "item_entity_id": item,
                    "from_owner_entity_id": owner_b,
                    "action": "consume",
                    "quantity": 2,
                },
            ),
            (
                "inventory-lose",
                {
                    "event_type": "inventory",
                    "item_entity_id": item,
                    "from_owner_entity_id": owner_a,
                    "action": "lose",
                    "quantity": 1,
                },
            ),
            (
                "inventory-acquire-more",
                {
                    "event_type": "inventory",
                    "item_entity_id": item,
                    "to_owner_entity_id": owner_a,
                    "action": "acquire",
                    "quantity": 5,
                },
            ),
            (
                "inventory-set",
                {
                    "event_type": "inventory",
                    "item_entity_id": item,
                    "to_owner_entity_id": owner_a,
                    "action": "set",
                    "quantity": 4,
                },
            ),
        ]
        for chapter, (artifact_id, event) in enumerate(actions, start=1):
            self.accept(
                self.proposal(
                    [event],
                    artifact_id=artifact_id,
                    chapter=chapter,
                )
            )

        self.assertEqual(self.balances(item), {owner_a: 4.0, owner_b: 1.0})
        first = self.service.replay()
        after_first = self.balances(item)
        second = self.service.replay()
        self.assertEqual(after_first, self.balances(item))
        self.assertEqual(
            first["projection_hash"],
            second["projection_hash"],
        )

    def test_transfer_consume_and_lose_overdraw_fail_closed(self):
        owner_a = self.entity("character", "甲")
        owner_b = self.entity("character", "乙")
        item = self.entity("item", "灵石")
        self.accept(
            self.proposal(
                [
                    {
                        "event_type": "inventory",
                        "item_entity_id": item,
                        "to_owner_entity_id": owner_a,
                        "action": "acquire",
                        "quantity": 2,
                    }
                ],
                artifact_id="inventory-balance-seed",
            )
        )

        for chapter, action in enumerate(
            (
                {
                    "event_type": "inventory",
                    "item_entity_id": item,
                    "from_owner_entity_id": owner_a,
                    "to_owner_entity_id": owner_b,
                    "action": "transfer",
                    "quantity": 3,
                },
                {
                    "event_type": "inventory",
                    "item_entity_id": item,
                    "from_owner_entity_id": owner_a,
                    "action": "consume",
                    "quantity": 3,
                },
                {
                    "event_type": "inventory",
                    "item_entity_id": item,
                    "from_owner_entity_id": owner_a,
                    "action": "lose",
                    "quantity": 3,
                },
            ),
            start=2,
        ):
            proposal = self.proposal(
                [action],
                artifact_id=f"inventory-overdraw-{chapter}",
                chapter=chapter,
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
                caught.exception.code,
                "INVENTORY_INSUFFICIENT_BALANCE",
            )
            self.assertEqual(
                self.service.get_canon_revisions()["active"],
                revision,
            )
            self.assertEqual(self.balances(item), {owner_a: 2.0})

    def test_non_unique_event_shapes_fail_closed(self):
        owner = self.entity("character", "甲")
        item = self.entity("item", "灵石")
        invalid_events = (
            {
                "event_type": "inventory",
                "item_entity_id": item,
                "action": "consume",
                "quantity": 1,
            },
            {
                "event_type": "inventory",
                "item_entity_id": item,
                "from_owner_entity_id": owner,
                "to_owner_entity_id": owner,
                "action": "transfer",
                "quantity": 1,
            },
            {
                "event_type": "inventory",
                "item_entity_id": item,
                "to_owner_entity_id": owner,
                "action": "acquire",
                "quantity": 0,
            },
        )
        for index, event in enumerate(invalid_events, start=1):
            with self.subTest(index=index):
                with self.assertRaises(ContinuityError):
                    self.proposal(
                        [event],
                        artifact_id=f"inventory-invalid-shape-{index}",
                    )


if __name__ == "__main__":
    unittest.main()
