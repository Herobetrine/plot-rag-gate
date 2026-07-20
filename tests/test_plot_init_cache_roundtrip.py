from __future__ import annotations

import copy
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from plot_init import (  # noqa: E402
    MemoryRemoteResponseCache,
    PlotInitError,
    PlotInitService,
    SQLiteRemoteResponseCache,
    export_normalized_bundle,
    normalization_diff,
    normalized_hash,
    render_normalized_bundle,
)
from plot_init.normalized import recompute_bundle_hash  # noqa: E402
from tests.test_plot_init import complete_seed, file_fingerprints  # noqa: E402


class PlotInitRemoteCacheTestCase(unittest.TestCase):
    def test_cache_bounds_require_exact_positive_integers(self) -> None:
        cache_types = (MemoryRemoteResponseCache, SQLiteRemoteResponseCache)
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "remote-cache.sqlite3"
            for cache_type in cache_types:
                for field in ("max_entries", "max_age_seconds"):
                    for value in (True, 1.5, "2", 0, -1):
                        with self.subTest(
                            cache=cache_type.__name__,
                            field=field,
                            value=repr(value),
                        ):
                            keyword_arguments = {field: value}
                            if cache_type is SQLiteRemoteResponseCache:
                                with self.assertRaises(PlotInitError):
                                    cache_type(database, **keyword_arguments)
                            else:
                                with self.assertRaises(PlotInitError):
                                    cache_type(**keyword_arguments)

            memory = MemoryRemoteResponseCache()
            sqlite = SQLiteRemoteResponseCache(database)
            for cache in (memory, sqlite):
                for field in ("max_entries", "max_age_seconds"):
                    for value in (True, 1.5, "2", 0, -1):
                        with self.subTest(
                            cache=type(cache).__name__,
                            prune_field=field,
                            value=repr(value),
                        ):
                            with self.assertRaises(PlotInitError):
                                cache.prune(**{field: value})

    def test_dry_run_binds_process_memory_cache_without_creating_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            before = file_fingerprints(workspace)
            result = PlotInitService(workspace).dry_run(
                project_root=workspace / "novel",
                mode="new",
                seed=complete_seed(),
            )
            after = file_fingerprints(workspace)

            self.assertEqual(before, after)
            self.assertFalse((workspace / ".plot-rag-init").exists())
            binding = result["remote_cache_binding"]
            self.assertEqual("memory", binding["storage_mode"])
            self.assertFalse(binding["persistent"])
            self.assertEqual(
                ["model", "prompt_hash", "schema_hash", "source_hash"],
                binding["key_fields"],
            )

    def test_persistent_cache_hits_redacts_secrets_invalidates_and_prunes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            service = PlotInitService(
                workspace,
                remote_cache_max_entries=2,
                remote_cache_max_age_seconds=3600,
            )
            started = service.start(
                project_root=workspace / "novel",
                mode="new",
                seed=complete_seed(),
                idempotency_key="cache-start",
            )
            cache = service.remote_cache_for_session(
                started["session_id"],
                stage="EXTRACT",
            )
            secret = "-".join(
                ("sk", "TESTONLY0123456789abcdefghijklmnopqrstuvwxyz")
            )
            calls: list[str] = []

            first = cache.resolve(
                model="Qwen/Qwen3-30B-A3B-Instruct-2507",
                prompt={"task": "extract", "text": "测试角色甲位于测试城"},
                schema={"type": "object", "required": ["claims"]},
                source_hash="a" * 64,
                loader=lambda: calls.append("first")
                or {
                    "claims": [{"subject": "测试角色甲", "location": "测试城"}],
                    "api_key": secret,
                    "model_echo": f"credential echo {secret}",
                },
            )
            replay = cache.resolve(
                model="Qwen/Qwen3-30B-A3B-Instruct-2507",
                prompt={"task": "extract", "text": "测试角色甲位于测试城"},
                schema={"type": "object", "required": ["claims"]},
                source_hash="a" * 64,
                loader=lambda: calls.append("unexpected") or {},
            )

            self.assertFalse(first["cache_hit"])
            self.assertTrue(replay["cache_hit"])
            self.assertEqual(["first"], calls)
            self.assertNotIn("api_key", first["response"])
            self.assertNotIn(secret, json.dumps(first["response"], ensure_ascii=False))
            self.assertEqual(first["cache_key"], replay["cache_key"])
            with closing(sqlite3.connect(service.database_path)) as connection:
                connection.row_factory = sqlite3.Row
                persisted = connection.execute(
                    """
                    SELECT model, prompt_hash, schema_hash, source_hash,
                           response_json, hit_count
                    FROM initialization_remote_response_cache
                    WHERE cache_key=?
                    """,
                    (first["cache_key"],),
                ).fetchone()
            self.assertIsNotNone(persisted)
            self.assertEqual(first["model"], persisted["model"])
            self.assertEqual(first["prompt_hash"], persisted["prompt_hash"])
            self.assertEqual(first["schema_hash"], persisted["schema_hash"])
            self.assertEqual(first["source_hash"], persisted["source_hash"])
            self.assertEqual(1, persisted["hit_count"])
            self.assertNotIn(secret, persisted["response_json"])

            second = cache.resolve(
                model="Qwen/Qwen3-30B-A3B-Instruct-2507",
                prompt="same prompt",
                schema={"type": "array"},
                source_hash="b" * 64,
                loader=lambda: {"value": 2},
            )
            third = cache.resolve(
                model="Qwen/Qwen3-30B-A3B-Instruct-2507",
                prompt="third prompt",
                schema={"type": "object"},
                source_hash="c" * 64,
                loader=lambda: {"value": 3},
            )
            self.assertNotEqual(second["cache_key"], third["cache_key"])

            with closing(sqlite3.connect(service.database_path)) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT cache_key, model, prompt_hash, schema_hash, source_hash,
                           response_json, hit_count
                    FROM initialization_remote_response_cache
                    ORDER BY accessed_at DESC
                    """
                ).fetchall()
            self.assertEqual(2, len(rows))
            self.assertTrue(all(len(row["prompt_hash"]) == 64 for row in rows))
            self.assertTrue(all(len(row["schema_hash"]) == 64 for row in rows))
            self.assertTrue(all(len(row["source_hash"]) == 64 for row in rows))
            self.assertNotIn(
                secret,
                "\n".join(str(row["response_json"]) for row in rows),
            )

            removed = cache.invalidate(source_hash=third["source_hash"])
            self.assertEqual(1, removed)
            missed = cache.resolve(
                model="Qwen/Qwen3-30B-A3B-Instruct-2507",
                prompt="third prompt",
                schema={"type": "object"},
                source_hash="c" * 64,
                loader=lambda: {"value": "reloaded"},
            )
            self.assertFalse(missed["cache_hit"])
            self.assertEqual("reloaded", missed["response"]["value"])

            with self.assertRaises(PlotInitError) as invalid_stage:
                service.remote_cache_for_session(
                    started["session_id"],
                    stage="APPLY",
                )
            self.assertEqual(
                "INVALID_REMOTE_CACHE_STAGE",
                invalid_stage.exception.code,
            )

    def test_sqlite_single_flight_prevents_concurrent_loader_stampede(self) -> None:
        workers = 12
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "remote-cache.sqlite3"
            caches = [
                SQLiteRemoteResponseCache(database_path)
                for _ in range(workers)
            ]
            start = threading.Barrier(workers)
            counter_lock = threading.Lock()
            loader_calls = 0

            def loader() -> dict[str, object]:
                nonlocal loader_calls
                with counter_lock:
                    loader_calls += 1
                time.sleep(0.10)
                return {"claims": [{"subject": "测试角色甲"}]}

            def resolve(index: int) -> dict[str, object]:
                start.wait(timeout=5)
                return caches[index].resolve(
                    model="mock-single-flight",
                    prompt={"task": "extract", "text": "测试角色甲"},
                    schema={"type": "object"},
                    source_hash="d" * 64,
                    loader=loader,
                )

            with ThreadPoolExecutor(max_workers=workers) as executor:
                results = list(executor.map(resolve, range(workers)))

        self.assertEqual(1, loader_calls)
        self.assertEqual(1, sum(not item["cache_hit"] for item in results))
        self.assertEqual(
            workers - 1,
            sum(bool(item["cache_hit"]) for item in results),
        )
        self.assertTrue(
            all(
                item["response"] == {"claims": [{"subject": "测试角色甲"}]}
                for item in results
            )
        )

    def test_failed_memory_single_flight_is_shared_and_later_retry_succeeds(
        self,
    ) -> None:
        workers = 8
        root = MemoryRemoteResponseCache()
        caches = [
            root.bind(session_id=f"session-{index}", stage="EXTRACT")
            for index in range(workers)
        ]
        start = threading.Barrier(workers)
        counter_lock = threading.Lock()
        loader_calls = 0

        def failing_loader() -> dict[str, object]:
            nonlocal loader_calls
            with counter_lock:
                loader_calls += 1
            time.sleep(0.10)
            raise RuntimeError("synthetic provider failure")

        def resolve_failure(index: int) -> str:
            start.wait(timeout=5)
            with self.assertRaisesRegex(
                RuntimeError,
                "synthetic provider failure",
            ):
                caches[index].resolve(
                    model="mock-single-flight-error",
                    prompt="same failure",
                    schema={"type": "object"},
                    source_hash="e" * 64,
                    loader=failing_loader,
                )
            return "failed"

        with ThreadPoolExecutor(max_workers=workers) as executor:
            outcomes = list(executor.map(resolve_failure, range(workers)))

        self.assertEqual(["failed"] * workers, outcomes)
        self.assertEqual(1, loader_calls)
        retried = root.resolve(
            model="mock-single-flight-error",
            prompt="same failure",
            schema={"type": "object"},
            source_hash="e" * 64,
            loader=lambda: {"status": "recovered"},
        )
        self.assertFalse(retried["cache_hit"])
        self.assertEqual({"status": "recovered"}, retried["response"])


class PlotInitNormalizeRoundTripTestCase(unittest.TestCase):
    @staticmethod
    def _status_fixture(bundle: dict[str, object]) -> dict[str, object]:
        value = copy.deepcopy(bundle)
        field_states = value["field_states"]
        assert isinstance(field_states, dict)
        unknown_paths = [
            path
            for path, state in field_states.items()
            if isinstance(state, dict) and state.get("field_status") == "unknown"
        ]
        deferred_path, conflicted_path = unknown_paths[:2]
        field_states[deferred_path]["field_status"] = "deferred"
        field_states[conflicted_path]["field_status"] = "conflicted"

        conflicts = value["conflicts"]
        assert isinstance(conflicts, list)
        conflicts.append(
            {
                "conflict_id": "conflict-roundtrip-fixture",
                "type": "semantic_contradiction",
                "status": "open",
                "field_paths": [conflicted_path],
                "resolution": None,
            }
        )
        provenance = value["provenance"]
        assert isinstance(provenance, dict)
        provenance["roundtrip_fixture"] = {
            "deferred_path": deferred_path,
            "conflicted_path": conflicted_path,
            "owner": value["source_ownership"][conflicted_path],
        }
        validation = value["validation"]
        assert isinstance(validation, dict)
        validation["normalization_hash"] = normalized_hash(value)
        value["bundle_hash"] = recompute_bundle_hash(value)
        return value

    def test_export_ingest_normalize_is_lossless_and_hash_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            service = PlotInitService(workspace)
            original_result = service.dry_run(
                project_root=project,
                mode="new",
                target_profile="plot_ready",
                seed=complete_seed(),
            )
            original = self._status_fixture(original_result["bundle"])
            envelope = export_normalized_bundle(original)
            export_path = workspace / "normalized-export.json"
            export_path.write_text(
                json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )

            imported_result = service.dry_run(
                project_root=project,
                mode="ingest",
                target_profile="plot_ready",
                sources=[export_path],
            )
            imported = imported_result["bundle"]
            roundtrip = imported_result["normalization_roundtrip"]

            self.assertEqual(original["bundle_hash"], imported["bundle_hash"])
            self.assertEqual(normalized_hash(original), normalized_hash(imported))
            self.assertEqual([], normalization_diff(original, imported))
            self.assertTrue(roundtrip["zero_diff"])
            self.assertTrue(roundtrip["stable_hash"])
            self.assertTrue(roundtrip["bundle_hash_stable"])
            self.assertEqual(original["field_states"], imported["field_states"])
            self.assertEqual(
                original["source_ownership"],
                imported["source_ownership"],
            )
            self.assertEqual(original["conflicts"], imported["conflicts"])
            self.assertEqual(original["provenance"], imported["provenance"])
            self.assertTrue(
                any(
                    state["field_status"] == "unknown"
                    for state in imported["field_states"].values()
                )
            )
            self.assertTrue(
                any(
                    state["field_status"] == "deferred"
                    for state in imported["field_states"].values()
                )
            )
            self.assertTrue(
                any(
                    state["field_status"] == "conflicted"
                    for state in imported["field_states"].values()
                )
            )

            second_export = workspace / "normalized-export-2.json"
            second_export.write_text(
                render_normalized_bundle(imported),
                encoding="utf-8",
            )
            second_import = service.dry_run(
                project_root=project,
                mode="ingest",
                target_profile="plot_ready",
                sources=[second_export],
            )
            self.assertEqual(
                original["bundle_hash"],
                second_import["bundle"]["bundle_hash"],
            )
            self.assertEqual(
                [],
                normalization_diff(original, second_import["bundle"]),
            )

    def test_tampered_normalized_export_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            service = PlotInitService(workspace)
            result = service.dry_run(
                project_root=project,
                mode="new",
                seed=complete_seed(),
            )
            envelope = export_normalized_bundle(result["bundle"])
            envelope["initialization_bundle"]["story_engine"][
                "actionable_goal"
            ] = "tampered"
            path = workspace / "tampered-normalized.json"
            path.write_text(
                json.dumps(envelope, ensure_ascii=False),
                encoding="utf-8",
            )

            with self.assertRaises(PlotInitError) as mismatch:
                service.dry_run(
                    project_root=project,
                    mode="ingest",
                    sources=[path],
                )
            self.assertEqual(
                "NORMALIZED_EXPORT_BUNDLE_HASH_MISMATCH",
                mismatch.exception.code,
            )


if __name__ == "__main__":
    unittest.main()
