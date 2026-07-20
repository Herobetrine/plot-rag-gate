"""Deterministic v1.5 retrieval performance benchmark.

The benchmark is deliberately offline.  It exercises the production
``AuthorityIndex`` with deterministic embedding and rerank providers while
recording wall-clock observations separately from semantic fingerprints.
The checked-in fixture contains only synthetic novel facts.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import subprocess
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(SCRIPTS))

from longform.authority import AuthorityIndex, AuthoritySource  # noqa: E402


BENCHMARK_SUITE = "plot-rag-v15-performance"
FIXTURE_SCHEMA_VERSION = "plot-rag-v15-performance-fixture/v1"
RESULT_SCHEMA_VERSION = "plot-rag-v15-performance-result/v1"
REDACTED_MANIFEST_SCHEMA_VERSION = (
    "plot-rag-v15-performance-redacted-manifest/v1"
)
FIXTURE_MANIFEST_VERSION = 1
RUNNER_VERSION = 3
DEFAULT_ITERATIONS = 5
DEFAULT_WARMUP_ITERATIONS = 1
DEFAULT_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "v15_performance_manifest.v1.json"
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]+")
_SAFE_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,95}\Z")
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CLOCK$",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_WINDOWS_INVALID_COMPONENT_CHARS = frozenset('<>:"|?*')
DEFAULT_ARTIFACT_ROOT = Path(".plot-rag-benchmark")
ARTIFACT_TIMESTAMP_INVALID_CODE = "V15_ARTIFACT_TIMESTAMP_INVALID"
_ARTIFACT_RFC3339_RE = re.compile(
    r"(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"(?P<fraction>\.[0-9]{1,6})?"
    r"(?P<zone>Z|(?P<offset_sign>[+-])"
    r"(?P<offset_hour>[0-9]{2}):(?P<offset_minute>[0-9]{2}))\Z"
)


class BenchmarkFixtureError(ValueError):
    """The offline benchmark fixture is malformed."""


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def default_fixture_manifest() -> dict[str, Any]:
    """Return the canonical synthetic v1.5 benchmark fixture."""

    need_ids = ["location", "relation", "item", "story_time", "power"]
    return {
        "manifest_version": FIXTURE_MANIFEST_VERSION,
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "suite": BENCHMARK_SUITE,
        "fixture_id": "synthetic-authority-v2",
        "sources": [
            {
                "glob": "canon/**/*.md",
                "role": "canon",
                "priority": 100,
                "scope_policy": "current",
                "ingest_policy": "include",
            },
            {
                "glob": "setting/**/*.md",
                "role": "setting",
                "priority": 80,
                "scope_policy": "timeless",
                "ingest_policy": "include",
            },
        ],
        "files": [
            {
                "path": "canon/001-location.md",
                "content": (
                    "# Fixture A\n"
                    "BENCH_LOCATION ACTOR_A is currently at "
                    "ZONE_A_PLATFORM; TRANSIT_A arrives after SIGNAL_A.\n"
                ),
            },
            {
                "path": "canon/002-relation.md",
                "content": (
                    "# Fixture B\n"
                    "BENCH_RELATION ACTOR_A and ACTOR_B are temporary "
                    "allies protecting WITNESS_A.\n"
                ),
            },
            {
                "path": "canon/003-item.md",
                "content": (
                    "# Fixture C\n"
                    "BENCH_ITEM ITEM_A is physically held by ACTOR_A and "
                    "opens ACCESS_POINT_A.\n"
                ),
            },
            {
                "path": "canon/004-time.md",
                "content": (
                    "# Fixture D\n"
                    "BENCH_TIME the current story coordinate is TIME_A and "
                    "DEADLINE_A is one interval away.\n"
                ),
            },
            {
                "path": "setting/005-power.md",
                "content": (
                    "# Fixture E\n"
                    "BENCH_POWER ACTOR_A may use ABILITY_A three times; "
                    "a fourth use triggers COST_A.\n"
                ),
            },
            {
                "path": "setting/006-transport.md",
                "content": (
                    "# Fixture F\n"
                    "TRANSIT_A requires INFRASTRUCTURE_A and CHECK_A; "
                    "METHOD_B cannot bypass either rule.\n"
                ),
            },
            {
                "path": "canon/007-distractor.md",
                "content": (
                    "# Fixture G\n"
                    "ACTOR_B once searched for ITEM_B, but that history "
                    "does not change ITEM_A custody.\n"
                ),
            },
        ],
        "needs": [
            {
                "id": "location",
                "query": (
                    "BENCH_LOCATION ACTOR_A current location ZONE_A_PLATFORM"
                ),
                "expected_path": "canon/001-location.md",
            },
            {
                "id": "relation",
                "query": (
                    "BENCH_RELATION ACTOR_A ACTOR_B temporary allies"
                ),
                "expected_path": "canon/002-relation.md",
            },
            {
                "id": "item",
                "query": (
                    "BENCH_ITEM ITEM_A physical holder ACTOR_A ACCESS_POINT_A"
                ),
                "expected_path": "canon/003-item.md",
            },
            {
                "id": "story_time",
                "query": "BENCH_TIME current story coordinate TIME_A DEADLINE_A",
                "expected_path": "canon/004-time.md",
            },
            {
                "id": "power",
                "query": (
                    "BENCH_POWER ACTOR_A ABILITY_A three uses COST_A"
                ),
                "expected_path": "setting/005-power.md",
            },
        ],
        "scenarios": [
            {
                "id": "needs-1-batched",
                "need_ids": need_ids[:1],
                "embedding_batch_mode": "ok",
                "single_failure_need_ids": [],
                "rerank_max_concurrency": 5,
            },
            {
                "id": "needs-3-batched",
                "need_ids": need_ids[:3],
                "embedding_batch_mode": "ok",
                "single_failure_need_ids": [],
                "rerank_max_concurrency": 5,
            },
            {
                "id": "needs-5-batched",
                "need_ids": need_ids,
                "embedding_batch_mode": "ok",
                "single_failure_need_ids": [],
                "rerank_max_concurrency": 5,
            },
            {
                "id": "needs-5-cap-2",
                "need_ids": need_ids,
                "embedding_batch_mode": "ok",
                "single_failure_need_ids": [],
                "rerank_max_concurrency": 2,
            },
            {
                "id": "needs-5-batch-fallback",
                "need_ids": need_ids,
                "embedding_batch_mode": "fail",
                "single_failure_need_ids": ["story_time"],
                "rerank_max_concurrency": 5,
            },
            {
                "id": "needs-5-batch-wrong-length",
                "need_ids": need_ids,
                "embedding_batch_mode": "wrong_length",
                "single_failure_need_ids": [],
                "rerank_max_concurrency": 5,
            },
            {
                "id": "needs-5-batch-bad-index",
                "need_ids": need_ids,
                "embedding_batch_mode": "bad_index",
                "single_failure_need_ids": [],
                "rerank_max_concurrency": 5,
            },
            {
                "id": "needs-5-batch-duplicate-index",
                "need_ids": need_ids,
                "embedding_batch_mode": "duplicate_index",
                "single_failure_need_ids": [],
                "rerank_max_concurrency": 5,
            },
        ],
        "settings": {
            "limit": 3,
            "embedding_batch_size": 32,
            "embedding_batch_max_chars": 24000,
            "rerank_delay_ms": 20,
        },
    }


def load_fixture_manifest(path: str | Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise BenchmarkFixtureError("fixture manifest is invalid JSON") from error
    if not isinstance(value, dict):
        raise BenchmarkFixtureError("fixture manifest root must be an object")
    return value


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str):
        raise BenchmarkFixtureError("fixture path must be a string")
    text = value
    if text != text.strip():
        raise BenchmarkFixtureError(
            f"fixture path cannot have leading or trailing whitespace: {text!r}"
        )
    if (
        not text
        or "\\" in text
        or text.startswith("/")
        or text.startswith("//")
        or ":" in text
        or "\x00" in text
        or any(part in {"", "."} for part in text.split("/"))
    ):
        raise BenchmarkFixtureError(
            f"fixture path must be a safe project-relative path: {text!r}"
        )
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or ".." in path.parts
        or any(
            part.casefold() in {"", ".", ".git", ".plot-rag"}
            for part in path.parts
        )
    ):
        raise BenchmarkFixtureError(
            f"fixture path must be a safe project-relative path: {text!r}"
        )
    for part in path.parts:
        if (
            part.endswith((" ", "."))
            or any(character in _WINDOWS_INVALID_COMPONENT_CHARS for character in part)
            or any(ord(character) < 32 for character in part)
        ):
            raise BenchmarkFixtureError(
                f"fixture path has a non-portable component: {text!r}"
            )
        reserved_key = part.rstrip(" .").split(".", 1)[0].upper()
        if reserved_key in _WINDOWS_RESERVED_NAMES:
            raise BenchmarkFixtureError(
                f"fixture path uses a reserved Windows name: {text!r}"
            )
    return path.as_posix()


def _safe_identifier(value: Any, *, field: str) -> str:
    text = str(value or "")
    if not _SAFE_IDENTIFIER_RE.fullmatch(text):
        raise BenchmarkFixtureError(
            f"{field} must be a lowercase opaque identifier"
        )
    return text


def _safe_source_glob(value: Any) -> str:
    if not isinstance(value, str):
        raise BenchmarkFixtureError("source glob must be a string")
    text = value
    path = PurePosixPath(text)
    if (
        not text
        or text != text.strip()
        or "\\" in text
        or ":" in text
        or "\x00" in text
        or text.startswith("/")
        or text.startswith("//")
        or path.is_absolute()
        or ".." in path.parts
        or any(part in {"", "."} for part in text.split("/"))
    ):
        raise BenchmarkFixtureError(
            f"source glob must stay project-relative: {text!r}"
        )
    for part in path.parts:
        if part in {"", "."} or part.endswith((" ", ".")):
            raise BenchmarkFixtureError(
                f"source glob has a non-portable component: {text!r}"
            )
        literal_prefix = re.split(r"[*?\[]", part, maxsplit=1)[0]
        reserved_key = literal_prefix.rstrip(" .").split(".", 1)[0].upper()
        if literal_prefix and reserved_key in _WINDOWS_RESERVED_NAMES:
            raise BenchmarkFixtureError(
                f"source glob uses a reserved Windows name: {text!r}"
            )
    return path.as_posix()


def _contained_fixture_path(root: Path, relative: str) -> Path:
    root_resolved = root.resolve()
    destination = root_resolved.joinpath(*PurePosixPath(relative).parts)
    try:
        destination.resolve(strict=False).relative_to(root_resolved)
    except ValueError as error:
        raise BenchmarkFixtureError(
            f"fixture path escaped the temporary project: {relative!r}"
        ) from error
    return destination


def _require_mapping_list(
    manifest: Mapping[str, Any],
    field: str,
) -> list[Mapping[str, Any]]:
    value = manifest.get(field)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, Mapping) for item in value)
    ):
        raise BenchmarkFixtureError(f"{field} must be a non-empty object list")
    return list(value)


def validate_fixture_manifest(
    manifest_or_path: Mapping[str, Any] | str | Path = DEFAULT_FIXTURE,
) -> dict[str, Any]:
    """Validate the checked-in benchmark fixture and return stable metadata."""

    manifest = (
        load_fixture_manifest(manifest_or_path)
        if isinstance(manifest_or_path, (str, Path))
        else deepcopy(dict(manifest_or_path))
    )
    if manifest.get("manifest_version") != FIXTURE_MANIFEST_VERSION:
        raise BenchmarkFixtureError("unsupported fixture manifest_version")
    if manifest.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        raise BenchmarkFixtureError("unsupported fixture schema_version")
    if manifest.get("suite") != BENCHMARK_SUITE:
        raise BenchmarkFixtureError("fixture suite does not match benchmark")
    fixture_id = _safe_identifier(
        manifest.get("fixture_id"),
        field="fixture_id",
    )

    sources = _require_mapping_list(manifest, "sources")
    for source in sources:
        _safe_source_glob(source.get("glob"))
        AuthoritySource.from_mapping(source)

    files = _require_mapping_list(manifest, "files")
    file_paths: set[str] = set()
    portable_file_paths: set[str] = set()
    for file_record in files:
        relative = _safe_relative_path(file_record.get("path"))
        portable_key = relative.casefold()
        if relative in file_paths or portable_key in portable_file_paths:
            raise BenchmarkFixtureError(f"duplicate fixture path: {relative}")
        file_paths.add(relative)
        portable_file_paths.add(portable_key)
        content = file_record.get("content")
        if not isinstance(content, str) or not content.strip():
            raise BenchmarkFixtureError(
                f"fixture file content is empty: {relative}"
            )

    needs = _require_mapping_list(manifest, "needs")
    need_ids: set[str] = set()
    for need in needs:
        need_id = _safe_identifier(need.get("id"), field="need.id")
        query = str(need.get("query") or "").strip()
        expected_path = _safe_relative_path(need.get("expected_path"))
        if need_id in need_ids:
            raise BenchmarkFixtureError("need ids must be non-empty and unique")
        if not query:
            raise BenchmarkFixtureError(f"query is empty for need {need_id}")
        if expected_path not in file_paths:
            raise BenchmarkFixtureError(
                f"expected_path is not a fixture file for need {need_id}"
            )
        need_ids.add(need_id)

    scenarios = _require_mapping_list(manifest, "scenarios")
    scenario_ids: set[str] = set()
    need_counts: set[int] = set()
    fallback_covered = False
    for scenario in scenarios:
        scenario_id = _safe_identifier(
            scenario.get("id"),
            field="scenario.id",
        )
        selected = scenario.get("need_ids")
        if scenario_id in scenario_ids:
            raise BenchmarkFixtureError(
                "scenario ids must be non-empty and unique"
            )
        if (
            not isinstance(selected, list)
            or not selected
            or len(selected) != len(set(map(str, selected)))
            or any(str(item) not in need_ids for item in selected)
        ):
            raise BenchmarkFixtureError(
                f"invalid need_ids for scenario {scenario_id}"
            )
        batch_mode = str(scenario.get("embedding_batch_mode") or "")
        if batch_mode not in {
            "ok",
            "fail",
            "wrong_length",
            "bad_index",
            "duplicate_index",
        }:
            raise BenchmarkFixtureError(
                f"invalid embedding_batch_mode for scenario {scenario_id}"
            )
        failures = scenario.get("single_failure_need_ids")
        if (
            not isinstance(failures, list)
            or any(str(item) not in set(map(str, selected)) for item in failures)
        ):
            raise BenchmarkFixtureError(
                f"invalid single_failure_need_ids for scenario {scenario_id}"
            )
        concurrency = scenario.get("rerank_max_concurrency")
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or concurrency < 1
        ):
            raise BenchmarkFixtureError(
                f"invalid rerank_max_concurrency for scenario {scenario_id}"
            )
        scenario_ids.add(scenario_id)
        need_counts.add(len(selected))
        fallback_covered = fallback_covered or batch_mode != "ok"

    if not {1, 3, 5}.issubset(need_counts):
        raise BenchmarkFixtureError("fixture must cover 1, 3, and 5 needs")
    if not fallback_covered:
        raise BenchmarkFixtureError("fixture must cover batch fallback")

    settings = manifest.get("settings")
    if not isinstance(settings, Mapping):
        raise BenchmarkFixtureError("settings must be an object")
    for field in (
        "limit",
        "embedding_batch_size",
        "embedding_batch_max_chars",
        "rerank_delay_ms",
    ):
        value = settings.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BenchmarkFixtureError(
                f"settings.{field} must be a non-negative integer"
            )
    if int(settings["limit"]) < 1:
        raise BenchmarkFixtureError("settings.limit must be positive")
    if int(settings["embedding_batch_size"]) < 1:
        raise BenchmarkFixtureError(
            "settings.embedding_batch_size must be positive"
        )
    if int(settings["embedding_batch_max_chars"]) < 1:
        raise BenchmarkFixtureError(
            "settings.embedding_batch_max_chars must be positive"
        )

    return {
        "status": "valid",
        "suite": BENCHMARK_SUITE,
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "manifest_version": FIXTURE_MANIFEST_VERSION,
        "fixture_id_sha256": _sha256_text(fixture_id),
        "fixture_sha256": _sha256_json(manifest),
        "file_count": len(files),
        "need_count": len(needs),
        "scenario_count": len(scenarios),
        "covered_need_counts": sorted(need_counts),
        "batch_fallback_covered": fallback_covered,
    }


def _normalized_tokens(text: str) -> list[str]:
    return [match.group(0).casefold() for match in _TOKEN_RE.finditer(text)]


def _deterministic_vector(text: str, dimensions: int = 24) -> list[float]:
    vector = [0.0] * dimensions
    for token in _normalized_tokens(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:2], "big") % dimensions
        sign = 1.0 if digest[2] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0:
        return [1.0] + [0.0] * (dimensions - 1)
    return [round(value / norm, 12) for value in vector]


def _offline_provider_cache_identity(
    *,
    provider_type: str,
    protocol: str,
    behavior: Mapping[str, Any],
    isolation_id: str,
) -> str:
    """Return one bounded identity for process-scoped provider caches.

    Raw scenario labels, failure markers, and any caller-supplied isolation
    material are committed into a digest rather than exposed in shared cache
    keys or benchmark artifacts.
    """

    normalized_isolation = str(isolation_id)
    if not normalized_isolation or len(normalized_isolation) > 512:
        raise ValueError(
            "offline provider isolation_id must contain 1 to 512 characters"
        )
    digest = _sha256_json(
        {
            "protocol": protocol,
            "provider_type": provider_type,
            "behavior": dict(behavior),
            "isolation_sha256": _sha256_text(normalized_isolation),
        }
    )
    return f"{protocol}:{digest}"


def _offline_provider_isolation_id(
    *,
    run_id: str,
    scenario_id: str,
    run_label: str,
    lane: str,
) -> str:
    """Commit run/scenario/lane scope without retaining the raw identifiers."""

    return _sha256_json(
        {
            "run_id": str(run_id),
            "scenario_id": str(scenario_id),
            "run_label": str(run_label),
            "lane": str(lane),
        }
    )


class OfflineEmbeddingProvider:
    """Thread-safe deterministic provider with injectable batch failures."""

    cache_identity = "plot-rag-v15-offline-embedding/v2"

    def __init__(
        self,
        *,
        batch_mode: str,
        failure_markers: Iterable[str],
        isolation_id: str,
    ) -> None:
        self.batch_mode = str(batch_mode)
        self.failure_markers = tuple(
            sorted(
                {
                    str(item).casefold()
                    for item in failure_markers
                }
            )
        )
        self.cache_identity = _offline_provider_cache_identity(
            provider_type="embedding",
            protocol=type(self).cache_identity,
            behavior={
                "batch_mode": self.batch_mode,
                "failure_markers": list(self.failure_markers),
            },
            isolation_id=isolation_id,
        )
        self.query_phase = False
        self._lock = threading.Lock()
        self.single_calls = 0
        self.batch_calls = 0
        self.batch_failures = 0
        self.single_failures = 0

    def begin_query_phase(self) -> None:
        self.query_phase = True

    def __call__(self, text: str) -> list[float]:
        normalized = text.casefold()
        with self._lock:
            self.single_calls += 1
            should_fail = self.query_phase and any(
                marker in normalized for marker in self.failure_markers
            )
            if should_fail:
                self.single_failures += 1
        if should_fail:
            raise TimeoutError("offline per-need embedding failure")
        return _deterministic_vector(text)

    def embed_many(self, texts: Sequence[str]) -> Any:
        with self._lock:
            self.batch_calls += 1
            active_mode = self.batch_mode if self.query_phase else "ok"
            should_fail = active_mode != "ok"
            if should_fail:
                self.batch_failures += 1
        if active_mode == "fail":
            raise TimeoutError("offline batch embedding failure")
        vectors = [_deterministic_vector(text) for text in texts]
        if active_mode == "wrong_length":
            return vectors[:-1]
        if active_mode in {"bad_index", "duplicate_index"}:
            indexed = [
                {"index": index, "embedding": vector}
                for index, vector in enumerate(vectors)
            ]
            if indexed and active_mode == "bad_index":
                indexed[-1]["index"] = len(indexed) + 7
            elif len(indexed) > 1:
                indexed[-1]["index"] = indexed[0]["index"]
            return indexed
        return vectors

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "embedding_single_calls": self.single_calls,
                "embedding_batch_calls": self.batch_calls,
                "embedding_batch_failures": self.batch_failures,
                "embedding_single_failures": self.single_failures,
            }


class OfflineRerankProvider:
    """Deterministic reranker that exposes actual concurrency telemetry."""

    cache_identity = "plot-rag-v15-offline-rerank/v2"

    def __init__(
        self,
        *,
        expected_parallelism: int,
        delay_ms: int,
        isolation_id: str,
    ) -> None:
        self.expected_parallelism = max(1, int(expected_parallelism))
        self.delay_ms = max(0, int(delay_ms))
        self.delay_seconds = self.delay_ms / 1000.0
        self.cache_identity = _offline_provider_cache_identity(
            provider_type="rerank",
            protocol=type(self).cache_identity,
            behavior={
                "delay_ms": self.delay_ms,
                "expected_parallelism": self.expected_parallelism,
            },
            isolation_id=isolation_id,
        )
        self._condition = threading.Condition()
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.total_call_ns = 0
        self._initial_group_released = self.expected_parallelism == 1

    def __call__(
        self,
        query: str,
        documents: Sequence[str],
        _top_n: int,
    ) -> list[tuple[int, float]]:
        started = time.perf_counter_ns()
        with self._condition:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if not self._initial_group_released:
                if self.active >= self.expected_parallelism:
                    self._initial_group_released = True
                    self._condition.notify_all()
                else:
                    self._condition.wait_for(
                        lambda: self._initial_group_released,
                        timeout=1.0,
                    )
        try:
            if self.delay_seconds:
                time.sleep(self.delay_seconds)
            query_tokens = set(_normalized_tokens(query))
            ranked: list[tuple[int, float]] = []
            for index, document in enumerate(documents):
                document_tokens = set(_normalized_tokens(document))
                overlap = len(query_tokens & document_tokens)
                score = overlap / max(1, len(query_tokens))
                ranked.append((index, round(score, 8)))
            return ranked
        finally:
            elapsed = time.perf_counter_ns() - started
            with self._condition:
                self.total_call_ns += elapsed
                self.active -= 1
                self._condition.notify_all()

    def snapshot(self) -> dict[str, int]:
        with self._condition:
            return {
                "rerank_calls": self.calls,
                "rerank_max_active": self.max_active,
                "rerank_total_call_ns": self.total_call_ns,
            }


def _counter_delta(
    before: Mapping[str, int],
    after: Mapping[str, int],
) -> dict[str, int]:
    keys = sorted(set(before) | set(after))
    delta = {
        key: int(after.get(key, 0)) - int(before.get(key, 0))
        for key in keys
        if key != "rerank_max_active"
    }
    delta["rerank_max_active"] = (
        int(after.get("rerank_max_active", 0))
        if delta.get("rerank_calls", 0) > 0
        else 0
    )
    return delta


def _timed_call(function: Any) -> tuple[Any, float]:
    started = time.perf_counter_ns()
    result = function()
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    return result, round(elapsed_ms, 6)


def _selected_results_payload(
    needs: Sequence[Mapping[str, Any]],
    results: Sequence[Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    normalized = []
    for need, need_results in zip(needs, results):
        normalized.append(
            {
                "need_id": str(need["id"]),
                "results": [
                    {
                        "chunk_id": str(item.get("chunk_id") or ""),
                        "path": str(item.get("path") or ""),
                        "ordinal": int(item.get("ordinal") or 0),
                        "start_line": int(item.get("start_line") or 0),
                        "end_line": int(item.get("end_line") or 0),
                        "content_sha256": str(
                            item.get("content_sha256") or ""
                        ),
                        "text_sha256": _sha256_text(
                            str(item.get("text") or "")
                        ),
                        "role": str(item.get("role") or ""),
                        "scope_policy": str(
                            item.get("scope_policy") or ""
                        ),
                        "ingest_policy": str(
                            item.get("ingest_policy") or ""
                        ),
                        "priority": int(item.get("priority") or 0),
                        "score": item.get("score"),
                        "base_score": item.get("base_score"),
                        "bm25": item.get("bm25"),
                        "lexical_score": item.get("lexical_score"),
                        "semantic_score": item.get("semantic_score"),
                        "vector_score": item.get("vector_score"),
                        "embedding_status": item.get("embedding_status"),
                        "embedding_model": item.get("embedding_model"),
                        "rerank_rank": item.get("rerank_rank"),
                        "rerank_score": item.get("rerank_score"),
                        "rerank_status": item.get("rerank_status"),
                        "rerank_model": item.get("rerank_model"),
                        "retrieval_mode": item.get("retrieval_mode"),
                    }
                    for item in need_results
                ],
            }
        )
    return normalized


def _semantic_results_hash(
    needs: Sequence[Mapping[str, Any]],
    results: Sequence[Sequence[Mapping[str, Any]]],
) -> str:
    return _sha256_json(_selected_results_payload(needs, results))


def _context_like_fingerprint(
    needs: Sequence[Mapping[str, Any]],
    results: Sequence[Sequence[Mapping[str, Any]]],
) -> str:
    contexts = []
    for need, need_results in zip(needs, results):
        context_parts = []
        for item in need_results:
            context_parts.append(
                "\n".join(
                    [
                        (
                            f"[{item.get('path', '')}#"
                            f"{int(item.get('ordinal') or 0)}:"
                            f"{int(item.get('start_line') or 0)}-"
                            f"{int(item.get('end_line') or 0)}]"
                        ),
                        str(item.get("text") or ""),
                    ]
                )
            )
        contexts.append(
            {
                "need_id": str(need["id"]),
                "context_sha256": _sha256_text("\n\n".join(context_parts)),
            }
        )
    return _sha256_json(contexts)


def compare_legacy_and_batched_results(
    needs: Sequence[Mapping[str, Any]],
    legacy_results: Sequence[Sequence[Mapping[str, Any]]],
    batched_results: Sequence[Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Compare selected chunks and context-like assembly byte-for-byte."""

    legacy_payload = _selected_results_payload(needs, legacy_results)
    batched_payload = _selected_results_payload(needs, batched_results)
    mismatched_need_indices = [
        index
        for index, (legacy_need, batched_need) in enumerate(
            zip(legacy_payload, batched_payload)
        )
        if legacy_need != batched_need
    ]
    if len(legacy_payload) != len(batched_payload):
        mismatched_need_indices.extend(
            range(
                min(len(legacy_payload), len(batched_payload)),
                max(len(legacy_payload), len(batched_payload)),
            )
        )
    legacy_semantic = _sha256_json(legacy_payload)
    batched_semantic = _sha256_json(batched_payload)
    legacy_context = _context_like_fingerprint(needs, legacy_results)
    batched_context = _context_like_fingerprint(needs, batched_results)
    return {
        "selected_chunks_equivalent": legacy_payload == batched_payload,
        "context_like_equivalent": legacy_context == batched_context,
        "legacy_semantic_sha256": legacy_semantic,
        "batched_semantic_sha256": batched_semantic,
        "legacy_context_like_sha256": legacy_context,
        "batched_context_like_sha256": batched_context,
        "mismatched_need_indices": sorted(set(mismatched_need_indices)),
        "passed": (
            legacy_payload == batched_payload
            and legacy_context == batched_context
        ),
    }


def _phase_summary(
    *,
    phase: str,
    needs: Sequence[Mapping[str, Any]],
    results: Sequence[Sequence[Mapping[str, Any]]],
    diagnostics: Mapping[str, Any],
    provider_delta: Mapping[str, int],
    duration_ms: float,
) -> dict[str, Any]:
    top_paths = [
        str(items[0].get("path") or "") if items else None
        for items in results
    ]
    expected_paths = [str(need["expected_path"]) for need in needs]
    query_health = [
        dict(item)
        for item in diagnostics.get("queries", [])
        if isinstance(item, Mapping)
    ]
    return {
        "phase": phase,
        "duration_ms": duration_ms,
        "need_count": len(needs),
        "result_count": sum(len(items) for items in results),
        "top_path_fingerprints": [
            None if path is None else _sha256_text(path)
            for path in top_paths
        ],
        "expected_path_fingerprints": [
            _sha256_text(path) for path in expected_paths
        ],
        "top1_matches": [
            actual == expected
            for actual, expected in zip(top_paths, expected_paths)
        ],
        "top1_accuracy": round(
            sum(
                actual == expected
                for actual, expected in zip(top_paths, expected_paths)
            )
            / max(1, len(needs)),
            8,
        ),
        "all_needs_have_results": all(bool(items) for items in results),
        "semantic_results_sha256": _semantic_results_hash(needs, results),
        "context_like_sha256": _context_like_fingerprint(needs, results),
        "authority": {
            key: deepcopy(value)
            for key, value in diagnostics.items()
            if key != "queries"
        }
        | {"queries": query_health},
        "providers": dict(provider_delta),
    }


def _materialize_fixture(
    root: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    hashes: dict[str, str] = {}
    byte_count = 0
    for record in manifest["files"]:
        relative = _safe_relative_path(record["path"])
        content = str(record["content"])
        path = _contained_fixture_path(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        payload = path.read_bytes()
        hashes[relative] = hashlib.sha256(payload).hexdigest()
        byte_count += len(payload)
    return {
        "file_count": len(hashes),
        "byte_count": byte_count,
        "content_manifest_sha256": _sha256_json(hashes),
    }


def build_redacted_run_manifest(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a stable manifest without prose, prompts, paths, or credentials."""

    validation = validate_fixture_manifest(manifest)
    needs = {
        str(item["id"]): item
        for item in manifest["needs"]
    }
    return {
        "schema_version": REDACTED_MANIFEST_SCHEMA_VERSION,
        "suite": BENCHMARK_SUITE,
        "runner_version": RUNNER_VERSION,
        "fixture_id_sha256": validation["fixture_id_sha256"],
        "fixture_sha256": validation["fixture_sha256"],
        "execution_mode": "offline",
        "network_required": False,
        "provider_contracts": {
            "embedding": OfflineEmbeddingProvider.cache_identity,
            "rerank": OfflineRerankProvider.cache_identity,
        },
        "scenarios": [
            {
                "scenario_index": scenario_index,
                "scenario_fingerprint": _sha256_text(
                    str(scenario["id"])
                ),
                "need_count": len(scenario["need_ids"]),
                "need_fingerprints": [
                    _sha256_text(
                        _stable_json(
                            {
                                "id": need_id,
                                "text": str(needs[need_id]["query"]),
                            }
                        )
                    )
                    for need_id in scenario["need_ids"]
                ],
                "embedding_batch_mode": str(
                    scenario["embedding_batch_mode"]
                ),
                "single_failure_count": len(
                    scenario["single_failure_need_ids"]
                ),
                "rerank_max_concurrency": int(
                    scenario["rerank_max_concurrency"]
                ),
                "cache_phases": ["cold", "hot"],
            }
            for scenario_index, scenario in enumerate(manifest["scenarios"])
        ],
    }


def build_redacted_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return the default publishable result without source prose or paths."""

    scenarios: list[dict[str, Any]] = []
    for scenario_index, raw_scenario in enumerate(result.get("scenarios", [])):
        if not isinstance(raw_scenario, Mapping):
            continue
        phases = []
        for raw_phase in raw_scenario.get("phases", []):
            if not isinstance(raw_phase, Mapping):
                continue
            authority = raw_phase.get("authority")
            providers = raw_phase.get("providers")
            phases.append(
                {
                    "phase": str(raw_phase.get("phase") or ""),
                    "duration_ms": raw_phase.get("duration_ms"),
                    "need_count": int(raw_phase.get("need_count") or 0),
                    "result_count": int(raw_phase.get("result_count") or 0),
                    "top1_accuracy": raw_phase.get("top1_accuracy"),
                    "all_needs_have_results": bool(
                        raw_phase.get("all_needs_have_results")
                    ),
                    "semantic_results_sha256": str(
                        raw_phase.get("semantic_results_sha256") or ""
                    ),
                    "context_like_sha256": str(
                        raw_phase.get("context_like_sha256") or ""
                    ),
                    "top_path_fingerprints": list(
                        raw_phase.get("top_path_fingerprints") or []
                    ),
                    "expected_path_fingerprints": list(
                        raw_phase.get("expected_path_fingerprints") or []
                    ),
                    "authority": deepcopy(
                        dict(authority)
                        if isinstance(authority, Mapping)
                        else {}
                    ),
                    "providers": deepcopy(
                        dict(providers)
                        if isinstance(providers, Mapping)
                        else {}
                    ),
                }
            )
        scenario_id = str(raw_scenario.get("scenario_id") or "")
        scenarios.append(
            {
                "scenario_index": scenario_index,
                "scenario_fingerprint": _sha256_text(scenario_id),
                "need_count": int(raw_scenario.get("need_count") or 0),
                "embedding_batch_mode": str(
                    raw_scenario.get("embedding_batch_mode") or ""
                ),
                "single_failure_count": int(
                    raw_scenario.get("single_failure_count") or 0
                ),
                "rerank_max_concurrency": int(
                    raw_scenario.get("rerank_max_concurrency") or 0
                ),
                "iterations": int(raw_scenario.get("iterations") or 0),
                "warmup_iterations": int(
                    raw_scenario.get("warmup_iterations") or 0
                ),
                "phases": phases,
                "comparison": deepcopy(
                    dict(raw_scenario.get("comparison") or {})
                ),
                "timing_summary": deepcopy(
                    dict(raw_scenario.get("timing_summary") or {})
                ),
                "measurements": deepcopy(
                    list(raw_scenario.get("measurements") or [])
                ),
                "quality_gate": deepcopy(
                    dict(raw_scenario.get("quality_gate") or {})
                ),
                "passed": bool(raw_scenario.get("passed")),
            }
        )
    return {
        "schema_version": str(result.get("schema_version") or ""),
        "suite": str(result.get("suite") or ""),
        "runner_version": int(result.get("runner_version") or 0),
        "execution_mode": str(result.get("execution_mode") or ""),
        "network_required": bool(result.get("network_required")),
        "status": str(result.get("status") or ""),
        "passed": bool(result.get("passed")),
        "provenance": deepcopy(dict(result.get("provenance") or {})),
        "fixture": deepcopy(dict(result.get("fixture") or {})),
        "redacted_manifest": deepcopy(
            dict(result.get("redacted_manifest") or {})
        ),
        "telemetry": deepcopy(dict(result.get("telemetry") or {})),
        "scenarios": scenarios,
        "quality_gate": deepcopy(dict(result.get("quality_gate") or {})),
    }


def _timing_distribution(values: Sequence[float]) -> dict[str, Any]:
    samples = sorted(round(float(value), 6) for value in values)
    if not samples:
        raise ValueError("timing distribution requires at least one sample")

    def nearest_rank(percentile: float) -> float:
        index = max(0, math.ceil(percentile * len(samples)) - 1)
        return samples[min(index, len(samples) - 1)]

    return {
        "sample_count": len(samples),
        "samples_ms": samples,
        "min_ms": samples[0],
        "p50_ms": nearest_rank(0.50),
        "p95_ms": nearest_rank(0.95),
        "max_ms": samples[-1],
    }


def evaluate_severe_regression(
    *,
    legacy_p95_ms: float,
    batched_cold_p95_ms: float,
    multiplier: float = 2.0,
    absolute_noise_floor_ms: float = 10.0,
    enforce: bool = True,
) -> dict[str, Any]:
    """Fail only severe regressions while tolerating sub-10ms scheduler noise."""

    legacy = max(0.0, float(legacy_p95_ms))
    batched = max(0.0, float(batched_cold_p95_ms))
    allowed = max(
        legacy * float(multiplier),
        legacy + float(absolute_noise_floor_ms),
    )
    observed_passed = batched <= allowed
    return {
        "legacy_p95_ms": round(legacy, 6),
        "batched_cold_p95_ms": round(batched, 6),
        "multiplier": float(multiplier),
        "absolute_noise_floor_ms": float(absolute_noise_floor_ms),
        "maximum_allowed_batched_p95_ms": round(allowed, 6),
        "enforced": bool(enforce),
        "observed_passed": observed_passed,
        "passed": observed_passed if enforce else True,
    }


def _build_offline_index(
    database_path: Path,
    *,
    embedding: OfflineEmbeddingProvider,
    rerank: OfflineRerankProvider,
    settings: Mapping[str, Any],
    rerank_max_concurrency: int,
    batched: bool,
) -> AuthorityIndex:
    return AuthorityIndex(
        database_path,
        embedding_provider=embedding,
        embedding_batch_provider=(
            embedding.embed_many if batched else None
        ),
        embedding_model=OfflineEmbeddingProvider.cache_identity,
        rerank_provider=rerank,
        rerank_model=OfflineRerankProvider.cache_identity,
        embedding_batch_size=int(settings["embedding_batch_size"]),
        embedding_batch_max_chars=int(
            settings["embedding_batch_max_chars"]
        ),
        rerank_max_concurrency=rerank_max_concurrency,
        query_embedding_cache_size=0,
    )


def _legacy_diagnostics(
    results: Sequence[Sequence[Mapping[str, Any]]],
    provider_delta: Mapping[str, int],
) -> dict[str, Any]:
    query_health = []
    for items in results:
        embedding_status = (
            str(items[0].get("embedding_status") or "unknown")
            if items
            else "unknown"
        )
        query_health.append(
            AuthorityIndex._result_health(
                items,
                embedding_status,
                source="legacy_serial",
            )
        )
    return {
        "query_count": len(results),
        "candidate_cache_hits": 0,
        "search_singleflight_waits": 0,
        "embedding_cache_hits": 0,
        "embedding_singleflight_waits": 0,
        "embedding_batch_calls": 0,
        "embedding_batch_failures": 0,
        "embedding_batch_ms": 0.0,
        "embedding_single_fallbacks": 0,
        "embedding_single_calls": int(
            provider_delta.get("embedding_single_calls", 0)
        ),
        "rerank_max_concurrency": 1,
        "queries": query_health,
    }


def _run_scenario_once(
    *,
    run_root: Path,
    project_root: Path,
    manifest: Mapping[str, Any],
    scenario: Mapping[str, Any],
    run_id: str,
    rerank_delay_ms: int,
    run_label: str,
) -> dict[str, Any]:
    needs_by_id = {
        str(item["id"]): item
        for item in manifest["needs"]
    }
    selected_needs = [
        needs_by_id[str(need_id)]
        for need_id in scenario["need_ids"]
    ]
    failure_markers = [
        str(needs_by_id[str(need_id)]["query"]).split()[0]
        for need_id in scenario["single_failure_need_ids"]
    ]
    scenario_id = str(scenario["id"])
    legacy_isolation_id = _offline_provider_isolation_id(
        run_id=run_id,
        scenario_id=scenario_id,
        run_label=run_label,
        lane="legacy",
    )
    new_isolation_id = _offline_provider_isolation_id(
        run_id=run_id,
        scenario_id=scenario_id,
        run_label=run_label,
        lane="new",
    )
    legacy_embedding = OfflineEmbeddingProvider(
        batch_mode="ok",
        failure_markers=failure_markers,
        isolation_id=legacy_isolation_id,
    )
    new_embedding = OfflineEmbeddingProvider(
        batch_mode=str(scenario["embedding_batch_mode"]),
        failure_markers=failure_markers,
        isolation_id=new_isolation_id,
    )
    legacy_rerank = OfflineRerankProvider(
        expected_parallelism=1,
        delay_ms=rerank_delay_ms,
        isolation_id=legacy_isolation_id,
    )
    expected_parallelism = min(
        len(selected_needs),
        int(scenario["rerank_max_concurrency"]),
    )
    new_rerank = OfflineRerankProvider(
        expected_parallelism=min(
            len(selected_needs),
            int(scenario["rerank_max_concurrency"]),
        ),
        delay_ms=rerank_delay_ms,
        isolation_id=new_isolation_id,
    )
    settings = manifest["settings"]
    legacy_index = _build_offline_index(
        run_root / f"{scenario['id']}-{run_label}-legacy.sqlite3",
        embedding=legacy_embedding,
        rerank=legacy_rerank,
        settings=settings,
        rerank_max_concurrency=1,
        batched=False,
    )
    new_index = _build_offline_index(
        run_root / f"{scenario['id']}-{run_label}-new.sqlite3",
        embedding=new_embedding,
        rerank=new_rerank,
        settings=settings,
        rerank_max_concurrency=int(scenario["rerank_max_concurrency"]),
        batched=True,
    )
    sources = [
        AuthoritySource.from_mapping(item)
        for item in manifest["sources"]
    ]
    legacy_refresh, legacy_refresh_duration_ms = _timed_call(
        lambda: legacy_index.refresh(project_root, sources)
    )
    new_refresh, new_refresh_duration_ms = _timed_call(
        lambda: new_index.refresh(project_root, sources)
    )
    legacy_embedding.begin_query_phase()
    new_embedding.begin_query_phase()

    queries = [str(need["query"]) for need in selected_needs]
    limit = int(settings["limit"])
    legacy_before = legacy_embedding.snapshot() | legacy_rerank.snapshot()
    legacy_results, legacy_duration_ms = _timed_call(
        lambda: [
            legacy_index._search_legacy(
                query,
                limit=limit,
                use_candidate_cache=False,
            )
            for query in queries
        ]
    )
    legacy_after = legacy_embedding.snapshot() | legacy_rerank.snapshot()
    legacy_delta = _counter_delta(legacy_before, legacy_after)
    phases: list[dict[str, Any]] = [
        _phase_summary(
            phase="legacy_serial",
            needs=selected_needs,
            results=legacy_results,
            diagnostics=_legacy_diagnostics(
                legacy_results,
                legacy_delta,
            ),
            provider_delta=legacy_delta,
            duration_ms=legacy_duration_ms,
        )
    ]
    new_results_by_phase: list[list[list[dict[str, Any]]]] = []
    for phase in ("cold", "hot"):
        provider_before = new_embedding.snapshot() | new_rerank.snapshot()
        results, duration_ms = _timed_call(
            lambda: new_index.search_many(
                queries,
                limit=limit,
                rerank_max_concurrency=int(
                    scenario["rerank_max_concurrency"]
                ),
            )
        )
        new_results_by_phase.append(results)
        provider_after = new_embedding.snapshot() | new_rerank.snapshot()
        phases.append(
            _phase_summary(
                phase=phase,
                needs=selected_needs,
                results=results,
                diagnostics=new_index.last_search_diagnostics(),
                provider_delta=_counter_delta(
                    provider_before,
                    provider_after,
                ),
                duration_ms=duration_ms,
            )
        )

    legacy, cold, hot = phases
    cold_results, _hot_results = new_results_by_phase
    comparison = compare_legacy_and_batched_results(
        selected_needs,
        legacy_results,
        cold_results,
    )
    fallback_mode = str(scenario["embedding_batch_mode"]) != "ok"
    cold_authority = cold["authority"]
    hot_authority = hot["authority"]
    configured_parallelism = int(scenario["rerank_max_concurrency"])
    cold_query_health = cold_authority["queries"]
    degraded_queries = [
        item
        for item in cold_query_health
        if item.get("embedding_status") == "failed"
    ]
    quality = {
        "top1_accuracy_required": 1.0,
        "legacy_top1_accuracy": legacy["top1_accuracy"],
        "cold_top1_accuracy": cold["top1_accuracy"],
        "hot_top1_accuracy": hot["top1_accuracy"],
        "cold_hot_semantic_equivalence": (
            cold["semantic_results_sha256"]
            == hot["semantic_results_sha256"]
        ),
        "cold_hot_context_like_equivalence": (
            cold["context_like_sha256"] == hot["context_like_sha256"]
        ),
        "legacy_batched_selected_chunks_equivalent": comparison[
            "selected_chunks_equivalent"
        ],
        "legacy_batched_context_like_equivalent": comparison[
            "context_like_equivalent"
        ],
        "cold_batch_call_count": int(
            cold_authority.get("embedding_batch_calls", 0)
        ),
        "cold_batch_failure_count": int(
            cold_authority.get("embedding_batch_failures", 0)
        ),
        "cold_single_fallback_count": int(
            cold_authority.get("embedding_single_fallbacks", 0)
        ),
        "cold_candidate_cache_hits": int(
            cold_authority.get("candidate_cache_hits", 0)
        ),
        "hot_candidate_cache_hits": int(
            hot_authority.get("candidate_cache_hits", 0)
        ),
        "expected_parallelism": expected_parallelism,
        "configured_parallelism": configured_parallelism,
        "observed_parallelism": int(
            cold["providers"].get("rerank_max_active", 0)
        ),
        "degraded_query_count": len(degraded_queries),
        "degraded_queries_preserved_results": all(
            int(item.get("result_count", 0)) > 0
            and item.get("miss_confirmed") is False
            for item in degraded_queries
        ),
    }
    if fallback_mode:
        expected_failures = len(scenario["single_failure_need_ids"])
        passed = (
            legacy["top1_accuracy"] == 1.0
            and cold["top1_accuracy"] == 1.0
            and hot["top1_accuracy"] == 1.0
            and quality["cold_hot_semantic_equivalence"]
            and quality["cold_hot_context_like_equivalence"]
            and comparison["passed"]
            and quality["cold_batch_call_count"] == 1
            and quality["cold_batch_failure_count"] == 1
            and quality["cold_single_fallback_count"]
            == len(selected_needs)
            and quality["degraded_query_count"] == expected_failures
            and quality["degraded_queries_preserved_results"]
            and quality["observed_parallelism"] >= expected_parallelism
            and quality["observed_parallelism"] <= configured_parallelism
            and quality["hot_candidate_cache_hits"]
            == len(selected_needs) - expected_failures
        )
    else:
        passed = (
            legacy["top1_accuracy"] == 1.0
            and cold["top1_accuracy"] == 1.0
            and hot["top1_accuracy"] == 1.0
            and quality["cold_hot_semantic_equivalence"]
            and quality["cold_hot_context_like_equivalence"]
            and comparison["passed"]
            and quality["cold_batch_call_count"] == 1
            and quality["cold_batch_failure_count"] == 0
            and quality["cold_single_fallback_count"] == 0
            and quality["cold_candidate_cache_hits"] == 0
            and quality["hot_candidate_cache_hits"] == len(selected_needs)
            and quality["observed_parallelism"] >= expected_parallelism
            and quality["observed_parallelism"] <= configured_parallelism
            and quality["degraded_query_count"] == 0
        )
    quality["passed"] = passed

    return {
        "scenario_id": str(scenario["id"]),
        "need_count": len(selected_needs),
        "embedding_batch_mode": str(
            scenario["embedding_batch_mode"]
        ),
        "single_failure_count": len(
            scenario["single_failure_need_ids"]
        ),
        "index_refresh": {
            "legacy_duration_ms": legacy_refresh_duration_ms,
            "new_duration_ms": new_refresh_duration_ms,
            "legacy_stats": legacy_refresh,
            "new_stats": new_refresh,
        },
        "phases": phases,
        "comparison": comparison,
        "quality_gate": quality,
        "passed": passed,
    }


def _run_scenario(
    *,
    run_root: Path,
    project_root: Path,
    manifest: Mapping[str, Any],
    scenario: Mapping[str, Any],
    run_id: str,
    rerank_delay_ms: int,
    iterations: int,
    warmup_iterations: int,
) -> dict[str, Any]:
    warmups = [
        _run_scenario_once(
            run_root=run_root,
            project_root=project_root,
            manifest=manifest,
            scenario=scenario,
            run_id=run_id,
            rerank_delay_ms=rerank_delay_ms,
            run_label=f"warmup-{index:03d}",
        )
        for index in range(warmup_iterations)
    ]
    measured = [
        _run_scenario_once(
            run_root=run_root,
            project_root=project_root,
            manifest=manifest,
            scenario=scenario,
            run_id=run_id,
            rerank_delay_ms=rerank_delay_ms,
            run_label=f"measure-{index:03d}",
        )
        for index in range(iterations)
    ]
    representative = measured[0]
    phase_names = [
        str(phase["phase"]) for phase in representative["phases"]
    ]
    timing_summary = {
        phase_name: _timing_distribution(
            [
                float(
                    next(
                        phase["duration_ms"]
                        for phase in run["phases"]
                        if phase["phase"] == phase_name
                    )
                )
                for run in measured
            ]
        )
        for phase_name in phase_names
    }
    refresh_timing_summary = {
        "legacy": _timing_distribution(
            [
                float(run["index_refresh"]["legacy_duration_ms"])
                for run in measured
            ]
        ),
        "new": _timing_distribution(
            [
                float(run["index_refresh"]["new_duration_ms"])
                for run in measured
            ]
        ),
    }
    quality = deepcopy(representative["quality_gate"])
    all_runs = [*warmups, *measured]
    quality["all_warmups_passed"] = all(
        bool(run["passed"]) for run in warmups
    )
    quality["all_iterations_passed"] = all(
        bool(run["passed"]) for run in measured
    )
    quality["all_comparisons_passed"] = all(
        bool(run["comparison"]["passed"]) for run in all_runs
    )
    performance_gate = evaluate_severe_regression(
        legacy_p95_ms=timing_summary["legacy_serial"]["p95_ms"],
        batched_cold_p95_ms=timing_summary["cold"]["p95_ms"],
        enforce=rerank_delay_ms >= 5,
    )
    quality["severe_regression_gate"] = performance_gate
    passed = (
        quality["all_warmups_passed"]
        and quality["all_iterations_passed"]
        and quality["all_comparisons_passed"]
        and performance_gate["passed"]
    )
    quality["passed"] = passed
    return {
        "scenario_id": representative["scenario_id"],
        "need_count": representative["need_count"],
        "embedding_batch_mode": representative["embedding_batch_mode"],
        "single_failure_count": representative["single_failure_count"],
        "rerank_max_concurrency": int(
            scenario["rerank_max_concurrency"]
        ),
        "iterations": iterations,
        "warmup_iterations": warmup_iterations,
        "index_refresh": {
            **representative["index_refresh"],
            "timing_summary": refresh_timing_summary,
        },
        "phases": representative["phases"],
        "comparison": representative["comparison"],
        "timing_summary": timing_summary,
        "measurements": [
            {
                "iteration": index,
                "passed": bool(run["passed"]),
                "comparison_passed": bool(run["comparison"]["passed"]),
                "phase_durations_ms": {
                    str(phase["phase"]): float(phase["duration_ms"])
                    for phase in run["phases"]
                },
            }
            for index, run in enumerate(measured)
        ],
        "quality_gate": quality,
        "passed": passed,
    }


def _git_output(*arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value or None


def _plugin_version() -> str | None:
    manifest_path = ROOT / ".codex-plugin" / "plugin.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    value = str(payload.get("version") or "").strip()
    return value or None


def _source_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _collect_provenance(
    *,
    manifest: Mapping[str, Any],
    validation: Mapping[str, Any],
    iterations: int,
    warmup_iterations: int,
    rerank_delay_ms: int,
    run_id: str,
    started_at_utc: str,
) -> dict[str, Any]:
    tracked_status = _git_output(
        "status",
        "--porcelain",
        "--untracked-files=no",
    )
    parameters = {
        "iterations": iterations,
        "warmup_iterations": warmup_iterations,
        "rerank_delay_ms": rerank_delay_ms,
        "workspace_mode": "temporary",
        "execution_mode": "offline",
    }
    config_payload = {
        "settings": deepcopy(dict(manifest["settings"])),
        "scenarios": [
            {
                "need_count": len(scenario["need_ids"]),
                "embedding_batch_mode": str(
                    scenario["embedding_batch_mode"]
                ),
                "single_failure_count": len(
                    scenario["single_failure_need_ids"]
                ),
                "rerank_max_concurrency": int(
                    scenario["rerank_max_concurrency"]
                ),
            }
            for scenario in manifest["scenarios"]
        ],
    }
    effective_config = {
        **config_payload,
        "runtime": parameters,
    }
    return {
        "schema_version": "plot-rag-v15-performance-provenance/v1",
        "run_id": run_id,
        "started_at_utc": started_at_utc,
        "git": {
            "commit": _git_output("rev-parse", "HEAD"),
            "tracked_dirty": bool(tracked_status),
        },
        "plugin_version": _plugin_version(),
        "runner_version": RUNNER_VERSION,
        "source_sha256": {
            "runner": _source_sha256(Path(__file__).resolve()),
            "authority": _source_sha256(
                ROOT / "scripts" / "longform" / "authority.py"
            ),
        },
        "fixture_sha256": str(validation["fixture_sha256"]),
        "config_sha256": _sha256_json(config_payload),
        "effective_config_sha256": _sha256_json(effective_config),
        "parameters": parameters,
        "parameters_sha256": _sha256_json(parameters),
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "cpu": {
            "logical_count": os.cpu_count(),
            "identifier": platform.processor() or None,
        },
    }


def run_v15_performance_benchmark(
    manifest_or_path: Mapping[str, Any] | str | Path = DEFAULT_FIXTURE,
    *,
    workspace_parent: str | Path | None = None,
    rerank_delay_ms: int | None = None,
    iterations: int = DEFAULT_ITERATIONS,
    warmup_iterations: int = DEFAULT_WARMUP_ITERATIONS,
) -> dict[str, Any]:
    """Run repeated legacy/new offline scenarios and return safe telemetry."""

    benchmark_started = time.perf_counter()
    started_at = datetime.now(timezone.utc)
    run_id = uuid.uuid4().hex
    if (
        isinstance(iterations, bool)
        or not isinstance(iterations, int)
        or iterations < 1
    ):
        raise ValueError("iterations must be a positive integer")
    if (
        isinstance(warmup_iterations, bool)
        or not isinstance(warmup_iterations, int)
        or warmup_iterations < 0
    ):
        raise ValueError("warmup_iterations must be a non-negative integer")
    manifest = (
        load_fixture_manifest(manifest_or_path)
        if isinstance(manifest_or_path, (str, Path))
        else deepcopy(dict(manifest_or_path))
    )
    validation = validate_fixture_manifest(manifest)
    configured_delay = int(manifest["settings"]["rerank_delay_ms"])
    if rerank_delay_ms is None:
        effective_delay = configured_delay
    else:
        if isinstance(rerank_delay_ms, bool) or int(rerank_delay_ms) < 0:
            raise ValueError("rerank_delay_ms must be non-negative")
        effective_delay = int(rerank_delay_ms)
    parent = (
        None
        if workspace_parent is None
        else str(Path(workspace_parent))
    )
    with tempfile.TemporaryDirectory(
        prefix="plot-rag-v15-benchmark-",
        dir=parent,
    ) as temporary:
        run_root = Path(temporary)
        project_root = run_root / "synthetic-project"
        materialized, materialize_duration_ms = _timed_call(
            lambda: _materialize_fixture(project_root, manifest)
        )
        scenarios = [
            _run_scenario(
                run_root=run_root,
                project_root=project_root,
                manifest=manifest,
                scenario=scenario,
                run_id=run_id,
                rerank_delay_ms=effective_delay,
                iterations=iterations,
                warmup_iterations=warmup_iterations,
            )
            for scenario in manifest["scenarios"]
        ]

    covered_need_counts = sorted(
        {
            int(scenario["need_count"])
            for scenario in scenarios
            if scenario["embedding_batch_mode"] == "ok"
        }
    )
    passed = (
        all(bool(scenario["passed"]) for scenario in scenarios)
        and {1, 3, 5}.issubset(covered_need_counts)
        and any(
            scenario["embedding_batch_mode"] != "ok"
            for scenario in scenarios
        )
        and all(
            bool(scenario["comparison"]["passed"])
            for scenario in scenarios
        )
    )
    telemetry_stages: list[dict[str, Any]] = [
        {
            "stage": "fixture.materialize",
            "duration_ms": materialize_duration_ms,
            **materialized,
        }
    ]
    for scenario_index, scenario in enumerate(scenarios):
        scenario_label = f"scenario-{scenario_index:03d}"
        for path_name in ("legacy", "new"):
            refresh_timing = scenario["index_refresh"]["timing_summary"][
                path_name
            ]
            refresh_stats = scenario["index_refresh"][
                f"{path_name}_stats"
            ]
            telemetry_stages.append(
                {
                    "stage": (
                        f"{scenario_label}."
                        f"{path_name}_index_refresh"
                    ),
                    **refresh_timing,
                    "duration_ms": refresh_timing["p50_ms"],
                    "files_hashed": refresh_stats["files_hashed"],
                    "chunks_written": refresh_stats["chunks_written"],
                }
            )
        telemetry_stages.extend(
            {
                "stage": (
                    f"{scenario_label}."
                    f"{phase['phase']}_search"
                ),
                **scenario["timing_summary"][str(phase["phase"])],
                "duration_ms": scenario["timing_summary"][
                    str(phase["phase"])
                ]["p50_ms"],
                "need_count": phase["need_count"],
                "candidate_cache_hits": phase["authority"].get(
                    "candidate_cache_hits",
                    0,
                ),
                "embedding_batch_calls": phase["authority"].get(
                    "embedding_batch_calls",
                    0,
                ),
                "embedding_single_fallbacks": phase["authority"].get(
                    "embedding_single_fallbacks",
                    0,
                ),
                "rerank_max_active": phase["providers"].get(
                    "rerank_max_active",
                    0,
                ),
            }
            for phase in scenario["phases"]
        )

    started_at_utc = started_at.isoformat().replace("+00:00", "Z")
    provenance = _collect_provenance(
        manifest=manifest,
        validation=validation,
        iterations=iterations,
        warmup_iterations=warmup_iterations,
        rerank_delay_ms=effective_delay,
        run_id=run_id,
        started_at_utc=started_at_utc,
    )
    provenance["finished_at_utc"] = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    provenance["benchmark_wall_ms"] = round(
        (time.perf_counter() - benchmark_started) * 1000.0,
        6,
    )
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "suite": BENCHMARK_SUITE,
        "runner_version": RUNNER_VERSION,
        "execution_mode": "offline",
        "network_required": False,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "provenance": provenance,
        "fixture": validation,
        "redacted_manifest": build_redacted_run_manifest(manifest),
        "telemetry": {"stages": telemetry_stages},
        "scenarios": scenarios,
        "quality_gate": {
            "covered_need_counts": covered_need_counts,
            "required_need_counts": [1, 3, 5],
            "cold_and_hot_cache_required": True,
            "batch_embedding_required": True,
            "per_need_fallback_required": True,
            "parallel_rerank_required": True,
            "legacy_batched_equivalence_required": True,
            "severe_regression_gate_required": True,
            "iterations": iterations,
            "warmup_iterations": warmup_iterations,
            "all_scenarios_passed": all(
                bool(scenario["passed"]) for scenario in scenarios
            ),
            "all_comparisons_passed": all(
                bool(scenario["comparison"]["passed"])
                for scenario in scenarios
            ),
            "passed": passed,
        },
    }


def _parse_artifact_started_at_utc(value: str) -> datetime:
    """Parse the deliberately narrow RFC3339 subset used by artifacts."""

    match = (
        _ARTIFACT_RFC3339_RE.fullmatch(value)
        if isinstance(value, str)
        else None
    )
    if match is None:
        raise ValueError(
            f"{ARTIFACT_TIMESTAMP_INVALID_CODE}: started_at_utc must use "
            "YYYY-MM-DDTHH:MM:SS[.ffffff]Z or an explicit +HH:MM/-HH:MM "
            "offset"
        )
    fraction = str(match.group("fraction") or "")
    microsecond = (
        int(fraction[1:].ljust(6, "0"))
        if fraction
        else 0
    )
    try:
        if match.group("zone") == "Z":
            parsed_zone = timezone.utc
        else:
            offset = timedelta(
                hours=int(match.group("offset_hour")),
                minutes=int(match.group("offset_minute")),
            )
            if match.group("offset_sign") == "-":
                offset = -offset
            parsed_zone = timezone(offset)
        parsed = datetime(
            year=int(match.group("year")),
            month=int(match.group("month")),
            day=int(match.group("day")),
            hour=int(match.group("hour")),
            minute=int(match.group("minute")),
            second=int(match.group("second")),
            microsecond=microsecond,
            tzinfo=parsed_zone,
        )
    except (OverflowError, ValueError) as error:
        raise ValueError(
            f"{ARTIFACT_TIMESTAMP_INVALID_CODE}: started_at_utc is not a "
            "valid RFC3339 instant"
        ) from error
    return parsed.astimezone(timezone.utc)


def create_run_artifact_directory(
    root: str | Path = DEFAULT_ARTIFACT_ROOT,
    *,
    run_id: str | None = None,
    started_at_utc: str | None = None,
) -> tuple[Path, str, str]:
    """Create one timestamped, collision-resistant artifact directory."""

    artifact_root = Path(root)
    if started_at_utc is None:
        started_at = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        started_at = _parse_artifact_started_at_utc(started_at_utc)
    artifact_root.mkdir(parents=True, exist_ok=True)
    timestamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    for _attempt in range(32):
        candidate_id = run_id or uuid.uuid4().hex
        if not re.fullmatch(r"[0-9a-f]{32}", candidate_id):
            raise ValueError("run_id must be 32 lowercase hexadecimal characters")
        destination = artifact_root / f"{timestamp}-{candidate_id[:12]}"
        try:
            destination.mkdir()
        except FileExistsError:
            if run_id is not None:
                raise
            continue
        return (
            destination,
            candidate_id,
            started_at.isoformat().replace("+00:00", "Z"),
        )
    raise FileExistsError("failed to allocate a unique benchmark run directory")


def write_json(
    path: str | Path,
    value: Any,
    *,
    pretty: bool = True,
    overwrite: bool = False,
) -> None:
    """Write one UTF-8 JSON artifact without replacing prior benchmark logs."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        if pretty
        else _stable_json(value)
    )
    if not overwrite:
        with destination.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")
        return
    temporary = destination.with_name(
        destination.name + f".{uuid.uuid4().hex}.tmp"
    )
    temporary.write_text(text + "\n", encoding="utf-8", newline="\n")
    temporary.replace(destination)
