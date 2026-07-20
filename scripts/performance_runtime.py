"""Read-only performance runtime API for plot-rag-gate v1.5.

The public functions in this module are deliberately transport-neutral so the
CLI and MCP surfaces can call them without spawning a subprocess:

``get_status(project_root)``
    Inspect project-local performance configuration and aggregate persisted
    prepare, extraction, cache, and remote telemetry through read-only SQLite
    connections.

``run_benchmark(project_root, manifest=None, path=None, options=None)``
    Run the deterministic offline v1.5 benchmark in temporary storage,
    aggregate p50/p95 telemetry, redact the report, and verify that accepted
    canon storage did not change.

``compare_reports(left, right)``
    Compare two mappings or JSON report paths and return metric-level deltas
    and regression/improvement classifications.

No function creates or migrates a project database.  The benchmark itself
materializes only the synthetic fixture under ``tempfile.TemporaryDirectory``.
"""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import json
import math
import os
import re
import sqlite3
import sys
import threading
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Mapping, MutableMapping, Sequence
from urllib.parse import quote


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_BENCHMARK_IMPORT_NAMESPACE = (
    "_plot_rag_gate_benchmark_"
    + hashlib.sha256(str(PLUGIN_ROOT).encode("utf-8")).hexdigest()[:16]
)
_BENCHMARK_IMPORT_LOCK = threading.RLock()
_BENCHMARK_MODULE: ModuleType | None = None

STATUS_SCHEMA_VERSION = "plot-rag-performance-status/v1"
REPORT_SCHEMA_VERSION = "plot-rag-performance-report/v1"
COMPARISON_SCHEMA_VERSION = "plot-rag-performance-comparison/v1"
RUNTIME_VERSION = 1
DEFAULT_STATUS_ROW_LIMIT = 1_000
MAX_BENCHMARK_ITERATIONS = 100

_SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|authorization|password|passwd|secret|access[_-]?token|"
    r"refresh[_-]?token|credential|cookie|set-cookie)",
    re.IGNORECASE,
)
_PROSE_OR_PATH_KEY_RE = re.compile(
    r"(?:^|_)(?:prompt|content|evidence|project_root|workspace_parent|"
    r"expected_path|top_paths?|paths?|base_url|endpoint|url|host)(?:$|_)",
    re.IGNORECASE,
)
_QUERY_TEXT_KEY_RE = re.compile(
    r"^(?:query|queries|query_text|search_query)$",
    re.IGNORECASE,
)
_WINDOWS_ABSOLUTE_RE = re.compile(
    r"(?i)(?:[a-z]:[\\/](?:[^<>:\"|?*\r\n]+[\\/]?)+|"
    r"\\\\[^\\/\s]+[\\/][^\\/\s]+(?:[\\/][^\r\n]*)?)"
)
_POSIX_ABSOLUTE_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?:[^/\s]+/)+[^/\s]*"
)
_URL_RE = re.compile(r"(?i)\bhttps?://[^\s\"'<>]+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_KEY_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk|sf|ak)-[A-Za-z0-9_-]{12,}"
)

_PREPARE_NEW_KEYS = frozenset(
    {
        "new_query_ms",
        "cold_ms",
        "cold_search_ms",
        "prepare_new_ms",
        "prepare_cold_ms",
    }
)
_PREPARE_CACHE_KEYS = frozenset(
    {
        "cache_hit_ms",
        "hot_ms",
        "hot_search_ms",
        "prepare_cache_hit_ms",
        "prepare_hot_ms",
    }
)
_PREPARE_ALL_KEYS = frozenset(
    {
        "prepare_ms",
        "prepare_duration_ms",
        "duration_ms",
        "latency_ms",
    }
)
_EXTRACTION_KEYS = {
    "enqueue": frozenset(
        {"enqueue_ms", "job_enqueue_ms", "stop_enqueue_ms"}
    ),
    "ready": frozenset(
        {
            "ready_ms",
            "extraction_ready_ms",
            "job_ready_ms",
        }
    ),
    "barrier_wait": frozenset(
        {"barrier_wait_ms", "next_turn_barrier_wait_ms"}
    ),
    "end_to_next_ready": frozenset(
        {"end_to_next_ready_ms", "next_ready_ms"}
    ),
}


class PerformanceRuntimeError(ValueError):
    """A runtime request or report is malformed."""


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _project_id(root: Path) -> str:
    normalized = os.path.normcase(str(root.resolve(strict=False)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _numeric_values(value: Any) -> list[float]:
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        values: list[float] = []
        for item in value:
            number = _finite_number(item)
            if number is not None and number >= 0:
                values.append(number)
        return values
    number = _finite_number(value)
    return [] if number is None or number < 0 else [number]


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    """Return a linearly interpolated percentile, matching common pXX tools."""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return round(ordered[0], 6)
    position = (len(ordered) - 1) * float(percentile)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return round(ordered[lower], 6)
    fraction = position - lower
    result = ordered[lower] + (
        ordered[upper] - ordered[lower]
    ) * fraction
    return round(result, 6)


def _latency_summary(values: Iterable[float]) -> dict[str, Any]:
    samples = sorted(
        float(value)
        for value in values
        if math.isfinite(float(value)) and float(value) >= 0
    )
    if not samples:
        return {
            "count": 0,
            "min_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "max_ms": None,
            "mean_ms": None,
        }
    return {
        "count": len(samples),
        "min_ms": round(samples[0], 6),
        "p50_ms": _percentile(samples, 0.50),
        "p95_ms": _percentile(samples, 0.95),
        "max_ms": round(samples[-1], 6),
        "mean_ms": round(sum(samples) / len(samples), 6),
    }


def _new_collector() -> dict[str, Any]:
    return {
        "prepare": {
            "new_query": [],
            "cache_hit": [],
            "other": [],
            "lifecycle_fallback": [],
        },
        "extraction": {
            "enqueue": [],
            "ready": [],
            "barrier_wait": [],
            "end_to_next_ready": [],
            "queue_wait": [],
        },
        "cache": {
            "hits": 0,
            "misses": 0,
            "entries": 0,
        },
        "remote": {
            "calls": 0,
            "failures": 0,
            "latencies": [],
            "services": defaultdict(
                lambda: {
                    "calls": 0,
                    "failures": 0,
                    "latencies": [],
                }
            ),
        },
    }


def _add_samples(target: list[float], value: Any) -> None:
    target.extend(_numeric_values(value))


def _add_remote(
    collector: MutableMapping[str, Any],
    service: str,
    *,
    calls: int = 0,
    failures: int = 0,
    latencies: Iterable[float] = (),
) -> None:
    normalized = service if service in {
        "embedding",
        "rerank",
        "extract",
    } else "other"
    remote = collector["remote"]
    service_record = remote["services"][normalized]
    safe_calls = max(0, int(calls))
    safe_failures = max(0, int(failures))
    samples = [
        float(value)
        for value in latencies
        if math.isfinite(float(value)) and float(value) >= 0
    ]
    if safe_calls == 0 and samples:
        safe_calls = len(samples)
    remote["calls"] += safe_calls
    remote["failures"] += safe_failures
    remote["latencies"].extend(samples)
    service_record["calls"] += safe_calls
    service_record["failures"] += safe_failures
    service_record["latencies"].extend(samples)


def _service_from_path(path: Sequence[str]) -> str:
    folded = ".".join(path).casefold()
    for service in ("embedding", "rerank", "extract"):
        if service in folded:
            return service
    return "other"


def _classify_duration(
    collector: MutableMapping[str, Any],
    path: Sequence[str],
    key: str,
    value: Any,
) -> bool:
    numbers = _numeric_values(value)
    if not numbers:
        return False
    folded_path = ".".join(path).casefold()
    normalized_key = key.casefold()
    if "extract" in folded_path or "job" in folded_path:
        for label, aliases in _EXTRACTION_KEYS.items():
            if normalized_key in aliases:
                collector["extraction"][label].extend(numbers)
                return True
    if "prepare" in folded_path:
        if normalized_key in _PREPARE_NEW_KEYS or "cold" in folded_path:
            collector["prepare"]["new_query"].extend(numbers)
        elif (
            normalized_key in _PREPARE_CACHE_KEYS
            or "cache" in folded_path
            or "hot" in folded_path
        ):
            collector["prepare"]["cache_hit"].extend(numbers)
        elif normalized_key in _PREPARE_ALL_KEYS:
            collector["prepare"]["other"].extend(numbers)
        else:
            return False
        return True
    return False


def _scan_runtime_payload(
    payload: Any,
    collector: MutableMapping[str, Any],
    path: tuple[str, ...] = (),
) -> None:
    """Extract numeric telemetry without retaining source text or identifiers."""

    if isinstance(payload, Mapping):
        folded_path = ".".join(path).casefold()
        service = _service_from_path(path)
        latency = _finite_number(payload.get("latency_ms"))
        explicit_calls = _finite_number(
            payload.get("calls", payload.get("call_count"))
        )
        explicit_failures = _finite_number(
            payload.get("failures", payload.get("failure_count"))
        )
        status = str(payload.get("status") or "").casefold()
        is_remote_record = (
            any(
                marker in folded_path
                for marker in ("remote", "embedding", "rerank", "extract")
            )
            and (
                latency is not None
                or explicit_calls is not None
                or "http_status" in payload
            )
        )
        if is_remote_record:
            calls = (
                max(0, int(explicit_calls))
                if explicit_calls is not None
                else (1 if latency is not None or "http_status" in payload else 0)
            )
            failures = (
                max(0, int(explicit_failures))
                if explicit_failures is not None
                else (1 if status in {"failed", "error", "timeout"} else 0)
            )
            _add_remote(
                collector,
                service,
                calls=calls,
                failures=failures,
                latencies=[] if latency is None else [latency],
            )

        for raw_key, value in payload.items():
            key = str(raw_key)
            normalized_key = key.casefold()
            next_path = path + (normalized_key,)
            _classify_duration(collector, path, normalized_key, value)
            if normalized_key in {
                "candidate_cache_hits",
                "cache_hits",
                "hits",
            } and (
                "cache" in normalized_key
                or "cache" in folded_path
                or normalized_key == "candidate_cache_hits"
            ):
                number = _finite_number(value)
                if number is not None:
                    collector["cache"]["hits"] += max(0, int(number))
            elif normalized_key in {
                "candidate_cache_misses",
                "cache_misses",
                "misses",
            } and (
                "cache" in normalized_key
                or "cache" in folded_path
                or normalized_key == "candidate_cache_misses"
            ):
                number = _finite_number(value)
                if number is not None:
                    collector["cache"]["misses"] += max(0, int(number))
            elif normalized_key in {
                "candidate_cache_entries",
                "cache_entries",
                "entries",
            } and "cache" in folded_path:
                number = _finite_number(value)
                if number is not None:
                    collector["cache"]["entries"] = max(
                        collector["cache"]["entries"],
                        max(0, int(number)),
                    )
            if isinstance(value, (Mapping, list, tuple)):
                _scan_runtime_payload(value, collector, next_path)
        return
    if isinstance(payload, Sequence) and not isinstance(
        payload, (str, bytes, bytearray)
    ):
        for index, value in enumerate(payload):
            _scan_runtime_payload(
                value,
                collector,
                path + (str(index),),
            )


def _collect_extra_telemetry(
    telemetry: Any,
    collector: MutableMapping[str, Any],
) -> None:
    if not isinstance(telemetry, Mapping):
        return
    prepare = telemetry.get("prepare")
    if isinstance(prepare, Mapping):
        for key, value in prepare.items():
            normalized = str(key).casefold()
            if normalized in _PREPARE_NEW_KEYS or any(
                marker in normalized for marker in ("new", "cold")
            ):
                _add_samples(collector["prepare"]["new_query"], value)
            elif normalized in _PREPARE_CACHE_KEYS or any(
                marker in normalized for marker in ("cache", "hot")
            ):
                _add_samples(collector["prepare"]["cache_hit"], value)
            elif normalized.endswith("_ms") or normalized in {
                "duration",
                "durations",
            }:
                _add_samples(collector["prepare"]["other"], value)
    elif prepare is not None:
        _add_samples(collector["prepare"]["other"], prepare)

    extraction = telemetry.get("extraction")
    if isinstance(extraction, Mapping):
        for key, value in extraction.items():
            normalized = str(key).casefold()
            matched = False
            for label, aliases in _EXTRACTION_KEYS.items():
                if normalized in aliases or label in normalized:
                    _add_samples(collector["extraction"][label], value)
                    matched = True
                    break
            if not matched and "queue" in normalized and "wait" in normalized:
                _add_samples(
                    collector["extraction"]["queue_wait"],
                    value,
                )

    cache = telemetry.get("cache")
    if isinstance(cache, Mapping):
        for key in ("hits", "cache_hits", "candidate_cache_hits"):
            number = _finite_number(cache.get(key))
            if number is not None:
                collector["cache"]["hits"] += max(0, int(number))
                break
        for key in ("misses", "cache_misses", "candidate_cache_misses"):
            number = _finite_number(cache.get(key))
            if number is not None:
                collector["cache"]["misses"] += max(0, int(number))
                break
        for key in ("entries", "cache_entries", "candidate_cache_entries"):
            number = _finite_number(cache.get(key))
            if number is not None:
                collector["cache"]["entries"] = max(
                    collector["cache"]["entries"],
                    max(0, int(number)),
                )
                break

    remote = telemetry.get("remote")
    if isinstance(remote, Mapping):
        for service, record in remote.items():
            if not isinstance(record, Mapping):
                continue
            calls = _finite_number(
                record.get("calls", record.get("call_count", 0))
            )
            failures = _finite_number(
                record.get("failures", record.get("failure_count", 0))
            )
            latencies = (
                record.get("latency_ms")
                if "latency_ms" in record
                else record.get("latencies_ms", [])
            )
            _add_remote(
                collector,
                str(service).casefold(),
                calls=0 if calls is None else int(calls),
                failures=0 if failures is None else int(failures),
                latencies=_numeric_values(latencies),
            )


def _collect_benchmark(
    result: Mapping[str, Any],
    collector: MutableMapping[str, Any],
) -> None:
    stages = (result.get("telemetry") or {}).get("stages")
    used_stage_phases = False
    if isinstance(stages, Sequence) and not isinstance(stages, str):
        for stage in stages:
            if not isinstance(stage, Mapping):
                continue
            name = str(stage.get("stage") or "").casefold()
            duration = _finite_number(stage.get("duration_ms"))
            if duration is not None:
                if name.endswith(".cold_search"):
                    collector["prepare"]["new_query"].append(duration)
                    used_stage_phases = True
                elif name.endswith(".hot_search"):
                    collector["prepare"]["cache_hit"].append(duration)
                    used_stage_phases = True
                elif "prepare" in name:
                    bucket = (
                        "cache_hit"
                        if any(marker in name for marker in ("cache", "hot"))
                        else "new_query"
                        if "cold" in name or "new" in name
                        else "other"
                    )
                    collector["prepare"][bucket].append(duration)
                elif "extract" in name:
                    collector["extraction"]["ready"].append(duration)
            hits = _finite_number(stage.get("candidate_cache_hits"))
            if hits is not None:
                collector["cache"]["hits"] += max(0, int(hits))

    scenarios = result.get("scenarios")
    if not isinstance(scenarios, Sequence) or isinstance(scenarios, str):
        _scan_runtime_payload(result.get("telemetry") or {}, collector)
        return
    for scenario in scenarios:
        if not isinstance(scenario, Mapping):
            continue
        phases = scenario.get("phases")
        if not isinstance(phases, Sequence) or isinstance(phases, str):
            continue
        for phase in phases:
            if not isinstance(phase, Mapping):
                continue
            phase_name = str(phase.get("phase") or "").casefold()
            duration = _finite_number(phase.get("duration_ms"))
            if duration is not None and not used_stage_phases:
                collector["prepare"][
                    "cache_hit" if phase_name == "hot" else "new_query"
                ].append(duration)
            authority = phase.get("authority")
            if isinstance(authority, Mapping) and not used_stage_phases:
                hits = _finite_number(
                    authority.get("candidate_cache_hits")
                )
                if hits is not None:
                    collector["cache"]["hits"] += max(0, int(hits))
            providers = phase.get("providers")
            if not isinstance(providers, Mapping):
                continue
            embedding_calls = sum(
                max(0, int(_finite_number(providers.get(key)) or 0))
                for key in (
                    "embedding_batch_calls",
                    "embedding_single_calls",
                )
            )
            embedding_failures = sum(
                max(0, int(_finite_number(providers.get(key)) or 0))
                for key in (
                    "embedding_batch_failures",
                    "embedding_single_failures",
                )
            )
            rerank_calls = max(
                0,
                int(_finite_number(providers.get("rerank_calls")) or 0),
            )
            _add_remote(
                collector,
                "embedding",
                calls=embedding_calls,
                failures=embedding_failures,
            )
            rerank_total_ns = _finite_number(
                providers.get("rerank_total_call_ns")
            )
            rerank_latencies: list[float] = []
            if rerank_calls and rerank_total_ns is not None:
                average_ms = rerank_total_ns / rerank_calls / 1_000_000
                rerank_latencies = [average_ms] * rerank_calls
            _add_remote(
                collector,
                "rerank",
                calls=rerank_calls,
                latencies=rerank_latencies,
            )


def _finalize_telemetry(
    collector: Mapping[str, Any],
) -> dict[str, Any]:
    prepare = collector["prepare"]
    prepare_all = [
        *prepare["new_query"],
        *prepare["cache_hit"],
        *prepare["other"],
    ]
    extraction = collector["extraction"]
    extraction_all = [
        *extraction["enqueue"],
        *extraction["ready"],
        *extraction["barrier_wait"],
        *extraction["end_to_next_ready"],
    ]
    cache = collector["cache"]
    lookups = int(cache["hits"]) + int(cache["misses"])
    remote = collector["remote"]
    services = {
        name: {
            "calls": int(record["calls"]),
            "failures": int(record["failures"]),
            "failure_rate": (
                None
                if int(record["calls"]) == 0
                else round(
                    int(record["failures"]) / int(record["calls"]),
                    8,
                )
            ),
            "latency": _latency_summary(record["latencies"]),
        }
        for name, record in sorted(remote["services"].items())
    }
    return {
        "prepare": {
            "all": _latency_summary(prepare_all),
            "new_query": _latency_summary(prepare["new_query"]),
            "cache_hit": _latency_summary(prepare["cache_hit"]),
            "other": _latency_summary(prepare["other"]),
            "lifecycle_fallback": _latency_summary(
                prepare["lifecycle_fallback"]
            ),
        },
        "extraction": {
            "all": _latency_summary(extraction_all),
            "enqueue": _latency_summary(extraction["enqueue"]),
            "ready": _latency_summary(extraction["ready"]),
            "barrier_wait": _latency_summary(
                extraction["barrier_wait"]
            ),
            "end_to_next_ready": _latency_summary(
                extraction["end_to_next_ready"]
            ),
            "queue_wait": _latency_summary(extraction["queue_wait"]),
        },
        "cache": {
            "hits": int(cache["hits"]),
            "misses": int(cache["misses"]),
            "lookups": lookups,
            "hit_rate": (
                None
                if lookups == 0
                else round(int(cache["hits"]) / lookups, 8)
            ),
            "entries": int(cache["entries"]),
        },
        "remote": {
            "calls": int(remote["calls"]),
            "failures": int(remote["failures"]),
            "failure_rate": (
                None
                if int(remote["calls"]) == 0
                else round(
                    int(remote["failures"]) / int(remote["calls"]),
                    8,
                )
            ),
            "latency": _latency_summary(remote["latencies"]),
            "services": services,
        },
    }


def _secret_values(value: Any) -> set[str]:
    secrets = {
        text
        for key, text in os.environ.items()
        if _SENSITIVE_KEY_RE.search(key)
        and isinstance(text, str)
        and len(text) >= 6
    }

    def visit(item: Any, key: str = "") -> None:
        if isinstance(item, Mapping):
            for nested_key, nested_value in item.items():
                visit(nested_value, str(nested_key))
        elif isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            for nested in item:
                visit(nested, key)
        elif (
            _SENSITIVE_KEY_RE.search(key)
            and isinstance(item, str)
            and len(item) >= 6
        ):
            secrets.add(item)

    visit(value)
    return secrets


def _redact_string(value: str, secrets: Iterable[str]) -> str:
    result = value
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            result = result.replace(secret, "<redacted>")
    result = _BEARER_RE.sub("Bearer <redacted>", result)
    result = _KEY_TOKEN_RE.sub("<redacted-key>", result)
    result = _URL_RE.sub("<redacted-url>", result)
    result = _WINDOWS_ABSOLUTE_RE.sub("<redacted-path>", result)
    result = _POSIX_ABSOLUTE_RE.sub("<redacted-path>", result)
    return result


def _redact(
    value: Any,
    *,
    secrets: Iterable[str] = (),
    key: str = "",
) -> Any:
    if _SENSITIVE_KEY_RE.search(key):
        return None
    if (
        _PROSE_OR_PATH_KEY_RE.search(key)
        or _QUERY_TEXT_KEY_RE.search(key)
    ):
        if key.casefold().endswith(("_sha256", "_count")):
            pass
        else:
            return None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for nested_key, nested_value in value.items():
            name = str(nested_key)
            if _SENSITIVE_KEY_RE.search(name):
                continue
            if (
                (
                    _PROSE_OR_PATH_KEY_RE.search(name)
                    or _QUERY_TEXT_KEY_RE.search(name)
                )
                and not name.casefold().endswith(("_sha256", "_count"))
            ):
                continue
            cleaned = _redact(
                nested_value,
                secrets=secrets,
                key=name,
            )
            result[name] = cleaned
        return result
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [
            _redact(
                item,
                secrets=secrets,
                key=key,
            )
            for item in value
        ]
    if isinstance(value, str):
        return _redact_string(value, secrets)
    if isinstance(value, Path):
        return "<redacted-path>"
    return value


def _safe_project_path(root: Path, value: Any, default: str) -> Path:
    raw = str(value or default)
    candidate = Path(raw).expanduser()
    resolved = (
        candidate.resolve(strict=False)
        if candidate.is_absolute()
        else (root / candidate).resolve(strict=False)
    )
    try:
        resolved.relative_to(root.resolve(strict=False))
    except ValueError as error:
        raise PerformanceRuntimeError(
            "performance diagnostics only read storage inside project_root"
        ) from error
    return resolved


def _load_config_readonly(root: Path) -> tuple[dict[str, Any], str | None]:
    path = root / ".plot-rag" / "config.json"
    if not path.is_file():
        return {}, "missing .plot-rag/config.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        return {}, f"invalid config: {type(error).__name__}"
    if not isinstance(value, dict):
        return {}, "config root is not an object"
    return value, None


def _windows_sqlite_readonly_uri(path: str | os.PathLike[str]) -> str:
    """Return a SQLite URI that preserves Windows local and UNC semantics."""

    raw = os.fspath(path)
    if raw.startswith("\\\\?\\UNC\\"):
        raw = "\\\\" + raw[len("\\\\?\\UNC\\") :]
    elif raw.startswith("\\\\?\\"):
        raw = raw[len("\\\\?\\") :]
    return "file:" + quote(raw, safe="") + "?mode=ro"


def _sqlite_readonly_uri(path: Path) -> str:
    resolved = path.resolve(strict=False)
    if os.name == "nt":
        return _windows_sqlite_readonly_uri(resolved)
    return resolved.as_uri() + "?mode=ro"


def _open_readonly(path: Path) -> sqlite3.Connection:
    uri = _sqlite_readonly_uri(path)
    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(
            f'PRAGMA table_info("{table}")'
        )
    }


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _elapsed_ms(start: Any, end: Any) -> float | None:
    left = _parse_time(start)
    right = _parse_time(end)
    if left is None or right is None:
        return None
    elapsed = (right - left).total_seconds() * 1_000
    return elapsed if elapsed >= 0 else None


def _json_object(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def _collect_state_database(
    path: Path,
    collector: MutableMapping[str, Any],
    *,
    row_limit: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exists": path.is_file(),
        "read_only": True,
        "tables": {},
        "turn_statuses": {},
        "extraction_job_statuses": {},
    }
    if not path.is_file():
        return result
    with closing(_open_readonly(path)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "turns" in tables:
            columns = _table_columns(connection, "turns")
            selected = [
                column
                for column in (
                    "status",
                    "started_at",
                    "completed_at",
                    "authority_json",
                    "remote_json",
                    "result_json",
                )
                if column in columns
            ]
            order = (
                "started_at DESC"
                if "started_at" in columns
                else "rowid DESC"
            )
            rows = connection.execute(
                f"SELECT {','.join(selected)} FROM turns "
                f"ORDER BY {order} LIMIT ?",
                (max(1, min(int(row_limit), 100_000)),),
            ).fetchall()
            result["tables"]["turns"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM turns"
                ).fetchone()[0]
            )
            statuses: dict[str, int] = defaultdict(int)
            for row in rows:
                record = dict(row)
                statuses[str(record.get("status") or "unknown")] += 1
                explicit_before = sum(
                    len(values)
                    for values in collector["prepare"].values()
                )
                for json_field in (
                    "authority_json",
                    "remote_json",
                    "result_json",
                ):
                    _scan_runtime_payload(
                        _json_object(record.get(json_field)),
                        collector,
                        (json_field,),
                    )
                explicit_after = sum(
                    len(values)
                    for values in collector["prepare"].values()
                )
                if explicit_after == explicit_before:
                    elapsed = _elapsed_ms(
                        record.get("started_at"),
                        record.get("completed_at"),
                    )
                    if elapsed is not None:
                        collector["prepare"][
                            "lifecycle_fallback"
                        ].append(elapsed)
            result["turn_statuses"] = dict(sorted(statuses.items()))

        if "extraction_jobs" in tables:
            columns = _table_columns(connection, "extraction_jobs")
            selected = [
                column
                for column in (
                    "job_status",
                    "created_at",
                    "started_at",
                    "completed_at",
                    "attempt_count",
                    "remote_status",
                )
                if column in columns
            ]
            order = (
                "created_at DESC"
                if "created_at" in columns
                else "rowid DESC"
            )
            rows = connection.execute(
                f"SELECT {','.join(selected)} FROM extraction_jobs "
                f"ORDER BY {order} LIMIT ?",
                (max(1, min(int(row_limit), 100_000)),),
            ).fetchall()
            result["tables"]["extraction_jobs"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM extraction_jobs"
                ).fetchone()[0]
            )
            statuses: dict[str, int] = defaultdict(int)
            attempt_total = 0
            for row in rows:
                record = dict(row)
                status = str(record.get("job_status") or "unknown")
                statuses[status] += 1
                attempt_total += max(
                    0,
                    int(_finite_number(record.get("attempt_count")) or 0),
                )
                queue_wait = _elapsed_ms(
                    record.get("created_at"),
                    record.get("started_at"),
                )
                if queue_wait is not None:
                    collector["extraction"]["queue_wait"].append(queue_wait)
                if status == "succeeded":
                    ready = _elapsed_ms(
                        record.get("created_at"),
                        record.get("completed_at"),
                    )
                    if ready is not None:
                        collector["extraction"]["ready"].append(ready)
                remote_status = str(
                    record.get("remote_status") or ""
                ).casefold()
                if remote_status:
                    _add_remote(
                        collector,
                        "extract",
                        calls=1,
                        failures=(
                            1
                            if remote_status
                            in {"failed", "error", "timeout"}
                            else 0
                        ),
                    )
            result["extraction_job_statuses"] = dict(
                sorted(statuses.items())
            )
            result["extraction_attempts_observed"] = attempt_total
    return result


def _collect_authority_database(
    path: Path,
    collector: MutableMapping[str, Any],
) -> dict[str, Any]:
    result = {
        "exists": path.is_file(),
        "read_only": True,
        "candidate_cache_entries": 0,
    }
    if not path.is_file():
        return result
    with closing(_open_readonly(path)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "rerank_candidate_cache" in tables:
            entries = int(
                connection.execute(
                    "SELECT COUNT(*) FROM rerank_candidate_cache"
                ).fetchone()[0]
            )
            collector["cache"]["entries"] = max(
                collector["cache"]["entries"],
                entries,
            )
            result["candidate_cache_entries"] = entries
    return result


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canon_snapshot(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    state_config = config.get("state")
    state_path = _safe_project_path(
        root,
        state_config.get("db_path") if isinstance(state_config, Mapping) else None,
        ".plot-rag/state.sqlite3",
    )
    commit_dir = _safe_project_path(
        root,
        state_config.get("commit_dir") if isinstance(state_config, Mapping) else None,
        ".plot-rag/commits",
    )
    snapshot_path = _safe_project_path(
        root,
        state_config.get("snapshot_path")
        if isinstance(state_config, Mapping)
        else None,
        ".plot-rag/state_snapshot.json",
    )
    files: list[Path] = []
    for candidate in (
        state_path,
        state_path.with_name(state_path.name + "-wal"),
        state_path.with_name(state_path.name + "-shm"),
        snapshot_path,
    ):
        if candidate.is_file():
            files.append(candidate)
    if commit_dir.is_dir():
        files.extend(
            path
            for path in sorted(commit_dir.rglob("*"))
            if path.is_file()
        )
    records = []
    total_bytes = 0
    for path in files:
        size = path.stat().st_size
        total_bytes += size
        records.append(
            {
                "relative_name_sha256": hashlib.sha256(
                    path.relative_to(root).as_posix().encode("utf-8")
                ).hexdigest(),
                "size": size,
                "content_sha256": _hash_file(path),
            }
        )
    return {
        "digest_sha256": _sha256_json(records),
        "file_count": len(records),
        "byte_count": total_bytes,
    }


def _performance_config(config: Mapping[str, Any]) -> dict[str, Any]:
    performance = config.get("performance")
    if not isinstance(performance, Mapping):
        return {}

    def scalar_only(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): scalar_only(nested)
                for key, nested in value.items()
                if isinstance(
                    nested,
                    (Mapping, bool, int, float, type(None), list, tuple),
                )
            }
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            return [
                scalar_only(item)
                for item in value
                if isinstance(item, (bool, int, float, type(None)))
            ]
        return value

    return scalar_only(performance)


def get_status(project_root: str | Path) -> dict[str, Any]:
    """Return a redacted, read-only project performance snapshot."""

    root = Path(project_root).expanduser().resolve(strict=False)
    config, config_error = _load_config_readonly(root)
    before = _canon_snapshot(root, config)
    collector = _new_collector()
    state_config = config.get("state")
    state_path = _safe_project_path(
        root,
        state_config.get("db_path")
        if isinstance(state_config, Mapping)
        else None,
        ".plot-rag/state.sqlite3",
    )
    authority_path = _safe_project_path(
        root,
        None,
        ".plot-rag/authority.v1.sqlite3",
    )
    errors: list[str] = []
    try:
        state = _collect_state_database(
            state_path,
            collector,
            row_limit=DEFAULT_STATUS_ROW_LIMIT,
        )
    except (OSError, sqlite3.Error, PerformanceRuntimeError) as error:
        state = {"exists": state_path.is_file(), "read_only": True}
        errors.append(f"state telemetry unavailable: {type(error).__name__}")
    try:
        authority = _collect_authority_database(
            authority_path,
            collector,
        )
    except (OSError, sqlite3.Error) as error:
        authority = {
            "exists": authority_path.is_file(),
            "read_only": True,
            "candidate_cache_entries": 0,
        }
        errors.append(
            f"authority telemetry unavailable: {type(error).__name__}"
        )
    after = _canon_snapshot(root, config)
    canon_unchanged = before == after
    configured = config_error is None
    if not canon_unchanged:
        status = "changed_during_snapshot"
    elif errors:
        status = "degraded"
    elif configured:
        status = "ready"
    else:
        status = "unconfigured"
    report = {
        "schema_version": STATUS_SCHEMA_VERSION,
        "runtime_version": RUNTIME_VERSION,
        "status": status,
        "observed_at": _utc_now(),
        "project_id": _project_id(root),
        "configured": configured,
        "config_error": config_error,
        "read_only": True,
        "performance_config": _performance_config(config),
        "storage": {
            "state": state,
            "authority": authority,
        },
        "telemetry": _finalize_telemetry(collector),
        "canon_guard": {
            "scope": "accepted_state_surfaces",
            "unchanged": canon_unchanged,
            "before_sha256": before["digest_sha256"],
            "after_sha256": after["digest_sha256"],
            "file_count": after["file_count"],
            "byte_count": after["byte_count"],
        },
        "diagnostics": errors,
    }
    return _redact(report, secrets=_secret_values(report))


def _load_module_from_path(
    module_name: str,
    path: Path,
    *,
    import_overrides: Mapping[str, ModuleType] | None = None,
) -> ModuleType:
    existing = sys.modules.get(module_name)
    if isinstance(existing, ModuleType):
        existing_path = Path(str(getattr(existing, "__file__", "")))
        if existing_path.resolve(strict=False) != path.resolve(strict=False):
            raise PerformanceRuntimeError(
                f"isolated benchmark module collision: {module_name}"
            )
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PerformanceRuntimeError(
            f"benchmark module is not loadable: {path.name}"
        )
    module = importlib.util.module_from_spec(spec)
    if import_overrides:
        real_import = builtins.__import__

        def isolated_import(
            name: str,
            globals: Mapping[str, Any] | None = None,
            locals: Mapping[str, Any] | None = None,
            fromlist: Sequence[str] = (),
            level: int = 0,
        ) -> Any:
            if level == 0 and name in import_overrides:
                return import_overrides[name]
            return real_import(name, globals, locals, fromlist, level)

        isolated_builtins = dict(vars(builtins))
        isolated_builtins["__import__"] = isolated_import
        module.__dict__["__builtins__"] = isolated_builtins
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if sys.modules.get(module_name) is module:
            sys.modules.pop(module_name, None)
        raise
    return module


def _load_benchmark_module() -> ModuleType:
    """Load the checked-in benchmark without trusting ambient top-level packages."""

    global _BENCHMARK_MODULE
    with _BENCHMARK_IMPORT_LOCK:
        if _BENCHMARK_MODULE is not None:
            return _BENCHMARK_MODULE
        namespace = _BENCHMARK_IMPORT_NAMESPACE
        sqlite_guard = _load_module_from_path(
            f"{namespace}.sqlite_guard",
            PLUGIN_ROOT / "scripts" / "sqlite_guard.py",
        )
        authority = _load_module_from_path(
            f"{namespace}.authority",
            PLUGIN_ROOT / "scripts" / "longform" / "authority.py",
            import_overrides={"sqlite_guard": sqlite_guard},
        )
        sys_proxy = ModuleType(f"{namespace}.sys_proxy")
        sys_proxy.path = list(sys.path)  # type: ignore[attr-defined]
        benchmark = _load_module_from_path(
            f"{namespace}.v15_performance",
            PLUGIN_ROOT / "benchmarks" / "v15_performance.py",
            import_overrides={
                "longform.authority": authority,
                "sys": sys_proxy,
            },
        )
        _BENCHMARK_MODULE = benchmark
        return benchmark


def _load_benchmark_api() -> tuple[Any, Any, Any, Any]:
    benchmark = _load_benchmark_module()
    return (
        benchmark.DEFAULT_FIXTURE,
        benchmark.build_redacted_run_manifest,
        benchmark.run_v15_performance_benchmark,
        benchmark.validate_fixture_manifest,
    )


def _load_benchmark_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path)
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise PerformanceRuntimeError(
            "benchmark manifest is not readable JSON"
        ) from error
    if not isinstance(loaded, dict):
        raise PerformanceRuntimeError(
            "benchmark manifest root must be an object"
        )
    return loaded


def _resolve_benchmark_request(
    manifest: Mapping[str, Any] | str | Path | None,
    path: str | Path | Mapping[str, Any] | None,
    options: Mapping[str, Any] | None,
) -> tuple[Any, dict[str, Any]]:
    if isinstance(path, Mapping):
        if options is not None:
            raise PerformanceRuntimeError(
                "positional options and keyword options cannot both be used"
            )
        options = path
        path = None
    selected_manifest: Any = manifest
    merged_options: dict[str, Any] = dict(options or {})
    if (
        isinstance(manifest, Mapping)
        and not any(
            key in manifest
            for key in (
                "schema_version",
                "manifest_version",
                "suite",
                "sources",
                "files",
                "needs",
            )
        )
        and not any(key in manifest for key in ("manifest", "path", "options"))
    ):
        merged_options = dict(manifest) | merged_options
        selected_manifest = None
    if (
        isinstance(manifest, Mapping)
        and "schema_version" not in manifest
        and any(key in manifest for key in ("manifest", "path", "options"))
    ):
        wrapper = dict(manifest)
        embedded_options = wrapper.get("options")
        if isinstance(embedded_options, Mapping):
            merged_options = dict(embedded_options) | merged_options
        if path is None and wrapper.get("path") is not None:
            path = wrapper["path"]
        selected_manifest = wrapper.get("manifest")
    if selected_manifest is None:
        embedded_manifest = merged_options.pop("manifest", None)
        embedded_path = merged_options.pop("path", None)
        if embedded_manifest is not None and embedded_path is not None:
            raise PerformanceRuntimeError(
                "pass either manifest or path, not both"
            )
        selected_manifest = (
            embedded_manifest
            if embedded_manifest is not None
            else embedded_path
        )
    if selected_manifest is not None and path is not None:
        raise PerformanceRuntimeError(
            "pass either manifest or path, not both"
        )
    if path is not None:
        selected_manifest = path
    return selected_manifest, merged_options


def _benchmark_run_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": result.get("schema_version"),
        "suite": result.get("suite"),
        "runner_version": result.get("runner_version"),
        "execution_mode": result.get("execution_mode"),
        "network_required": bool(result.get("network_required")),
        "status": result.get("status"),
        "passed": bool(result.get("passed")),
        "fixture": deepcopy(result.get("fixture") or {}),
        "redacted_manifest": deepcopy(
            result.get("redacted_manifest") or {}
        ),
        "quality_gate": deepcopy(result.get("quality_gate") or {}),
        "scenario_quality": [
            {
                "scenario_id": scenario.get("scenario_id"),
                "need_count": scenario.get("need_count"),
                "embedding_batch_mode": scenario.get(
                    "embedding_batch_mode"
                ),
                "passed": bool(scenario.get("passed")),
                "quality_gate": deepcopy(
                    scenario.get("quality_gate") or {}
                ),
            }
            for scenario in result.get("scenarios", [])
            if isinstance(scenario, Mapping)
        ],
    }


def run_benchmark(
    project_root: str | Path,
    manifest: Mapping[str, Any] | str | Path | None = None,
    path: str | Path | Mapping[str, Any] | None = None,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the offline benchmark and return a redacted aggregate report.

    Supported options:

    ``iterations`` / ``runs``
        Number of repeated deterministic runs (1..100).
    ``rerank_delay_ms``
        Optional synthetic provider delay forwarded directly to the benchmark.
    ``workspace_parent``
        Existing directory used as the parent for temporary benchmark folders.
    ``telemetry`` / ``observations``
        Additional numeric runtime samples to aggregate.  Strings, prompts,
        paths, credentials, and prose are never copied to the result.
    """

    root = Path(project_root).expanduser().resolve(strict=False)
    selected_manifest, selected_options = _resolve_benchmark_request(
        manifest,
        path,
        options,
    )
    (
        default_fixture,
        build_redacted_run_manifest,
        benchmark,
        validate_manifest,
    ) = _load_benchmark_api()
    if selected_manifest is None:
        selected_manifest = default_fixture
    iterations_raw = selected_options.get(
        "iterations",
        selected_options.get("runs", 1),
    )
    if isinstance(iterations_raw, bool):
        raise PerformanceRuntimeError("iterations must be an integer")
    try:
        iterations = int(iterations_raw)
    except (TypeError, ValueError) as error:
        raise PerformanceRuntimeError(
            "iterations must be an integer"
        ) from error
    if not 1 <= iterations <= MAX_BENCHMARK_ITERATIONS:
        raise PerformanceRuntimeError(
            f"iterations must be between 1 and {MAX_BENCHMARK_ITERATIONS}"
        )
    delay_raw = selected_options.get("rerank_delay_ms")
    rerank_delay_ms = None
    if delay_raw is not None:
        if isinstance(delay_raw, bool):
            raise PerformanceRuntimeError(
                "rerank_delay_ms must be a non-negative integer"
            )
        try:
            rerank_delay_ms = int(delay_raw)
        except (TypeError, ValueError) as error:
            raise PerformanceRuntimeError(
                "rerank_delay_ms must be a non-negative integer"
            ) from error
        if rerank_delay_ms < 0:
            raise PerformanceRuntimeError(
                "rerank_delay_ms must be a non-negative integer"
            )
    workspace_parent = selected_options.get("workspace_parent")
    if workspace_parent is not None:
        workspace_parent = Path(workspace_parent).expanduser().resolve(
            strict=False
        )
        if not workspace_parent.is_dir():
            raise PerformanceRuntimeError(
                "workspace_parent must be an existing directory"
            )

    config, _config_error = _load_config_readonly(root)
    canon_before = _canon_snapshot(root, config)
    benchmark_manifest: Mapping[str, Any]
    if isinstance(selected_manifest, Mapping):
        benchmark_manifest = selected_manifest
        validation = validate_manifest(benchmark_manifest)
        redacted_manifest = build_redacted_run_manifest(
            benchmark_manifest
        )
    else:
        benchmark_manifest = _load_benchmark_manifest(selected_manifest)
        validation = validate_manifest(benchmark_manifest)
        redacted_manifest = build_redacted_run_manifest(
            benchmark_manifest
        )

    collector = _new_collector()
    raw_runs: list[dict[str, Any]] = []
    for _iteration in range(iterations):
        result = benchmark(
            benchmark_manifest,
            workspace_parent=workspace_parent,
            rerank_delay_ms=rerank_delay_ms,
        )
        raw_runs.append(result)
        _collect_benchmark(result, collector)
    for key in ("telemetry", "observations"):
        _collect_extra_telemetry(selected_options.get(key), collector)
    canon_after = _canon_snapshot(root, config)
    canon_unchanged = canon_before == canon_after
    all_passed = all(bool(result.get("passed")) for result in raw_runs)
    passed = all_passed and canon_unchanged
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "runtime_version": RUNTIME_VERSION,
        "suite": validation.get("suite"),
        "status": "passed" if passed else "failed",
        "passed": passed,
        "execution_mode": "offline",
        "network_required": False,
        "project_id": _project_id(root),
        "iterations": iterations,
        "manifest": redacted_manifest,
        "manifest_validation": validation,
        "telemetry": _finalize_telemetry(collector),
        "quality_gate": {
            "all_benchmark_runs_passed": all_passed,
            "canon_unchanged": canon_unchanged,
            "passed": passed,
        },
        "canon_guard": {
            "scope": "accepted_state_surfaces",
            "unchanged": canon_unchanged,
            "before_sha256": canon_before["digest_sha256"],
            "after_sha256": canon_after["digest_sha256"],
            "file_count": canon_after["file_count"],
            "byte_count": canon_after["byte_count"],
        },
        "runs": [
            _benchmark_run_summary(result)
            for result in raw_runs
        ],
    }
    secrets = _secret_values(
        {
            "options": selected_options,
            "report": report,
        }
    )
    return _redact(report, secrets=secrets)


def _load_report(value: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return deepcopy(dict(value))
    path = Path(value).expanduser()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise PerformanceRuntimeError(
            "performance report is not readable JSON"
        ) from error
    if not isinstance(loaded, dict):
        raise PerformanceRuntimeError(
            "performance report root must be an object"
        )
    return loaded


def _numeric_metrics(
    value: Any,
    path: tuple[str, ...] = (),
) -> dict[str, float]:
    metrics: dict[str, float] = {}
    if isinstance(value, Mapping):
        for key, nested in value.items():
            metrics.update(
                _numeric_metrics(nested, path + (str(key),))
            )
    else:
        number = _finite_number(value)
        if number is not None:
            metrics[".".join(path)] = number
    return metrics


def _metric_direction(name: str, delta: float) -> str:
    if abs(delta) <= 1e-12:
        return "unchanged"
    folded = name.casefold()
    lower_is_better = any(
        marker in folded
        for marker in (
            "_ms",
            ".failures",
            ".failure_rate",
            ".misses",
        )
    )
    higher_is_better = any(
        marker in folded
        for marker in (
            ".hit_rate",
            "accuracy",
            "recall",
            ".passed",
        )
    )
    if lower_is_better:
        return "improved" if delta < 0 else "regressed"
    if higher_is_better:
        return "improved" if delta > 0 else "regressed"
    return "increased" if delta > 0 else "decreased"


def compare_reports(
    left: Mapping[str, Any] | str | Path,
    right: Mapping[str, Any] | str | Path,
) -> dict[str, Any]:
    """Compare two performance reports without copying their raw contents."""

    left_report = _load_report(left)
    right_report = _load_report(right)
    left_telemetry = left_report.get("telemetry")
    right_telemetry = right_report.get("telemetry")
    if not isinstance(left_telemetry, Mapping) or not isinstance(
        right_telemetry, Mapping
    ):
        raise PerformanceRuntimeError(
            "both reports must contain telemetry objects"
        )
    left_metrics = _numeric_metrics(left_telemetry)
    right_metrics = _numeric_metrics(right_telemetry)
    shared = sorted(set(left_metrics) & set(right_metrics))
    changes: dict[str, Any] = {}
    regressions: list[str] = []
    improvements: list[str] = []
    for name in shared:
        before = left_metrics[name]
        after = right_metrics[name]
        delta = after - before
        direction = _metric_direction(name, delta)
        if direction == "regressed":
            regressions.append(name)
        elif direction == "improved":
            improvements.append(name)
        changes[name] = {
            "left": round(before, 8),
            "right": round(after, 8),
            "delta": round(delta, 8),
            "delta_percent": (
                None
                if abs(before) <= 1e-12
                else round((delta / before) * 100, 8)
            ),
            "direction": direction,
        }
    missing_left = sorted(set(right_metrics) - set(left_metrics))
    missing_right = sorted(set(left_metrics) - set(right_metrics))
    compatible = bool(shared)
    result = {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "runtime_version": RUNTIME_VERSION,
        "status": "compared" if compatible else "no_shared_metrics",
        "comparable": compatible,
        "left_report_sha256": _sha256_json(
            _redact(
                left_report,
                secrets=_secret_values(left_report),
            )
        ),
        "right_report_sha256": _sha256_json(
            _redact(
                right_report,
                secrets=_secret_values(right_report),
            )
        ),
        "left_schema_version": left_report.get("schema_version"),
        "right_schema_version": right_report.get("schema_version"),
        "changes": changes,
        "regressions": regressions,
        "improvements": improvements,
        "missing_from_left": missing_left,
        "missing_from_right": missing_right,
        "quality": {
            "left_passed": bool(left_report.get("passed")),
            "right_passed": bool(right_report.get("passed")),
            "regression_count": len(regressions),
            "improvement_count": len(improvements),
        },
    }
    return _redact(result, secrets=_secret_values(result))


__all__ = [
    "COMPARISON_SCHEMA_VERSION",
    "PerformanceRuntimeError",
    "REPORT_SCHEMA_VERSION",
    "RUNTIME_VERSION",
    "STATUS_SCHEMA_VERSION",
    "compare_reports",
    "get_status",
    "run_benchmark",
]
