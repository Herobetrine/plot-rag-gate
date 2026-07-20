from __future__ import annotations

import gc
import tempfile
import threading
import time
import unittest
import uuid
import weakref
from pathlib import Path
from typing import Any, Callable

import scripts.longform.authority as authority_module
from scripts.longform import (
    AuthorityIndex,
    AuthorityIndexError,
    AuthoritySource,
    ContextContractBuilder,
)


class RerankCacheTests(unittest.TestCase):
    @staticmethod
    def _wait_until(
        predicate: Callable[[], bool],
        *,
        timeout: float = 2.0,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()

    @staticmethod
    def _canon_sources() -> list[AuthoritySource]:
        return [
            AuthoritySource(
                glob="正文/**/*.md",
                role="canon",
                priority=100,
                scope_policy="infer_and_review",
                ingest_policy="include",
            )
        ]

    def _seed_search_index(
        self,
        root: Path,
        *,
        rerank_provider: Callable[
            [str, list[str], int],
            list[tuple[int, float]],
        ],
        rerank_model: str = "test-rerank-v1",
        database_name: str = "authority.sqlite3",
    ) -> AuthorityIndex:
        project = root / "project"
        (project / "正文").mkdir(parents=True)
        (project / "正文" / "第一章.md").write_text(
            "RERANK_DIAGNOSTIC_MARKER 测试角色甲仍在测试城。",
            encoding="utf-8",
        )
        index = AuthorityIndex(
            root / database_name,
            rerank_provider=rerank_provider,
            rerank_model=rerank_model,
        )
        index.refresh(project, self._canon_sources())
        return index

    def test_explicit_identity_shares_across_provider_instances(self) -> None:
        identity = "test-rerank-explicit/" + uuid.uuid4().hex

        class Provider:
            def __init__(self, score_offset: float) -> None:
                self.calls = 0
                self.score_offset = score_offset
                self.cache_identity = identity

            def __call__(
                self,
                _query: str,
                documents: list[str],
                _top_n: int,
            ) -> list[tuple[int, float]]:
                self.calls += 1
                return [
                    (
                        index,
                        self.score_offset
                        + float(len(documents) - index),
                    )
                    for index in range(len(documents))
                ]

        first_provider = Provider(0.0)
        second_provider = Provider(100.0)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = AuthorityIndex(
                root / "first.sqlite3",
                rerank_provider=first_provider,
                rerank_model="test-rerank-v1",
            )
            second = AuthorityIndex(
                root / "second.sqlite3",
                rerank_provider=second_provider,
                rerank_model="test-rerank-v1",
            )

            expected = [(0, 3.0), (1, 2.0), (2, 1.0)]
            self.assertEqual(
                expected,
                first._exact_rerank("同一查询", ["甲", "乙", "丙"], 3),
            )
            self.assertEqual(
                expected,
                second._exact_rerank("同一查询", ["甲", "乙", "丙"], 3),
            )
            self.assertEqual(1, first_provider.calls)
            self.assertEqual(0, second_provider.calls)

    def test_implicit_identity_is_same_object_only_and_retains_lifetime(
        self,
    ) -> None:
        class Provider:
            def __init__(self, score: float) -> None:
                self.calls = 0
                self.score = score

            def __call__(
                self,
                _query: str,
                _documents: list[str],
                _top_n: int,
            ) -> list[tuple[int, float]]:
                self.calls += 1
                return [(0, self.score)]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = Provider(1.0)
            first = AuthorityIndex(
                root / "implicit-first.sqlite3",
                rerank_provider=provider,
                rerank_model="test-rerank-v1",
            )
            second = AuthorityIndex(
                root / "implicit-second.sqlite3",
                rerank_provider=provider,
                rerank_model="test-rerank-v1",
            )
            request = ("对象身份", ["甲"], 1)
            old_key = first._rerank_result_cache_key(*request)

            self.assertEqual(
                [(0, 1.0)],
                first._exact_rerank(*request),
            )
            self.assertEqual(
                [(0, 1.0)],
                second._exact_rerank(*request),
            )
            self.assertEqual(1, provider.calls)

            provider_ref = weakref.ref(provider)
            del first
            del second
            del provider
            gc.collect()
            self.assertIsNotNone(
                provider_ref(),
                "the completed cache must retain an implicit provider owner",
            )

            replacement = Provider(9.0)
            third = AuthorityIndex(
                root / "implicit-third.sqlite3",
                rerank_provider=replacement,
                rerank_model="test-rerank-v1",
            )
            self.assertEqual(
                [(0, 9.0)],
                third._exact_rerank(*request),
            )
            self.assertEqual(1, replacement.calls)

            replacement_key = third._rerank_result_cache_key(*request)
            with third._flight_lock:
                third._rerank_result_cache.pop(old_key, None)
                third._rerank_result_cache.pop(replacement_key, None)
            del third
            del replacement
            gc.collect()
            self.assertIsNone(provider_ref())

    def test_lru_hit_moves_to_tail_and_eviction_releases_implicit_owner(
        self,
    ) -> None:
        class ImplicitProvider:
            def __init__(self) -> None:
                self.calls = 0

            def __call__(
                self,
                _query: str,
                _documents: list[str],
                _top_n: int,
            ) -> list[tuple[int, float]]:
                self.calls += 1
                return [(0, 1.0)]

        class FillerProvider:
            def __init__(self) -> None:
                self.calls = 0
                self.cache_identity = (
                    "test-rerank-lru-filler/" + uuid.uuid4().hex
                )

            def __call__(
                self,
                _query: str,
                _documents: list[str],
                _top_n: int,
            ) -> list[tuple[int, float]]:
                self.calls += 1
                return [(0, 1.0)]

        cache = authority_module._SHARED_RERANK_RESULT_CACHE
        flights = authority_module._SHARED_RERANK_RESULT_FLIGHTS
        lock = authority_module._SHARED_FLIGHT_LOCK
        with lock:
            self.assertEqual({}, flights)
            original_cache = list(cache.items())
            cache.clear()

        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                implicit_provider = ImplicitProvider()
                provider_ref = weakref.ref(implicit_provider)
                implicit_index = AuthorityIndex(
                    root / "implicit-lru.sqlite3",
                    rerank_provider=implicit_provider,
                    rerank_model="test-rerank-v1",
                )
                filler_provider = FillerProvider()
                filler_index = AuthorityIndex(
                    root / "filler-lru.sqlite3",
                    rerank_provider=filler_provider,
                    rerank_model="test-rerank-v1",
                )
                request = ("implicit-owner", ["甲"], 1)
                implicit_key = implicit_index._rerank_result_cache_key(
                    *request
                )
                first_filler_request = ("evict-first", ["甲"], 1)
                first_filler_key = (
                    filler_index._rerank_result_cache_key(
                        *first_filler_request
                    )
                )

                implicit_index._exact_rerank(*request)
                filler_index._exact_rerank(*first_filler_request)
                implicit_index._exact_rerank(*request)
                self.assertEqual(1, implicit_provider.calls)

                for index in range(4095):
                    filler_index._exact_rerank(
                        f"fill-{index:04d}",
                        ["甲"],
                        1,
                    )

                with lock:
                    self.assertEqual(
                        authority_module._RERANK_RESULT_CACHE_SIZE,
                        len(cache),
                    )
                    self.assertNotIn(first_filler_key, cache)
                    self.assertIn(implicit_key, cache)

                del implicit_index
                del implicit_provider
                gc.collect()
                retained = provider_ref()
                self.assertIsNotNone(
                    retained,
                    "a non-evicted implicit cache entry must retain its owner",
                )
                del retained

                filler_index._exact_rerank(
                    "fill-4095",
                    ["甲"],
                    1,
                )
                with lock:
                    self.assertEqual(
                        authority_module._RERANK_RESULT_CACHE_SIZE,
                        len(cache),
                    )
                    self.assertNotIn(implicit_key, cache)
                gc.collect()
                self.assertIsNone(
                    provider_ref(),
                    "evicting the entry must release its implicit owner",
                )
                self.assertEqual(4097, filler_provider.calls)
        finally:
            with lock:
                cache.clear()
                cache.update(original_cache)

    def test_request_dimensions_are_all_isolated(self) -> None:
        identity_root = "test-rerank-isolation/" + uuid.uuid4().hex

        class Provider:
            def __init__(self, identity: str) -> None:
                self.cache_identity = identity
                self.calls: list[tuple[str, list[str], int]] = []

            def __call__(
                self,
                query: str,
                documents: list[str],
                top_n: int,
            ) -> list[tuple[int, float]]:
                self.calls.append((query, list(documents), top_n))
                return [
                    (index, float(len(documents) - index))
                    for index in range(len(documents))
                ]

        first_provider = Provider(identity_root + "/provider-a")
        second_provider = Provider(identity_root + "/provider-b")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = AuthorityIndex(
                root / "primary.sqlite3",
                rerank_provider=first_provider,
                rerank_model="model-a",
            )
            changed_model = AuthorityIndex(
                root / "changed-model.sqlite3",
                rerank_provider=first_provider,
                rerank_model="model-b",
            )
            changed_provider = AuthorityIndex(
                root / "changed-provider.sqlite3",
                rerank_provider=second_provider,
                rerank_model="model-a",
            )
            documents = ["甲文档", "乙文档"]

            primary._exact_rerank("查询一", documents, 2)
            primary._exact_rerank("查询二", documents, 2)
            primary._exact_rerank(
                "查询一",
                list(reversed(documents)),
                2,
            )
            primary._exact_rerank(
                "查询一",
                ["甲文档", "丙文档"],
                2,
            )
            primary._exact_rerank("查询一", documents, 1)
            changed_model._exact_rerank("查询一", documents, 2)
            changed_provider._exact_rerank("查询一", documents, 2)

            # The exact original request is the only completed-cache hit.
            primary._exact_rerank("查询一", documents, 2)

            self.assertEqual(6, len(first_provider.calls))
            self.assertEqual(1, len(second_provider.calls))
            self.assertIn(
                ("查询一", ["乙文档", "甲文档"], 2),
                first_provider.calls,
            )
            self.assertIn(
                ("查询一", ["甲文档", "乙文档"], 1),
                first_provider.calls,
            )

    def test_malformed_results_are_not_cached_and_allow_healthy_retry(
        self,
    ) -> None:
        cases: tuple[tuple[str, list[Any]], ...] = (
            ("empty", []),
            ("duplicate_index", [(0, 1.0), (0, 0.5)]),
            ("negative_index", [(-1, 1.0)]),
            ("out_of_range_index", [(1, 1.0)]),
            ("bool_index", [(True, 1.0)]),
            ("bool_score", [(0, True)]),
            ("nan_score", [(0, float("nan"))]),
            ("positive_inf_score", [(0, float("inf"))]),
            ("negative_inf_score", [(0, float("-inf"))]),
            ("malformed_pair", [(0, 1.0, "extra")]),
        )

        for name, malformed in cases:
            with self.subTest(name=name):
                class Provider:
                    def __init__(self) -> None:
                        self.calls = 0
                        self.healthy = False
                        self.cache_identity = (
                            "test-rerank-malformed/"
                            + name
                            + "/"
                            + uuid.uuid4().hex
                        )

                    def __call__(
                        self,
                        _query: str,
                        _documents: list[str],
                        _top_n: int,
                    ) -> list[Any]:
                        self.calls += 1
                        if self.healthy:
                            return [(0, 1.0)]
                        return list(malformed)

                provider = Provider()
                with tempfile.TemporaryDirectory() as temporary:
                    index = AuthorityIndex(
                        Path(temporary) / f"{name}.sqlite3",
                        rerank_provider=provider,
                        rerank_model="test-rerank-v1",
                    )
                    request = ("畸形响应", ["甲"], 1)
                    cache_key = index._rerank_result_cache_key(*request)
                    first_stats: dict[str, int] = {}

                    with self.assertRaises(AuthorityIndexError):
                        index._exact_rerank(
                            *request,
                            stats=first_stats,
                        )
                    self.assertEqual(1, provider.calls)
                    self.assertEqual(1, first_stats["cache_misses"])
                    with index._flight_lock:
                        self.assertNotIn(
                            cache_key,
                            index._rerank_result_cache,
                        )
                        self.assertNotIn(
                            cache_key,
                            index._rerank_result_flights,
                        )

                    provider.healthy = True
                    retry_stats: dict[str, int] = {}
                    self.assertEqual(
                        [(0, 1.0)],
                        index._exact_rerank(
                            *request,
                            stats=retry_stats,
                        ),
                    )
                    self.assertEqual(2, provider.calls)
                    self.assertEqual(1, retry_stats["cache_misses"])

                    cached_stats: dict[str, int] = {}
                    self.assertEqual(
                        [(0, 1.0)],
                        index._exact_rerank(
                            *request,
                            stats=cached_stats,
                        ),
                    )
                    self.assertEqual(2, provider.calls)
                    self.assertEqual(1, cached_stats["cache_hits"])
                    with index._flight_lock:
                        index._rerank_result_cache.pop(cache_key, None)

    def test_partial_full_pool_results_are_not_cached(self) -> None:
        calls = 0
        healthy = False

        def provider(
            _query: str,
            documents: list[str],
            _top_n: int,
        ) -> list[tuple[int, float]]:
            nonlocal calls, healthy
            calls += 1
            if not healthy:
                return [(0, 1.0)]
            return [
                (index, float(len(documents) - index))
                for index in range(len(documents))
            ]

        with tempfile.TemporaryDirectory() as temporary:
            index = AuthorityIndex(
                Path(temporary) / "partial.sqlite3",
                rerank_provider=provider,
                rerank_model="fixture-partial-v1",
            )
            request = ("partial", ["甲", "乙"], 2)
            with self.assertRaises(AuthorityIndexError):
                index._exact_rerank(*request)
            cache_key = index._rerank_result_cache_key(*request)
            with index._flight_lock:
                self.assertNotIn(cache_key, index._rerank_result_cache)
                self.assertNotIn(
                    cache_key,
                    index._rerank_result_flights,
                )
            healthy = True
            self.assertEqual(
                [(0, 2.0), (1, 1.0)],
                index._exact_rerank(*request),
            )
            self.assertEqual(2, calls)

    def test_completed_results_are_returned_as_independent_copies(self) -> None:
        class Provider:
            def __init__(self) -> None:
                self.calls = 0
                self.cache_identity = (
                    "test-rerank-copy/" + uuid.uuid4().hex
                )
                self.response = [(0, 2.0), (1, 1.0)]

            def __call__(
                self,
                _query: str,
                _documents: list[str],
                _top_n: int,
            ) -> list[tuple[int, float]]:
                self.calls += 1
                return self.response

        provider = Provider()
        with tempfile.TemporaryDirectory() as temporary:
            index = AuthorityIndex(
                Path(temporary) / "copies.sqlite3",
                rerank_provider=provider,
                rerank_model="test-rerank-v1",
            )
            expected = [(0, 2.0), (1, 1.0)]
            first = index._exact_rerank("副本", ["甲", "乙"], 2)
            provider.response.clear()
            first.clear()

            second = index._exact_rerank("副本", ["甲", "乙"], 2)
            self.assertEqual(expected, second)
            second.append((0, -1.0))
            third = index._exact_rerank("副本", ["甲", "乙"], 2)

            self.assertEqual(expected, third)
            self.assertIsNot(second, third)
            self.assertEqual(1, provider.calls)

    def test_real_concurrent_singleflight_calls_provider_once(self) -> None:
        identity = "test-rerank-singleflight/" + uuid.uuid4().hex
        entered = threading.Event()
        release = threading.Event()
        call_lock = threading.Lock()
        call_count = 0

        class Provider:
            cache_identity = identity

            def __call__(
                self,
                _query: str,
                documents: list[str],
                _top_n: int,
            ) -> list[tuple[int, float]]:
                nonlocal call_count
                with call_lock:
                    call_count += 1
                entered.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("test release timed out")
                return [
                    (index, float(len(documents) - index))
                    for index in range(len(documents))
                ]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = AuthorityIndex(
                root / "singleflight-first.sqlite3",
                rerank_provider=Provider(),
                rerank_model="test-rerank-v1",
            )
            second = AuthorityIndex(
                root / "singleflight-second.sqlite3",
                rerank_provider=Provider(),
                rerank_model="test-rerank-v1",
            )
            leader_stats: dict[str, int] = {}
            follower_stats: dict[str, int] = {}
            results: dict[str, list[tuple[int, float]]] = {}
            errors: list[BaseException] = []

            def run(
                name: str,
                index: AuthorityIndex,
                stats: dict[str, int],
            ) -> None:
                try:
                    results[name] = index._exact_rerank(
                        "并发查询",
                        ["甲", "乙"],
                        2,
                        stats=stats,
                    )
                except BaseException as exc:
                    errors.append(exc)

            leader = threading.Thread(
                target=run,
                args=("leader", first, leader_stats),
            )
            follower = threading.Thread(
                target=run,
                args=("follower", second, follower_stats),
            )
            leader.start()
            entered_ok = entered.wait(timeout=2)
            follower_started = False
            follower_joined = False
            if entered_ok:
                follower.start()
                follower_started = True
                follower_joined = self._wait_until(
                    lambda: (
                        follower_stats.get("singleflight_waits", 0) == 1
                    )
                )
            release.set()
            leader.join(timeout=5)
            if follower_started:
                follower.join(timeout=5)

            self.assertTrue(entered_ok)
            self.assertTrue(follower_joined)
            self.assertFalse(leader.is_alive())
            self.assertFalse(follower.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(1, call_count)
            self.assertEqual(
                [(0, 2.0), (1, 1.0)],
                results["leader"],
            )
            self.assertEqual(results["leader"], results["follower"])
            self.assertIsNot(results["leader"], results["follower"])
            self.assertEqual(1, leader_stats["cache_misses"])
            self.assertEqual(
                1,
                follower_stats["singleflight_waits"],
            )

            completed_stats: dict[str, int] = {}
            self.assertEqual(
                results["leader"],
                second._exact_rerank(
                    "并发查询",
                    ["甲", "乙"],
                    2,
                    stats=completed_stats,
                ),
            )
            self.assertEqual(1, completed_stats["cache_hits"])
            self.assertEqual(1, call_count)

    def test_concurrent_failure_propagates_cleans_flight_and_retries(
        self,
    ) -> None:
        identity = "test-rerank-failure/" + uuid.uuid4().hex
        entered = threading.Event()
        release = threading.Event()
        state_lock = threading.Lock()
        state = {"calls": 0, "fail": True}

        class Provider:
            cache_identity = identity

            def __call__(
                self,
                _query: str,
                _documents: list[str],
                _top_n: int,
            ) -> list[tuple[int, float]]:
                with state_lock:
                    state["calls"] += 1
                    should_fail = bool(state["fail"])
                if should_fail:
                    entered.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("test release timed out")
                    raise TimeoutError("fixture concurrent failure")
                return [(0, 1.0)]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = AuthorityIndex(
                root / "failure-first.sqlite3",
                rerank_provider=Provider(),
                rerank_model="test-rerank-v1",
            )
            second = AuthorityIndex(
                root / "failure-second.sqlite3",
                rerank_provider=Provider(),
                rerank_model="test-rerank-v1",
            )
            leader_stats: dict[str, int] = {}
            follower_stats: dict[str, int] = {}
            errors: dict[str, BaseException] = {}

            def run(
                name: str,
                index: AuthorityIndex,
                stats: dict[str, int],
            ) -> None:
                try:
                    index._exact_rerank(
                        "失败查询",
                        ["甲"],
                        1,
                        stats=stats,
                    )
                except BaseException as exc:
                    errors[name] = exc

            leader = threading.Thread(
                target=run,
                args=("leader", first, leader_stats),
            )
            follower = threading.Thread(
                target=run,
                args=("follower", second, follower_stats),
            )
            leader.start()
            entered_ok = entered.wait(timeout=2)
            follower_started = False
            follower_joined = False
            if entered_ok:
                follower.start()
                follower_started = True
                follower_joined = self._wait_until(
                    lambda: (
                        follower_stats.get("singleflight_waits", 0) == 1
                    )
                )
            release.set()
            leader.join(timeout=5)
            if follower_started:
                follower.join(timeout=5)

            self.assertTrue(entered_ok)
            self.assertTrue(follower_joined)
            self.assertFalse(leader.is_alive())
            self.assertFalse(follower.is_alive())
            self.assertEqual(1, state["calls"])
            self.assertEqual({"leader", "follower"}, set(errors))
            for error in errors.values():
                self.assertIsInstance(error, TimeoutError)
                self.assertEqual(
                    "fixture concurrent failure",
                    str(error),
                )
            self.assertEqual(1, leader_stats["cache_misses"])
            self.assertEqual(
                1,
                follower_stats["singleflight_waits"],
            )

            cache_key = first._rerank_result_cache_key(
                "失败查询",
                ["甲"],
                1,
            )
            with first._flight_lock:
                self.assertNotIn(
                    cache_key,
                    first._rerank_result_flights,
                )
                self.assertNotIn(
                    cache_key,
                    first._rerank_result_cache,
                )

            state["fail"] = False
            retry_stats: dict[str, int] = {}
            self.assertEqual(
                [(0, 1.0)],
                second._exact_rerank(
                    "失败查询",
                    ["甲"],
                    1,
                    stats=retry_stats,
                ),
            )
            self.assertEqual(2, state["calls"])
            self.assertEqual(1, retry_stats["cache_misses"])

            cached_stats: dict[str, int] = {}
            self.assertEqual(
                [(0, 1.0)],
                first._exact_rerank(
                    "失败查询",
                    ["甲"],
                    1,
                    stats=cached_stats,
                ),
            )
            self.assertEqual(2, state["calls"])
            self.assertEqual(1, cached_stats["cache_hits"])

    def test_leader_base_exceptions_clean_flight_and_allow_retry(
        self,
    ) -> None:
        for exception_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(exception_type=exception_type.__name__):
                class Provider:
                    def __init__(self) -> None:
                        self.calls = 0
                        self.healthy = False
                        self.cache_identity = (
                            "test-rerank-base-exception/"
                            + exception_type.__name__
                            + "/"
                            + uuid.uuid4().hex
                        )

                    def __call__(
                        self,
                        _query: str,
                        _documents: list[str],
                        _top_n: int,
                    ) -> list[tuple[int, float]]:
                        self.calls += 1
                        if not self.healthy:
                            raise exception_type("fixture leader termination")
                        return [(0, 1.0)]

                provider = Provider()
                with tempfile.TemporaryDirectory() as temporary:
                    index = AuthorityIndex(
                        Path(temporary) / (
                            exception_type.__name__ + ".sqlite3"
                        ),
                        rerank_provider=provider,
                        rerank_model="test-rerank-v1",
                    )
                    request = ("leader-termination", ["甲"], 1)
                    cache_key = index._rerank_result_cache_key(*request)

                    with self.assertRaises(exception_type):
                        index._exact_rerank(*request)
                    self.assertEqual(1, provider.calls)
                    with index._flight_lock:
                        self.assertNotIn(
                            cache_key,
                            index._rerank_result_cache,
                        )
                        self.assertNotIn(
                            cache_key,
                            index._rerank_result_flights,
                        )

                    provider.healthy = True
                    retry_stats: dict[str, int] = {}
                    self.assertEqual(
                        [(0, 1.0)],
                        index._exact_rerank(
                            *request,
                            stats=retry_stats,
                        ),
                    )
                    self.assertEqual(2, provider.calls)
                    self.assertEqual(1, retry_stats["cache_misses"])

                    cached_stats: dict[str, int] = {}
                    self.assertEqual(
                        [(0, 1.0)],
                        index._exact_rerank(
                            *request,
                            stats=cached_stats,
                        ),
                    )
                    self.assertEqual(2, provider.calls)
                    self.assertEqual(1, cached_stats["cache_hits"])
                    with index._flight_lock:
                        index._rerank_result_cache.pop(cache_key, None)

    def test_singleflight_opt_out_reuses_completed_exact_result(self) -> None:
        class Provider:
            def __init__(self) -> None:
                self.calls = 0
                self.cache_identity = (
                    "test-rerank-opt-out/" + uuid.uuid4().hex
                )

            def __call__(
                self,
                _query: str,
                _documents: list[str],
                _top_n: int,
            ) -> list[tuple[int, float]]:
                self.calls += 1
                return [(0, 1.0)]

        provider = Provider()
        with tempfile.TemporaryDirectory() as temporary:
            index = AuthorityIndex(
                Path(temporary) / "opt-out.sqlite3",
                rerank_provider=provider,
                rerank_model="test-rerank-v1",
                singleflight_enabled=False,
            )
            stats: dict[str, int] = {}
            index._exact_rerank("直连", ["甲"], 1, stats=stats)
            index._exact_rerank("直连", ["甲"], 1, stats=stats)
            self.assertEqual(1, provider.calls)
            self.assertEqual(1, stats["cache_misses"])
            self.assertEqual(1, stats["cache_hits"])

    def test_legacy_v2_sharing_and_diagnostic_aggregation(self) -> None:
        identity = "test-rerank-diagnostics/" + uuid.uuid4().hex

        class Provider:
            def __init__(self) -> None:
                self.calls = 0
                self.cache_identity = identity

            def __call__(
                self,
                _query: str,
                documents: list[str],
                _top_n: int,
            ) -> list[tuple[int, float]]:
                self.calls += 1
                return [
                    (index, float(len(documents) - index))
                    for index in range(len(documents))
                ]

        first_provider = Provider()
        second_provider = Provider()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_index = self._seed_search_index(
                root,
                rerank_provider=first_provider,
            )
            v2_index = AuthorityIndex(
                root / "authority.sqlite3",
                rerank_provider=second_provider,
                rerank_model="test-rerank-v1",
            )
            query = "RERANK_DIAGNOSTIC_MARKER 测试城"
            filters: dict[str, Any] = {
                "limit": 1,
                "roles": ("canon",),
                "scope_policies": ("infer_and_review",),
                "ingest_policies": ("include",),
                "use_candidate_cache": False,
            }

            legacy = legacy_index._search_legacy(query, **filters)
            current = v2_index.search_many(
                [{"query": query, **filters}]
            )[0]
            hit_diagnostics = v2_index.last_search_diagnostics()

            self.assertEqual(legacy, current)
            self.assertEqual(1, first_provider.calls)
            self.assertEqual(0, second_provider.calls)
            self.assertEqual(
                1,
                hit_diagnostics["rerank_cache_hits"],
            )
            self.assertEqual(
                0,
                hit_diagnostics["rerank_cache_misses"],
            )
            self.assertEqual(
                0,
                hit_diagnostics["rerank_singleflight_waits"],
            )
            self.assertEqual(0, hit_diagnostics["cache_hit_count"])
            self.assertEqual(1, hit_diagnostics["cache_miss_count"])

            v2_index.search_many(
                [
                    {
                        "query": (
                            "测试城 RERANK_DIAGNOSTIC_MARKER"
                        ),
                        **filters,
                    }
                ]
            )
            miss_diagnostics = v2_index.last_search_diagnostics()
            self.assertEqual(1, second_provider.calls)
            self.assertEqual(
                0,
                miss_diagnostics["rerank_cache_hits"],
            )
            self.assertEqual(
                1,
                miss_diagnostics["rerank_cache_misses"],
            )
            self.assertEqual(0, miss_diagnostics["cache_hit_count"])
            self.assertEqual(1, miss_diagnostics["cache_miss_count"])

            aggregated = (
                ContextContractBuilder._aggregate_retrieval_diagnostics(
                    [hit_diagnostics, miss_diagnostics]
                )
            )
            self.assertEqual(2, aggregated["query_count"])
            self.assertEqual(1, aggregated["rerank_cache_hits"])
            self.assertEqual(
                0,
                aggregated["rerank_singleflight_waits"],
            )
            self.assertEqual(1, aggregated["rerank_cache_misses"])
            self.assertEqual(0, aggregated["cache_hit_count"])
            self.assertEqual(2, aggregated["cache_miss_count"])
            self.assertEqual(2, aggregated["search_calls"])


if __name__ == "__main__":
    unittest.main()
