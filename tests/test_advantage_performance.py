from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock


from benchmarks.advantage_performance import (
    BENCHMARK_SUITE,
    DEFAULT_FIXTURE,
    FIXTURE_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    SUPPORTED_PROFILES,
    AdvantageBenchmarkFixtureError,
    compare_advantage_results,
    load_advantage_fixture,
    run_advantage_performance_benchmark,
    validate_advantage_fixture,
)
from benchmarks.v15_performance import (  # noqa: E402
    _deterministic_vector,
    _normalized_tokens,
)


class AdvantagePerformanceBenchmarkTests(unittest.TestCase):
    def test_checked_in_fixture_has_required_top_level_contract(self) -> None:
        records = load_advantage_fixture(DEFAULT_FIXTURE)
        validation = validate_advantage_fixture(records)

        self.assertEqual("valid", validation["status"])
        self.assertEqual(BENCHMARK_SUITE, validation["suite"])
        self.assertEqual(FIXTURE_SCHEMA_VERSION, validation["schema_version"])
        self.assertEqual(SUPPORTED_PROFILES, set(validation["profiles"]))
        self.assertEqual(5, validation["case_count"])
        self.assertEqual(10, validation["critical_fact_count"])
        self.assertEqual(20, validation["mandatory_section_count"])
        for record in records:
            self.assertEqual(
                {
                    "schema_version",
                    "case_id",
                    "profile",
                    "prompt",
                    "expected_advantage_ids",
                    "expected_module_ids",
                    "critical_facts",
                    "mandatory_sections",
                    "authority_text",
                },
                set(record),
            )

    def test_fixture_rejects_declared_fact_missing_from_authority(self) -> None:
        records = load_advantage_fixture(DEFAULT_FIXTURE)
        malformed = deepcopy(records)
        malformed[0]["critical_facts"].append("FACT_NOT_IN_AUTHORITY")

        with self.assertRaisesRegex(
            AdvantageBenchmarkFixtureError,
            "missing declared markers",
        ):
            validate_advantage_fixture(malformed)

    def test_offline_run_reports_latency_requests_quality_and_caches(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_advantage_performance_benchmark(
                DEFAULT_FIXTURE,
                workspace_parent=temporary,
                iterations=2,
                warmup_iterations=1,
                max_concurrency=4,
            )

        self.assertTrue(result["passed"])
        self.assertEqual("passed", result["status"])
        self.assertEqual(RESULT_SCHEMA_VERSION, result["schema_version"])
        self.assertFalse(result["network_used"])
        self.assertEqual("not_requested", result["live"]["status"])

        contract = result["provider_contract"]["embedding"]
        self.assertEqual("singleton_exact", contract["input_semantics"])
        self.assertFalse(contract["batch_provider_enabled"])
        self.assertEqual(4, contract["embedding_single_max_concurrency"])
        self.assertEqual(
            1,
            result["offline"]["accepted_snapshot"]["read_count"],
        )
        self.assertTrue(
            result["offline"]["accepted_snapshot"]["manifest_gated"]
        )

        for phase in (
            "reference_serial",
            "optimized_cold",
            "optimized_exact_cache",
            "optimized_candidate_cache",
        ):
            distribution = result["offline"]["latency"][phase]
            self.assertEqual(2, distribution["sample_count"])
            self.assertGreaterEqual(
                distribution["p95_ms"],
                distribution["p50_ms"],
            )
            self.assertGreaterEqual(distribution["p50_ms"], 0.0)

        case_count = result["offline"]["case_count"]
        requests = result["offline"]["request_counts"]
        self.assertEqual(
            case_count * 4,
            requests["embedding_requests"],
        )
        self.assertEqual(
            case_count * 4,
            requests["rerank_requests"],
        )
        self.assertEqual(
            case_count * 8,
            requests["inference_provider_requests"],
        )
        self.assertEqual(0, requests["inference_http_requests"])

        quality = result["offline"]["quality"]
        self.assertEqual(0, quality["critical_fact_mismatch_count"])
        self.assertEqual(0, quality["selected_mismatch_count"])
        self.assertEqual(0, quality["stable_id_mismatch_count"])
        self.assertEqual(0, quality["mandatory_section_mismatch_count"])
        self.assertEqual(0, quality["expected_selected_mismatch_count"])
        self.assertTrue(quality["passed"])

        cache = result["offline"]["cache_gate"]["representative"]
        self.assertEqual(case_count, cache["cold_single_embedding_requests"])
        self.assertEqual(case_count, cache["cold_rerank_requests"])
        self.assertEqual(case_count, cache["exact_embedding_cache_hits"])
        self.assertEqual(case_count, cache["exact_rerank_cache_hits"])
        self.assertEqual(case_count, cache["candidate_cache_hits"])
        self.assertEqual(0, cache["exact_provider_request_count"])
        self.assertEqual(0, cache["candidate_provider_request_count"])
        self.assertTrue(cache["passed"])

        self.assertEqual(
            result["offline"]["latency"]["optimized_cold"]["p50_ms"],
            result["summary"]["offline_p50_ms"],
        )
        self.assertEqual(
            result["offline"]["latency"]["optimized_cold"]["p95_ms"],
            result["summary"]["offline_p95_ms"],
        )

    def test_report_is_redacted_from_fixture_prose_and_credentials(self) -> None:
        records = load_advantage_fixture(DEFAULT_FIXTURE)
        fake_key = "TEST_ONLY_ADVANTAGE_SILICONFLOW_KEY"
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ,
            {"SILICONFLOW_API_KEY": fake_key},
            clear=False,
        ):
            result = run_advantage_performance_benchmark(
                DEFAULT_FIXTURE,
                workspace_parent=temporary,
                iterations=1,
                warmup_iterations=0,
                include_live=False,
            )
        serialized = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
        )

        self.assertNotIn(fake_key, serialized)
        for record in records:
            self.assertNotIn(record["prompt"], serialized)
            self.assertNotIn(record["authority_text"], serialized)
        self.assertNotIn(str(DEFAULT_FIXTURE.resolve()), serialized)

    def test_requested_live_lane_skips_cleanly_without_environment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ,
            {"SILICONFLOW_API_KEY": ""},
            clear=False,
        ):
            result = run_advantage_performance_benchmark(
                DEFAULT_FIXTURE,
                workspace_parent=temporary,
                iterations=1,
                warmup_iterations=0,
                include_live=True,
            )

        self.assertTrue(result["passed"])
        self.assertEqual("skipped", result["live"]["status"])
        self.assertFalse(result["live"]["network_used"])
        self.assertEqual("missing_environment", result["live"]["reason"])

    def test_live_lane_uses_siliconflow_protocol_with_stubbed_transport(
        self,
    ) -> None:
        def embedding_call(_service, inputs):  # type: ignore[no-untyped-def]
            return (
                [_deterministic_vector(value) for value in inputs],
                {
                    "status": "ok",
                    "attempts": 1,
                    "retry_count": 0,
                },
            )

        def rerank_call(  # type: ignore[no-untyped-def]
            _service,
            query,
            documents,
            top_n,
        ):
            query_tokens = set(_normalized_tokens(query))
            ranked = []
            for index, document in enumerate(documents):
                document_tokens = set(_normalized_tokens(document))
                score = len(query_tokens & document_tokens) / max(
                    1,
                    len(query_tokens),
                )
                ranked.append((index, score))
            ranked.sort(key=lambda item: item[1], reverse=True)
            return (
                ranked[:top_n],
                {
                    "status": "ok",
                    "attempts": 1,
                    "retry_count": 0,
                },
            )

        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(
                os.environ,
                {"SILICONFLOW_API_KEY": "TEST_ONLY_STUBBED_KEY"},
                clear=False,
            ),
            mock.patch(
                "benchmarks.advantage_performance.state_rag._embedding_call",
                side_effect=embedding_call,
            ),
            mock.patch(
                "benchmarks.advantage_performance.state_rag._rerank_call",
                side_effect=rerank_call,
            ),
        ):
            result = run_advantage_performance_benchmark(
                DEFAULT_FIXTURE,
                workspace_parent=temporary,
                iterations=1,
                warmup_iterations=0,
                include_live=True,
                live_iterations=1,
            )

        self.assertTrue(result["passed"])
        self.assertEqual("passed", result["live"]["status"])
        self.assertTrue(result["live"]["network_used"])
        self.assertEqual(0, result["live"]["quality"]["selected_mismatch_count"])
        self.assertEqual(
            0,
            result["live"]["quality"]["critical_fact_mismatch_count"],
        )
        self.assertGreater(
            result["live"]["request_counts"]["inference_http_requests"],
            0,
        )
        self.assertEqual(
            result["live"]["latency"]["optimized_cold"]["p50_ms"],
            result["summary"]["live_p50_ms"],
        )

    def test_comparison_counts_selected_and_critical_fact_mismatch(
        self,
    ) -> None:
        case = deepcopy(load_advantage_fixture(DEFAULT_FIXTURE)[0])
        case["expected_path"] = "canon/advantages/inheritance-001.md"
        reference_text = case["authority_text"]
        reference = [
            [
                {
                    "chunk_id": "chunk-reference",
                    "path": case["expected_path"],
                    "ordinal": 0,
                    "content_sha256": "a" * 64,
                    "text": reference_text,
                }
            ]
        ]
        candidate = [
            [
                {
                    "chunk_id": "chunk-wrong",
                    "path": "canon/advantages/wrong.md",
                    "ordinal": 0,
                    "content_sha256": "b" * 64,
                    "text": "[advantage_definition]\nwrong context",
                }
            ]
        ]

        comparison = compare_advantage_results(
            [case],
            reference,
            candidate,
        )

        self.assertFalse(comparison["passed"])
        self.assertEqual(1, comparison["selected_mismatch_count"])
        self.assertEqual(
            len(case["critical_facts"]),
            comparison["critical_fact_mismatch_count"],
        )
        self.assertEqual(
            len(case["expected_advantage_ids"])
            + len(case["expected_module_ids"]),
            comparison["stable_id_mismatch_count"],
        )
        self.assertGreater(
            comparison["mandatory_section_mismatch_count"],
            0,
        )
        self.assertEqual(1, comparison["expected_selected_mismatch_count"])


if __name__ == "__main__":
    unittest.main()
