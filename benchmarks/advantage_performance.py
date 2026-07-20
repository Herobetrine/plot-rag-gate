"""Advantage retrieval performance and quality benchmark.

The default lane is deterministic and offline.  It exercises the production
``AuthorityIndex`` with the same provider-aware singleton-exact decision used
by Prepare v2, an immutable accepted-source manifest, the query/rerank exact
caches, and the persistent candidate cache.

An opt-in SiliconFlow lane is available when ``SILICONFLOW_API_KEY`` exists.
Neither fixture prose nor environment values are copied into the report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import state_rag  # noqa: E402
import v1_runtime  # noqa: E402
from longform.authority import AuthorityIndex, AuthoritySource  # noqa: E402

from benchmarks.v15_performance import (  # noqa: E402
    OfflineEmbeddingProvider,
    OfflineRerankProvider,
    _timing_distribution,
)


BENCHMARK_SUITE = "plot-rag-advantage-performance"
FIXTURE_SCHEMA_VERSION = "plot-rag-advantage-performance-fixture/v1"
RESULT_SCHEMA_VERSION = "plot-rag-advantage-performance-result/v1"
RUNNER_VERSION = 1
DEFAULT_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "advantage_prompts.v1.jsonl"
)
DEFAULT_ITERATIONS = 5
DEFAULT_WARMUP_ITERATIONS = 1
DEFAULT_LIMIT = 2
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
SILICONFLOW_EMBEDDING_MODEL = "BAAI/bge-m3"
SILICONFLOW_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
SILICONFLOW_API_KEY_ENV = "SILICONFLOW_API_KEY"
SUPPORTED_PROFILES = frozenset(
    {
        "inheritance",
        "resource_transformer",
        "growth_relic",
        "pocket_domain",
        "companion_mentor",
    }
)
REQUIRED_CASE_FIELDS = frozenset(
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
    }
)
_SAFE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,95}\Z")


class AdvantageBenchmarkFixtureError(ValueError):
    """The Advantage benchmark fixture is malformed."""


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_text(_stable_json(value))


def _string_list(
    value: Any,
    *,
    field: str,
    case_id: str,
) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise AdvantageBenchmarkFixtureError(
            f"{case_id}.{field} must be a non-empty string array"
        )
    normalized = [item.strip() for item in value]
    if len(normalized) != len(set(normalized)):
        raise AdvantageBenchmarkFixtureError(
            f"{case_id}.{field} must not contain duplicates"
        )
    return normalized


def load_advantage_fixture(
    path: str | Path = DEFAULT_FIXTURE,
) -> list[dict[str, Any]]:
    """Load and validate the checked-in JSONL fixture."""

    fixture_path = Path(path)
    records: list[dict[str, Any]] = []
    try:
        lines = fixture_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as error:
        raise AdvantageBenchmarkFixtureError(
            "Advantage benchmark fixture is not readable"
        ) from error
    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as error:
            raise AdvantageBenchmarkFixtureError(
                f"fixture line {line_number} is invalid JSON"
            ) from error
        if not isinstance(value, dict):
            raise AdvantageBenchmarkFixtureError(
                f"fixture line {line_number} must be an object"
            )
        records.append(dict(value))
    validate_advantage_fixture(records)
    return records


def validate_advantage_fixture(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate stable IDs, expected facts, and mandatory context sections."""

    if not records:
        raise AdvantageBenchmarkFixtureError(
            "Advantage benchmark fixture must contain at least one case"
        )
    case_ids: set[str] = set()
    profiles: set[str] = set()
    normalized_records: list[dict[str, Any]] = []
    for index, raw in enumerate(records):
        if not isinstance(raw, Mapping):
            raise AdvantageBenchmarkFixtureError(
                f"fixture case {index} must be an object"
            )
        missing = sorted(REQUIRED_CASE_FIELDS - set(raw))
        if missing:
            raise AdvantageBenchmarkFixtureError(
                f"fixture case {index} is missing fields: {', '.join(missing)}"
            )
        if raw.get("schema_version") != FIXTURE_SCHEMA_VERSION:
            raise AdvantageBenchmarkFixtureError(
                f"fixture case {index} has an unsupported schema_version"
            )
        case_id = str(raw.get("case_id") or "").strip()
        if not _SAFE_ID_RE.fullmatch(case_id):
            raise AdvantageBenchmarkFixtureError(
                f"fixture case {index} has an invalid case_id"
            )
        if case_id in case_ids:
            raise AdvantageBenchmarkFixtureError(
                f"duplicate Advantage fixture case_id: {case_id}"
            )
        case_ids.add(case_id)
        profile = str(raw.get("profile") or "").strip()
        if profile not in SUPPORTED_PROFILES:
            raise AdvantageBenchmarkFixtureError(
                f"{case_id}.profile is unsupported: {profile}"
            )
        profiles.add(profile)
        prompt = str(raw.get("prompt") or "").strip()
        authority_text = str(raw.get("authority_text") or "")
        if not prompt or len(prompt) > 4_000:
            raise AdvantageBenchmarkFixtureError(
                f"{case_id}.prompt must contain 1 to 4000 characters"
            )
        if not authority_text.strip() or len(authority_text) > 16_000:
            raise AdvantageBenchmarkFixtureError(
                f"{case_id}.authority_text must contain 1 to 16000 characters"
            )
        advantage_ids = _string_list(
            raw.get("expected_advantage_ids"),
            field="expected_advantage_ids",
            case_id=case_id,
        )
        module_ids = _string_list(
            raw.get("expected_module_ids"),
            field="expected_module_ids",
            case_id=case_id,
        )
        critical_facts = _string_list(
            raw.get("critical_facts"),
            field="critical_facts",
            case_id=case_id,
        )
        mandatory_sections = _string_list(
            raw.get("mandatory_sections"),
            field="mandatory_sections",
            case_id=case_id,
        )
        authority_folded = authority_text.casefold()
        required_markers = [
            *advantage_ids,
            *module_ids,
            *critical_facts,
            *(f"[{section}]" for section in mandatory_sections),
        ]
        missing_markers = [
            marker
            for marker in required_markers
            if marker.casefold() not in authority_folded
        ]
        if missing_markers:
            raise AdvantageBenchmarkFixtureError(
                f"{case_id}.authority_text is missing declared markers"
            )
        normalized_records.append(
            {
                "schema_version": FIXTURE_SCHEMA_VERSION,
                "case_id": case_id,
                "profile": profile,
                "prompt": prompt,
                "expected_advantage_ids": advantage_ids,
                "expected_module_ids": module_ids,
                "critical_facts": critical_facts,
                "mandatory_sections": mandatory_sections,
                "authority_text": authority_text.replace("\r\n", "\n"),
            }
        )
    if profiles != SUPPORTED_PROFILES:
        missing_profiles = sorted(SUPPORTED_PROFILES - profiles)
        raise AdvantageBenchmarkFixtureError(
            "fixture does not cover every first-wave profile: "
            + ", ".join(missing_profiles)
        )
    return {
        "status": "valid",
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "suite": BENCHMARK_SUITE,
        "case_count": len(normalized_records),
        "profiles": sorted(profiles),
        "fixture_sha256": _sha256_json(normalized_records),
        "prompt_set_sha256": _sha256_json(
            [
                {
                    "case_id": record["case_id"],
                    "prompt_sha256": _sha256_text(record["prompt"]),
                }
                for record in normalized_records
            ]
        ),
        "critical_fact_count": sum(
            len(record["critical_facts"]) for record in normalized_records
        ),
        "mandatory_section_count": sum(
            len(record["mandatory_sections"])
            for record in normalized_records
        ),
    }


def _siliconflow_embedding_service() -> state_rag.ServiceConfig:
    return state_rag.ServiceConfig(
        name="embedding",
        enabled=True,
        base_url=SILICONFLOW_BASE_URL,
        model=SILICONFLOW_EMBEDDING_MODEL,
        api_key_env=SILICONFLOW_API_KEY_ENV,
        api_key_required=True,
        endpoint="embeddings",
        timeout_seconds=30.0,
    )


def _siliconflow_rerank_service() -> state_rag.ServiceConfig:
    return state_rag.ServiceConfig(
        name="rerank",
        enabled=True,
        base_url=SILICONFLOW_BASE_URL,
        model=SILICONFLOW_RERANK_MODEL,
        api_key_env=SILICONFLOW_API_KEY_ENV,
        api_key_required=True,
        endpoint="rerank",
        timeout_seconds=30.0,
    )


def _embedding_contract() -> dict[str, Any]:
    service = _siliconflow_embedding_service()
    batch_exact = v1_runtime._embedding_batch_is_exact(service)
    return {
        "provider": "siliconflow",
        "base_url_host": "api.siliconflow.cn",
        "model": service.model,
        "input_semantics": (
            "batch_independent" if batch_exact else "singleton_exact"
        ),
        "batch_provider_enabled": bool(batch_exact),
        "embedding_single_max_concurrency": 4 if not batch_exact else 1,
    }


class _OfflineSingletonEmbedding:
    """Counted deterministic embedding with a singleton-exact cache identity."""

    def __init__(self, *, isolation_id: str, delay_ms: int = 1) -> None:
        self._delegate = OfflineEmbeddingProvider(
            batch_mode="ok",
            failure_markers=[],
            isolation_id=isolation_id,
        )
        self.delay_seconds = max(0, int(delay_ms)) / 1000.0
        self.cache_identity = _stable_json(
            {
                "benchmark": BENCHMARK_SUITE,
                "transport": "offline_deterministic",
                "provider_identity": self._delegate.cache_identity,
                "model": SILICONFLOW_EMBEDDING_MODEL,
                "input_semantics": "singleton_exact",
            }
        )
        self._lock = threading.Lock()
        self._active = 0
        self._max_active = 0

    def __call__(self, text: str) -> Sequence[float]:
        with self._lock:
            self._active += 1
            self._max_active = max(self._max_active, self._active)
        try:
            if self.delay_seconds:
                time.sleep(self.delay_seconds)
            return self._delegate(text)
        finally:
            with self._lock:
                self._active -= 1

    def snapshot(self) -> dict[str, int]:
        delegate = self._delegate.snapshot()
        with self._lock:
            return {
                "embedding_requests": int(
                    delegate["embedding_single_calls"]
                ),
                "embedding_http_requests": 0,
                "embedding_retries": 0,
                "embedding_failures": int(
                    delegate["embedding_single_failures"]
                ),
                "embedding_max_active": self._max_active,
            }


class _OfflineExactRerank:
    """Counted deterministic rerank provider with exact-cache identity."""

    def __init__(
        self,
        *,
        isolation_id: str,
        expected_parallelism: int,
        delay_ms: int = 1,
    ) -> None:
        self._delegate = OfflineRerankProvider(
            expected_parallelism=max(1, expected_parallelism),
            delay_ms=max(0, int(delay_ms)),
            isolation_id=isolation_id,
        )
        self.cache_identity = _stable_json(
            {
                "benchmark": BENCHMARK_SUITE,
                "transport": "offline_deterministic",
                "provider_identity": self._delegate.cache_identity,
                "model": SILICONFLOW_RERANK_MODEL,
                "normalization": "exact_ordered_documents",
            }
        )

    def __call__(
        self,
        query: str,
        documents: Sequence[str],
        top_n: int,
    ) -> Sequence[tuple[int, float]]:
        return self._delegate(query, documents, top_n)

    def snapshot(self) -> dict[str, int]:
        delegate = self._delegate.snapshot()
        return {
            "rerank_requests": int(delegate["rerank_calls"]),
            "rerank_http_requests": 0,
            "rerank_retries": 0,
            "rerank_failures": 0,
            "rerank_max_active": int(delegate["rerank_max_active"]),
        }


class _LiveSingletonEmbedding:
    """SiliconFlow singleton wrapper with logical and HTTP request telemetry."""

    def __init__(self, *, isolation_id: str) -> None:
        self.service = _siliconflow_embedding_service()
        self.cache_identity = _stable_json(
            {
                "benchmark": BENCHMARK_SUITE,
                "transport": "siliconflow",
                "base_url": self.service.base_url,
                "endpoint": self.service.endpoint,
                "model": self.service.model,
                "input_semantics": "singleton_exact",
                "isolation_sha256": _sha256_text(isolation_id),
            }
        )
        self._lock = threading.Lock()
        self._requests = 0
        self._http_requests = 0
        self._retries = 0
        self._failures = 0
        self._active = 0
        self._max_active = 0

    def __call__(self, text: str) -> Sequence[float]:
        with self._lock:
            self._requests += 1
            self._active += 1
            self._max_active = max(self._max_active, self._active)
        try:
            vectors, status = state_rag._embedding_call(
                self.service,
                [text],
            )
            with self._lock:
                self._http_requests += int(status.get("attempts") or 1)
                self._retries += int(status.get("retry_count") or 0)
            return vectors[0]
        except Exception:
            with self._lock:
                self._failures += 1
            raise
        finally:
            with self._lock:
                self._active -= 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "embedding_requests": self._requests,
                "embedding_http_requests": self._http_requests,
                "embedding_retries": self._retries,
                "embedding_failures": self._failures,
                "embedding_max_active": self._max_active,
            }


class _LiveExactRerank:
    """SiliconFlow rerank wrapper that preserves the exact ordered input."""

    def __init__(self, *, isolation_id: str) -> None:
        self.service = _siliconflow_rerank_service()
        self.cache_identity = _stable_json(
            {
                "benchmark": BENCHMARK_SUITE,
                "transport": "siliconflow",
                "base_url": self.service.base_url,
                "endpoint": self.service.endpoint,
                "model": self.service.model,
                "normalization": "exact_ordered_documents",
                "isolation_sha256": _sha256_text(isolation_id),
            }
        )
        self._lock = threading.Lock()
        self._requests = 0
        self._http_requests = 0
        self._retries = 0
        self._failures = 0
        self._active = 0
        self._max_active = 0

    def __call__(
        self,
        query: str,
        documents: Sequence[str],
        top_n: int,
    ) -> Sequence[tuple[int, float]]:
        with self._lock:
            self._requests += 1
            self._active += 1
            self._max_active = max(self._max_active, self._active)
        try:
            ranked, status = state_rag._rerank_call(
                self.service,
                query,
                documents,
                top_n,
            )
            with self._lock:
                self._http_requests += int(status.get("attempts") or 1)
                self._retries += int(status.get("retry_count") or 0)
            return ranked
        except Exception:
            with self._lock:
                self._failures += 1
            raise
        finally:
            with self._lock:
                self._active -= 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "rerank_requests": self._requests,
                "rerank_http_requests": self._http_requests,
                "rerank_retries": self._retries,
                "rerank_failures": self._failures,
                "rerank_max_active": self._max_active,
            }


def _counter_delta(
    before: Mapping[str, int],
    after: Mapping[str, int],
) -> dict[str, int]:
    keys = sorted(set(before) | set(after))
    delta = {
        key: int(after.get(key, 0)) - int(before.get(key, 0))
        for key in keys
        if not key.endswith("_max_active")
    }
    for key in keys:
        if key.endswith("_max_active"):
            request_key = key.replace("_max_active", "_requests")
            delta[key] = (
                int(after.get(key, 0))
                if int(delta.get(request_key, 0)) > 0
                else 0
            )
    return delta


def _provider_snapshot(
    embedding: Any,
    rerank: Any | None,
) -> dict[str, int]:
    result = dict(embedding.snapshot())
    if rerank is not None:
        result.update(rerank.snapshot())
    else:
        result.update(
            {
                "rerank_requests": 0,
                "rerank_http_requests": 0,
                "rerank_retries": 0,
                "rerank_failures": 0,
                "rerank_max_active": 0,
            }
        )
    return result


def _materialize_accepted_snapshot(
    root: Path,
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Create one immutable accepted-source manifest for the entire run."""

    accepted_hashes: dict[str, str] = {}
    runtime_cases: list[dict[str, Any]] = []
    for record in records:
        case_id = str(record["case_id"])
        relative_path = f"canon/advantages/{case_id}.md"
        content = str(record["authority_text"]).replace("\r\n", "\n")
        target = root / Path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        payload = target.read_bytes()
        accepted_hashes[relative_path] = hashlib.sha256(payload).hexdigest()
        runtime_case = deepcopy(dict(record))
        runtime_case["expected_path"] = relative_path
        runtime_cases.append(runtime_case)
    snapshot_payload = {
        "accepted_hashes": dict(sorted(accepted_hashes.items())),
        "case_contracts": [
            {
                "case_id": str(record["case_id"]),
                "profile": str(record["profile"]),
                "expected_advantage_ids": list(
                    record["expected_advantage_ids"]
                ),
                "expected_module_ids": list(record["expected_module_ids"]),
                "critical_facts": list(record["critical_facts"]),
                "mandatory_sections": list(record["mandatory_sections"]),
            }
            for record in runtime_cases
        ],
    }
    return {
        "read_count": 1,
        "snapshot_sha256": _sha256_json(snapshot_payload),
        "accepted_hashes": accepted_hashes,
        "cases": runtime_cases,
    }


def _build_index(
    database_path: Path,
    *,
    embedding: Any,
    rerank: Any | None,
    max_concurrency: int,
) -> AuthorityIndex:
    contract = _embedding_contract()
    if contract["input_semantics"] != "singleton_exact":
        raise RuntimeError(
            "Advantage benchmark requires the SiliconFlow singleton-exact guard"
        )
    return AuthorityIndex(
        database_path,
        embedding_provider=embedding,
        embedding_batch_provider=None,
        embedding_model=SILICONFLOW_EMBEDDING_MODEL,
        rerank_provider=rerank,
        rerank_model=(
            SILICONFLOW_RERANK_MODEL if rerank is not None else "disabled"
        ),
        embedding_single_max_concurrency=min(4, max(1, max_concurrency)),
        rerank_max_concurrency=max(1, max_concurrency),
        query_embedding_cache_size=2048,
        singleflight_enabled=True,
    )


def _selected_signature(
    results: Sequence[Sequence[Mapping[str, Any]]],
) -> list[list[dict[str, Any]]]:
    return [
        [
            {
                "chunk_id": str(item.get("chunk_id") or ""),
                "path": str(item.get("path") or ""),
                "ordinal": int(item.get("ordinal") or 0),
                "content_sha256": str(item.get("content_sha256") or ""),
                "text_sha256": _sha256_text(str(item.get("text") or "")),
            }
            for item in case_results
        ]
        for case_results in results
    ]


def _case_context(case_results: Sequence[Mapping[str, Any]]) -> str:
    return "\n\n".join(str(item.get("text") or "") for item in case_results)


def _quality_for_phase(
    phase: str,
    cases: Sequence[Mapping[str, Any]],
    results: Sequence[Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    critical_mismatches: list[dict[str, str]] = []
    stable_id_mismatches: list[dict[str, str]] = []
    section_mismatches: list[dict[str, str]] = []
    expected_selection_mismatches: list[str] = []
    for case, case_results in zip(cases, results):
        case_id = str(case["case_id"])
        top_path = (
            str(case_results[0].get("path") or "") if case_results else ""
        )
        if top_path != str(case["expected_path"]):
            expected_selection_mismatches.append(_sha256_text(case_id))
        context = _case_context(case_results).casefold()
        for fact in case["critical_facts"]:
            if str(fact).casefold() not in context:
                critical_mismatches.append(
                    {
                        "case_sha256": _sha256_text(case_id),
                        "fact_sha256": _sha256_text(str(fact)),
                    }
                )
        for stable_id in (
            *case["expected_advantage_ids"],
            *case["expected_module_ids"],
        ):
            if str(stable_id).casefold() not in context:
                stable_id_mismatches.append(
                    {
                        "case_sha256": _sha256_text(case_id),
                        "stable_id_sha256": _sha256_text(str(stable_id)),
                    }
                )
        for section in case["mandatory_sections"]:
            marker = f"[{section}]"
            if marker.casefold() not in context:
                section_mismatches.append(
                    {
                        "case_sha256": _sha256_text(case_id),
                        "section_sha256": _sha256_text(str(section)),
                    }
                )
    return {
        "phase": phase,
        "critical_fact_mismatch_count": len(critical_mismatches),
        "stable_id_mismatch_count": len(stable_id_mismatches),
        "mandatory_section_mismatch_count": len(section_mismatches),
        "expected_selected_mismatch_count": len(
            expected_selection_mismatches
        ),
        "critical_fact_mismatches": critical_mismatches,
        "stable_id_mismatches": stable_id_mismatches,
        "mandatory_section_mismatches": section_mismatches,
        "expected_selected_mismatches": expected_selection_mismatches,
    }


def compare_advantage_results(
    cases: Sequence[Mapping[str, Any]],
    reference: Sequence[Sequence[Mapping[str, Any]]],
    candidate: Sequence[Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Compare selected chunks and mandatory Advantage facts."""

    reference_signature = _selected_signature(reference)
    candidate_signature = _selected_signature(candidate)
    selected_mismatches: list[str] = []
    for index, case in enumerate(cases):
        reference_case = (
            reference_signature[index]
            if index < len(reference_signature)
            else []
        )
        candidate_case = (
            candidate_signature[index]
            if index < len(candidate_signature)
            else []
        )
        if reference_case != candidate_case:
            selected_mismatches.append(
                _sha256_text(str(case["case_id"]))
            )
    phase_quality = _quality_for_phase("candidate", cases, candidate)
    passed = (
        not selected_mismatches
        and phase_quality["critical_fact_mismatch_count"] == 0
        and phase_quality["stable_id_mismatch_count"] == 0
        and phase_quality["mandatory_section_mismatch_count"] == 0
        and phase_quality["expected_selected_mismatch_count"] == 0
    )
    return {
        "selected_mismatch_count": len(selected_mismatches),
        "selected_mismatches": selected_mismatches,
        "reference_selected_sha256": _sha256_json(reference_signature),
        "candidate_selected_sha256": _sha256_json(candidate_signature),
        **{
            key: value
            for key, value in phase_quality.items()
            if key != "phase"
        },
        "passed": passed,
    }


def _timed(function: Callable[[], Any]) -> tuple[Any, float]:
    started = time.perf_counter_ns()
    result = function()
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    return result, round(elapsed_ms, 6)


def _phase_payload(
    *,
    name: str,
    results: Sequence[Sequence[Mapping[str, Any]]],
    duration_ms: float,
    requests: Mapping[str, int],
    diagnostics: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "phase": name,
        "duration_ms": duration_ms,
        "selected_sha256": _sha256_json(_selected_signature(results)),
        "result_count": sum(len(items) for items in results),
        "requests": dict(requests),
        "authority": {
            key: deepcopy(value)
            for key, value in diagnostics.items()
            if key != "queries"
        },
    }


def _run_iteration(
    *,
    run_root: Path,
    seed_database: Path,
    cases: Sequence[Mapping[str, Any]],
    mode: str,
    run_scope: str,
    iteration_label: str,
    max_concurrency: int,
) -> dict[str, Any]:
    reference_db = run_root / f"{iteration_label}-reference.sqlite3"
    optimized_db = run_root / f"{iteration_label}-optimized.sqlite3"
    shutil.copy2(seed_database, reference_db)
    shutil.copy2(seed_database, optimized_db)

    embedding_factory: Callable[..., Any]
    rerank_factory: Callable[..., Any]
    if mode == "offline":
        embedding_factory = _OfflineSingletonEmbedding
        rerank_factory = _OfflineExactRerank
    elif mode == "live":
        embedding_factory = _LiveSingletonEmbedding
        rerank_factory = _LiveExactRerank
    else:
        raise ValueError(f"unsupported Advantage benchmark mode: {mode}")

    reference_embedding = embedding_factory(
        isolation_id=f"{run_scope}/{iteration_label}/reference"
    )
    optimized_embedding = embedding_factory(
        isolation_id=f"{run_scope}/{iteration_label}/optimized"
    )
    if mode == "offline":
        reference_rerank = rerank_factory(
            isolation_id=f"{run_scope}/{iteration_label}/reference",
            expected_parallelism=1,
        )
        optimized_rerank = rerank_factory(
            isolation_id=f"{run_scope}/{iteration_label}/optimized",
            expected_parallelism=min(max_concurrency, len(cases)),
        )
    else:
        reference_rerank = rerank_factory(
            isolation_id=f"{run_scope}/{iteration_label}/reference"
        )
        optimized_rerank = rerank_factory(
            isolation_id=f"{run_scope}/{iteration_label}/optimized"
        )

    reference_index = _build_index(
        reference_db,
        embedding=reference_embedding,
        rerank=reference_rerank,
        max_concurrency=1,
    )
    optimized_index = _build_index(
        optimized_db,
        embedding=optimized_embedding,
        rerank=optimized_rerank,
        max_concurrency=max_concurrency,
    )
    prompts = [str(case["prompt"]) for case in cases]

    before = _provider_snapshot(reference_embedding, reference_rerank)

    def reference_search() -> list[list[dict[str, Any]]]:
        return [
            reference_index.search(
                prompt,
                limit=DEFAULT_LIMIT,
                use_candidate_cache=False,
            )
            for prompt in prompts
        ]

    reference_results, reference_ms = _timed(reference_search)
    reference_after = _provider_snapshot(
        reference_embedding,
        reference_rerank,
    )
    reference_requests = _counter_delta(before, reference_after)
    reference_phase = _phase_payload(
        name="reference_serial",
        results=reference_results,
        duration_ms=reference_ms,
        requests=reference_requests,
        diagnostics={},
    )

    before = _provider_snapshot(optimized_embedding, optimized_rerank)
    cold_results, cold_ms = _timed(
        lambda: optimized_index.search_many(
            prompts,
            limit=DEFAULT_LIMIT,
            use_candidate_cache=True,
            rerank_max_concurrency=max_concurrency,
        )
    )
    cold_after = _provider_snapshot(optimized_embedding, optimized_rerank)
    cold_requests = _counter_delta(before, cold_after)
    cold_diagnostics = optimized_index.last_search_diagnostics()
    cold_phase = _phase_payload(
        name="optimized_cold",
        results=cold_results,
        duration_ms=cold_ms,
        requests=cold_requests,
        diagnostics=cold_diagnostics,
    )

    before = _provider_snapshot(optimized_embedding, optimized_rerank)
    exact_results, exact_ms = _timed(
        lambda: optimized_index.search_many(
            prompts,
            limit=DEFAULT_LIMIT,
            use_candidate_cache=False,
            rerank_max_concurrency=max_concurrency,
        )
    )
    exact_after = _provider_snapshot(optimized_embedding, optimized_rerank)
    exact_requests = _counter_delta(before, exact_after)
    exact_diagnostics = optimized_index.last_search_diagnostics()
    exact_phase = _phase_payload(
        name="optimized_exact_cache",
        results=exact_results,
        duration_ms=exact_ms,
        requests=exact_requests,
        diagnostics=exact_diagnostics,
    )

    before = _provider_snapshot(optimized_embedding, optimized_rerank)
    candidate_results, candidate_ms = _timed(
        lambda: optimized_index.search_many(
            prompts,
            limit=DEFAULT_LIMIT,
            use_candidate_cache=True,
            rerank_max_concurrency=max_concurrency,
        )
    )
    candidate_after = _provider_snapshot(
        optimized_embedding,
        optimized_rerank,
    )
    candidate_requests = _counter_delta(before, candidate_after)
    candidate_diagnostics = optimized_index.last_search_diagnostics()
    candidate_phase = _phase_payload(
        name="optimized_candidate_cache",
        results=candidate_results,
        duration_ms=candidate_ms,
        requests=candidate_requests,
        diagnostics=candidate_diagnostics,
    )

    comparisons = {
        "optimized_cold": compare_advantage_results(
            cases,
            reference_results,
            cold_results,
        ),
        "optimized_exact_cache": compare_advantage_results(
            cases,
            reference_results,
            exact_results,
        ),
        "optimized_candidate_cache": compare_advantage_results(
            cases,
            reference_results,
            candidate_results,
        ),
    }
    reference_quality = _quality_for_phase(
        "reference_serial",
        cases,
        reference_results,
    )
    phase_count = len(cases)
    cache_gate = {
        "cold_single_embedding_requests": int(
            cold_requests.get("embedding_requests", 0)
        ),
        "cold_rerank_requests": int(
            cold_requests.get("rerank_requests", 0)
        ),
        "exact_embedding_cache_hits": int(
            exact_diagnostics.get("embedding_cache_hits", 0)
        ),
        "exact_rerank_cache_hits": int(
            exact_diagnostics.get("rerank_cache_hits", 0)
        ),
        "candidate_cache_hits": int(
            candidate_diagnostics.get("candidate_cache_hits", 0)
        ),
        "exact_provider_request_count": int(
            exact_requests.get("embedding_requests", 0)
        )
        + int(exact_requests.get("rerank_requests", 0)),
        "candidate_provider_request_count": int(
            candidate_requests.get("embedding_requests", 0)
        )
        + int(candidate_requests.get("rerank_requests", 0)),
    }
    cache_gate["passed"] = (
        cache_gate["cold_single_embedding_requests"] == phase_count
        and cache_gate["cold_rerank_requests"] == phase_count
        and cache_gate["exact_embedding_cache_hits"] == phase_count
        and cache_gate["exact_rerank_cache_hits"] == phase_count
        and cache_gate["candidate_cache_hits"] == phase_count
        and cache_gate["exact_provider_request_count"] == 0
        and cache_gate["candidate_provider_request_count"] == 0
    )
    reference_passed = (
        reference_quality["critical_fact_mismatch_count"] == 0
        and reference_quality["stable_id_mismatch_count"] == 0
        and reference_quality["mandatory_section_mismatch_count"] == 0
        and reference_quality["expected_selected_mismatch_count"] == 0
    )
    passed = (
        reference_passed
        and cache_gate["passed"]
        and all(
            bool(comparison["passed"])
            for comparison in comparisons.values()
        )
    )
    return {
        "iteration_label": iteration_label,
        "phases": [
            reference_phase,
            cold_phase,
            exact_phase,
            candidate_phase,
        ],
        "reference_quality": reference_quality,
        "comparisons": comparisons,
        "cache_gate": cache_gate,
        "passed": passed,
    }


def _sum_requests(
    measurements: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    totals: dict[str, int] = {}
    for measurement in measurements:
        for phase in measurement["phases"]:
            for key, value in phase["requests"].items():
                if key.endswith("_max_active"):
                    totals[key] = max(totals.get(key, 0), int(value))
                else:
                    totals[key] = totals.get(key, 0) + int(value)
    totals["inference_provider_requests"] = (
        totals.get("embedding_requests", 0)
        + totals.get("rerank_requests", 0)
    )
    totals["inference_http_requests"] = (
        totals.get("embedding_http_requests", 0)
        + totals.get("rerank_http_requests", 0)
    )
    return totals


def _aggregate_mode(
    *,
    mode: str,
    run_root: Path,
    cases: Sequence[Mapping[str, Any]],
    accepted_snapshot: Mapping[str, Any],
    iterations: int,
    warmup_iterations: int,
    max_concurrency: int,
) -> dict[str, Any]:
    mode_root = run_root / mode
    mode_root.mkdir(parents=True, exist_ok=True)
    run_scope = f"{mode}/{uuid.uuid4().hex}"
    if mode == "offline":
        seed_embedding: Any = _OfflineSingletonEmbedding(
            isolation_id=f"{run_scope}/seed"
        )
    else:
        seed_embedding = _LiveSingletonEmbedding(
            isolation_id=f"{run_scope}/seed"
        )
    seed_database = mode_root / "accepted-seed.sqlite3"
    seed_index = _build_index(
        seed_database,
        embedding=seed_embedding,
        rerank=None,
        max_concurrency=max_concurrency,
    )
    seed_before = _provider_snapshot(seed_embedding, None)
    refresh, refresh_ms = _timed(
        lambda: seed_index.refresh(
            run_root / "project",
            [
                AuthoritySource(
                    glob="canon/advantages/*.md",
                    role="canon",
                    priority=100,
                    scope_policy="current",
                    ingest_policy="include",
                )
            ],
            accepted_hashes=accepted_snapshot["accepted_hashes"],
        )
    )
    seed_after = _provider_snapshot(seed_embedding, None)
    seed_requests = _counter_delta(seed_before, seed_after)
    if (
        int(refresh.get("manifest_hash_mismatches", 0)) != 0
        or int(refresh.get("chunks_written", 0)) < len(cases)
    ):
        raise RuntimeError("accepted Advantage snapshot did not index cleanly")

    warmups = [
        _run_iteration(
            run_root=mode_root,
            seed_database=seed_database,
            cases=cases,
            mode=mode,
            run_scope=run_scope,
            iteration_label=f"warmup-{index:03d}",
            max_concurrency=max_concurrency,
        )
        for index in range(warmup_iterations)
    ]
    measurements = [
        _run_iteration(
            run_root=mode_root,
            seed_database=seed_database,
            cases=cases,
            mode=mode,
            run_scope=run_scope,
            iteration_label=f"measure-{index:03d}",
            max_concurrency=max_concurrency,
        )
        for index in range(iterations)
    ]
    phase_names = [
        str(phase["phase"]) for phase in measurements[0]["phases"]
    ]
    latency = {
        phase_name: _timing_distribution(
            [
                float(
                    next(
                        phase["duration_ms"]
                        for phase in measurement["phases"]
                        if phase["phase"] == phase_name
                    )
                )
                for measurement in measurements
            ]
        )
        for phase_name in phase_names
    }

    selected_mismatch_cases: set[str] = set()
    critical_mismatches: set[tuple[str, str]] = set()
    stable_id_mismatches: set[tuple[str, str]] = set()
    section_mismatches: set[tuple[str, str]] = set()
    expected_selection_mismatches: set[str] = set()
    for measurement in [*warmups, *measurements]:
        reference_quality = measurement["reference_quality"]
        expected_selection_mismatches.update(
            reference_quality["expected_selected_mismatches"]
        )
        for mismatch in reference_quality["critical_fact_mismatches"]:
            critical_mismatches.add(
                (mismatch["case_sha256"], mismatch["fact_sha256"])
            )
        for mismatch in reference_quality["stable_id_mismatches"]:
            stable_id_mismatches.add(
                (
                    mismatch["case_sha256"],
                    mismatch["stable_id_sha256"],
                )
            )
        for mismatch in reference_quality["mandatory_section_mismatches"]:
            section_mismatches.add(
                (
                    mismatch["case_sha256"],
                    mismatch["section_sha256"],
                )
            )
        for comparison in measurement["comparisons"].values():
            selected_mismatch_cases.update(
                comparison["selected_mismatches"]
            )
            expected_selection_mismatches.update(
                comparison["expected_selected_mismatches"]
            )
            for mismatch in comparison["critical_fact_mismatches"]:
                critical_mismatches.add(
                    (mismatch["case_sha256"], mismatch["fact_sha256"])
                )
            for mismatch in comparison["stable_id_mismatches"]:
                stable_id_mismatches.add(
                    (
                        mismatch["case_sha256"],
                        mismatch["stable_id_sha256"],
                    )
                )
            for mismatch in comparison["mandatory_section_mismatches"]:
                section_mismatches.add(
                    (
                        mismatch["case_sha256"],
                        mismatch["section_sha256"],
                    )
                )
    quality = {
        "selected_mismatch_count": len(selected_mismatch_cases),
        "critical_fact_mismatch_count": len(critical_mismatches),
        "stable_id_mismatch_count": len(stable_id_mismatches),
        "mandatory_section_mismatch_count": len(section_mismatches),
        "expected_selected_mismatch_count": len(
            expected_selection_mismatches
        ),
        "all_warmups_passed": all(
            bool(measurement["passed"]) for measurement in warmups
        ),
        "all_iterations_passed": all(
            bool(measurement["passed"]) for measurement in measurements
        ),
    }
    quality["passed"] = (
        quality["selected_mismatch_count"] == 0
        and quality["critical_fact_mismatch_count"] == 0
        and quality["stable_id_mismatch_count"] == 0
        and quality["mandatory_section_mismatch_count"] == 0
        and quality["expected_selected_mismatch_count"] == 0
        and quality["all_warmups_passed"]
        and quality["all_iterations_passed"]
    )
    return {
        "status": "passed" if quality["passed"] else "failed",
        "passed": quality["passed"],
        "execution_mode": mode,
        "iterations": iterations,
        "warmup_iterations": warmup_iterations,
        "case_count": len(cases),
        "accepted_snapshot": {
            "read_count": int(accepted_snapshot["read_count"]),
            "snapshot_sha256": str(
                accepted_snapshot["snapshot_sha256"]
            ),
            "manifest_gated": True,
        },
        "index_refresh": {
            "duration_ms": refresh_ms,
            "files_hashed": int(refresh.get("files_hashed", 0)),
            "chunks_written": int(refresh.get("chunks_written", 0)),
            "requests": seed_requests,
        },
        "latency": latency,
        "request_counts": _sum_requests(measurements),
        "quality": quality,
        "cache_gate": {
            "all_warmups_passed": all(
                bool(measurement["cache_gate"]["passed"])
                for measurement in warmups
            ),
            "all_iterations_passed": all(
                bool(measurement["cache_gate"]["passed"])
                for measurement in measurements
            ),
            "representative": deepcopy(measurements[0]["cache_gate"]),
        },
        "measurements": measurements,
    }


def run_advantage_performance_benchmark(
    fixture: str | Path = DEFAULT_FIXTURE,
    *,
    workspace_parent: str | Path | None = None,
    iterations: int = DEFAULT_ITERATIONS,
    warmup_iterations: int = DEFAULT_WARMUP_ITERATIONS,
    include_live: bool = False,
    live_iterations: int = 1,
    max_concurrency: int = 4,
) -> dict[str, Any]:
    """Run the offline benchmark and the opt-in SiliconFlow lane."""

    for name, value, minimum in (
        ("iterations", iterations, 1),
        ("warmup_iterations", warmup_iterations, 0),
        ("live_iterations", live_iterations, 1),
        ("max_concurrency", max_concurrency, 1),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    records = load_advantage_fixture(fixture)
    fixture_validation = validate_advantage_fixture(records)
    effective_concurrency = min(4, max_concurrency)
    parent = (
        None
        if workspace_parent is None
        else str(Path(workspace_parent).resolve())
    )
    with tempfile.TemporaryDirectory(
        prefix="plot-rag-advantage-benchmark-",
        dir=parent,
    ) as temporary:
        run_root = Path(temporary)
        project_root = run_root / "project"
        project_root.mkdir(parents=True)
        accepted_snapshot = _materialize_accepted_snapshot(
            project_root,
            records,
        )
        cases = accepted_snapshot["cases"]
        offline = _aggregate_mode(
            mode="offline",
            run_root=run_root,
            cases=cases,
            accepted_snapshot=accepted_snapshot,
            iterations=iterations,
            warmup_iterations=warmup_iterations,
            max_concurrency=effective_concurrency,
        )
        if not include_live:
            live: dict[str, Any] = {
                "status": "not_requested",
                "passed": True,
                "network_used": False,
            }
        elif not str(os.environ.get(SILICONFLOW_API_KEY_ENV) or "").strip():
            live = {
                "status": "skipped",
                "passed": True,
                "network_used": False,
                "reason": "missing_environment",
                "required_environment_sha256": _sha256_text(
                    SILICONFLOW_API_KEY_ENV
                ),
            }
        else:
            try:
                live = _aggregate_mode(
                    mode="live",
                    run_root=run_root,
                    cases=cases,
                    accepted_snapshot=accepted_snapshot,
                    iterations=live_iterations,
                    warmup_iterations=0,
                    max_concurrency=effective_concurrency,
                )
                live["network_used"] = True
            except Exception as error:
                live = {
                    "status": "failed",
                    "passed": False,
                    "network_used": True,
                    "error_type": type(error).__name__,
                }

    passed = bool(offline["passed"]) and bool(live["passed"])
    summary = {
        "offline_p50_ms": float(
            offline["latency"]["optimized_cold"]["p50_ms"]
        ),
        "offline_p95_ms": float(
            offline["latency"]["optimized_cold"]["p95_ms"]
        ),
        "offline_request_count": int(
            offline["request_counts"]["inference_provider_requests"]
        ),
        "selected_mismatch_count": int(
            offline["quality"]["selected_mismatch_count"]
        ),
        "critical_fact_mismatch_count": int(
            offline["quality"]["critical_fact_mismatch_count"]
        ),
    }
    if live.get("status") in {"passed", "failed"} and "latency" in live:
        summary.update(
            {
                "live_p50_ms": float(
                    live["latency"]["optimized_cold"]["p50_ms"]
                ),
                "live_p95_ms": float(
                    live["latency"]["optimized_cold"]["p95_ms"]
                ),
                "live_http_request_count": int(
                    live["request_counts"]["inference_http_requests"]
                ),
                "live_selected_mismatch_count": int(
                    live["quality"]["selected_mismatch_count"]
                ),
                "live_critical_fact_mismatch_count": int(
                    live["quality"]["critical_fact_mismatch_count"]
                ),
            }
        )
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "suite": BENCHMARK_SUITE,
        "runner_version": RUNNER_VERSION,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "network_used": bool(live.get("network_used")),
        "fixture": fixture_validation,
        "provider_contract": {
            "embedding": _embedding_contract(),
            "rerank": {
                "provider": "siliconflow",
                "base_url_host": "api.siliconflow.cn",
                "model": SILICONFLOW_RERANK_MODEL,
                "cache_identity": (
                    "exact query + ordered documents + top_n + model"
                ),
            },
            "accepted_state": {
                "single_snapshot_required": True,
                "read_count": 1,
            },
        },
        "summary": summary,
        "offline": offline,
        "live": live,
    }


def _write_json(path: Path, value: Mapping[str, Any], *, overwrite: bool) -> None:
    target = path.resolve()
    if target.exists() and not overwrite:
        raise FileExistsError(
            f"refusing to replace existing benchmark report: {target}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(payload + "\n", encoding="utf-8", newline="\n")
    os.replace(temporary, target)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Advantage singleton-exact retrieval benchmark."
        )
    )
    parser.add_argument(
        "command",
        choices=("validate", "run"),
        nargs="?",
        default="run",
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument(
        "--warmup-iterations",
        type=int,
        default=DEFAULT_WARMUP_ITERATIONS,
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--live-iterations", type=int, default=1)
    parser.add_argument("--max-concurrency", type=int, default=4)
    parser.add_argument("--workspace-parent", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "validate":
        result: Mapping[str, Any] = validate_advantage_fixture(
            load_advantage_fixture(args.fixture)
        )
    else:
        result = run_advantage_performance_benchmark(
            args.fixture,
            workspace_parent=args.workspace_parent,
            iterations=args.iterations,
            warmup_iterations=args.warmup_iterations,
            include_live=args.live,
            live_iterations=args.live_iterations,
            max_concurrency=args.max_concurrency,
        )
        if args.output is not None:
            _write_json(args.output, result, overwrite=args.overwrite)
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            allow_nan=False,
        )
    )
    return 0 if bool(result.get("passed", result.get("status") == "valid")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
