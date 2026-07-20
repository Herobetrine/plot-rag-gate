from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from benchmarks.v15_performance import (  # noqa: E402
    ARTIFACT_TIMESTAMP_INVALID_CODE,
    BENCHMARK_SUITE,
    BenchmarkFixtureError,
    DEFAULT_FIXTURE,
    OfflineEmbeddingProvider,
    OfflineRerankProvider,
    REDACTED_MANIFEST_SCHEMA_VERSION,
    _build_offline_index,
    _materialize_fixture,
    build_redacted_result,
    build_redacted_run_manifest,
    compare_legacy_and_batched_results,
    create_run_artifact_directory,
    default_fixture_manifest,
    evaluate_severe_regression,
    load_fixture_manifest,
    run_v15_performance_benchmark,
    validate_fixture_manifest,
    write_json,
)


class V15PerformanceBenchmarkTests(unittest.TestCase):
    def test_offline_provider_cache_identity_binds_behavior_and_scope(
        self,
    ) -> None:
        marker = "TOKEN_SHOULD_NOT_LEAK"
        isolation = "run-secret/scenario-secret"
        embedding = OfflineEmbeddingProvider(
            batch_mode="fail",
            failure_markers=[marker, marker.casefold()],
            isolation_id=isolation,
        )
        equivalent_embedding = OfflineEmbeddingProvider(
            batch_mode="fail",
            failure_markers=[marker.casefold()],
            isolation_id=isolation,
        )
        changed_mode = OfflineEmbeddingProvider(
            batch_mode="wrong_length",
            failure_markers=[marker],
            isolation_id=isolation,
        )
        changed_marker = OfflineEmbeddingProvider(
            batch_mode="fail",
            failure_markers=["DIFFERENT_MARKER"],
            isolation_id=isolation,
        )
        changed_scope = OfflineEmbeddingProvider(
            batch_mode="fail",
            failure_markers=[marker],
            isolation_id="run-secret/scenario-other",
        )
        rerank = OfflineRerankProvider(
            expected_parallelism=2,
            delay_ms=17,
            isolation_id=isolation,
        )
        equivalent_rerank = OfflineRerankProvider(
            expected_parallelism=2,
            delay_ms=17,
            isolation_id=isolation,
        )
        changed_delay = OfflineRerankProvider(
            expected_parallelism=2,
            delay_ms=18,
            isolation_id=isolation,
        )
        changed_rerank_scope = OfflineRerankProvider(
            expected_parallelism=2,
            delay_ms=17,
            isolation_id="run-other/scenario-secret",
        )

        self.assertEqual(
            embedding.cache_identity,
            equivalent_embedding.cache_identity,
        )
        self.assertNotEqual(
            embedding.cache_identity,
            changed_mode.cache_identity,
        )
        self.assertNotEqual(
            embedding.cache_identity,
            changed_marker.cache_identity,
        )
        self.assertNotEqual(
            embedding.cache_identity,
            changed_scope.cache_identity,
        )
        self.assertEqual(
            rerank.cache_identity,
            equivalent_rerank.cache_identity,
        )
        self.assertNotEqual(
            rerank.cache_identity,
            changed_delay.cache_identity,
        )
        self.assertNotEqual(
            rerank.cache_identity,
            changed_rerank_scope.cache_identity,
        )
        self.assertTrue(
            embedding.cache_identity.startswith(
                "plot-rag-v15-offline-embedding/v2:"
            )
        )
        self.assertTrue(
            rerank.cache_identity.startswith(
                "plot-rag-v15-offline-rerank/v2:"
            )
        )
        for identity in (
            embedding.cache_identity,
            rerank.cache_identity,
        ):
            self.assertLessEqual(len(identity), 128)
            self.assertNotIn(marker.casefold(), identity.casefold())
            self.assertNotIn(isolation.casefold(), identity.casefold())

    def test_process_singleflight_isolated_across_offline_scenarios(
        self,
    ) -> None:
        leader_entered = threading.Event()
        release_leader = threading.Event()
        follower_entered = threading.Event()

        class BlockingEmbeddingProvider(OfflineEmbeddingProvider):
            def embed_many(self, texts):  # type: ignore[no-untyped-def]
                if self.query_phase:
                    leader_entered.set()
                    if not release_leader.wait(5.0):
                        raise TimeoutError("test leader release timed out")
                return super().embed_many(texts)

        class SignalingEmbeddingProvider(OfflineEmbeddingProvider):
            def embed_many(self, texts):  # type: ignore[no-untyped-def]
                if self.query_phase:
                    follower_entered.set()
                return super().embed_many(texts)

        manifest = default_fixture_manifest()
        query = str(manifest["needs"][0]["query"])
        failure_marker = query.split()[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_root = root / "synthetic-project"
            _materialize_fixture(project_root, manifest)
            leader_embedding = BlockingEmbeddingProvider(
                batch_mode="ok",
                failure_markers=[],
                isolation_id="run-a/scenario-a",
            )
            follower_embedding = SignalingEmbeddingProvider(
                batch_mode="fail",
                failure_markers=[failure_marker],
                isolation_id="run-b/scenario-b",
            )
            leader_rerank = OfflineRerankProvider(
                expected_parallelism=1,
                delay_ms=0,
                isolation_id="run-a/scenario-a",
            )
            follower_rerank = OfflineRerankProvider(
                expected_parallelism=1,
                delay_ms=19,
                isolation_id="run-b/scenario-b",
            )
            settings = manifest["settings"]
            leader_index = _build_offline_index(
                root / "leader.sqlite3",
                embedding=leader_embedding,
                rerank=leader_rerank,
                settings=settings,
                rerank_max_concurrency=1,
                batched=True,
            )
            follower_index = _build_offline_index(
                root / "follower.sqlite3",
                embedding=follower_embedding,
                rerank=follower_rerank,
                settings=settings,
                rerank_max_concurrency=1,
                batched=True,
            )
            leader_index.refresh(project_root, manifest["sources"])
            follower_index.refresh(project_root, manifest["sources"])
            leader_embedding.begin_query_phase()
            follower_embedding.begin_query_phase()
            follower_before = follower_embedding.snapshot()
            results: dict[str, object] = {}
            errors: dict[str, BaseException] = {}

            def invoke(name: str, index) -> None:  # type: ignore[no-untyped-def]
                try:
                    results[name] = index.search_many([query], limit=3)
                    results[f"{name}_diagnostics"] = (
                        index.last_search_diagnostics()
                    )
                except BaseException as error:
                    errors[name] = error

            leader_thread = threading.Thread(
                target=invoke,
                args=("leader", leader_index),
                daemon=True,
            )
            follower_thread = threading.Thread(
                target=invoke,
                args=("follower", follower_index),
                daemon=True,
            )
            leader_started = False
            follower_started = False
            follower_reached_provider = False
            try:
                leader_thread.start()
                leader_started = leader_entered.wait(5.0)
                if leader_started:
                    follower_thread.start()
                    follower_started = True
                    follower_reached_provider = follower_entered.wait(5.0)
            finally:
                release_leader.set()
                leader_thread.join(5.0)
                if follower_started:
                    follower_thread.join(5.0)

            self.assertTrue(leader_started)
            self.assertTrue(follower_reached_provider)
            self.assertFalse(leader_thread.is_alive())
            self.assertFalse(follower_thread.is_alive())
            self.assertEqual({}, errors)
            follower_after = follower_embedding.snapshot()
            self.assertEqual(
                1,
                follower_after["embedding_batch_calls"]
                - follower_before["embedding_batch_calls"],
            )
            self.assertEqual(
                1,
                follower_after["embedding_batch_failures"]
                - follower_before["embedding_batch_failures"],
            )
            self.assertEqual(
                1,
                follower_after["embedding_single_calls"]
                - follower_before["embedding_single_calls"],
            )
            self.assertEqual(
                1,
                follower_after["embedding_single_failures"]
                - follower_before["embedding_single_failures"],
            )
            follower_diagnostics = results["follower_diagnostics"]
            self.assertIsInstance(follower_diagnostics, dict)
            assert isinstance(follower_diagnostics, dict)
            self.assertEqual(
                0,
                follower_diagnostics["search_singleflight_waits"],
            )
            follower_results = results["follower"]
            self.assertIsInstance(follower_results, list)
            assert isinstance(follower_results, list)
            self.assertEqual(
                "failed",
                follower_results[0][0]["embedding_status"],
            )

    def test_checked_in_fixture_is_valid_and_reproducible(self) -> None:
        checked_in = load_fixture_manifest(DEFAULT_FIXTURE)
        validation = validate_fixture_manifest(checked_in)

        self.assertEqual("valid", validation["status"])
        self.assertEqual(BENCHMARK_SUITE, validation["suite"])
        self.assertEqual([1, 3, 5], validation["covered_need_counts"])
        self.assertTrue(validation["batch_fallback_covered"])
        self.assertEqual(default_fixture_manifest(), checked_in)
        serialized = json.dumps(checked_in, ensure_ascii=False)
        for project_identifier in (
            "合成样例",
            "测试角色甲",
            "测试城",
            "测试角色丙",
            "测试角色乙",
        ):
            self.assertNotIn(project_identifier, serialized)

    def test_fixture_paths_reject_windows_and_traversal_escapes(self) -> None:
        invalid_paths = (
            "C:/private/novel.md",
            "//server/share/novel.md",
            "canon/novel.md:secret",
            "canon/CON.txt",
            "canon/LPT1.md",
            "canon/trailing-dot.",
            "../outside.md",
            "canon/./outside.md",
            "canon//outside.md",
            "canon\\outside.md",
        )
        for invalid_path in invalid_paths:
            with self.subTest(path=invalid_path):
                manifest = default_fixture_manifest()
                manifest["files"][0]["path"] = invalid_path
                with self.assertRaises(BenchmarkFixtureError):
                    validate_fixture_manifest(manifest)

    def test_fixture_paths_reject_case_insensitive_collisions(self) -> None:
        manifest = default_fixture_manifest()
        duplicate = deepcopy(manifest["files"][0])
        duplicate["path"] = str(duplicate["path"]).upper()
        manifest["files"].append(duplicate)
        with self.assertRaisesRegex(
            BenchmarkFixtureError,
            "duplicate fixture path",
        ):
            validate_fixture_manifest(manifest)

    def test_fixture_source_globs_reject_non_portable_boundaries(self) -> None:
        for invalid_glob in (
            "C:canon/**/*.md",
            "C:/canon/**/*.md",
            "//server/share/**/*.md",
            "..\\outside\\**\\*.md",
            "../outside/**/*.md",
            "canon//**/*.md",
        ):
            with self.subTest(glob=invalid_glob):
                manifest = default_fixture_manifest()
                manifest["sources"][0]["glob"] = invalid_glob
                with self.assertRaises(BenchmarkFixtureError):
                    validate_fixture_manifest(manifest)

    def test_offline_run_covers_cache_batch_fallback_and_parallelism(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_v15_performance_benchmark(
                DEFAULT_FIXTURE,
                workspace_parent=temporary,
                rerank_delay_ms=1,
                iterations=3,
                warmup_iterations=1,
            )

        self.assertTrue(result["passed"])
        self.assertEqual("passed", result["status"])
        self.assertFalse(result["network_required"])
        self.assertEqual(
            [1, 3, 5],
            result["quality_gate"]["covered_need_counts"],
        )
        by_id = {
            scenario["scenario_id"]: scenario
            for scenario in result["scenarios"]
        }
        for count in (1, 3, 5):
            scenario = by_id[f"needs-{count}-batched"]
            phases = {
                phase["phase"]: phase for phase in scenario["phases"]
            }
            legacy = phases["legacy_serial"]
            cold = phases["cold"]
            hot = phases["hot"]
            self.assertEqual(
                legacy["semantic_results_sha256"],
                cold["semantic_results_sha256"],
            )
            self.assertEqual(1, cold["authority"]["embedding_batch_calls"])
            self.assertEqual(
                0,
                cold["authority"]["embedding_single_fallbacks"],
            )
            self.assertEqual(0, cold["authority"]["candidate_cache_hits"])
            self.assertEqual(
                count,
                hot["authority"]["candidate_cache_hits"],
            )
            self.assertEqual(
                cold["semantic_results_sha256"],
                hot["semantic_results_sha256"],
            )
            self.assertEqual(1.0, cold["top1_accuracy"])
            self.assertGreaterEqual(
                cold["providers"]["rerank_max_active"],
                count,
            )
            self.assertTrue(scenario["comparison"]["passed"])
            self.assertEqual(
                3,
                scenario["timing_summary"]["cold"]["sample_count"],
            )
            self.assertGreaterEqual(
                scenario["timing_summary"]["cold"]["p95_ms"],
                scenario["timing_summary"]["cold"]["p50_ms"],
            )
            self.assertTrue(
                scenario["quality_gate"]["severe_regression_gate"]["passed"]
            )

        fallback = by_id["needs-5-batch-fallback"]
        fallback_phases = {
            phase["phase"]: phase for phase in fallback["phases"]
        }
        cold = fallback_phases["cold"]
        hot = fallback_phases["hot"]
        self.assertEqual(1, cold["authority"]["embedding_batch_calls"])
        self.assertEqual(1, cold["authority"]["embedding_batch_failures"])
        self.assertEqual(
            5,
            cold["authority"]["embedding_single_fallbacks"],
        )
        self.assertEqual(5, cold["authority"]["embedding_single_calls"])
        degraded = [
            health
            for health in cold["authority"]["queries"]
            if health["embedding_status"] == "failed"
        ]
        self.assertEqual(1, len(degraded))
        self.assertGreater(degraded[0]["result_count"], 0)
        self.assertFalse(degraded[0]["miss_confirmed"])
        self.assertEqual(4, hot["authority"]["candidate_cache_hits"])
        self.assertEqual(1.0, cold["top1_accuracy"])
        self.assertTrue(
            fallback["quality_gate"]["cold_hot_semantic_equivalence"]
        )

        capped = by_id["needs-5-cap-2"]
        capped_cold = {
            phase["phase"]: phase for phase in capped["phases"]
        }["cold"]
        self.assertGreaterEqual(
            capped_cold["providers"]["rerank_max_active"],
            2,
        )
        self.assertLessEqual(
            capped_cold["providers"]["rerank_max_active"],
            2,
        )

        for mode in ("wrong-length", "bad-index", "duplicate-index"):
            malformed = by_id[f"needs-5-batch-{mode}"]
            malformed_cold = {
                phase["phase"]: phase for phase in malformed["phases"]
            }["cold"]
            self.assertEqual(
                1,
                malformed_cold["authority"]["embedding_batch_failures"],
            )
            self.assertEqual(
                5,
                malformed_cold["authority"]["embedding_single_fallbacks"],
            )
            self.assertEqual(1.0, malformed_cold["top1_accuracy"])
            self.assertTrue(malformed["comparison"]["passed"])

        stages = result["telemetry"]["stages"]
        self.assertTrue(stages)
        self.assertEqual("fixture.materialize", stages[0]["stage"])
        self.assertTrue(
            any(stage["stage"].endswith(".cold_search") for stage in stages)
        )
        self.assertTrue(
            all(float(stage["duration_ms"]) >= 0.0 for stage in stages)
        )

    def test_redacted_manifest_has_hashes_not_fixture_prose_or_paths(
        self,
    ) -> None:
        fixture = load_fixture_manifest(DEFAULT_FIXTURE)
        redacted = build_redacted_run_manifest(fixture)
        serialized = json.dumps(
            redacted,
            ensure_ascii=False,
            sort_keys=True,
        )

        self.assertEqual(
            REDACTED_MANIFEST_SCHEMA_VERSION,
            redacted["schema_version"],
        )
        self.assertFalse(redacted["network_required"])
        for need in fixture["needs"]:
            self.assertNotIn(need["query"], serialized)
            self.assertNotIn(need["expected_path"], serialized)
        for record in fixture["files"]:
            self.assertNotIn(record["content"], serialized)
            self.assertNotIn(record["path"], serialized)
        self.assertNotIn(str(PLUGIN_ROOT.resolve()), serialized)
        self.assertNotIn("api_key", serialized.casefold())
        self.assertEqual(
            len(fixture["scenarios"]),
            len(redacted["scenarios"]),
        )
        self.assertTrue(
            all(
                len(fingerprint) == 64
                for scenario in redacted["scenarios"]
                for fingerprint in scenario["need_fingerprints"]
            )
        )

    def test_default_result_is_redacted_as_a_whole(self) -> None:
        fixture = load_fixture_manifest(DEFAULT_FIXTURE)
        raw = run_v15_performance_benchmark(
            fixture,
            rerank_delay_ms=0,
            iterations=1,
            warmup_iterations=0,
        )
        redacted = build_redacted_result(raw)
        serialized = json.dumps(
            redacted,
            ensure_ascii=False,
            sort_keys=True,
        )
        for need in fixture["needs"]:
            self.assertNotIn(need["query"], serialized)
            self.assertNotIn(need["expected_path"], serialized)
        for record in fixture["files"]:
            self.assertNotIn(record["content"], serialized)
            self.assertNotIn(record["path"], serialized)
        self.assertNotIn(str(PLUGIN_ROOT.resolve()), serialized)
        self.assertNotIn(str(fixture["fixture_id"]), serialized)
        provenance = redacted["provenance"]
        self.assertEqual(1, provenance["parameters"]["iterations"])
        self.assertEqual(0, provenance["parameters"]["warmup_iterations"])
        self.assertEqual(0, provenance["parameters"]["rerank_delay_ms"])
        self.assertEqual(64, len(provenance["fixture_sha256"]))
        self.assertEqual(64, len(provenance["config_sha256"]))
        self.assertEqual(
            64,
            len(provenance["effective_config_sha256"]),
        )
        self.assertEqual(64, len(provenance["parameters_sha256"]))
        self.assertIn("version", provenance["python"])
        self.assertIn("system", provenance["platform"])

    def test_comparator_detects_selected_chunk_or_context_drift(self) -> None:
        needs = [{"id": "need-a"}]
        baseline = [
            [
                {
                    "path": "canon/a.md",
                    "ordinal": 0,
                    "start_line": 1,
                    "end_line": 1,
                    "content_sha256": "a" * 64,
                    "text": "alpha",
                    "role": "canon",
                    "scope_policy": "current",
                    "ingest_policy": "include",
                    "priority": 100,
                    "score": 1.0,
                    "base_score": 1.0,
                    "rerank_rank": 0,
                    "rerank_score": 1.0,
                    "retrieval_mode": "fixture",
                }
            ]
        ]
        equivalent = compare_legacy_and_batched_results(
            needs,
            baseline,
            deepcopy(baseline),
        )
        self.assertTrue(equivalent["passed"])
        drifted = deepcopy(baseline)
        drifted[0][0]["text"] = "changed"
        mismatch = compare_legacy_and_batched_results(
            needs,
            baseline,
            drifted,
        )
        self.assertFalse(mismatch["passed"])
        self.assertEqual([0], mismatch["mismatched_need_indices"])

    def test_severe_regression_gate_has_noise_floor_and_fails_large_drift(
        self,
    ) -> None:
        self.assertTrue(
            evaluate_severe_regression(
                legacy_p95_ms=0.1,
                batched_cold_p95_ms=9.9,
            )["passed"]
        )
        self.assertFalse(
            evaluate_severe_regression(
                legacy_p95_ms=20.0,
                batched_cold_p95_ms=41.0,
            )["passed"]
        )

    def test_artifact_directory_is_unique_and_json_refuses_overwrite(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, first_id, _first_started = (
                create_run_artifact_directory(root)
            )
            second, second_id, _second_started = (
                create_run_artifact_directory(root)
            )
            self.assertNotEqual(first, second)
            self.assertNotEqual(first_id, second_id)
            output = first / "result.redacted.json"
            write_json(output, {"status": "passed"})
            with self.assertRaises(FileExistsError):
                write_json(output, {"status": "different"})
            self.assertEqual(
                {"status": "passed"},
                json.loads(output.read_text(encoding="utf-8")),
            )

    def test_artifact_timestamp_is_strict_rfc3339_and_normalized_to_utc(
        self,
    ) -> None:
        invalid_values = (
            "2026-07-17T12:30:40",
            "2026-W29-5T12:30:40Z",
            "2026-07-17 12:30:40Z",
            "2026-07-17T12:30:40z",
            "2026-07-17T12:30:40.1234567Z",
            "2026-07-17T12:30:40+0800",
            "2026-02-30T12:30:40Z",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, value in enumerate(invalid_values):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(
                        ValueError,
                        ARTIFACT_TIMESTAMP_INVALID_CODE,
                    ):
                        create_run_artifact_directory(
                            root / f"invalid-{index}",
                            run_id="a" * 32,
                            started_at_utc=value,
                        )
                    self.assertFalse((root / f"invalid-{index}").exists())
            directory, run_id, started_at = (
                create_run_artifact_directory(
                    root / "valid",
                    run_id="b" * 32,
                    started_at_utc=(
                        "2026-07-17T20:30:40.123456+08:00"
                    ),
                )
            )

        self.assertEqual("b" * 32, run_id)
        self.assertEqual(
            "20260717T123040Z-bbbbbbbbbbbb",
            directory.name,
        )
        self.assertEqual(
            "2026-07-17T12:30:40.123456Z",
            started_at,
        )

    def test_artifact_timestamp_is_host_timezone_independent(self) -> None:
        script = "\n".join(
            (
                "import json, sys",
                (
                    "from benchmarks.v15_performance import "
                    "create_run_artifact_directory"
                ),
                (
                    "path, run_id, started_at = "
                    "create_run_artifact_directory("
                ),
                "    sys.argv[1],",
                "    run_id='c' * 32,",
                (
                    "    started_at_utc="
                    "'2026-07-17T20:30:40+08:00',"
                ),
                ")",
                (
                    "print(json.dumps("
                    "{'name': path.name, 'started_at': started_at}, "
                    "sort_keys=True))"
                ),
            )
        )
        observations = []
        with tempfile.TemporaryDirectory() as temporary:
            for zone in ("UTC", "Asia/Shanghai"):
                environment = os.environ.copy()
                environment["TZ"] = zone
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-X",
                        "utf8",
                        "-c",
                        script,
                        str(Path(temporary) / zone.replace("/", "-")),
                    ],
                    cwd=PLUGIN_ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                    env=environment,
                )
                observations.append(json.loads(completed.stdout))

        self.assertEqual(observations[0], observations[1])
        self.assertEqual(
            {
                "name": "20260717T123040Z-cccccccccccc",
                "started_at": "2026-07-17T12:30:40Z",
            },
            observations[0],
        )

    def test_cli_validate_and_run_write_json_artifacts(self) -> None:
        runner = (
            PLUGIN_ROOT
            / "benchmarks"
            / "run_v15_performance_benchmark.py"
        )
        validate = subprocess.run(
            [
                sys.executable,
                str(runner),
                "validate",
                "--manifest",
                str(DEFAULT_FIXTURE),
            ],
            cwd=PLUGIN_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual("valid", json.loads(validate.stdout)["status"])

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.json"
            redacted = Path(temporary) / "run-manifest.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "run",
                    "--manifest",
                    str(DEFAULT_FIXTURE),
                    "--rerank-delay-ms",
                    "0",
                    "--iterations",
                    "1",
                    "--warmup-iterations",
                    "0",
                    "--output",
                    str(output),
                    "--redacted-manifest-output",
                    str(redacted),
                ],
                cwd=PLUGIN_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            stdout_result = json.loads(completed.stdout)
            file_result = json.loads(output.read_text(encoding="utf-8"))
            run_manifest = json.loads(redacted.read_text(encoding="utf-8"))

        self.assertTrue(stdout_result["passed"])
        self.assertTrue(file_result["passed"])
        self.assertEqual(
            REDACTED_MANIFEST_SCHEMA_VERSION,
            run_manifest["schema_version"],
        )

    def test_cli_default_run_uses_unique_timestamped_directory(self) -> None:
        runner = (
            PLUGIN_ROOT
            / "benchmarks"
            / "run_v15_performance_benchmark.py"
        )
        with tempfile.TemporaryDirectory() as temporary:
            artifact_root = Path(temporary) / "artifacts"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "run",
                    "--manifest",
                    str(DEFAULT_FIXTURE),
                    "--rerank-delay-ms",
                    "0",
                    "--iterations",
                    "1",
                    "--warmup-iterations",
                    "0",
                    "--artifact-root",
                    str(artifact_root),
                ],
                cwd=PLUGIN_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            stdout_result = json.loads(completed.stdout)
            run_directories = list(artifact_root.iterdir())
            self.assertEqual(1, len(run_directories))
            run_directory = run_directories[0]
            self.assertTrue(run_directory.is_dir())
            self.assertTrue(
                (run_directory / "result.redacted.json").is_file()
            )
            self.assertTrue(
                (run_directory / "run-manifest.redacted.json").is_file()
            )
            self.assertEqual(
                run_directory.name.split("-", 1)[1],
                stdout_result["artifact_run"]["run_id"][:12],
            )

    def test_cli_preflights_both_artifacts_before_writing(self) -> None:
        runner = (
            PLUGIN_ROOT
            / "benchmarks"
            / "run_v15_performance_benchmark.py"
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.json"
            redacted = Path(temporary) / "run-manifest.json"
            sentinel = '{"sentinel":true}\n'
            redacted.write_text(sentinel, encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "run",
                    "--manifest",
                    str(DEFAULT_FIXTURE),
                    "--rerank-delay-ms",
                    "0",
                    "--iterations",
                    "1",
                    "--warmup-iterations",
                    "0",
                    "--output",
                    str(output),
                    "--redacted-manifest-output",
                    str(redacted),
                ],
                cwd=PLUGIN_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertFalse(output.exists())
            self.assertEqual(sentinel, redacted.read_text(encoding="utf-8"))
            self.assertIn("pass --overwrite", completed.stderr)

    def test_cli_overwrite_explicitly_replaces_both_artifacts(self) -> None:
        runner = (
            PLUGIN_ROOT
            / "benchmarks"
            / "run_v15_performance_benchmark.py"
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.json"
            redacted = Path(temporary) / "run-manifest.json"
            output.write_text('{"old":"result"}\n', encoding="utf-8")
            redacted.write_text('{"old":"manifest"}\n', encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "run",
                    "--manifest",
                    str(DEFAULT_FIXTURE),
                    "--rerank-delay-ms",
                    "0",
                    "--iterations",
                    "1",
                    "--warmup-iterations",
                    "0",
                    "--output",
                    str(output),
                    "--redacted-manifest-output",
                    str(redacted),
                    "--overwrite",
                ],
                cwd=PLUGIN_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )

            stdout_result = json.loads(completed.stdout)
            file_result = json.loads(output.read_text(encoding="utf-8"))
            run_manifest = json.loads(redacted.read_text(encoding="utf-8"))
            self.assertTrue(stdout_result["passed"])
            self.assertTrue(file_result["passed"])
            self.assertEqual(
                REDACTED_MANIFEST_SCHEMA_VERSION,
                run_manifest["schema_version"],
            )


if __name__ == "__main__":
    unittest.main()
