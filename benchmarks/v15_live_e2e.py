from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
for candidate in (str(SCRIPTS_ROOT), str(PLUGIN_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import state_rag  # noqa: E402
import v1_runtime  # noqa: E402
from continuity import ContinuityService  # noqa: E402
from event_experience_runtime import ensure_locked_manifest  # noqa: E402
from extraction_jobs import (  # noqa: E402
    ExtractionJobQueue,
    ExtractionWorkResult,
)
from grill_gate import GrillGateService  # noqa: E402
from longform.continuity import ContextContractBuilder  # noqa: E402


REPORT_SCHEMA = "plot-rag-v15-live-e2e-report/v1"
PROVENANCE_SCHEMA = "plot-rag-benchmark-provenance/v1"
CHAT_EXTRACTION_SMOKE_SCHEMA = "plot-rag-v15-chat-extraction-smoke/v1"
PROMPT_FIXTURE_SCHEMA = "plot-rag-v15-live-prompts/v1"
STATE_MATRIX: tuple[dict[str, Any], ...] = (
    {
        "state": "FF",
        "enabled": False,
        "shadow": False,
        "expected_chosen_path": "v1",
    },
    {
        "state": "FT",
        "enabled": False,
        "shadow": True,
        "expected_chosen_path": "v1",
    },
    {
        "state": "TF",
        "enabled": True,
        "shadow": False,
        "expected_chosen_path": "v2",
    },
    {
        "state": "TT",
        "enabled": True,
        "shadow": True,
        "expected_chosen_path": "v1",
    },
)
SAFE_QUERY_STATUSES = {
    "ok",
    "candidate_cache_hit",
    "legacy_serial",
    "exact_state_short_circuit",
}
TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("sk_token", re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}")),
    ("sf_token", re.compile(r"(?i)\bsf-[A-Za-z0-9_-]{8,}")),
    ("ak_token", re.compile(r"(?i)\bak-[A-Za-z0-9_-]{8,}")),
    (
        "credential_field",
        re.compile(
            r"""(?ix)
            \b(
                authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|
                client[_-]?secret|password|passwd|secret|cookie
            )\b
            \s*[:=]\s*
            ["']?[^"'\s,;}]{6,}
            """
        ),
    ),
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_hash(value: Any) -> str:
    return _sha256_text(_canonical_json(value))


def _git_bytes(*arguments: str) -> bytes | None:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=PLUGIN_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return bytes(completed.stdout)


def _source_manifest() -> dict[str, Any]:
    tracked = _git_bytes(
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    if tracked is None:
        candidates = [
            Path(__file__).resolve(),
            (PLUGIN_ROOT / "benchmarks" / "run_v15_live_e2e.py").resolve(),
        ]
        scope = "runner_fallback"
    else:
        candidates = [
            PLUGIN_ROOT / os.fsdecode(raw_path)
            for raw_path in tracked.split(b"\0")
            if raw_path
        ]
        scope = "git_source_worktree"

    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        path = Path(candidate)
        if not _is_within(path, PLUGIN_ROOT):
            continue
        relative = unicodedata.normalize(
            "NFC",
            path.relative_to(PLUGIN_ROOT).as_posix(),
        )
        if path.is_symlink():
            payload = os.fsencode(os.readlink(path))
            kind = "symlink"
        elif path.is_file():
            payload = path.read_bytes()
            kind = "file"
        else:
            payload = b""
            kind = "missing"
        rows.append(
            {
                "path": relative,
                "kind": kind,
                "bytes": len(payload),
                "sha256": _sha256_bytes(payload),
            }
        )
    rows.sort(key=lambda item: str(item["path"]))
    return {
        "scope": scope,
        "file_count": len(rows),
        "sha256": _canonical_hash(rows),
    }


def collect_benchmark_provenance() -> dict[str, Any]:
    """Return credential-free build/runtime identity for benchmark replay."""

    source_manifest = _source_manifest()
    status = _git_bytes(
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    commit = _git_bytes("rev-parse", "--verify", "HEAD")
    tree = _git_bytes("rev-parse", "--verify", "HEAD^{tree}")
    status_bytes = status or b""
    dirty_fingerprint = _sha256_bytes(
        status_bytes
        + b"\0"
        + str(source_manifest["sha256"]).encode("ascii")
    )
    plugin_version = ""
    plugin_manifest = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
    if plugin_manifest.is_file():
        try:
            plugin_value = json.loads(
                plugin_manifest.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            plugin_value = {}
        if isinstance(plugin_value, Mapping):
            plugin_version = str(plugin_value.get("version") or "")

    runner_path = Path(__file__).resolve()
    cli_path = PLUGIN_ROOT / "benchmarks" / "run_v15_live_e2e.py"
    version_info = sys.version_info
    return {
        "schema_version": PROVENANCE_SCHEMA,
        "plugin": {
            "version": plugin_version,
            "git_available": commit is not None,
            "git_commit": (
                commit.decode("ascii", errors="replace").strip()
                if commit is not None
                else ""
            ),
            "git_tree": (
                tree.decode("ascii", errors="replace").strip()
                if tree is not None
                else ""
            ),
            "git_dirty": (
                bool(status_bytes.strip()) if status is not None else None
            ),
            "git_dirty_entry_count": (
                len(status_bytes.splitlines()) if status is not None else None
            ),
            "git_dirty_fingerprint_sha256": dirty_fingerprint,
            "worktree_source_scope": source_manifest["scope"],
            "worktree_source_file_count": source_manifest["file_count"],
            "worktree_source_sha256": source_manifest["sha256"],
        },
        "runner": {
            "module": "benchmarks.v15_live_e2e",
            "source_sha256": _sha256_bytes(runner_path.read_bytes()),
            "cli_source_sha256": (
                _sha256_bytes(cli_path.read_bytes())
                if cli_path.is_file()
                else ""
            ),
            "report_schema": REPORT_SCHEMA,
        },
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "version_info": {
                "major": int(version_info.major),
                "minor": int(version_info.minor),
                "micro": int(version_info.micro),
                "releaselevel": str(version_info.releaselevel),
                "serial": int(version_info.serial),
            },
            "compiler": platform.python_compiler(),
            "cache_tag": str(sys.implementation.cache_tag or ""),
            "byteorder": sys.byteorder,
        },
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "architecture": platform.architecture()[0],
        },
    }


def _normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", str(value)).replace("\r\n", "\n")
    return "\n".join(
        " ".join(line.split())
        for line in normalized.splitlines()
        if line.strip()
    )


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil((percentile / 100.0) * len(ordered)))
    return round(ordered[min(rank - 1, len(ordered) - 1)], 3)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _relative_nfc(path: Path, root: Path) -> str:
    return unicodedata.normalize(
        "NFC",
        path.relative_to(root).as_posix(),
    )


def _attributes(stat_result: os.stat_result) -> int:
    return int(getattr(stat_result, "st_file_attributes", 0) or 0)


def tree_snapshot(project_root: Path | str) -> dict[str, Any]:
    """Return the frozen three-hash tree snapshot used by the live harness."""

    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("project_root must be an existing directory")
    content_rows: list[str] = []
    metadata_rows: list[str] = []
    directory_rows: list[str] = []
    file_paths: list[str] = []
    directory_paths: list[str] = []
    file_mtimes: dict[str, int] = {}
    directory_mtimes: dict[str, int] = {}
    total_bytes = 0
    for current, directories, files in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        directories.sort(
            key=lambda value: unicodedata.normalize("NFC", value)
        )
        files.sort(key=lambda value: unicodedata.normalize("NFC", value))
        for name in directories:
            path = current_path / name
            relative = _relative_nfc(path, root)
            stat_result = path.stat(follow_symlinks=False)
            directory_paths.append(relative)
            directory_mtimes[relative] = int(stat_result.st_mtime_ns)
            directory_rows.append(
                "\t".join(
                    (
                        "D",
                        relative,
                        str(int(stat_result.st_mtime_ns)),
                        str(_attributes(stat_result)),
                    )
                )
            )
        for name in files:
            path = current_path / name
            relative = _relative_nfc(path, root)
            stat_result = path.stat(follow_symlinks=False)
            if path.is_symlink():
                payload = os.readlink(path).encode(
                    "utf-8",
                    errors="surrogatepass",
                )
            else:
                payload = path.read_bytes()
            digest = _sha256_bytes(payload)
            size = int(stat_result.st_size)
            total_bytes += size
            file_paths.append(relative)
            file_mtimes[relative] = int(stat_result.st_mtime_ns)
            content_rows.append(
                "\t".join(("F", relative, str(size), digest))
            )
            metadata_rows.append(
                "\t".join(
                    (
                        "F",
                        relative,
                        str(size),
                        str(int(stat_result.st_mtime_ns)),
                        str(_attributes(stat_result)),
                        digest,
                    )
                )
            )
    content_rows.sort()
    metadata_rows.sort()
    directory_rows.sort()
    file_paths.sort()
    directory_paths.sort()

    def rows_hash(rows: Sequence[str]) -> str:
        return _sha256_text("\n".join(rows) + ("\n" if rows else ""))

    return {
        "content_hash": rows_hash(content_rows),
        "metadata_hash": rows_hash(metadata_rows),
        "directory_hash": rows_hash(directory_rows),
        "file_count": len(file_paths),
        "directory_count": len(directory_paths),
        "total_bytes": total_bytes,
        "file_path_set_hash": _canonical_hash(file_paths),
        "directory_path_set_hash": _canonical_hash(directory_paths),
        "file_mtime_hash": _canonical_hash(file_mtimes),
        "directory_mtime_hash": _canonical_hash(directory_mtimes),
        "_file_paths": file_paths,
        "_directory_paths": directory_paths,
        "_file_mtimes": file_mtimes,
        "_directory_mtimes": directory_mtimes,
    }


def public_tree_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: snapshot[key]
        for key in (
            "content_hash",
            "metadata_hash",
            "directory_hash",
            "file_count",
            "directory_count",
            "total_bytes",
            "file_path_set_hash",
            "directory_path_set_hash",
            "file_mtime_hash",
            "directory_mtime_hash",
        )
    }


def compare_tree_snapshots(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    keys = (
        "content_hash",
        "metadata_hash",
        "directory_hash",
        "file_count",
        "directory_count",
        "total_bytes",
        "file_path_set_hash",
        "directory_path_set_hash",
        "file_mtime_hash",
        "directory_mtime_hash",
    )
    mismatches = [key for key in keys if before.get(key) != after.get(key)]
    return {
        "unchanged": not mismatches,
        "mismatch_fields": mismatches,
        "before": public_tree_snapshot(before),
        "after": public_tree_snapshot(after),
    }


def load_prompt_fixture(path: Path | str) -> list[dict[str, Any]]:
    fixture_path = Path(path).expanduser().resolve()
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != PROMPT_FIXTURE_SCHEMA:
        raise ValueError("unexpected live prompt fixture schema")
    prompts = payload.get("prompts")
    if not isinstance(prompts, list) or len(prompts) < 25:
        raise ValueError("live prompt fixture must contain at least 25 prompts")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(prompts):
        if not isinstance(raw, Mapping):
            raise ValueError(f"prompts[{index}] must be an object")
        prompt_id = str(raw.get("prompt_id") or "").strip()
        prompt = str(raw.get("prompt") or "").strip()
        if not prompt_id or prompt_id in seen or not prompt:
            raise ValueError(f"prompts[{index}] has an invalid identity or text")
        seen.add(prompt_id)
        normalized.append(
            {
                "prompt_id": prompt_id,
                "prompt": prompt,
                "artifact_stage": str(
                    raw.get("artifact_stage") or "outline"
                ),
                "task": str(raw.get("task") or "outline"),
                "branch_id": str(raw.get("branch_id") or "main"),
                "chapter_no": raw.get("chapter_no"),
                "scene_index": raw.get("scene_index"),
            }
        )
    return normalized


def load_jsonl_annotations(path: Path | str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        Path(path).expanduser().resolve().read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"annotation line {line_number} must be an object")
        values.append(value)
    return values


class RemoteCallRecorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: list[dict[str, Any]] = []

    def mark(self) -> int:
        with self._lock:
            return len(self._calls)

    def record(
        self,
        *,
        service: str,
        model: str,
        latency_ms: float,
        status: str,
        call_kind: str,
    ) -> None:
        with self._lock:
            self._calls.append(
                {
                    "service": str(service),
                    "model": str(model),
                    "latency_ms": round(float(latency_ms), 3),
                    "status": str(status),
                    "call_kind": str(call_kind),
                }
            )

    def summary(self, start: int = 0, end: int | None = None) -> dict[str, Any]:
        with self._lock:
            calls = list(self._calls[start:end])
        by_service: dict[str, dict[str, Any]] = {}
        for service in sorted({str(call["service"]) for call in calls}):
            selected = [
                call for call in calls if str(call["service"]) == service
            ]
            by_service[service] = {
                "call_count": len(selected),
                "models": sorted(
                    {str(call["model"]) for call in selected if call["model"]}
                ),
                "status_counts": dict(
                    sorted(Counter(call["status"] for call in selected).items())
                ),
                "latency_sum_ms": round(
                    sum(float(call["latency_ms"]) for call in selected),
                    3,
                ),
                "latency_p50_ms": _percentile(
                    [float(call["latency_ms"]) for call in selected],
                    50,
                ),
                "latency_p95_ms": _percentile(
                    [float(call["latency_ms"]) for call in selected],
                    95,
                ),
            }
        return {
            "call_count": len(calls),
            "by_service": by_service,
        }


def _offline_vector(text: str, dimensions: int = 48) -> list[float]:
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    values: list[float] = []
    block = seed
    while len(values) < dimensions:
        for byte in block:
            values.append((float(byte) / 127.5) - 1.0)
            if len(values) == dimensions:
                break
        block = hashlib.sha256(block).digest()
    return values


def _lexical_units(value: str) -> set[str]:
    normalized = _normalized_text(value).casefold()
    units = {char for char in normalized if not char.isspace()}
    units.update(
        normalized[index : index + 2]
        for index in range(max(0, len(normalized) - 1))
        if not normalized[index : index + 2].isspace()
    )
    return units


@contextlib.contextmanager
def transport_context(
    mode: str,
    recorder: RemoteCallRecorder,
) -> Iterator[None]:
    normalized = str(mode).strip().casefold()
    if normalized not in {"offline", "live"}:
        raise ValueError("transport mode must be offline or live")
    if normalized == "offline":

        def embedding(
            service: Any,
            inputs: Sequence[str],
        ) -> tuple[list[list[float]], dict[str, Any]]:
            started = time.perf_counter()
            vectors = [_offline_vector(value) for value in inputs]
            elapsed = (time.perf_counter() - started) * 1000.0
            recorder.record(
                service=str(service.name),
                model=str(service.model),
                latency_ms=elapsed,
                status="ok",
                call_kind="embedding",
            )
            return vectors, {
                "status": "ok",
                "configured": True,
                "model": str(service.model),
                "latency_ms": round(elapsed, 3),
                "transport": "offline",
            }

        def rerank(
            service: Any,
            query: str,
            documents: Sequence[str],
            top_n: int,
        ) -> tuple[list[tuple[int, float]], dict[str, Any]]:
            started = time.perf_counter()
            query_units = _lexical_units(query)
            rows: list[tuple[int, float]] = []
            for index, document in enumerate(documents):
                document_units = _lexical_units(document)
                overlap = len(query_units.intersection(document_units))
                denominator = max(1, len(query_units.union(document_units)))
                tie = int(
                    _sha256_text(f"{query}\0{document}")[:12],
                    16,
                ) / float(16**12)
                rows.append(
                    (
                        index,
                        (overlap / denominator) + (tie * 1e-9),
                    )
                )
            rows.sort(key=lambda item: (-item[1], item[0]))
            elapsed = (time.perf_counter() - started) * 1000.0
            recorder.record(
                service=str(service.name),
                model=str(service.model),
                latency_ms=elapsed,
                status="ok",
                call_kind="rerank",
            )
            return rows[: int(top_n)], {
                "status": "ok",
                "configured": True,
                "model": str(service.model),
                "latency_ms": round(elapsed, 3),
                "transport": "offline",
            }

        with mock.patch.object(
            state_rag,
            "_embedding_call",
            new=embedding,
        ), mock.patch.object(
            state_rag,
            "_rerank_call",
            new=rerank,
        ):
            yield
        return

    if not str(os.environ.get("SILICONFLOW_API_KEY") or "").strip():
        raise ValueError("SILICONFLOW_API_KEY is required for live transport")
    original = state_rag._remote_json

    def counted_remote(
        service: Any,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        started = time.perf_counter()
        status = "error"
        try:
            result = original(service, payload)
            status = str((result[1] or {}).get("status") or "ok")
            return result
        finally:
            recorder.record(
                service=str(service.name),
                model=str(service.model),
                latency_ms=(time.perf_counter() - started) * 1000.0,
                status=status,
                call_kind=str(service.name),
            )

    with mock.patch.object(
        state_rag,
        "_remote_json",
        new=counted_remote,
    ):
        yield


def _remote_latency_sum(summary: Mapping[str, Any]) -> float:
    return round(
        sum(
            float(service.get("latency_sum_ms") or 0.0)
            for service in (summary.get("by_service") or {}).values()
            if isinstance(service, Mapping)
        ),
        3,
    )


def _remote_error_call_count(summary: Mapping[str, Any]) -> int:
    """Count failed logical remote calls in a recorder summary.

    The transport wrapper records one logical call after bounded retries.  A
    retry that eventually succeeds is therefore reported as ``ok`` and does
    not trip this gate; a call that exhausts retries remains a non-``ok``
    status.  The count deliberately walks every service so failures in a
    non-authoritative shadow path cannot disappear behind a healthy chosen
    path.
    """

    total = 0
    for service in (summary.get("by_service") or {}).values():
        if not isinstance(service, Mapping):
            continue
        statuses = service.get("status_counts") or {}
        if isinstance(statuses, Mapping):
            total += sum(
                int(count or 0)
                for status, count in statuses.items()
                if str(status) not in {"ok", "not_called"}
            )
    return total


def _validate_chat_extraction_smoke_semantics(
    deltas: Sequence[Any],
    skipped: Sequence[Any],
    assistant_text: str,
) -> dict[str, Any]:
    expected_subject = "基准角色甲"
    expected_object = "基准南站"
    failure_codes: set[str] = set()
    invalid_delta_indices: list[int] = []
    target_movement_count = 0

    if not deltas:
        failure_codes.add("CHAT_SMOKE_NO_DELTAS")
    if len(deltas) != 1:
        failure_codes.add("CHAT_SMOKE_UNEXPECTED_DELTA_COUNT")
    if skipped:
        failure_codes.add("CHAT_SMOKE_SKIPPED_DELTAS")

    for index, delta in enumerate(deltas):
        delta_failure_codes: set[str] = set()
        if not isinstance(delta, Mapping):
            delta_failure_codes.add("CHAT_SMOKE_INVALID_DELTA_SHAPE")
        else:
            if delta.get("event_type") != "movement":
                delta_failure_codes.add("CHAT_SMOKE_EVENT_TYPE_MISMATCH")
            if delta.get("action") not in {"arrive", "enter"}:
                delta_failure_codes.add("CHAT_SMOKE_ACTION_MISMATCH")
            if delta.get("subject") != expected_subject:
                delta_failure_codes.add("CHAT_SMOKE_SUBJECT_MISMATCH")
            if delta.get("object") != expected_object:
                delta_failure_codes.add("CHAT_SMOKE_OBJECT_MISMATCH")
            if delta.get("field") != "current":
                delta_failure_codes.add("CHAT_SMOKE_FIELD_MISMATCH")
            if not isinstance(delta.get("value"), dict) or delta.get("value"):
                delta_failure_codes.add("CHAT_SMOKE_VALUE_MISMATCH")
            if delta.get("story_coordinate") is not None:
                delta_failure_codes.add(
                    "CHAT_SMOKE_STORY_COORDINATE_FORBIDDEN"
                )

            confidence = delta.get("confidence")
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(float(confidence))
                or not 0.0 < float(confidence) <= 1.0
            ):
                delta_failure_codes.add("CHAT_SMOKE_CONFIDENCE_INVALID")

            evidence = delta.get("evidence")
            if (
                not isinstance(evidence, str)
                or not evidence
                or evidence not in assistant_text
            ):
                delta_failure_codes.add("CHAT_SMOKE_EVIDENCE_NOT_EXACT")
            elif (
                expected_subject not in evidence
                or expected_object not in evidence
            ):
                delta_failure_codes.add(
                    "CHAT_SMOKE_EVIDENCE_DOES_NOT_SUPPORT_LOCATION"
                )

        if delta_failure_codes:
            invalid_delta_indices.append(index)
            failure_codes.update(delta_failure_codes)
        else:
            target_movement_count += 1

    if target_movement_count != 1:
        failure_codes.add("CHAT_SMOKE_TARGET_MOVEMENT_COUNT_MISMATCH")

    passed = bool(
        len(deltas) == 1
        and not skipped
        and target_movement_count == 1
        and not invalid_delta_indices
    )
    return {
        "passed": passed,
        "status": "valid" if passed else "invalid",
        "target_movement_count": target_movement_count,
        "invalid_delta_count": len(invalid_delta_indices),
        "invalid_delta_indices": invalid_delta_indices,
        "failure_codes": sorted(failure_codes),
    }


def run_live_chat_extraction_smoke(
    project_root: Path | str,
    recorder: RemoteCallRecorder,
) -> dict[str, Any]:
    """Measure one real configured Chat extraction without persisting deltas."""

    root = Path(project_root).expanduser().resolve()
    runtime = state_rag._load_runtime_config(root)
    prompt = (
        "记录基准角色甲当前所在位置；只提取文本明确建立的持久事实。"
        "只提取位置；非物品 movement 必须严格包含完整 v3 字段 "
        "event_type、action、subject、object、field、value、confidence、evidence，"
        "其中 field=current、value={}；不要输出 story_coordinate。"
    )
    assistant_text = "基准角色甲已经抵达基准南站。"
    start_call = recorder.mark()
    started = time.perf_counter()
    try:
        deltas, skipped, status = state_rag._chat_extract(
            runtime,
            assistant_text,
            prompt,
            [],
        )
    except Exception as exc:
        wall_ms = round((time.perf_counter() - started) * 1000.0, 3)
        remote_calls = recorder.summary(start_call)
        remote_ms = _remote_latency_sum(remote_calls)
        return {
            "schema_version": CHAT_EXTRACTION_SMOKE_SCHEMA,
            "requested": True,
            "executed": True,
            "passed": False,
            "status": "error",
            "transport": "siliconflow_chat",
            "service": "extract",
            "model": str(runtime.extract.model),
            "endpoint": str(runtime.extract.endpoint),
            "mutates_continuity": False,
            "prompt_sha256": _sha256_text(prompt),
            "assistant_text_sha256": _sha256_text(assistant_text),
            "wall_ms": wall_ms,
            "remote_latency_sum_ms": remote_ms,
            "local_overhead_ms": round(max(0.0, wall_ms - remote_ms), 3),
            "attempt_count": int(remote_calls.get("call_count") or 0),
            "delta_count": 0,
            "skipped_delta_count": 0,
            "remote_calls": remote_calls,
            "error_type": type(exc).__name__,
            "error_sha256": _sha256_text(str(exc)),
        }

    wall_ms = round((time.perf_counter() - started) * 1000.0, 3)
    remote_calls = recorder.summary(start_call)
    remote_ms = _remote_latency_sum(remote_calls)
    public_status = {
        key: status[key]
        for key in (
            "status",
            "http_status",
            "latency_ms",
            "service",
            "model",
            "attempts",
            "repair_applied",
            "repair_reason",
        )
        if key in status
    }
    semantic_validation = _validate_chat_extraction_smoke_semantics(
        deltas,
        skipped,
        assistant_text,
    )
    passed = bool(semantic_validation["passed"])
    return {
        "schema_version": CHAT_EXTRACTION_SMOKE_SCHEMA,
        "requested": True,
        "executed": True,
        "passed": passed,
        "status": "passed" if passed else "semantic_validation_failed",
        "transport": "siliconflow_chat",
        "service": "extract",
        "model": str(runtime.extract.model),
        "endpoint": str(runtime.extract.endpoint),
        "mutates_continuity": False,
        "prompt_sha256": _sha256_text(prompt),
        "assistant_text_sha256": _sha256_text(assistant_text),
        "wall_ms": wall_ms,
        "remote_latency_sum_ms": remote_ms,
        "local_overhead_ms": round(max(0.0, wall_ms - remote_ms), 3),
        "attempt_count": int(
            public_status.get("attempts")
            or remote_calls.get("call_count")
            or 0
        ),
        "delta_count": len(deltas),
        "delta_sha256": _canonical_hash(deltas),
        "skipped_delta_count": len(skipped),
        "skipped_delta_sha256": _canonical_hash(skipped),
        "semantic_validation": semantic_validation,
        "remote_status": public_status,
        "remote_calls": remote_calls,
    }


def _configure_state(
    project_root: Path,
    *,
    enabled: bool,
    shadow: bool,
    strict: bool = False,
) -> None:
    path = project_root / ".plot-rag" / "config.json"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PLUGIN_ROOT / "templates" / "config.v3.json", path)
    config = json.loads(path.read_text(encoding="utf-8"))
    config["config_version"] = 3
    config["enabled"] = True
    performance = dict(config.get("performance") or {})
    prepare = dict(performance.get("prepare_v2") or {})
    prepare.update(
        {
            "enabled": bool(enabled),
            "shadow": bool(shadow),
            "single_read_snapshot": True,
            "exact_state_short_circuit": True,
            "batch_embedding": True,
            "batch_failure_fallback_single": True,
            "rerank_max_concurrency": max(
                1,
                int(prepare.get("rerank_max_concurrency") or 4),
            ),
            "remote_total_concurrency": max(
                1,
                int(prepare.get("remote_total_concurrency") or 6),
            ),
            "singleflight": True,
            "persistent_exact_cache": True,
            "http_keep_alive": True,
        }
    )
    extraction = dict(performance.get("extraction") or {})
    if strict:
        extraction.update(
            {
                "mode": "async",
                "async_shadow": False,
                "next_plot_turn_barrier": True,
                "barrier_requires_proposal_resolution": True,
            }
        )
    performance["prepare_v2"] = prepare
    performance["extraction"] = extraction
    config["performance"] = performance
    craft = dict(config.get("craft") or {})
    craft.update(
        {
            "enabled": True,
            "auto_retrieve": True,
            "use_embedding": True,
            "use_rerank": True,
        }
    )
    config["craft"] = craft
    if strict:
        lifecycle = dict(config.get("lifecycle") or {})
        lifecycle["strict"] = True
        config["lifecycle"] = lifecycle
        event_experience = dict(config.get("event_experience") or {})
        event_experience.update(
            {
                "enabled": True,
                "required_before_event_design": True,
                "event_seed_required": True,
                "receipt_hash_binding": True,
            }
        )
        config["event_experience"] = event_experience
        items = dict(config.get("items") or {})
        items.update(
            {
                "schema_version": "plot-rag-item/v1",
                "delta_version": "plot-rag-delta/v4",
                "strict_runtime_validation": True,
                "power_binding_bridge": True,
                "readable_projection": True,
            }
        )
        config["items"] = items
    path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def create_isolated_states(
    project_root: Path | str,
    workspace_root: Path | str,
) -> tuple[Path, dict[str, Path], Path]:
    source = Path(project_root).expanduser().resolve()
    workspace = Path(workspace_root).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    seed = workspace / "seed"
    shutil.copytree(source, seed, copy_function=shutil.copy2)
    states: dict[str, Path] = {}
    for spec in STATE_MATRIX:
        target = workspace / f"state-{spec['state']}"
        shutil.copytree(seed, target, copy_function=shutil.copy2)
        _configure_state(
            target,
            enabled=bool(spec["enabled"]),
            shadow=bool(spec["shadow"]),
        )
        states[str(spec["state"])] = target
    strict_root = workspace / "state-STRICT-TF"
    shutil.copytree(seed, strict_root, copy_function=shutil.copy2)
    _configure_state(
        strict_root,
        enabled=True,
        shadow=False,
        strict=True,
    )
    return seed, states, strict_root


def _query_observation(contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for item in contract.get("needs") or []:
        query = str(item.get("query") or "")
        values.append(
            {
                "need_index": int(item.get("need_index") or 0),
                "category": str(item.get("category") or ""),
                "mandatory": bool(item.get("mandatory")),
                "query_sha256": _sha256_text(query),
                "query_chars": len(query),
            }
        )
    return values


def _critical_fact_observation(result: Mapping[str, Any]) -> dict[str, Any]:
    precise = list((result.get("precise") or {}).get("facts") or [])
    open_loops = list((result.get("open_loops") or {}).get("facts") or [])
    power = result.get("power_state") or {}
    item = result.get("item_context") or {}
    item_records = list(item.get("records") or [])
    return {
        "precise": {
            "count": len(precise),
            "sha256": _canonical_hash(precise),
        },
        "open_loops": {
            "count": len(open_loops),
            "sha256": _canonical_hash(open_loops),
        },
        "power": {
            "count": len(power.get("actors") or power.get("states") or []),
            "sha256": _canonical_hash(power),
        },
        "items": {
            "required": bool(item.get("required")),
            "status": str(item.get("status") or ""),
            "record_count": len(item_records),
            "inventory_count": int(
                item.get("accepted_inventory_count") or 0
            ),
            "sha256": _canonical_hash(item_records),
        },
    }


def _degraded_increment(result: Mapping[str, Any]) -> int:
    if str(result.get("status") or "") not in {"ready", "completed"}:
        return 1
    retrieval = dict(
        (result.get("contract") or {}).get("retrieval_telemetry") or {}
    )
    for query in retrieval.get("queries") or []:
        if str(query.get("status") or "") not in SAFE_QUERY_STATUSES:
            return 1
        if str(query.get("embedding_status") or "ok") not in {
            "ok",
            "not_called",
        }:
            return 1
        if any(
            str(status) not in {"ok", "not_called"}
            for status in query.get("rerank_statuses") or []
        ):
            return 1
    return 0


def observe_round(
    result: Mapping[str, Any],
    *,
    state: str,
    prompt_id: str,
    wall_ms: float,
    remote_calls: Mapping[str, Any],
) -> dict[str, Any]:
    contract = dict(result.get("contract") or {})
    telemetry = dict(result.get("telemetry") or {})
    rollout = dict(telemetry.get("prepare_v2") or {})
    chosen = str(rollout.get("chosen_path") or "")
    path_record = dict(rollout.get(chosen) or {})
    selected = v1_runtime._prepare_v2_selected_chunks(contract)
    task = str(contract.get("task") or "prose")
    roles = list(
        ContextContractBuilder.TASK_ROLE_ORDER.get(
            task,
            ("canon", "setting"),
        )
    )
    normalized_contract = _normalized_text(
        str(contract.get("context_text") or "")
    )
    normalized_context = _normalized_text(str(result.get("context") or ""))
    stage_timings = {
        key: round(float(telemetry.get(key) or 0.0), 3)
        for key in (
            "authority_refresh_ms",
            "exact_state_ms",
            "item_context_ms",
            "authority_contract_ms",
            "embedding_batch_ms",
            "rerank_wall_ms",
            "rerank_sum_ms",
            "context_assembly_ms",
            "longform_total_ms",
        )
    }
    return {
        "prompt_id": prompt_id,
        "state": state,
        "status": str(result.get("status") or ""),
        "chosen_path": chosen,
        "executed_paths": list(rollout.get("executed_paths") or []),
        "internal_comparison": dict(rollout.get("comparison") or {}),
        "needs": _query_observation(contract),
        "needs_sha256": _canonical_hash(contract.get("needs") or []),
        "query_contract": {
            "roles": roles,
            "scope_policies": None,
            "ingest_policies": ["include", "review"],
            "authority_limit": 12,
            "embedding_model": str(
                (result.get("index") or {})
                .get("query_policy", {})
                .get("embedding_model")
                or ""
            ),
            "rerank_model": str(
                (result.get("index") or {})
                .get("query_policy", {})
                .get("rerank_model")
                or ""
            ),
        },
        "selected_chunks_sha256": _canonical_hash(selected),
        "selected_chunk_count": sum(len(items) for items in selected),
        "selected_order_sha256": _canonical_hash(
            [
                [
                    (
                        item.get("chunk_id"),
                        item.get("ordinal"),
                        item.get("content_sha256"),
                    )
                    for item in items
                ]
                for items in selected
            ]
        ),
        "semantic_hash": str(path_record.get("semantic_hash") or ""),
        "contract_context_hash": str(
            path_record.get("context_hash")
            or _sha256_text(normalized_contract)
        ),
        "normalized_contract_sha256": _sha256_text(normalized_contract),
        "normalized_context_sha256": _sha256_text(normalized_context),
        "critical_facts": _critical_fact_observation(result),
        "degraded_turn_increment": _degraded_increment(result),
        "wall_ms": round(float(wall_ms), 3),
        "stage_timings_ms": stage_timings,
        "remote_calls": dict(remote_calls),
    }


def compare_prompt_rounds(
    prompt_id: str,
    rounds: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    reference = rounds["FF"]
    fields = (
        "needs_sha256",
        "query_contract",
        "selected_chunks_sha256",
        "selected_order_sha256",
        "semantic_hash",
        "contract_context_hash",
        "normalized_contract_sha256",
        "normalized_context_sha256",
        "critical_facts",
    )
    mismatches: list[dict[str, Any]] = []
    expected_paths = {
        str(item["state"]): str(item["expected_chosen_path"])
        for item in STATE_MATRIX
    }
    for state in ("FF", "FT", "TF", "TT"):
        observed = rounds[state]
        if observed.get("chosen_path") != expected_paths[state]:
            mismatches.append(
                {
                    "state": state,
                    "field": "chosen_path",
                    "expected_sha256": _sha256_text(expected_paths[state]),
                    "actual_sha256": _sha256_text(
                        str(observed.get("chosen_path") or "")
                    ),
                }
            )
        for field in fields:
            if observed.get(field) != reference.get(field):
                mismatches.append(
                    {
                        "state": state,
                        "field": field,
                        "expected_sha256": _canonical_hash(
                            reference.get(field)
                        ),
                        "actual_sha256": _canonical_hash(
                            observed.get(field)
                        ),
                    }
                )
        if state in {"FT", "TT"} and not bool(
            (observed.get("internal_comparison") or {}).get("equivalent")
        ):
            mismatches.append(
                {
                    "state": state,
                    "field": "internal_v1_v2_equivalence",
                    "expected_sha256": _sha256_text("true"),
                    "actual_sha256": _sha256_text("false"),
                }
            )
    return {
        "prompt_id": prompt_id,
        "passed": not mismatches,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }


def _strict_sentences() -> dict[str, str]:
    return {
        "relation": "基准角色甲和基准角色乙建立了临时信任。",
        "movement": "基准角色甲抵达基准南站。",
        "time": "故事时间推进到基准历第9001刻。",
        "definition": "基准刃被明确记录为唯一武器。",
        "function": "基准刃具有消耗能量进行切割的功能。",
        "binding": "切割功能被绑定到基准刃。",
        "instance": "基准刃实例出现在基准角色甲手中。",
        "custody": "基准刃归基准角色甲所有并由他携带。",
        "equip": "基准角色甲把基准刃装备在右手。",
        "use": "基准角色甲发动了基准刃的切割功能。",
        "observation": "基准角色乙观察到基准刃出现轻微磨损。",
    }


def _strict_events(
    service: ContinuityService,
) -> tuple[list[dict[str, Any]], dict[str, str], str]:
    actor = service.register_entity("character", "基准角色甲")["entity_id"]
    observer = service.register_entity("character", "基准角色乙")["entity_id"]
    location = service.register_entity("location", "基准南站")["entity_id"]
    sentences = _strict_sentences()
    assistant_text = "\n".join(sentences.values())
    coordinate = {"calendar_id": "v15-benchmark", "ordinal": 9001}

    def item_event(
        event_type: str,
        action: str,
        quote: str,
        **fields: Any,
    ) -> dict[str, Any]:
        return {
            "schema_version": "plot-rag-delta/v4",
            "event_type": event_type,
            "action": action,
            "story_coordinate": dict(coordinate),
            "knowledge_plane": "objective",
            "evidence": {"quote": quote},
            **fields,
        }

    events: list[dict[str, Any]] = [
        {
            "event_type": "relation",
            "source_entity_id": actor,
            "target_entity_id": observer,
            "dimension": "trust",
            "value": 0.6,
            "evidence": {"quote": sentences["relation"]},
        },
        {
            "event_type": "movement",
            "actor_entity_id": actor,
            "to_location_entity_id": location,
            "action": "arrive",
            "story_coordinate": dict(coordinate),
            "evidence": {"quote": sentences["movement"]},
        },
        {
            "event_type": "time",
            "field": "current_time",
            "value": "基准历第9001刻",
            "story_coordinate": dict(coordinate),
            "evidence": {"quote": sentences["time"]},
        },
        item_event(
            "item_spec",
            "define",
            sentences["definition"],
            spec_type="item_definition",
            spec_id="bench_v15_definition_blade",
            definition={
                "name": "基准刃",
                "item_kind": "weapon",
                "stack_policy": "non_stackable",
                "uniqueness_policy": "unique_definition",
                "max_durability": 10,
                "max_energy": 5,
            },
        ),
        item_event(
            "item_spec",
            "define",
            sentences["function"],
            spec_type="function_definition",
            spec_id="bench_v15_function_cut",
            definition={
                "item_definition_id": "bench_v15_definition_blade",
                "effect_owner": "inline",
                "inline_effects": [{"kind": "cut"}],
                "charges": 2,
                "durability_cost": 1,
                "costs": [{"kind": "energy", "amount": 2}],
                "cooldown": 2,
            },
        ),
        item_event(
            "item_spec",
            "define",
            sentences["binding"],
            spec_type="function_binding",
            spec_id="bench_v15_binding_cut",
            definition={
                "item_definition_id": "bench_v15_definition_blade",
                "function_id": "bench_v15_function_cut",
            },
        ),
        item_event(
            "item_instance",
            "instantiate",
            sentences["instance"],
            subject_type="item_instance",
            subject_id="bench_v15_instance_blade",
            item_instance_id="bench_v15_instance_blade",
            item_definition_id="bench_v15_definition_blade",
            attributes={},
        ),
        item_event(
            "item_custody",
            "acquire",
            sentences["custody"],
            subject_type="item_instance",
            subject_id="bench_v15_instance_blade",
            item_instance_id="bench_v15_instance_blade",
            to_legal_owner_entity_id=actor,
            to_custodian_entity_id=actor,
            to_carrier_entity_id=actor,
        ),
        item_event(
            "item_runtime",
            "equip",
            sentences["equip"],
            subject_type="item_instance",
            subject_id="bench_v15_instance_blade",
            item_instance_id="bench_v15_instance_blade",
            actor_entity_id=actor,
            slot_key="right_hand",
            delta={},
        ),
        item_event(
            "item_use",
            "use",
            sentences["use"],
            subject_type="item_instance",
            subject_id="bench_v15_instance_blade",
            item_instance_id="bench_v15_instance_blade",
            actor_entity_id=actor,
            function_id="bench_v15_function_cut",
            delta={},
        ),
        item_event(
            "item_observation",
            "observe",
            sentences["observation"],
            subject_type="item_instance",
            subject_id="bench_v15_instance_blade",
            item_instance_id="bench_v15_instance_blade",
            observer_entity_id=observer,
            function_id="bench_v15_function_cut",
            knowledge_plane="actor_belief",
            observation={"durability": "slightly_worn"},
        ),
    ]
    return events, {
        "actor": str(actor),
        "observer": str(observer),
        "location": str(location),
    }, assistant_text


def _item_surfaces(
    service: ContinuityService,
    identities: Mapping[str, str],
) -> dict[str, Any]:
    definition = service.query_item_definition(
        "bench_v15_definition_blade"
    )
    instance = service.query_item_instance("bench_v15_instance_blade")
    function = service.query_item_function(
        "bench_v15_function_cut",
        item_instance_id="bench_v15_instance_blade",
    )
    history = service.query_item_history(
        item_instance_id="bench_v15_instance_blade"
    )
    observations = service.query_item_observations(
        item_instance_id="bench_v15_instance_blade",
        observer_entity_id=identities["observer"],
        knowledge_plane="actor_belief",
    )
    surfaces = {
        "definition": definition,
        "instance": {
            key: value
            for key, value in instance.items()
            if key not in {"custody", "runtime", "function_runtime"}
        },
        "custody": instance.get("custody"),
        "function": function,
        "runtime": {
            "item": instance.get("runtime"),
            "function": instance.get("function_runtime"),
        },
        "history": history,
        "observation": observations,
    }
    return {
        "surface_count": len(surfaces),
        "surface_hashes": {
            key: _canonical_hash(value)
            for key, value in sorted(surfaces.items())
        },
        "actor_inventory_sha256": _canonical_hash(
            service.query_actor_inventory(identities["actor"])
        ),
    }


class StrictChainFailure(RuntimeError):
    """Redacted strict-chain failure with stable machine-readable identity."""

    def __init__(
        self,
        *,
        stage: str,
        error_code: str,
    ) -> None:
        self.stage = str(stage)
        self.error_code = str(error_code)
        super().__init__(f"{self.error_code}@{self.stage}")


def _stable_strict_error_code(exc: BaseException) -> str:
    candidate = str(
        getattr(exc, "code", None)
        or getattr(exc, "error_code", None)
        or ""
    ).strip()
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,95}", candidate):
        return candidate
    name = re.sub(
        r"(?<!^)(?=[A-Z])",
        "_",
        type(exc).__name__,
    ).upper()
    name = re.sub(r"[^A-Z0-9_]+", "_", name).strip("_")
    return f"STRICT_CHAIN_{name or 'ERROR'}"


def _strict_error_observation(exc: BaseException) -> dict[str, Any]:
    stage = str(getattr(exc, "stage", None) or "strict_chain")
    code = str(
        getattr(exc, "error_code", None)
        or _stable_strict_error_code(exc)
    )
    return {
        "passed": False,
        "status": "error",
        "error_type": type(exc).__name__,
        "error_code": code,
        "error_stage": stage,
        "error_sha256": _sha256_text(f"{code}@{stage}"),
    }


def _run_strict_chain_impl(
    project_root: Path | str,
    recorder: RemoteCallRecorder,
    stage_state: dict[str, str],
) -> dict[str, Any]:
    stage_state["stage"] = "continuity_setup"
    root = Path(project_root).expanduser().resolve()
    service = ContinuityService(root)
    events, identities, assistant_text = _strict_events(service)
    prompt = (
        "推演下一章：基准角色甲要在基准南站验证基准刃的切割功能，"
        "基准角色乙负责观察；以完成一次受控使用并留下轻微磨损为终点，"
        "让读者先紧张后获得有限兑现，保持角色限知且不新增其他核心物品。"
    )
    session_id = "v15-strict-session"
    turn_id = "v15-strict-turn"
    artifact_context = {
        "artifact_id": "v15-strict-artifact",
        "artifact_stage": "final",
        "artifact_revision": 1,
        "branch_id": "main",
        "chapter_no": 999,
        "scene_index": 0,
        "task": "scene",
    }
    stage_ms: dict[str, float] = {}
    chain_started = time.perf_counter()

    stage_state["stage"] = "grill"
    grill_started = time.perf_counter()
    grill = GrillGateService(root / ".plot-rag" / "grill.sqlite3")
    grill_result = grill.process(
        project_root=root,
        prompt=prompt,
        task_family="plot",
        host_session_id=session_id,
        turn_id=turn_id,
    )
    stage_ms["grill_ms"] = round(
        (time.perf_counter() - grill_started) * 1000.0,
        3,
    )
    if str(grill_result.get("action") or "") != "proceed":
        raise RuntimeError("strict benchmark Grill did not lock intent")

    stage_state["stage"] = "experience_lock"
    experience_started = time.perf_counter()
    experience = ensure_locked_manifest(
        root,
        prompt=prompt,
        artifact_context=artifact_context,
        intent_contract=grill_result,
        session_identity=session_id,
        turn_identity=turn_id,
        idempotency_key="v15-strict-experience",
    )
    stage_ms["experience_ms"] = round(
        (time.perf_counter() - experience_started) * 1000.0,
        3,
    )
    if str(experience.get("action") or "") != "locked":
        raise RuntimeError("strict benchmark event experience did not lock")
    experience_manifest = dict(experience.get("manifest") or {})
    experience_contracts = [
        dict(item)
        for item in experience_manifest.get("contracts") or []
        if isinstance(item, Mapping)
    ]
    lifecycle_identity = {
        "intent_contract_hash": str(
            experience_manifest.get("source_intent_contract_hash") or ""
        ),
        "event_seed_manifest_hash": str(
            experience_manifest.get("event_seed_manifest_hash") or ""
        ),
        "experience_contract_hashes": sorted(
            {
                str(item.get("contract_hash") or "")
                for item in experience_contracts
                if item.get("contract_hash")
            }
        ),
        "event_experience_control_revision": int(
            experience_manifest.get("control_revision") or 0
        ),
        "event_seed_references": sorted(
            [
                {
                    "event_seed_id": str(
                        item.get("event_seed_id") or ""
                    ),
                    "event_seed_revision": int(
                        item.get("event_seed_revision") or 0
                    ),
                }
                for item in experience.get("seed_references") or []
                if isinstance(item, Mapping)
            ],
            key=lambda item: (
                item["event_seed_id"],
                item["event_seed_revision"],
            ),
        ),
    }

    stage_state["stage"] = "prepare"
    prepare_started = time.perf_counter()
    remote_mark = recorder.mark()
    prepared = v1_runtime.prepare_plot_turn(
        root,
        prompt,
        session_id=session_id,
        turn_id=turn_id,
        artifact_stage="final",
        branch_id="main",
        chapter_no=999,
        scene_index=0,
        artifact_id="v15-strict-artifact",
        task="scene",
        lifecycle_identity=lifecycle_identity,
    )
    stage_ms["prepare_ms"] = round(
        (time.perf_counter() - prepare_started) * 1000.0,
        3,
    )
    if str(prepared.get("status") or "") not in {"ready", "degraded"}:
        raise RuntimeError("strict benchmark Prepare did not create a receipt")

    queue = ExtractionJobQueue(root)
    lifecycle = dict(prepared.get("lifecycle_identity") or {})
    queued_artifact = {
        **dict(prepared.get("artifact_context") or artifact_context),
        "artifact_revision": 1,
        "_plot_rag_v15": {
            "extraction_execution_mode": "async_strict",
            "benchmark": True,
        },
    }
    stage_state["stage"] = "enqueue"
    enqueue_started = time.perf_counter()
    queued = queue.enqueue(
        receipt_id=str(prepared["receipt_id"]),
        request_id=str(prepared["request_id"]),
        assistant_text=assistant_text,
        prompt_hash=str(prepared["prompt_hash"]),
        retrieved_context_digest=str(
            prepared["retrieved_context_digest"]
        ),
        prepared_canon_revision=int(prepared["prepared_canon_revision"]),
        active_projection_hash=str(prepared["active_projection_hash"]),
        intent_contract_hash=str(
            lifecycle.get("intent_contract_hash") or ""
        ),
        event_seed_manifest_hash=str(
            lifecycle.get("event_seed_manifest_hash") or ""
        ),
        event_experience_control_revision=int(
            lifecycle.get("event_experience_control_revision") or 0
        ),
        event_seed_references=list(
            lifecycle.get("event_seed_references") or []
        ),
        experience_contract_hashes=list(
            lifecycle.get("experience_contract_hashes") or []
        ),
        artifact_context=queued_artifact,
        branch_id="main",
        sequence_no=9001,
        extract_provider="siliconflow",
        extract_base_url="https://api.siliconflow.cn/v1",
        extract_model="Qwen/Qwen3-30B-A3B-Instruct-2507",
        extract_schema_hash=_sha256_text("plot-rag-delta/v4"),
        extract_prompt_template_hash=_sha256_text(
            "v15-live-e2e-deterministic-typed-events"
        ),
        min_confidence=0.99,
        generation_params={
            "temperature": 0,
            "max_tokens": 4096,
            "protocol": "deterministic_typed_events",
        },
        job_id="extract-v15-strict-e2e",
    )
    stage_ms["enqueue_ms"] = round(
        (time.perf_counter() - enqueue_started) * 1000.0,
        3,
    )

    def proposal_factory(
        job: Mapping[str, Any],
        text: str,
    ) -> ExtractionWorkResult:
        if _sha256_text(text) != str(job["assistant_sha256"]):
            raise RuntimeError("strict benchmark assistant payload drift")
        binding = queue.proposal_binding(job)
        artifact = dict(job.get("artifact_context") or {})
        proposal = service.save_proposal(
            events=events,
            payload={
                "runtime_version": v1_runtime.RUNTIME_VERSION,
                "assistant_sha256": str(job["assistant_sha256"]),
                **binding,
                "lifecycle_identity": {
                    "intent_contract_hash": binding[
                        "intent_contract_hash"
                    ],
                    "event_seed_manifest_hash": binding[
                        "event_seed_manifest_hash"
                    ],
                    "experience_contract_hashes": list(
                        binding["experience_contract_hashes"]
                    ),
                    "event_experience_control_revision": int(
                        binding["event_experience_control_revision"]
                    ),
                    "event_seed_references": list(
                        binding["event_seed_references"]
                    ),
                },
            },
            artifact_id=str(artifact["artifact_id"]),
            artifact_stage=str(artifact["artifact_stage"]),
            branch_id=str(artifact["branch_id"]),
            chapter_no=artifact.get("chapter_no"),
            scene_index=artifact.get("scene_index"),
            artifact_revision=int(artifact.get("artifact_revision") or 1),
            prepared_canon_revision=int(job["prepared_canon_revision"]),
            proposal_kind="story_delta",
            idempotency_key="v15-strict-proposal",
        )
        return ExtractionWorkResult(
            validator_passed=True,
            result_proposal_id=str(proposal["proposal_id"]),
            result_kind="proposal",
            remote_status="deterministic_typed_events",
        )

    stage_state["stage"] = "worker"
    worker_started = time.perf_counter()
    worker = queue.run_once(
        worker_id="v15-strict-worker",
        proposal_factory=proposal_factory,
        lease_seconds=60,
        recover_stale=True,
    )
    stage_ms["worker_ms"] = round(
        (time.perf_counter() - worker_started) * 1000.0,
        3,
    )
    stage_ms["extraction_ready_ms"] = round(
        (time.perf_counter() - enqueue_started) * 1000.0,
        3,
    )
    if str(worker.get("status") or "") != "succeeded":
        raise RuntimeError("strict benchmark extraction worker failed")
    completed = dict(worker.get("job") or {})
    proposal_id = str(completed.get("result_proposal_id") or "")
    stage_state["stage"] = "pending_barrier"
    pending = queue.barrier_status(
        branch_id="main",
        sequence_no=9001,
        include_prior=True,
    )
    if pending.get("code") != "pending_review" or not pending.get(
        "blocking"
    ):
        raise RuntimeError("strict benchmark pending-review barrier missing")

    active_revision = service.get_canon_revisions()["active"]
    stage_state["stage"] = "accept"
    accept_started = time.perf_counter()
    grant = v1_runtime.issue_host_approval(
        root,
        proposal_id,
        expected_canon_revision=active_revision,
        issuer="v15-live-e2e",
        channel="interactive_test",
        operations=("accept",),
    )
    accepted = v1_runtime.accept_plot_proposal(
        root,
        proposal_id,
        approval_id=str(grant["grant"]["approval_id"]),
        expected_canon_revision=active_revision,
    )
    stage_ms["accept_ms"] = round(
        (time.perf_counter() - accept_started) * 1000.0,
        3,
    )
    stage_state["stage"] = "accepted_barrier"
    accepted_barrier = queue.barrier_status(
        branch_id="main",
        sequence_no=9001,
        include_prior=True,
    )
    if accepted_barrier.get("code") != "accepted" or accepted_barrier.get(
        "blocking"
    ):
        raise RuntimeError("strict benchmark accepted barrier did not clear")

    stage_state["stage"] = "item_surfaces"
    surfaces = _item_surfaces(service, identities)
    if surfaces["surface_count"] != 7:
        raise RuntimeError("strict benchmark did not expose seven item surfaces")

    stage_state["stage"] = "replay"
    replay_started = time.perf_counter()
    replay = v1_runtime.replay_continuity(root)
    replay_again = service.replay()
    stage_ms["replay_ms"] = round(
        (time.perf_counter() - replay_started) * 1000.0,
        3,
    )
    replay_projection = str(
        (replay.get("replay") or {}).get("projection_hash") or ""
    )
    replay_item_projection = str(
        (replay.get("replay") or {}).get("item_projection_hash") or ""
    )
    replay_stable = (
        replay_projection == str(replay_again.get("projection_hash") or "")
        and replay_item_projection
        == str(replay_again.get("item_projection_hash") or "")
    )

    stage_state["stage"] = "next_prepare"
    next_prepare_started = time.perf_counter()
    next_prepared = v1_runtime.prepare_plot_turn(
        root,
        prompt,
        session_id=session_id,
        turn_id="v15-strict-next-turn",
        artifact_stage="final",
        branch_id="main",
        chapter_no=1000,
        scene_index=0,
        artifact_id="v15-strict-next-artifact",
        task="scene",
        lifecycle_identity=lifecycle_identity,
    )
    stage_ms["next_prepare_ms"] = round(
        (time.perf_counter() - next_prepare_started) * 1000.0,
        3,
    )
    stage_ms["end_to_next_ready_ms"] = round(
        (time.perf_counter() - enqueue_started) * 1000.0,
        3,
    )
    stage_ms["strict_total_ms"] = round(
        (time.perf_counter() - chain_started) * 1000.0,
        3,
    )
    next_ready = str(next_prepared.get("status") or "") in {
        "ready",
        "degraded",
    }
    accepted_commit = dict(accepted.get("commit") or {})
    return {
        "passed": bool(replay_stable and next_ready),
        "grill_action": str(grill_result.get("action") or ""),
        "experience_action": str(experience.get("action") or ""),
        "prepare_status": str(prepared.get("status") or ""),
        "enqueue_status": str(queued.get("status") or ""),
        "worker_status": str(worker.get("status") or ""),
        "pending_barrier": {
            "code": str(pending.get("code") or ""),
            "blocking": bool(pending.get("blocking")),
        },
        "accepted_barrier": {
            "code": str(accepted_barrier.get("code") or ""),
            "blocking": bool(accepted_barrier.get("blocking")),
        },
        "accepted_projection_sha256": _sha256_text(
            str(accepted_commit.get("projection_hash") or "")
        ),
        "accepted_item_projection_sha256": _sha256_text(
            str(accepted_commit.get("item_projection_hash") or "")
        ),
        "replay_stable": replay_stable,
        "next_prepare_ready": next_ready,
        "extraction_transport": "deterministic_typed_events",
        "item_surfaces": surfaces,
        "continuity_surfaces": {
            "relation_sha256": _canonical_hash(
                service.query_facts(
                    entity_id=identities["actor"],
                    fact_type="relation",
                )
            ),
            "location_sha256": _canonical_hash(
                service.query_facts(
                    entity_id=identities["actor"],
                    fact_type="location",
                )
            ),
            "time_sha256": _canonical_hash(
                service.query_facts(fact_type="time")
            ),
        },
        "stage_timings_ms": stage_ms,
        "remote_calls": recorder.summary(remote_mark),
    }


def run_strict_chain(
    project_root: Path | str,
    recorder: RemoteCallRecorder,
) -> dict[str, Any]:
    stage_state = {"stage": "initialization"}
    try:
        return _run_strict_chain_impl(
            project_root,
            recorder,
            stage_state,
        )
    except StrictChainFailure:
        raise
    except Exception as exc:
        raise StrictChainFailure(
            stage=stage_state["stage"],
            error_code=_stable_strict_error_code(exc),
        ) from exc


def _timing_summary(rounds: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_state: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for round_result in rounds:
        by_state[str(round_result["state"])].append(round_result)
    summary: dict[str, Any] = {}
    for state, values in sorted(by_state.items()):
        wall = [float(value.get("wall_ms") or 0.0) for value in values]
        stage_names = sorted(
            {
                stage
                for value in values
                for stage in (value.get("stage_timings_ms") or {})
            }
        )
        summary[state] = {
            "sample_count": len(values),
            "wall_p50_ms": _percentile(wall, 50),
            "wall_p95_ms": _percentile(wall, 95),
            "wall_max_ms": round(max(wall) if wall else 0.0, 3),
            "stages": {
                stage: {
                    "p50_ms": _percentile(
                        [
                            float(
                                (value.get("stage_timings_ms") or {}).get(
                                    stage
                                )
                                or 0.0
                            )
                            for value in values
                        ],
                        50,
                    ),
                    "p95_ms": _percentile(
                        [
                            float(
                                (value.get("stage_timings_ms") or {}).get(
                                    stage
                                )
                                or 0.0
                            )
                            for value in values
                        ],
                        95,
                    ),
                }
                for stage in stage_names
            },
        }
    return summary


def _sensitive_environment_values() -> list[tuple[str, str]]:
    markers = (
        "KEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "PASSWD",
        "AUTHORIZATION",
        "COOKIE",
    )
    values: list[tuple[str, str]] = []
    for name, value in os.environ.items():
        if (
            value
            and len(value) >= 8
            and any(marker in name.upper() for marker in markers)
        ):
            values.append((name, value))
    return values


def scan_text_for_credentials(text: str) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for name, value in _sensitive_environment_values():
        start = 0
        while True:
            index = text.find(value, start)
            if index < 0:
                break
            findings.append(
                {
                    "type": "environment_value",
                    "name_sha256": _sha256_text(name)[:16],
                    "match_sha256": _sha256_text(value)[:16],
                }
            )
            start = index + len(value)
    for kind, pattern in TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(
                {
                    "type": kind,
                    "match_sha256": _sha256_text(match.group(0))[:16],
                }
            )
    return {
        "finding_count": len(findings),
        "type_counts": dict(
            sorted(Counter(item["type"] for item in findings).items())
        ),
        "finding_hashes": sorted(
            {str(item["match_sha256"]) for item in findings}
        ),
    }


def scan_artifacts_for_credentials(paths: Iterable[Path | str]) -> dict[str, Any]:
    totals: Counter[str] = Counter()
    hashes: set[str] = set()
    file_count = 0
    finding_count = 0
    for raw in paths:
        path = Path(raw)
        selected = (
            sorted(item for item in path.rglob("*") if item.is_file())
            if path.is_dir()
            else [path]
        )
        for item in selected:
            if not item.is_file():
                continue
            file_count += 1
            try:
                text = item.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            result = scan_text_for_credentials(text)
            finding_count += int(result["finding_count"])
            totals.update(result["type_counts"])
            hashes.update(result["finding_hashes"])
    return {
        "scanned_file_count": file_count,
        "finding_count": finding_count,
        "type_counts": dict(sorted(totals.items())),
        "finding_hashes": sorted(hashes),
    }


def run_v15_live_e2e(
    *,
    project_root: Path | str,
    prompts_path: Path | str,
    transport: str = "offline",
    workspace_parent: Path | str | None = None,
    prompt_limit: int | None = None,
    warmup: bool = False,
    include_strict: bool = True,
    include_chat_extraction_smoke: bool = False,
    keep_workspace: bool = False,
) -> dict[str, Any]:
    normalized_transport = str(transport).strip().casefold()
    if normalized_transport not in {"offline", "live"}:
        raise ValueError("transport must be offline or live")
    if include_chat_extraction_smoke and normalized_transport != "live":
        raise ValueError(
            "chat extraction smoke requires live transport"
        )
    source = Path(project_root).expanduser().resolve()
    provenance = collect_benchmark_provenance()
    before = tree_snapshot(source)
    prompts = load_prompt_fixture(prompts_path)
    if prompt_limit is not None:
        if isinstance(prompt_limit, bool) or int(prompt_limit) < 1:
            raise ValueError("prompt_limit must be a positive integer")
        prompts = prompts[: int(prompt_limit)]
    if not prompts:
        raise ValueError("at least one prompt is required")
    parent = (
        Path(workspace_parent).expanduser().resolve()
        if workspace_parent is not None
        else None
    )
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix="plot-rag-v15-live-e2e-",
            dir=str(parent) if parent is not None else None,
        )
    )
    recorder = RemoteCallRecorder()
    rounds: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    strict_result: dict[str, Any] = {
        "passed": True,
        "status": "skipped",
    }
    chat_extraction_smoke: dict[str, Any] = {
        "schema_version": CHAT_EXTRACTION_SMOKE_SCHEMA,
        "requested": bool(include_chat_extraction_smoke),
        "executed": False,
        "passed": not include_chat_extraction_smoke,
        "status": (
            "not_requested"
            if not include_chat_extraction_smoke
            else "pending"
        ),
        "transport": (
            "not_requested"
            if not include_chat_extraction_smoke
            else "siliconflow_chat"
        ),
        "mutates_continuity": False,
        "remote_calls": {
            "call_count": 0,
            "by_service": {},
        },
    }
    warmup_remote_calls = {
        "call_count": 0,
        "by_service": {},
    }
    prepare_remote_calls = {
        "call_count": 0,
        "by_service": {},
    }
    strict_remote_calls = {
        "call_count": 0,
        "by_service": {},
    }
    run_started = time.perf_counter()
    try:
        _, state_roots, strict_root = create_isolated_states(
            source,
            temporary,
        )
        with transport_context(normalized_transport, recorder):
            warmup_start_call = recorder.mark()
            if warmup:
                for state, state_root in state_roots.items():
                    v1_runtime.build_longform_context(
                        state_root,
                        "校验当前角色状态、位置、物品、力量与故事时间。",
                        artifact_context={
                            "artifact_stage": "outline",
                            "task": "outline",
                            "branch_id": "main",
                        },
                    )
            warmup_end_call = recorder.mark()
            warmup_remote_calls = recorder.summary(
                warmup_start_call,
                warmup_end_call,
            )
            prepare_start_call = recorder.mark()
            for prompt in prompts:
                prompt_rounds: dict[str, dict[str, Any]] = {}
                for spec in STATE_MATRIX:
                    state = str(spec["state"])
                    start_call = recorder.mark()
                    started = time.perf_counter()
                    try:
                        result = v1_runtime.build_longform_context(
                            state_roots[state],
                            str(prompt["prompt"]),
                            artifact_context={
                                "artifact_stage": prompt[
                                    "artifact_stage"
                                ],
                                "task": prompt["task"],
                                "branch_id": prompt["branch_id"],
                                "chapter_no": prompt["chapter_no"],
                                "scene_index": prompt["scene_index"],
                            },
                        )
                        observation = observe_round(
                            result,
                            state=state,
                            prompt_id=str(prompt["prompt_id"]),
                            wall_ms=(
                                time.perf_counter() - started
                            )
                            * 1000.0,
                            remote_calls=recorder.summary(start_call),
                        )
                    except Exception as exc:
                        observation = {
                            "prompt_id": str(prompt["prompt_id"]),
                            "state": state,
                            "status": "error",
                            "chosen_path": "",
                            "executed_paths": [],
                            "internal_comparison": {},
                            "needs": [],
                            "needs_sha256": "",
                            "query_contract": {},
                            "selected_chunks_sha256": "",
                            "selected_chunk_count": 0,
                            "selected_order_sha256": "",
                            "semantic_hash": "",
                            "contract_context_hash": "",
                            "normalized_contract_sha256": "",
                            "normalized_context_sha256": "",
                            "critical_facts": {},
                            "degraded_turn_increment": 1,
                            "wall_ms": round(
                                (time.perf_counter() - started) * 1000.0,
                                3,
                            ),
                            "stage_timings_ms": {},
                            "remote_calls": recorder.summary(start_call),
                            "error_type": type(exc).__name__,
                            "error_sha256": _sha256_text(str(exc)),
                        }
                    rounds.append(observation)
                    prompt_rounds[state] = observation
                comparisons.append(
                    compare_prompt_rounds(
                        str(prompt["prompt_id"]),
                        prompt_rounds,
                    )
                )
            prepare_end_call = recorder.mark()
            prepare_remote_calls = recorder.summary(
                prepare_start_call,
                prepare_end_call,
            )
            if include_chat_extraction_smoke:
                chat_extraction_smoke = run_live_chat_extraction_smoke(
                    strict_root,
                    recorder,
                )
            if include_strict:
                strict_start_call = recorder.mark()
                try:
                    strict_result = run_strict_chain(
                        strict_root,
                        recorder,
                    )
                    strict_result["status"] = (
                        "passed" if strict_result["passed"] else "failed"
                    )
                except Exception as exc:
                    strict_result = _strict_error_observation(exc)
                strict_end_call = recorder.mark()
                strict_remote_calls = recorder.summary(
                    strict_start_call,
                    strict_end_call,
                )
        after = tree_snapshot(source)
        tree_result = compare_tree_snapshots(before, after)
        mismatch_count = sum(
            int(item["mismatch_count"]) for item in comparisons
        )
        critical_mismatch_count = sum(
            1
            for comparison in comparisons
            for mismatch in comparison["mismatches"]
            if mismatch["field"] == "critical_facts"
        )
        selected_mismatch_count = sum(
            1
            for comparison in comparisons
            for mismatch in comparison["mismatches"]
            if mismatch["field"]
            in {"selected_chunks_sha256", "selected_order_sha256"}
        )
        error_round_count = sum(
            1 for item in rounds if item.get("status") == "error"
        )
        degraded_turn_count = sum(
            int(item.get("degraded_turn_increment") or 0)
            for item in rounds
        )
        all_remote_calls = recorder.summary()
        prepare_remote_error_call_count = _remote_error_call_count(
            prepare_remote_calls
        )
        remote_error_call_count = _remote_error_call_count(all_remote_calls)
        report: dict[str, Any] = {
            "schema_version": REPORT_SCHEMA,
            "suite": "plot-rag-v15-live-e2e",
            "transport": normalized_transport,
            "provenance": provenance,
            "transport_contract": {
                "retrieval_transport": normalized_transport,
                "retrieval_is_deterministic": (
                    normalized_transport == "offline"
                ),
                "prepare_matrix_uses_remote_embedding_rerank": (
                    normalized_transport == "live"
                ),
                "prepare_matrix_uses_remote_chat": False,
                "strict_extraction_transport": (
                    "deterministic_typed_events"
                    if include_strict
                    else "skipped"
                ),
                "strict_extraction_uses_remote_chat": False,
                "chat_extraction_smoke_transport": (
                    "siliconflow_chat"
                    if include_chat_extraction_smoke
                    else "not_requested"
                ),
                "chat_extraction_smoke_uses_remote_chat": bool(
                    include_chat_extraction_smoke
                ),
            },
            "source_snapshot_sha256": _canonical_hash(
                public_tree_snapshot(before)
            ),
            "prompt_fixture_sha256": _sha256_bytes(
                Path(prompts_path).expanduser().resolve().read_bytes()
            ),
            "prompt_count": len(prompts),
            "state_count": len(STATE_MATRIX),
            "measured_round_count": len(rounds),
            "expected_measured_round_count": len(prompts)
            * len(STATE_MATRIX),
            "state_matrix": [
                {
                    "state": item["state"],
                    "enabled": item["enabled"],
                    "shadow": item["shadow"],
                    "expected_chosen_path": item[
                        "expected_chosen_path"
                    ],
                }
                for item in STATE_MATRIX
            ],
            "rounds": rounds,
            "comparisons": comparisons,
            "strict_chain": strict_result,
            "chat_extraction_smoke": chat_extraction_smoke,
            "timing_summary": _timing_summary(rounds),
            "latency_contract": {
                "prepare_matrix": {
                    "operation": "build_longform_context",
                    "sample_count": len(rounds),
                    "wall_metric": "rounds[].wall_ms",
                    "summary_metric": "timing_summary",
                    "includes_remote_embedding_rerank": (
                        normalized_transport == "live"
                    ),
                    "includes_remote_chat_extraction": False,
                },
                "chat_extraction_smoke": {
                    "requested": bool(include_chat_extraction_smoke),
                    "operation": "state_rag._chat_extract",
                    "wall_metric": "chat_extraction_smoke.wall_ms",
                    "remote_metric": (
                        "chat_extraction_smoke.remote_latency_sum_ms"
                    ),
                    "includes_prepare": False,
                    "includes_remote_chat_extraction": bool(
                        include_chat_extraction_smoke
                    ),
                },
                "strict_chain": {
                    "operation": "deterministic_continuity_lifecycle",
                    "includes_remote_chat_extraction": False,
                },
            },
            "remote_calls": all_remote_calls,
            "remote_call_phases": {
                "warmup": warmup_remote_calls,
                "prepare_matrix": prepare_remote_calls,
                "chat_extraction_smoke": dict(
                    chat_extraction_smoke.get("remote_calls") or {}
                ),
                "strict_chain": strict_remote_calls,
            },
            "formal_project_tree": tree_result,
            "quality_gate": {
                "mismatch_count": mismatch_count,
                "critical_fact_mismatch_count": critical_mismatch_count,
                "selected_chunk_mismatch_count": selected_mismatch_count,
                "error_round_count": error_round_count,
                "degraded_turn_count": degraded_turn_count,
                "prepare_remote_error_call_count": (
                    prepare_remote_error_call_count
                ),
                "remote_error_call_count": remote_error_call_count,
                "strict_chain_required": bool(include_strict),
                "strict_chain_passed": bool(strict_result.get("passed")),
                "chat_extraction_smoke_required": bool(
                    include_chat_extraction_smoke
                ),
                "chat_extraction_smoke_passed": bool(
                    chat_extraction_smoke.get("passed")
                ),
                "formal_project_unchanged": bool(
                    tree_result["unchanged"]
                ),
                "latency_threshold_enforced": False,
                "live_latency_is_non_blocking": normalized_transport
                == "live",
                "live_degraded_gate_enforced": normalized_transport == "live",
                "live_remote_error_gate_enforced": (
                    normalized_transport == "live"
                ),
            },
            "benchmark_wall_ms": round(
                (time.perf_counter() - run_started) * 1000.0,
                3,
            ),
        }
        live_remote_health_passed = (
            normalized_transport != "live"
            or (
                degraded_turn_count == 0
                and prepare_remote_error_call_count == 0
                and remote_error_call_count == 0
            )
        )
        report["quality_gate"]["passed"] = bool(
            mismatch_count == 0
            and error_round_count == 0
            and tree_result["unchanged"]
            and live_remote_health_passed
            and (not include_strict or strict_result.get("passed"))
            and (
                not include_chat_extraction_smoke
                or chat_extraction_smoke.get("passed")
            )
        )
        report["passed"] = bool(report["quality_gate"]["passed"])
        in_memory_scan = scan_text_for_credentials(_canonical_json(report))
        report["credential_scan"] = {
            **in_memory_scan,
            "scope": "in_memory_report",
        }
        if in_memory_scan["finding_count"]:
            report["passed"] = False
            report["quality_gate"]["passed"] = False
        return report
    finally:
        if not keep_workspace:
            shutil.rmtree(temporary, ignore_errors=False)


def write_redacted_report(
    report: Mapping[str, Any],
    output: Path | str,
    *,
    overwrite: bool = False,
    pretty: bool = False,
) -> dict[str, Any]:
    target = Path(output).expanduser().resolve()
    if target.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )
    scan = scan_text_for_credentials(text)
    if scan["finding_count"]:
        raise ValueError("redacted report failed credential preflight")
    temporary = target.with_name(
        f".{target.name}.tmp-{os.getpid()}-{time.time_ns()}"
    )
    temporary.write_text(text + "\n", encoding="utf-8", newline="\n")
    os.replace(temporary, target)
    artifact_scan = scan_artifacts_for_credentials([target])
    if artifact_scan["finding_count"]:
        target.unlink(missing_ok=True)
        raise ValueError("written report failed credential scan")
    return {
        "output": str(target),
        "sha256": _sha256_bytes(target.read_bytes()),
        "credential_scan": artifact_scan,
    }
