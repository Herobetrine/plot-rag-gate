from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import performance_runtime as runtime  # noqa: E402


class PerformanceRuntimeTests(unittest.TestCase):
    def _project(self, parent: str) -> Path:
        root = Path(parent) / "novel"
        state_dir = root / ".plot-rag"
        state_dir.mkdir(parents=True)
        (state_dir / "config.json").write_text(
            json.dumps(
                {
                    "config_version": 3,
                    "state": {
                        "db_path": ".plot-rag/state.sqlite3",
                        "snapshot_path": ".plot-rag/state_snapshot.json",
                        "commit_dir": ".plot-rag/commits",
                    },
                    "performance": {
                        "prepare_v2": {
                            "enabled": True,
                            "shadow": True,
                            "rerank_max_concurrency": 4,
                        },
                        "extraction": {
                            "mode": "async_strict",
                            "next_plot_turn_barrier": True,
                        },
                    },
                    "remote": {
                        "extract": {
                            "api_key": "runtime-secret-test-value-123456789",
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        database = state_dir / "state.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            with connection:
                connection.executescript(
                    """
                CREATE TABLE turns(
                    receipt_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    authority_json TEXT NOT NULL,
                    remote_json TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE TABLE extraction_jobs(
                    job_id TEXT PRIMARY KEY,
                    job_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    attempt_count INTEGER NOT NULL,
                    remote_status TEXT NOT NULL
                );
                """
                )
                connection.executemany(
                    """
                INSERT INTO turns VALUES(?,?,?,?,?,?,?)
                """,
                    [
                    (
                        "r1",
                        "committed",
                        "2026-07-17T00:00:00+00:00",
                        "2026-07-17T00:00:01+00:00",
                        json.dumps(
                            {
                                "prepare": {
                                    "new_query_ms": 1000,
                                    "candidate_cache_hits": 0,
                                    "candidate_cache_misses": 1,
                                }
                            }
                        ),
                        json.dumps(
                            {
                                "embedding": {
                                    "status": "ok",
                                    "latency_ms": 100,
                                    "api_key": "runtime-secret-test-value-123456789",
                                    "host": "api.example.invalid",
                                }
                            }
                        ),
                        "{}",
                    ),
                    (
                        "r2",
                        "pending",
                        "2026-07-17T00:00:02+00:00",
                        None,
                        json.dumps(
                            {
                                "prepare": {
                                    "cache_hit_ms": 200,
                                    "candidate_cache_hits": 1,
                                }
                            }
                        ),
                        json.dumps(
                            {
                                "rerank": {
                                    "status": "failed",
                                    "latency_ms": 300,
                                }
                            }
                        ),
                        "{}",
                    ),
                    ],
                )
                connection.executemany(
                    "INSERT INTO extraction_jobs VALUES(?,?,?,?,?,?,?)",
                    [
                    (
                        "j1",
                        "succeeded",
                        "2026-07-17T00:00:00+00:00",
                        "2026-07-17T00:00:00.100000+00:00",
                        "2026-07-17T00:00:00.800000+00:00",
                        1,
                        "ok",
                    ),
                    (
                        "j2",
                        "failed",
                        "2026-07-17T00:00:02+00:00",
                        "2026-07-17T00:00:02.200000+00:00",
                        "2026-07-17T00:00:03+00:00",
                        2,
                        "failed",
                    ),
                    ],
                )
        authority = state_dir / "authority.v1.sqlite3"
        with closing(sqlite3.connect(authority)) as connection:
            with connection:
                connection.execute(
                    "CREATE TABLE rerank_candidate_cache(cache_key TEXT)"
                )
                connection.executemany(
                    "INSERT INTO rerank_candidate_cache VALUES(?)",
                    [("a",), ("b",)],
                )
        return root

    def test_get_status_is_read_only_aggregated_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._project(temporary)
            state = root / ".plot-rag" / "state.sqlite3"
            before = state.read_bytes()

            result = runtime.get_status(root)

            self.assertEqual("ready", result["status"])
            self.assertTrue(result["read_only"])
            self.assertTrue(result["canon_guard"]["unchanged"])
            self.assertEqual(before, state.read_bytes())
            self.assertEqual(
                600.0,
                result["telemetry"]["prepare"]["all"]["p50_ms"],
            )
            self.assertEqual(
                800.0,
                result["telemetry"]["extraction"]["ready"]["p50_ms"],
            )
            self.assertEqual(2, result["telemetry"]["cache"]["entries"])
            self.assertEqual(2, result["telemetry"]["remote"]["failures"])
            serialized = json.dumps(result, ensure_ascii=False).casefold()
            self.assertNotIn("runtime-secret-test-value", serialized)
            self.assertNotIn("api_key", serialized)
            self.assertNotIn(str(root).casefold(), serialized)
            self.assertNotIn("api.example.invalid", serialized)

    def test_run_benchmark_uses_python_api_and_preserves_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._project(temporary)
            sentinel = root / "正文" / "001.md"
            sentinel.parent.mkdir()
            sentinel.write_text("正典原文保持原样", encoding="utf-8")
            before = sentinel.read_bytes()

            result = runtime.run_benchmark(
                root,
                options={
                    "iterations": 1,
                    "rerank_delay_ms": 0,
                    "telemetry": {
                        "extraction": {
                            "enqueue_ms": [100, 300],
                            "barrier_wait_ms": [20, 40],
                        },
                        "remote": {
                            "extract": {
                                "calls": 2,
                                "failures": 0,
                                "latencies_ms": [10, 30],
                                "api_key": "extra-secret-test-value-123456",
                            }
                        },
                    },
                },
            )

            self.assertTrue(result["passed"])
            self.assertTrue(result["canon_guard"]["unchanged"])
            self.assertEqual(before, sentinel.read_bytes())
            self.assertEqual(
                200.0,
                result["telemetry"]["extraction"]["enqueue"]["p50_ms"],
            )
            self.assertGreater(
                result["telemetry"]["prepare"]["new_query"]["count"],
                0,
            )
            self.assertGreater(result["telemetry"]["cache"]["hits"], 0)
            self.assertGreater(result["telemetry"]["remote"]["calls"], 0)
            serialized = json.dumps(result, ensure_ascii=False).casefold()
            self.assertNotIn("extra-secret-test-value", serialized)
            self.assertNotIn(str(root).casefold(), serialized)
            self.assertNotIn("api_key", serialized)

    def test_path_request_and_wrapper_request_are_supported(self) -> None:
        fixture = (
            PLUGIN_ROOT
            / "benchmarks"
            / "fixtures"
            / "v15_performance_manifest.v1.json"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plain-project"
            root.mkdir()
            direct = runtime.run_benchmark(
                root,
                fixture,
                {"rerank_delay_ms": 0},
            )
            wrapped = runtime.run_benchmark(
                root,
                {
                    "path": str(fixture),
                    "options": {"rerank_delay_ms": 0},
                },
            )
        self.assertTrue(direct["passed"])
        self.assertTrue(wrapped["passed"])
        self.assertEqual(
            direct["manifest_validation"]["fixture_sha256"],
            wrapped["manifest_validation"]["fixture_sha256"],
        )

    def test_compare_reports_calculates_deltas_and_trends(self) -> None:
        left = {
            "schema_version": runtime.REPORT_SCHEMA_VERSION,
            "passed": True,
            "telemetry": {
                "prepare": {
                    "all": {"p50_ms": 1000, "p95_ms": 2000}
                },
                "cache": {"hit_rate": 0.5, "hits": 5},
                "remote": {"failures": 1},
            },
        }
        right = {
            "schema_version": runtime.REPORT_SCHEMA_VERSION,
            "passed": True,
            "telemetry": {
                "prepare": {
                    "all": {"p50_ms": 500, "p95_ms": 1000}
                },
                "cache": {"hit_rate": 0.75, "hits": 9},
                "remote": {"failures": 2},
            },
        }

        result = runtime.compare_reports(left, right)

        self.assertEqual("compared", result["status"])
        self.assertEqual(
            -1000,
            result["changes"]["prepare.all.p95_ms"]["delta"],
        )
        self.assertEqual(
            "improved",
            result["changes"]["prepare.all.p95_ms"]["direction"],
        )
        self.assertEqual(
            "improved",
            result["changes"]["cache.hit_rate"]["direction"],
        )
        self.assertEqual(
            "regressed",
            result["changes"]["remote.failures"]["direction"],
        )

    def test_percentiles_use_linear_interpolation(self) -> None:
        summary = runtime._latency_summary([1, 2, 3, 4])
        self.assertEqual(2.5, summary["p50_ms"])
        self.assertEqual(3.85, summary["p95_ms"])


if __name__ == "__main__":
    unittest.main()
