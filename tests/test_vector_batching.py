from __future__ import annotations

import math
import unittest
from unittest import mock

from scripts import state_rag
from scripts.longform import authority


def _legacy_authority_cosine(
    left: list[float],
    right: list[float],
) -> float | None:
    if not left or len(left) != len(right):
        return None
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 0.0 or right_norm <= 0.0:
        return None
    score = dot / (left_norm * right_norm)
    if not math.isfinite(score):
        return None
    return max(-1.0, min(1.0, score))


def _legacy_state_cosine(
    left: list[float],
    right: list[float],
) -> float:
    if not left or len(left) != len(right):
        return -1.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return -1.0
    return dot / (left_norm * right_norm)


class AuthorityVectorBatchingTests(unittest.TestCase):
    def test_batch_scores_are_scalar_exact_and_query_norm_is_computed_once(
        self,
    ) -> None:
        query = [3.0, 4.0]
        candidates = [
            [4.0, 3.0],
            [-3.0, 4.0],
            [9.0, 12.0],
        ]
        expected = [
            _legacy_authority_cosine(query, candidate)
            for candidate in candidates
        ]
        norm_inputs: list[tuple[float, ...]] = []
        original = authority._vector_norm

        def observed(values):
            norm_inputs.append(tuple(values))
            return original(values)

        with mock.patch.object(
            authority,
            "_vector_norm",
            side_effect=observed,
        ):
            actual = authority._cosine_many(query, candidates)

        self.assertEqual(expected, actual)
        self.assertEqual(1, norm_inputs.count(tuple(query)))
        self.assertEqual(
            [expected[0]],
            [authority._cosine(query, candidates[0])],
        )

    def test_invalid_candidate_degrades_locally_without_reordering_siblings(
        self,
    ) -> None:
        query = [1.0, 2.0]
        candidates = [
            [1.0, 2.0],
            [0.0, 0.0],
            [1.0],
            [float("nan"), 2.0],
            None,
            [2.0, 1.0],
        ]

        scores = authority._cosine_many(query, candidates)

        self.assertEqual(
            [
                _legacy_authority_cosine(query, [1.0, 2.0]),
                None,
                None,
                None,
                None,
                _legacy_authority_cosine(query, [2.0, 1.0]),
            ],
            scores,
        )


class StateVectorBatchingTests(unittest.TestCase):
    def test_batch_scores_are_scalar_exact_and_query_norm_is_computed_once(
        self,
    ) -> None:
        query = [5.0, 12.0]
        candidates = [
            [12.0, 5.0],
            [-5.0, 12.0],
            [10.0, 24.0],
        ]
        expected = [
            _legacy_state_cosine(query, candidate)
            for candidate in candidates
        ]
        norm_inputs: list[tuple[float, ...]] = []
        original = state_rag._vector_norm

        def observed(values):
            norm_inputs.append(tuple(values))
            return original(values)

        with mock.patch.object(
            state_rag,
            "_vector_norm",
            side_effect=observed,
        ):
            actual = state_rag._cosine_many(query, candidates)

        self.assertEqual(expected, actual)
        self.assertEqual(1, norm_inputs.count(tuple(query)))
        self.assertEqual(
            expected[0],
            state_rag._cosine(query, candidates[0]),
        )

    def test_invalid_candidate_degrades_only_its_slot(self) -> None:
        query = [1.0, 2.0]
        candidates = [
            [1.0, 2.0],
            [0.0, 0.0],
            [1.0],
            [True, 2.0],
            [float("inf"), 2.0],
            {"not": "a-vector"},
            [2.0, 1.0],
        ]

        scores = state_rag._cosine_many(query, candidates)

        self.assertEqual(
            [
                _legacy_state_cosine(query, [1.0, 2.0]),
                -1.0,
                -1.0,
                -1.0,
                -1.0,
                -1.0,
                _legacy_state_cosine(query, [2.0, 1.0]),
            ],
            scores,
        )


if __name__ == "__main__":
    unittest.main()
