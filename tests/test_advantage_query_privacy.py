from __future__ import annotations

import json
import sqlite3
import unittest

from scripts.continuity.advantages import (
    ensure_advantage_schema,
    query_advantage_context,
    query_advantage_contexts,
    query_advantage_knowledge,
    query_advantage_ledger,
)


class AdvantageQueryPrivacyTests(unittest.TestCase):
    ADVANTAGE_ID = "advantage-fixture"

    @staticmethod
    def _generation_claim_ids(
        rows: list[dict[str, object]],
    ) -> set[str]:
        return {
            str((row.get("claim") or {}).get("text") or "").removeprefix(
                "secret:"
            )
            for row in rows
        }

    def connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        ensure_advantage_schema(connection)
        self._insert_definition(connection)
        return connection

    def _insert_definition(
        self,
        connection: sqlite3.Connection,
        *,
        advantage_id: str | None = None,
        authority: str = "canon",
        lifecycle: str = "active",
    ) -> None:
        connection.execute(
            """
            INSERT INTO advantage_definitions(
                advantage_id, title, profiles_json, anchor_type,
                acquisition_mode, uniqueness, advantage_status,
                lifecycle_status, updated_order
            ) VALUES(?, ?, '[]', 'virtual_system', 'inheritance',
                     'unique', ?, ?, 1)
            """,
            (
                advantage_id or self.ADVANTAGE_ID,
                "Fixture Advantage",
                authority,
                lifecycle,
            ),
        )

    def _insert_module(
        self,
        connection: sqlite3.Connection,
        module_id: str,
        *,
        status: str = "available",
        authority: str = "canon",
        stage: str = "initial",
    ) -> None:
        connection.execute(
            """
            INSERT INTO advantage_module_definitions(
                module_id, advantage_id, title, module_kind,
                authority_status, module_status, stage, updated_order
            ) VALUES(?, ?, ?, 'ability', ?, ?, ?, 1)
            """,
            (
                module_id,
                self.ADVANTAGE_ID,
                module_id,
                authority,
                status,
                stage,
            ),
        )

    def _insert_knowledge(
        self,
        connection: sqlite3.Connection,
        knowledge_id: str,
        plane: str,
        stage: str,
        *,
        status: str = "canon",
        observer: str | None = None,
        module_id: str | None = None,
        ordinal: int | None = None,
        updated_order: int = 1,
    ) -> None:
        coordinate = (
            {"calendar_id": "chapter_scene", "ordinal": ordinal}
            if ordinal is not None
            else {}
        )
        connection.execute(
            """
            INSERT INTO advantage_knowledge(
                knowledge_id, advantage_id, module_id,
                observer_entity_id, knowledge_plane, knowledge_status,
                claim_json, confidence, reveal_stage,
                story_coordinate_json, updated_order
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 1.0, ?, ?, ?)
            """,
            (
                knowledge_id,
                self.ADVANTAGE_ID,
                module_id,
                observer,
                plane,
                status,
                json.dumps(
                    {"text": f"secret:{knowledge_id}"},
                    ensure_ascii=False,
                ),
                stage,
                json.dumps(coordinate, ensure_ascii=False),
                updated_order,
            ),
        )

    def _insert_runtime(
        self,
        connection: sqlite3.Connection,
        *,
        ordinal: int | None,
        source_event_id: str | None = "event-runtime",
    ) -> None:
        coordinate = (
            {"calendar_id": "chapter_scene", "ordinal": ordinal}
            if ordinal is not None
            else {}
        )
        connection.execute(
            """
            INSERT INTO advantage_runtime_state(
                runtime_key, advantage_id, branch_id, owner_entity_id,
                stage, enabled, charges, max_charges,
                cooldown_until_json, unlocked_modules_json,
                source_event_id, story_coordinate_json, updated_order
            ) VALUES(
                'runtime-fixture', ?, 'main', 'actor-a',
                'initial', 1, 3, 5, 'null', ?,
                ?, ?, 10
            )
            """,
            (
                self.ADVANTAGE_ID,
                json.dumps(["module-visible"]),
                source_event_id,
                json.dumps(coordinate),
            ),
        )

    def _insert_ledger(
        self,
        connection: sqlite3.Connection,
        entry_id: str,
        entry_kind: str,
        *,
        order: int,
        input_value: object | None = None,
        output_value: object | None = None,
        loss_value: object | None = None,
        provenance: object | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO advantage_ledger(
                entry_id, advantage_id, branch_id, entry_kind,
                input_json, output_json, loss_json, provenance_json,
                source_event_id, story_coordinate_json, updated_order
            ) VALUES(?, ?, 'main', ?, ?, ?, ?, ?, ?, '{}', ?)
            """,
            (
                entry_id,
                self.ADVANTAGE_ID,
                entry_kind,
                json.dumps(input_value or {}, ensure_ascii=False),
                json.dumps(output_value or {}, ensure_ascii=False),
                json.dumps(loss_value or {}, ensure_ascii=False),
                json.dumps(provenance or {}, ensure_ascii=False),
                f"event-{entry_id}",
                order,
            ),
        )

    def test_generation_is_default_and_inspection_is_explicit(self) -> None:
        connection = self.connection()
        try:
            self._insert_knowledge(
                connection,
                "knowledge-public",
                "public_narrative",
                "initial",
            )
            self._insert_knowledge(
                connection,
                "knowledge-reader",
                "reader_disclosed",
                "revealed",
            )
            self._insert_knowledge(
                connection,
                "knowledge-objective",
                "objective",
                "initial",
            )
            self._insert_knowledge(
                connection,
                "knowledge-author-plan",
                "author_plan",
                "future",
                status="planned",
            )

            default_rows = query_advantage_knowledge(
                connection,
                self.ADVANTAGE_ID,
                include_noncanon=True,
            )
            self.assertEqual(
                {"knowledge-public", "knowledge-reader"},
                self._generation_claim_ids(default_rows),
            )
            self.assertEqual(
                [],
                query_advantage_knowledge(
                    connection,
                    self.ADVANTAGE_ID,
                    knowledge_plane="objective",
                ),
            )

            inspected = query_advantage_knowledge(
                connection,
                self.ADVANTAGE_ID,
                include_noncanon=True,
                visibility="inspection",
            )
            self.assertEqual(
                {
                    "knowledge-public",
                    "knowledge-reader",
                    "knowledge-objective",
                    "knowledge-author-plan",
                },
                {row["knowledge_id"] for row in inspected},
            )
        finally:
            connection.close()

    def test_generation_pov_isolated_and_added_to_default_planes(self) -> None:
        connection = self.connection()
        try:
            self._insert_knowledge(
                connection,
                "belief-a",
                "actor_belief",
                "initial",
                observer="actor-a",
            )
            self._insert_knowledge(
                connection,
                "belief-b",
                "actor_belief",
                "initial",
                observer="actor-b",
            )
            self._insert_knowledge(
                connection,
                "public",
                "public_narrative",
                "initial",
            )

            rows = query_advantage_knowledge(
                connection,
                self.ADVANTAGE_ID,
                observer_entity_id="actor-a",
                visibility="generation",
            )
            self.assertEqual(
                {"belief-a", "public"},
                self._generation_claim_ids(rows),
            )
            anonymous = query_advantage_knowledge(
                connection,
                self.ADVANTAGE_ID,
                visibility="generation",
            )
            self.assertEqual(
                {"public"},
                self._generation_claim_ids(anonymous),
            )
        finally:
            connection.close()

    def test_reveal_stage_is_fail_closed_without_cursor(self) -> None:
        connection = self.connection()
        try:
            for knowledge_id, stage, ordinal in (
                ("initial", "initial", None),
                ("first-use-unpositioned", "first_use", None),
                ("unknown-unpositioned", "chapter-seven-secret", None),
                ("first-use-positioned", "first_use", 2_000_000),
                ("custom-positioned", "chapter-two-public", 2_000_000),
            ):
                self._insert_knowledge(
                    connection,
                    knowledge_id,
                    "public_narrative",
                    stage,
                    ordinal=ordinal,
                )

            without_cursor = query_advantage_knowledge(
                connection,
                self.ADVANTAGE_ID,
                visibility="generation",
            )
            self.assertEqual(
                {"initial"},
                self._generation_claim_ids(without_cursor),
            )

            at_chapter_two = query_advantage_knowledge(
                connection,
                self.ADVANTAGE_ID,
                visibility="generation",
                chapter_no=2,
                scene_index=0,
            )
            self.assertEqual(
                {
                    "initial",
                    "first-use-positioned",
                    "custom-positioned",
                },
                self._generation_claim_ids(at_chapter_two),
            )
        finally:
            connection.close()

    def test_ledger_filters_before_applying_limit(self) -> None:
        connection = self.connection()
        try:
            self._insert_module(connection, "module-visible")
            self._insert_module(connection, "module-hidden", status="locked")
            rows = (
                (
                    "future",
                    "module-visible",
                    3_000_000,
                    30,
                ),
                (
                    "hidden-module",
                    "module-hidden",
                    1_000_000,
                    20,
                ),
                (
                    "visible-older",
                    "module-visible",
                    1_000_000,
                    10,
                ),
            )
            for entry_id, module_id, ordinal, order in rows:
                connection.execute(
                    """
                    INSERT INTO advantage_ledger(
                        entry_id, advantage_id, module_id, branch_id,
                        entry_kind, story_coordinate_json, updated_order
                    ) VALUES(?, ?, ?, 'main', 'use', ?, ?)
                    """,
                    (
                        entry_id,
                        self.ADVANTAGE_ID,
                        module_id,
                        json.dumps(
                            {
                                "calendar_id": "chapter_scene",
                                "ordinal": ordinal,
                            }
                        ),
                        order,
                    ),
                )

            visible = query_advantage_ledger(
                connection,
                self.ADVANTAGE_ID,
                branch_id="main",
                chapter_no=1,
                scene_index=0,
                visible_module_ids=["module-visible"],
                limit=1,
            )
            self.assertEqual(
                ["visible-older"],
                [row["entry_id"] for row in visible],
            )
        finally:
            connection.close()

    def test_generation_context_filters_modules_contracts_and_runtime_shape(
        self,
    ) -> None:
        connection = self.connection()
        try:
            self._insert_module(connection, "module-visible")
            self._insert_module(connection, "module-locked", status="locked")
            self._insert_module(
                connection,
                "module-planned",
                status="available",
                authority="planned",
            )
            self._insert_runtime(connection, ordinal=1_000_000)
            for slot_id, module_id, status in (
                ("slot-visible", "module-visible", "available"),
                ("slot-locked-status", "module-visible", "locked"),
                ("slot-hidden-module", "module-locked", "available"),
            ):
                connection.execute(
                    """
                    INSERT INTO advantage_runtime_slots(
                        slot_id, advantage_id, module_id, stage,
                        authority_status, slot_status, updated_order
                    ) VALUES(?, ?, ?, 'initial', 'canon', ?, 1)
                    """,
                    (
                        slot_id,
                        self.ADVANTAGE_ID,
                        module_id,
                        status,
                    ),
                )
            for contract_id, status in (
                ("contract-proposed", "proposed"),
                ("contract-active", "active"),
            ):
                connection.execute(
                    """
                    INSERT INTO advantage_contracts(
                        contract_id, advantage_id, authority_status,
                        contract_status, story_coordinate_json, updated_order
                    ) VALUES(?, ?, 'canon', ?, ?, 1)
                    """,
                    (
                        contract_id,
                        self.ADVANTAGE_ID,
                        status,
                        json.dumps(
                            {
                                "calendar_id": "chapter_scene",
                                "ordinal": 1_000_000,
                            }
                        ),
                    ),
                )

            context = query_advantage_context(
                connection,
                self.ADVANTAGE_ID,
                visibility="generation",
                chapter_no=1,
                scene_index=0,
            )
            self.assertEqual(
                ["module-visible"],
                [row["module_id"] for row in context["modules"]],
            )
            self.assertEqual(
                ["slot-visible"],
                [
                    row["slot_id"]
                    for row in context["progression"]["slots"]
                ],
            )
            self.assertEqual(
                ["contract-active"],
                [row["contract_id"] for row in context["contracts"]],
            )
            self.assertEqual(1, len(context["module_runtime"]))
            module_runtime = context["module_runtime"][0]
            self.assertEqual("module-visible", module_runtime["module_id"])
            self.assertTrue(module_runtime["unlocked"])
            self.assertTrue(module_runtime["enabled"])
            for misleading_global in (
                "charges",
                "max_charges",
                "cooldown_until",
            ):
                self.assertNotIn(misleading_global, module_runtime)
        finally:
            connection.close()

    def test_timeless_bootstrap_runtime_remains_visible_at_story_cursor(
        self,
    ) -> None:
        connection = self.connection()
        try:
            self._insert_module(connection, "module-visible")
            self._insert_runtime(
                connection,
                ordinal=None,
                source_event_id=None,
            )

            context = query_advantage_context(
                connection,
                self.ADVANTAGE_ID,
                visibility="generation",
                chapter_no=3,
                scene_index=0,
            )
            self.assertEqual("visible", context["visibility_status"])
            self.assertIsNotNone(context["runtime"])
            self.assertEqual(
                ["module-visible"],
                [row["module_id"] for row in context["modules"]],
            )
        finally:
            connection.close()

    def test_missing_or_noncanon_explicit_ids_do_not_create_context_cards(
        self,
    ) -> None:
        connection = self.connection()
        try:
            self._insert_definition(
                connection,
                advantage_id="advantage-planned",
                authority="planned",
            )
            cards = query_advantage_contexts(
                connection,
                [
                    "advantage-missing",
                    "advantage-planned",
                    self.ADVANTAGE_ID,
                ],
                visibility="generation",
            )
            self.assertEqual(
                [self.ADVANTAGE_ID],
                [
                    str(card["definition"]["advantage_id"])
                    for card in cards
                ],
            )
        finally:
            connection.close()

    def test_generation_hides_reveal_control_metadata_everywhere(self) -> None:
        connection = self.connection()
        try:
            self._insert_knowledge(
                connection,
                "knowledge-public",
                "public_narrative",
                "initial",
                updated_order=30,
            )
            self._insert_knowledge(
                connection,
                "knowledge-objective",
                "objective",
                "initial",
                updated_order=20,
            )
            self._insert_knowledge(
                connection,
                "knowledge-author",
                "author_plan",
                "future",
                updated_order=10,
            )
            self._insert_ledger(
                connection,
                "reveal-public",
                "reveal",
                order=50,
                input_value={"knowledge_plane": "public_narrative"},
                output_value={
                    "knowledge_id": "knowledge-public",
                    "reveal_stage": "initial",
                },
            )
            self._insert_ledger(
                connection,
                "reveal-author",
                "reveal",
                order=40,
                input_value={"knowledge_plane": "author_plan"},
                output_value={
                    "knowledge_id": "knowledge-author",
                    "reveal_stage": "future",
                },
            )
            self._insert_ledger(
                connection,
                "use-author-tainted",
                "use",
                order=35,
                input_value={"costs": []},
                output_value={"effects": []},
                provenance={"knowledge_plane": "author_plan"},
            )
            self._insert_ledger(
                connection,
                "use-visible",
                "use",
                order=5,
                input_value={
                    "costs": [{"resource": "charge", "amount": 1}],
                    "nullable": None,
                },
                output_value={
                    "effects": [{"kind": "observe"}],
                    "reveal_stage": "chapter_99",
                    "planned_secret": "FINAL_BOSS",
                },
                loss_value={"exposure_delta": 0.1, "cooldown": None},
                provenance={"author_note": "must stay out of generation"},
            )
            connection.execute(
                """
                INSERT INTO advantage_narrative_contracts(
                    narrative_contract_id, advantage_id, authority_status,
                    contract_status, reveal_ladder_json, updated_order
                ) VALUES(
                    'narrative-fixture', ?, 'canon', 'active', ?, 1
                )
                """,
                (
                    self.ADVANTAGE_ID,
                    json.dumps(
                        ["future reveal", "author_plan"],
                        ensure_ascii=False,
                    ),
                ),
            )

            standalone = query_advantage_ledger(
                connection,
                self.ADVANTAGE_ID,
                visibility="generation",
                limit=1,
            )
            self.assertEqual(
                ["use-visible"],
                [row["entry_id"] for row in standalone],
            )
            self.assertEqual(
                {
                    "entry_id",
                    "advantage_id",
                    "module_id",
                    "branch_id",
                    "entry_kind",
                    "actor_entity_id",
                    "target_entity_id",
                    "input_json",
                    "output_json",
                    "loss_json",
                },
                set(standalone[0]),
            )
            self.assertEqual(
                {"effects": [{"kind": "observe"}]},
                standalone[0]["output_json"],
            )
            self.assertIn("nullable", standalone[0]["input_json"])
            self.assertIsNone(standalone[0]["input_json"]["nullable"])
            self.assertIn("cooldown", standalone[0]["loss_json"])
            self.assertIsNone(standalone[0]["loss_json"]["cooldown"])

            context = query_advantage_context(
                connection,
                self.ADVANTAGE_ID,
                visibility="generation",
            )
            self.assertEqual(1, len(context["knowledge"]))
            self.assertEqual(
                {"text": "secret:knowledge-public"},
                context["knowledge"][0]["claim"],
            )
            self.assertEqual(
                {
                    "knowledge_plane",
                    "knowledge_status",
                    "observer_entity_id",
                    "claim",
                },
                set(context["knowledge"][0]),
            )
            self.assertNotIn(
                "reveal_ladder_json",
                context["narrative_contract"],
            )
            serialized = json.dumps(
                context,
                ensure_ascii=False,
                sort_keys=True,
            )
            self.assertNotIn("author_plan", serialized)
            self.assertNotIn("knowledge-objective", serialized)
            self.assertNotIn("reveal_stage", serialized)

            inspected = query_advantage_ledger(
                connection,
                self.ADVANTAGE_ID,
                visibility="inspection",
            )
            self.assertEqual(
                {
                    "reveal-public",
                    "reveal-author",
                    "use-author-tainted",
                    "use-visible",
                },
                {row["entry_id"] for row in inspected},
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
