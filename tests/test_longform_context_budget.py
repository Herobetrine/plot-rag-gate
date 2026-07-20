from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v1_runtime  # noqa: E402


class _FakeContinuityService:
    def __init__(self, _root: Path) -> None:
        pass

    @staticmethod
    def get_canon_revisions() -> dict[str, int]:
        return {"active": 17, "latest": 17}

    @staticmethod
    def query_facts(**kwargs: object) -> dict[str, object]:
        if kwargs.get("fact_type") == "open_loop":
            kind = "open_loop"
            count = 12
        elif kwargs.get("scope") == "planned":
            kind = "planned"
            count = 18
        elif kwargs.get("scope") == "historical":
            kind = "historical"
            count = 18
        else:
            kind = "current"
            count = 40
        return {
            "facts": [
                {
                    "scope": kind,
                    "fact_type": kind,
                    "entity_id": f"entity-{index}",
                    "field_name": "payload",
                    "value": f"{kind}-marker-{index}-" + ("状态约束" * 80),
                }
                for index in range(count)
            ]
        }


class _FakeContractBuilder:
    seen_budget = 0

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def build(self, _prompt: str, **kwargs: object) -> dict[str, object]:
        budget = int(kwargs["max_context_chars"])
        type(self).seen_budget = budget
        text = ("AUTHORITY-MANDATORY-CONTEXT\n" + ("权威原文" * 800))[:budget]
        return {
            "needs": [
                {
                    "category": "resource",
                    "mandatory": True,
                }
            ],
            "missing_mandatory": ["resource"],
            "mandatory_shortfall": {"resource": 1},
            "context_text": text,
            "context_chars": len(text),
            "max_context_chars": budget,
            "within_budget": len(text) <= budget,
        }


class _FakeAuthorityIndex:
    @staticmethod
    def schema_info() -> dict[str, object]:
        return {
            "embedding_enabled": False,
            "embedding_model": "lexical-only",
            "rerank_enabled": False,
            "rerank_model": "disabled",
        }


class _FakeMethodPack:
    @staticmethod
    def retrieve(*_args: object, **_kwargs: object) -> list[dict[str, str]]:
        return [{"id": "method", "title": "method"}]

    @staticmethod
    def render_guidance(
        _cards: object,
        *,
        expose_internal_checks: bool,
    ) -> str:
        self_check = "hidden" if not expose_internal_checks else "shown"
        return f"METHOD-{self_check}-" + ("写作方法" * 500)


class _FakePatternStore:
    def __init__(self, _path: Path) -> None:
        pass

    @staticmethod
    def query(*_args: object, **_kwargs: object) -> list[dict[str, str]]:
        return [{"pattern_text": "PATTERN-" + ("项目模式" * 500)}]


class LongformContextBudgetTests(unittest.TestCase):
    @staticmethod
    def _build_with_fakes(budget: int) -> dict[str, object]:
        config = {
            "state": {"max_context_chars": 24000},
            "lifecycle": {"longform_context_chars": budget},
            "craft": {"use_embedding": False, "use_rerank": False},
        }
        power_state = {
            "abilities": [
                {
                    "ability_id": "oversized-power-spec",
                    "power_spec": "POWERSPEC-" + ("力量定义" * 5000),
                }
            ]
        }
        patchers = (
            patch.object(v1_runtime, "load_config", return_value=config),
            patch.object(
                v1_runtime,
                "ContinuityService",
                _FakeContinuityService,
            ),
            patch.object(
                v1_runtime,
                "refresh_longform_index",
                return_value={"status": "ready"},
            ),
            patch.object(
                v1_runtime,
                "_authority_index",
                return_value=_FakeAuthorityIndex(),
            ),
            patch.object(
                v1_runtime,
                "LayeredMemoryStore",
                lambda _path: object(),
            ),
            patch.object(
                v1_runtime,
                "AcceptedSummaryStore",
                lambda _path: object(),
            ),
            patch.object(
                v1_runtime,
                "ContextContractBuilder",
                _FakeContractBuilder,
            ),
            patch.object(
                v1_runtime,
                "query_power_state",
                return_value=power_state,
            ),
            patch.object(
                v1_runtime,
                "WebnovelMethodPack",
                _FakeMethodPack,
            ),
            patch.object(
                v1_runtime,
                "ProjectPatternStore",
                _FakePatternStore,
            ),
        )
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)
            return v1_runtime.build_longform_context(
                Path(temporary),
                "推演战斗并核对伏笔、力量、能力与资源",
                artifact_context={
                    "artifact_stage": "outline",
                    "task": "outline",
                    "branch_id": "main",
                    "chapter_no": 8,
                    "scene_index": 1,
                },
                max_context_chars=budget,
                _accepted_identity={
                    "head_canon_revision": 0,
                    "active_canon_revision": 0,
                    "active_projection_hash": "f" * 64,
                },
            )

    def test_final_envelope_enforces_budget_and_preserves_priority_contract(
        self,
    ) -> None:
        budget = 12000
        result = self._build_with_fakes(budget)

        context = result["context"]
        self.assertLessEqual(len(context), budget)
        self.assertEqual(len(context), result["context_chars"])
        self.assertEqual(budget, result["max_context_chars"])
        self.assertTrue(result["within_budget"])
        self.assertEqual(len(context), result["context_budget"]["context_chars"])
        self.assertTrue(result["context_budget"]["within_budget"])
        self.assertTrue(result["context_budget"]["boundary_complete"])
        self.assertTrue(context.startswith("[WEBNOVEL_CONTINUITY_CONTRACT]"))
        self.assertTrue(context.endswith("[/WEBNOVEL_CONTINUITY_CONTRACT]"))
        for heading in (
            "[ACCEPTED_PRECISE_STATE]",
            "[ACTIVE_OPEN_LOOPS]",
            "[ACCEPTED_POWER_STATE]",
            "[LONGFORM_AUTHORITY_AND_MEMORY]",
        ):
            self.assertIn(heading, context)
        self.assertIn("current-marker-0", context)
        self.assertIn("open_loop-marker-0", context)
        self.assertIn("POWERSPEC-", context)
        self.assertIn(result["contract"]["context_text"], context)
        self.assertEqual(["resource"], result["contract"]["missing_mandatory"])
        self.assertEqual(
            {"resource": 1},
            result["contract"]["mandatory_shortfall"],
        )
        self.assertEqual(
            _FakeContractBuilder.seen_budget,
            result["contract"]["max_context_chars"],
        )
        self.assertEqual(
            _FakeContractBuilder.seen_budget,
            result["context_budget"]["contract_content_quota"],
        )
        self.assertLessEqual(
            result["contract"]["context_chars"],
            result["contract"]["max_context_chars"],
        )

    def test_every_small_limit_is_a_true_hard_limit(self) -> None:
        for budget in (1, 2, 31, 63, 64, 65, 96, 127, 255, 511, 1024):
            with self.subTest(budget=budget):
                result = self._build_with_fakes(budget)
                self.assertLessEqual(len(result["context"]), budget)
                self.assertEqual(len(result["context"]), result["context_chars"])
                self.assertTrue(result["within_budget"])
                self.assertEqual(
                    budget,
                    result["context_budget"]["max_context_chars"],
                )

    def test_multiline_truncation_never_exceeds_remaining_one(self) -> None:
        text = "a\n" + ("b" * 80)
        for budget in range(0, len(text) + 1):
            with self.subTest(budget=budget):
                fitted, truncated = v1_runtime._truncate_context_text(
                    text,
                    budget,
                )
                self.assertLessEqual(len(fitted), budget)
                self.assertEqual(len(text) > budget, truncated)


if __name__ == "__main__":
    unittest.main()
