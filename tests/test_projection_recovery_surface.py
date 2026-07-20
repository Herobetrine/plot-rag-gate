from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from longform import ProjectionJournal, stable_normalized_hash  # noqa: E402
import longform.projections as projections  # noqa: E402
import plot_rag_mcp as mcp  # noqa: E402
import plot_state  # noqa: E402
import v1_runtime as v1  # noqa: E402


def make_project(base: Path) -> Path:
    root = base / "novel"
    (root / ".plot-rag").mkdir(parents=True)
    (root / "正文").mkdir()
    (root / "正文" / "第一章.md").write_text(
        "测试角色甲在测试城南站等待列车。",
        encoding="utf-8",
    )
    config = {
        "config_version": 3,
        "enabled": True,
        "authority_sources": [
            {
                "glob": "正文/**/*.md",
                "role": "canon",
                "scope_policy": "infer_and_review",
                "ingest_policy": "include",
                "priority": 100,
            }
        ],
        "remote": {
            "embedding": {"enabled": False},
            "rerank": {"enabled": False},
            "extract": {"enabled": False},
        },
    }
    (root / ".plot-rag" / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return root


def seed_projection_run(
    root: Path,
    run_id: str,
    *,
    status: str = "running",
    owner: str = "ownerless",
) -> Path:
    database = root / ".plot-rag" / "projection-runs.v1.sqlite3"
    ProjectionJournal(database)
    payload = {
        "commit_id": f"commit-{run_id}",
        "canon_status": "accepted",
        "operation": "accept",
        "events": [],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    values: dict[str, object] = {
        "run_id": run_id,
        "projection_name": "vector",
        "commit_id": payload["commit_id"],
        "input_sha256": stable_normalized_hash(payload),
        "input_json": encoded,
        "status": status,
        "attempt": 1,
        "retry_of": None,
        "output_sha256": None,
        "error_text": (
            "fixture degraded"
            if status == "degraded"
            else "fixture failed"
            if status == "failed"
            else None
        ),
        "started_at": "2026-07-16T00:00:00+00:00",
        "finished_at": (
            None
            if status == "running"
            else "2026-07-16T00:01:00+00:00"
        ),
    }
    if owner == "live":
        values.update(
            {
                "owner_host": socket.gethostname().strip().casefold(),
                "owner_pid": os.getpid(),
                # A legacy row with a live PID but no birth token is
                # deliberately unverifiable and must remain fail-closed.
                "owner_token": None,
            }
        )
    elif owner == "dead":
        values.update(
            {
                "owner_host": socket.gethostname().strip().casefold(),
                "owner_pid": 424242,
                "owner_token": "linux-start:fixture:1",
            }
        )
    with closing(sqlite3.connect(database)) as connection, connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(projection_runs)")
        }
        selected = {
            key: value for key, value in values.items() if key in columns
        }
        names = ", ".join(selected)
        placeholders = ", ".join("?" for _ in selected)
        connection.execute(
            f"INSERT INTO projection_runs({names}) VALUES ({placeholders})",
            tuple(selected.values()),
        )
    return database


def seed_projection_output(database: Path, run_id: str) -> None:
    output = {"status": "success", "projected": True}
    with closing(sqlite3.connect(database)) as connection, connection:
        row = connection.execute(
            """
            SELECT projection_name, commit_id, input_sha256
            FROM projection_runs
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise AssertionError(f"missing fixture projection run: {run_id}")
        connection.execute(
            """
            INSERT INTO projection_outputs(
                projection_name, commit_id, input_sha256,
                output_sha256, output_json, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row[0],
                row[1],
                row[2],
                stable_normalized_hash(output),
                json.dumps(output, sort_keys=True, separators=(",", ":")),
                "2026-07-16T00:02:00+00:00",
            ),
        )


def file_fingerprints(root: Path) -> dict[str, tuple[int, int, str]]:
    result: dict[str, tuple[int, int, str]] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        stat = path.stat()
        result[path.relative_to(root).as_posix()] = (
            stat.st_size,
            stat.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return result


class ProjectionRecoverySurfaceTestCase(unittest.TestCase):
    def test_public_runtime_exports_recovery(self) -> None:
        self.assertIn("recover_longform_projection", v1.__all__)

    def test_ownerless_vector_run_is_retried_once_then_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            database = seed_projection_run(root, "legacy-vector-run")
            projected = {
                "status": "success",
                "projected": True,
                "semantic_ready": True,
                "vector_count": 3,
            }
            with patch.object(
                v1,
                "_project_longform_vectors",
                return_value=projected,
            ) as projector:
                recovered = v1.recover_longform_projection(
                    root,
                    "legacy-vector-run",
                )
                cached = v1.recover_longform_projection(
                    root,
                    "legacy-vector-run",
                )

            self.assertEqual("succeeded", recovered["status"])
            self.assertEqual("legacy-vector-run", recovered["retry_of"])
            self.assertEqual(2, recovered["attempt"])
            self.assertIsNotNone(recovered["recovered_interrupted_run"])
            self.assertEqual("cached", cached["status"])
            self.assertEqual(recovered["run_id"], cached["run_id"])
            projector.assert_called_once()

            journal = ProjectionJournal(database)
            rows = journal.runs("vector")
            self.assertEqual(2, len(rows))
            source = next(
                row for row in rows if row["run_id"] == "legacy-vector-run"
            )
            retry = next(
                row for row in rows if row["run_id"] == recovered["run_id"]
            )
            self.assertEqual("failed", source["status"])
            self.assertEqual("succeeded", retry["status"])
            self.assertEqual("legacy-vector-run", retry["retry_of"])
            health = v1.doctor_v1(root)["components"]["longform_projection"]
            self.assertEqual("ok", health["status"])
            self.assertEqual(1, health["counts"]["failed_runs"])
            self.assertEqual(0, health["counts"]["unresolved_failed_runs"])

    def test_recovery_does_not_treat_pre_failure_output_as_completed_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            database = (
                root / ".plot-rag" / "projection-runs.v1.sqlite3"
            )
            journal = ProjectionJournal(database)
            commit = {
                "commit_id": "commit-stale-recovery-output",
                "canon_status": "accepted",
                "operation": "accept",
                "events": [],
            }
            journal.run(
                "vector",
                commit,
                lambda _payload: {"generation": 1},
            )
            degraded = journal.run(
                "vector",
                commit,
                lambda _payload: {
                    "status": "degraded",
                    "generation": 2,
                },
                force=True,
            )

            with patch.object(
                v1,
                "_project_longform_vectors",
                return_value={
                    "status": "success",
                    "generation": 3,
                },
            ) as projector:
                recovered = v1.recover_longform_projection(
                    root,
                    degraded["run_id"],
                )

            self.assertEqual("succeeded", recovered["status"])
            self.assertEqual(degraded["run_id"], recovered["retry_of"])
            projector.assert_called_once()

    def test_concurrent_recovery_waits_for_single_retry_then_returns_cached(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            database = seed_projection_run(
                root,
                "concurrent-recovery-run",
            )
            start = threading.Barrier(3)
            projector_entered = threading.Event()
            release_projector = threading.Event()
            call_lock = threading.Lock()
            projector_calls = 0
            results: list[dict[str, object]] = []
            errors: list[BaseException] = []

            def projector(_root: Path, _payload: object) -> dict[str, object]:
                nonlocal projector_calls
                with call_lock:
                    projector_calls += 1
                projector_entered.set()
                if not release_projector.wait(timeout=5):
                    raise TimeoutError("fixture projector was not released")
                return {
                    "status": "success",
                    "projected": True,
                    "semantic_ready": True,
                    "vector_count": 3,
                }

            def recover() -> None:
                try:
                    start.wait(timeout=5)
                    results.append(
                        v1.recover_longform_projection(
                            root,
                            "concurrent-recovery-run",
                        )
                    )
                except BaseException as error:
                    errors.append(error)

            with patch.object(
                v1,
                "_project_longform_vectors",
                side_effect=projector,
            ):
                threads = [
                    threading.Thread(target=recover)
                    for _ in range(2)
                ]
                for thread in threads:
                    thread.start()
                start.wait(timeout=5)
                self.assertTrue(projector_entered.wait(timeout=5))
                time.sleep(0.10)
                with call_lock:
                    self.assertEqual(1, projector_calls)
                release_projector.set()
                for thread in threads:
                    thread.join(timeout=10)
                    self.assertFalse(thread.is_alive())

            self.assertEqual([], errors)
            self.assertEqual(
                ["cached", "succeeded"],
                sorted(str(item["status"]) for item in results),
            )
            self.assertEqual(
                1,
                len({str(item["run_id"]) for item in results}),
            )
            rows = ProjectionJournal(database).runs("vector")
            retries = [
                row
                for row in rows
                if row["retry_of"] == "concurrent-recovery-run"
            ]
            self.assertEqual(1, len(retries))
            self.assertEqual(2, int(retries[0]["attempt"]))

    def test_dead_owner_recovery_receipt_is_returned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            seed_projection_run(
                root,
                "dead-owner-vector-run",
                owner="dead",
            )
            projected = {
                "status": "success",
                "projected": True,
                "semantic_ready": True,
                "vector_count": 3,
            }
            with (
                patch.object(
                    projections,
                    "_process_probe",
                    return_value=("dead", None),
                ),
                patch.object(
                    v1,
                    "_project_longform_vectors",
                    return_value=projected,
                ),
            ):
                recovered = v1.recover_longform_projection(
                    root,
                    "dead-owner-vector-run",
                )

            receipt = recovered["recovered_interrupted_run"]
            self.assertIsNotNone(receipt)
            self.assertEqual("dead-owner-vector-run", receipt["run_id"])
            self.assertEqual("failed", receipt["status"])
            self.assertIn("owner process", receipt["reason"])

    def test_concurrent_constructor_recovery_preserves_exact_receipt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            database = seed_projection_run(
                root,
                "concurrently-recovered-vector-run",
                owner="dead",
            )
            source_inspected = threading.Event()
            competing_recovery_done = threading.Event()
            first_inspection_lock = threading.Lock()
            first_inspection = True
            competitor_errors: list[BaseException] = []
            original_inspect = ProjectionJournal.inspect_run

            def inspect_then_wait_for_competitor(
                journal: ProjectionJournal,
                run_id: str,
                *,
                include_payload: bool = False,
            ) -> dict[str, object]:
                nonlocal first_inspection
                result = original_inspect(
                    journal,
                    run_id,
                    include_payload=include_payload,
                )
                with first_inspection_lock:
                    should_wait = first_inspection
                    first_inspection = False
                if should_wait:
                    source_inspected.set()
                    if not competing_recovery_done.wait(timeout=5):
                        raise TimeoutError(
                            "competing constructor did not finish recovery"
                        )
                return result

            def recover_from_competing_constructor() -> None:
                try:
                    if not source_inspected.wait(timeout=5):
                        raise TimeoutError(
                            "runtime did not inspect the source run"
                        )
                    ProjectionJournal(database)
                except BaseException as error:
                    competitor_errors.append(error)
                finally:
                    competing_recovery_done.set()

            competitor = threading.Thread(
                target=recover_from_competing_constructor
            )
            competitor.start()
            with (
                patch.object(
                    projections,
                    "_process_probe",
                    return_value=("dead", None),
                ),
                patch.object(
                    ProjectionJournal,
                    "inspect_run",
                    new=inspect_then_wait_for_competitor,
                ),
                patch.object(
                    v1,
                    "_project_longform_vectors",
                    return_value={
                        "status": "success",
                        "projected": True,
                    },
                ),
            ):
                recovered = v1.recover_longform_projection(
                    root,
                    "concurrently-recovered-vector-run",
                )
            competitor.join(timeout=10)

            self.assertFalse(competitor.is_alive())
            self.assertEqual([], competitor_errors)
            receipt = recovered["recovered_interrupted_run"]
            self.assertIsNotNone(receipt)
            self.assertEqual(
                "concurrently-recovered-vector-run",
                receipt["run_id"],
            )
            self.assertEqual("failed", receipt["status"])
            self.assertIn("owner process", receipt["reason"])
            retries = [
                row
                for row in ProjectionJournal(
                    database,
                    auto_recover=False,
                ).runs("vector")
                if row["retry_of"]
                == "concurrently-recovered-vector-run"
            ]
            self.assertEqual(1, len(retries))

    def test_explicit_recovery_does_not_touch_unrelated_dead_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            database = seed_projection_run(
                root,
                "unrelated-dead-vector-run",
                owner="live",
            )
            seed_projection_run(
                root,
                "target-dead-vector-run",
                owner="live",
            )
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    """
                    UPDATE projection_runs
                    SET started_at='2026-07-16T00:00:00+00:00',
                        owner_pid=424242,
                        owner_token='linux-start:fixture:1'
                    WHERE run_id='unrelated-dead-vector-run'
                    """
                )
                connection.execute(
                    """
                    UPDATE projection_runs
                    SET started_at='2026-07-16T00:01:00+00:00',
                        owner_pid=424242,
                        owner_token='linux-start:fixture:1'
                    WHERE run_id='target-dead-vector-run'
                    """
                )

            with (
                patch.object(
                    projections,
                    "_process_probe",
                    return_value=("dead", None),
                ),
                patch.object(
                    v1,
                    "_project_longform_vectors",
                    return_value={
                        "status": "success",
                        "projected": True,
                    },
                ),
            ):
                recovered = v1.recover_longform_projection(
                    root,
                    "target-dead-vector-run",
                )

            receipt = recovered["recovered_interrupted_run"]
            self.assertIsNotNone(receipt)
            self.assertEqual("target-dead-vector-run", receipt["run_id"])
            rows = {
                row["run_id"]: row
                for row in ProjectionJournal(
                    database,
                    auto_recover=False,
                ).runs("vector")
            }
            self.assertEqual(
                "running",
                rows["unrelated-dead-vector-run"]["status"],
            )
            self.assertEqual(
                "failed",
                rows["target-dead-vector-run"]["status"],
            )

    def test_recovery_uses_targeted_run_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            database = seed_projection_run(
                root,
                "targeted-failed-run",
                status="failed",
            )
            seed_projection_run(
                root,
                "unrelated-large-run",
                status="failed",
            )
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    """
                    UPDATE projection_runs
                    SET input_json=?
                    WHERE run_id='unrelated-large-run'
                    """,
                    (
                        json.dumps(
                            {"blob": "x" * 2_000_000},
                            separators=(",", ":"),
                        ),
                    ),
                )
            with (
                patch.object(
                    ProjectionJournal,
                    "runs",
                    side_effect=AssertionError(
                        "recovery must not scan projection history"
                    ),
                ),
                patch.object(
                    v1,
                    "_project_longform_vectors",
                    return_value={
                        "status": "success",
                        "projected": True,
                    },
                ),
            ):
                recovered = v1.recover_longform_projection(
                    root,
                    "targeted-failed-run",
                )
                cached = v1.recover_longform_projection(
                    root,
                    "targeted-failed-run",
                )

            self.assertEqual("succeeded", recovered["status"])
            self.assertEqual("cached", cached["status"])
            self.assertEqual(recovered["run_id"], cached["run_id"])

    def test_live_or_unverifiable_owner_is_not_taken_over(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            seed_projection_run(root, "live-vector-run", owner="live")
            with (
                patch.object(v1, "_project_longform_vectors") as projector,
                self.assertRaisesRegex(
                    ValueError,
                    "live or unverifiable",
                ),
            ):
                v1.recover_longform_projection(root, "live-vector-run")
            projector.assert_not_called()

    def test_cli_and_mcp_expose_mutating_recovery_dispatch(self) -> None:
        tool = next(
            item
            for item in mcp.TOOLS
            if item["name"] == "recover_longform_projection"
        )
        self.assertNotIn("annotations", tool)
        self.assertEqual(
            {"project_root", "run_id"},
            set(tool["inputSchema"]["required"]),
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            expected = {"status": "cached", "run_id": "retry-run"}
            with patch.object(
                v1,
                "recover_longform_projection",
                return_value=expected,
            ) as recover:
                mcp_result = mcp._dispatch_tool(
                    "recover_longform_projection",
                    {
                        "project_root": str(root),
                        "run_id": "stale-run",
                    },
                )
                args = plot_state._parser().parse_args(
                    [
                        "longform",
                        "recover",
                        "--project-root",
                        str(root),
                        "--run-id",
                        "stale-run",
                    ]
                )
                cli_result = plot_state._dispatch(args)

            self.assertEqual(expected, mcp_result)
            self.assertEqual(expected, cli_result)
            self.assertEqual(2, recover.call_count)
            for call in recover.call_args_list:
                self.assertEqual(root.resolve(), call.args[0])
                self.assertEqual("stale-run", call.args[1])

    def test_doctor_reports_projection_run_health_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            seed_projection_run(root, "running-run")
            seed_projection_run(root, "degraded-run", status="degraded")
            seed_projection_run(root, "failed-run", status="failed")
            database = seed_projection_run(
                root,
                "resolved-failed-run",
                status="failed",
            )
            seed_projection_output(database, "resolved-failed-run")
            before = file_fingerprints(root)

            report = v1.doctor_v1(root)

            after = file_fingerprints(root)
            component = report["components"]["longform_projection"]
            self.assertEqual(before, after)
            self.assertTrue(report["zero_write"])
            self.assertEqual("degraded", report["status"])
            self.assertEqual("degraded", component["status"])
            self.assertEqual(1, component["counts"]["running_runs"])
            self.assertEqual(1, component["counts"]["degraded_runs"])
            self.assertEqual(2, component["counts"]["failed_runs"])
            self.assertEqual(
                1,
                component["counts"]["unresolved_degraded_runs"],
            )
            self.assertEqual(
                1,
                component["counts"]["unresolved_failed_runs"],
            )
            self.assertIn("longform_projection", report["degraded_checks"])

    def test_doctor_accepts_completed_lexical_only_degradation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            journal = ProjectionJournal(
                root / ".plot-rag" / "projection-runs.v1.sqlite3"
            )
            result = journal.run(
                "vector",
                {
                    "commit_id": "lexical-only-commit",
                    "canon_status": "accepted",
                    "operation": "accept",
                    "events": [],
                },
                lambda _payload: {
                    "status": "degraded",
                    "projected": False,
                    "lexical_ready": True,
                    "semantic_ready": False,
                    "embedding_enabled": False,
                },
            )
            self.assertEqual("degraded", result["status"])

            report = v1.doctor_v1(root)
            component = report["components"]["longform_projection"]
            self.assertEqual("ok", component["status"])
            self.assertEqual(1, component["counts"]["degraded_runs"])
            self.assertEqual(
                0,
                component["counts"]["unresolved_degraded_runs"],
            )


if __name__ == "__main__":
    unittest.main()
