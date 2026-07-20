#!/usr/bin/env python3
"""Project-local retrieval for the plot RAG gate.

The implementation deliberately uses only the Python standard library.  The
SQLite database is derived data; configured project files remain authoritative.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

try:  # Package import, for example ``scripts.plot_rag``.
    from .sqlite_guard import (
        SQLiteComponentSchemaError,
        execute_sqlite_script_in_transaction,
        validate_sqlite_component_schema,
    )
except ImportError:  # Direct CLI/runtime import with ``scripts`` on sys.path.
    from sqlite_guard import (
        SQLiteComponentSchemaError,
        execute_sqlite_script_in_transaction,
        validate_sqlite_component_schema,
    )


STATUS_HIT = "HIT_CONFIRMED"
STATUS_AMBIGUOUS = "AMBIGUOUS"
STATUS_UNAVAILABLE = "INDEX_UNAVAILABLE"
STATUS_MISS = "MISS_CONFIRMED"

CONFIG_RELATIVE_PATH = Path(".plot-rag") / "config.json"
POINTER_FILE = ".plot-rag-current-project"
SCHEMA_VERSION = 1
LEGACY_STATE_SCHEMA_VERSION = 2
CONTINUITY_STATE_SCHEMA_VERSION = 7
AUTHORITY_INDEX_SCHEMA_VERSION = 1
DEFAULT_INDEX_PATH = ".plot-rag/index.sqlite3"
DEFAULT_STATE_DB_PATH = ".plot-rag/state.sqlite3"
DEFAULT_STATE_SNAPSHOT_PATH = ".plot-rag/state_snapshot.json"
DEFAULT_STATE_COMMIT_DIR = ".plot-rag/commits"
DEFAULT_INIT_DB_PATH = ".plot-rag/init.sqlite3"
DEFAULT_GRILL_DB_PATH = ".plot-rag/grill.sqlite3"
DEFAULT_AUTHORITY_INDEX_V1_PATH = ".plot-rag/authority.v1.sqlite3"
DEFAULT_LONGFORM_V1_PATH = ".plot-rag/longform.v1.sqlite3"
DEFAULT_PROJECTION_RUNS_V1_PATH = ".plot-rag/projection-runs.v1.sqlite3"
DEFAULT_MAX_CHUNK_CHARS = 1600
DEFAULT_MAX_FILE_BYTES = 8 * 1024 * 1024
DEFAULT_CANDIDATE_LIMIT = 5
DEFAULT_RELIABLE_COVERAGE = 0.32
DEFAULT_WEAK_COVERAGE = 0.16
LEGACY_INDEX_TABLES = frozenset({"meta", "files", "chunks"})
LEGACY_STATE_CATEGORIES = (
    "character_state",
    "relationship",
    "location",
    "inventory",
    "story_time",
    "world_state",
)
POWER_STATE_CATEGORIES = (
    "ability",
    "progression",
    "resource",
    "status",
    "binding",
    "qualification",
    "observation",
)
STATE_CATEGORIES = (*LEGACY_STATE_CATEGORIES, *POWER_STATE_CATEGORIES)
POWER_SYSTEM_MODES = {"auto", "enabled", "disabled", "mundane"}
POWER_COMPARISON_MODES = {"conditional", "disabled"}
POWER_UNKNOWN_POLICIES = {"quarantine", "preserve", "reject"}
SOURCE_ROLES = {"canon", "setting", "outline", "draft", "note", "reference"}
SCOPE_POLICIES = {
    "infer_and_review",
    "current_only",
    "planned_only",
    "historical_only",
    "timeless_only",
    "timeless_candidate",
    "preserve_unknown",
}
SCOPE_POLICY_ALIASES = {
    "current": "current_only",
    "planned": "planned_only",
    "historical": "historical_only",
    "timeless": "timeless_only",
    "timeless_candidate": "timeless_only",
}
INGEST_POLICIES = {"include", "review", "exclude"}
INIT_IGNORE_GLOBS = (
    ".git/**",
    ".plot-rag/**",
    ".plot-rag/init-sessions/**",
    ".plot-rag/init.sqlite3",
    ".plot-rag-init/**",
)
INITIALIZATION_MODES = {"auto", "new", "ingest", "hybrid"}
INITIALIZATION_TARGET_PROFILES = {
    "plot_ready",
    "world_bible",
    "normalize_only",
    "continuity_ready",
}
INITIALIZATION_INTERACTION_PROFILES = {"minimal", "balanced", "deep"}
GRILL_INTENT_FIELDS = (
    "problem_to_solve",
    "expected_deliverable",
    "reader_experience",
    "protagonist_drive_conflict",
    "scope_endpoint",
    "success_criteria",
    "hard_constraints",
    "model_autonomy",
)
DEFAULT_GRILL_SKIP_PHRASES = (
    "跳过 Grill",
    "跳过盘问",
    "跳过目的确认",
    "按现有要求直接执行",
    "直接执行，不要追问",
)
DEFAULT_GRILL_CANCEL_PHRASES = (
    "取消本轮 Grill",
    "结束本轮盘问",
    "停止本轮盘问",
    "放弃本轮任务",
)
REMOTE_ENV_ALLOWLIST = {
    "embedding": {
        "base_url_env": {"EMBED_BASE_URL", "PLOT_RAG_EMBED_BASE_URL"},
        "model_env": {"EMBED_MODEL", "PLOT_RAG_EMBED_MODEL"},
        "api_key_env": {"EMBED_API_KEY", "PLOT_RAG_EMBED_API_KEY", "SILICONFLOW_API_KEY"},
    },
    "rerank": {
        "base_url_env": {"RERANK_BASE_URL", "PLOT_RAG_RERANK_BASE_URL"},
        "model_env": {"RERANK_MODEL", "PLOT_RAG_RERANK_MODEL"},
        "api_key_env": {"RERANK_API_KEY", "PLOT_RAG_RERANK_API_KEY", "SILICONFLOW_API_KEY"},
    },
    "extract": {
        "base_url_env": {"PLOT_RAG_LLM_BASE_URL"},
        "model_env": {"PLOT_RAG_LLM_MODEL"},
        "api_key_env": {"PLOT_RAG_LLM_API_KEY", "SILICONFLOW_API_KEY"},
    },
}
PREPARE_V2_BOOLEAN_DEFAULTS: Mapping[str, bool] = {
    "enabled": False,
    "shadow": True,
    "single_read_snapshot": True,
    "exact_state_short_circuit": True,
    "batch_embedding": True,
    "batch_failure_fallback_single": True,
    "singleflight": True,
    "persistent_exact_cache": True,
    "http_keep_alive": True,
}
EXTRACTION_BOOLEAN_DEFAULTS: Mapping[str, bool] = {
    "async_shadow": True,
    "next_plot_turn_barrier": True,
    "barrier_requires_proposal_resolution": True,
}
EXTRACTION_MODES = {"sync", "async"}
DETERMINISTIC_REPAIRS = {"single_action_event_type_echo"}
EVENT_EXPERIENCE_BOOLEAN_DEFAULTS: Mapping[str, bool] = {
    "enabled": True,
    "required_before_event_design": True,
    "event_seed_required": True,
    "receipt_hash_binding": True,
    "derive_from_intent": True,
    "grill_on_structural_ambiguity": True,
    "one_question_per_turn": True,
    "visible_in_story_artifacts": False,
}
ITEM_BOOLEAN_DEFAULTS: Mapping[str, bool] = {
    "strict_runtime_validation": False,
    "power_binding_bridge": True,
    "readable_projection": True,
}
ITEM_SCHEMA_VERSIONS = {"plot-rag-item/v1"}
ITEM_DELTA_VERSIONS = {"plot-rag-delta/v4"}
ADVANTAGE_BOOLEAN_DEFAULTS: Mapping[str, bool] = {
    "enabled": False,
    "shadow": True,
    "strict_runtime_validation": False,
    "readable_projection": True,
    "mandatory_context": True,
}
ADVANTAGE_SCHEMA_VERSIONS = {"plot-rag-advantage/v1"}

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_ASCII_WORD_RE = re.compile(r"[a-z0-9_]+")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_COMPACT_RE = re.compile(r"[^a-z0-9\u3400-\u4dbf\u4e00-\u9fff]+")
_CORE_CJK_STOPWORDS = frozenset(
    {
        "什么",
        "为什",
        "为何",
        "怎么",
        "如何",
        "是否",
        "哪里",
        "哪儿",
        "哪个",
        "哪种",
        "哪一",
        "具体",
        "请问",
        "查询",
        "问题",
        "相关",
        "有关",
    }
)
_CORE_ASCII_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "are",
        "at",
        "be",
        "did",
        "do",
        "does",
        "how",
        "in",
        "is",
        "of",
        "on",
        "the",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
    }
)
_CJK_QUESTION_SUFFIXES = (
    "为什么",
    "是什么",
    "在哪里",
    "在哪儿",
    "怎么样",
    "哪一个",
    "哪一种",
    "哪一项",
    "如何",
    "怎么",
    "是否",
    "哪里",
    "哪儿",
    "何处",
    "哪个",
    "哪种",
    "为何",
    "吗",
    "呢",
)
_FOCUS_MODIFIERS = ("具体", "当前", "可见", "实际", "明确")
_NONCONFIRMING_EVIDENCE_RE = re.compile(
    r"(?:尚未|仍未|还未|暂未|并未|未曾)"
    r"(?:明确|确定|确认|决定|设定|给出|说明|揭示)|"
    r"(?:仍|尚|还|暂)?待(?:确认|确定|明确|决定|设定|补充|核实)|"
    r"(?:未知|不明|不详|无法确定|不能确定|无从确定)|"
    r"(?:不是|并非|不属于|不在|没有|不存在|否认|否定)"
)


class PlotRagError(RuntimeError):
    """A configuration or index problem that must never be treated as a miss."""


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    heading: str
    start_line: int
    end_line: int
    text: str
    search_text: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _read_pointer(pointer: Path) -> Path | None:
    try:
        raw = pointer.read_text(encoding="utf-8-sig").strip().strip('"')
    except OSError:
        return None
    if not raw:
        return None
    target = Path(raw).expanduser()
    if not target.is_absolute():
        target = pointer.parent / target
    return target.resolve()


def locate_project_root(start: Path | str | None = None) -> Path | None:
    """Locate a project without consulting any other writing plugin.

    Resolution order is an explicit environment variable, a project-local
    config in the current directory/parents, then this hook's own pointer file.
    """

    env_root = os.environ.get("PLOT_RAG_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    current = Path(start or os.getcwd()).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_RELATIVE_PATH.parent).is_dir():
            return candidate
        pointer = candidate / POINTER_FILE
        if pointer.is_file():
            target = _read_pointer(pointer)
            if target is not None:
                return target
    return None


def _validate_globs(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise PlotRagError("config.authority_globs must be a non-empty string array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise PlotRagError("config.authority_globs contains an empty or non-string value")
        pattern = item.strip().replace("\\", "/")
        pure = PurePosixPath(pattern)
        if pure.is_absolute() or ".." in pure.parts:
            raise PlotRagError(f"authority glob must stay inside the project: {item}")
        result.append(pattern)
    return result


def _legacy_source_descriptor(pattern: str) -> dict[str, Any]:
    lowered = pattern.casefold()
    if any(token in lowered for token in ("正文", "chapter", "chapters", "稿件")):
        role, scope_policy, ingest_policy, priority = (
            "canon",
            "infer_and_review",
            "include",
            100,
        )
    elif any(token in lowered for token in ("设定", "setting", "world", "角色")):
        role, scope_policy, ingest_policy, priority = (
            "setting",
            "infer_and_review",
            "include",
            90,
        )
    elif any(token in lowered for token in ("大纲", "outline", "章纲", "卷纲")):
        role, scope_policy, ingest_policy, priority = (
            "outline",
            "planned_only",
            "review",
            60,
        )
    elif any(token in lowered for token in ("灵感", "note", "idea", "todo")):
        role, scope_policy, ingest_policy, priority = (
            "note",
            "planned_only",
            "exclude",
            20,
        )
    else:
        role, scope_policy, ingest_policy, priority = (
            "setting",
            "infer_and_review",
            "review",
            50,
        )
    return {
        "glob": pattern,
        "role": role,
        "scope_policy": scope_policy,
        "ingest_policy": ingest_policy,
        "priority": priority,
    }


def _validate_authority_sources(
    value: Any,
    *,
    legacy_globs: list[str] | None,
) -> list[dict[str, Any]]:
    if value is None:
        if not legacy_globs:
            raise PlotRagError(
                "config requires authority_sources or legacy authority_globs"
            )
        return [_legacy_source_descriptor(pattern) for pattern in legacy_globs]
    if not isinstance(value, list) or not value:
        raise PlotRagError("config.authority_sources must be a non-empty array")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise PlotRagError(
                f"config.authority_sources[{index}] must be an object"
            )
        pattern = _validate_globs([item.get("glob")])[0]
        role = str(item.get("role") or "").strip().lower()
        if role not in SOURCE_ROLES:
            raise PlotRagError(
                f"config.authority_sources[{index}].role must be one of: "
                + ", ".join(sorted(SOURCE_ROLES))
            )
        default_scope = "planned_only" if role in {"outline", "draft", "note"} else "infer_and_review"
        scope_policy = str(item.get("scope_policy") or default_scope).strip().lower()
        scope_policy = SCOPE_POLICY_ALIASES.get(scope_policy, scope_policy)
        if scope_policy not in SCOPE_POLICIES:
            raise PlotRagError(
                f"config.authority_sources[{index}].scope_policy must be one of: "
                + ", ".join(sorted(SCOPE_POLICIES))
            )
        default_ingest = (
            "include"
            if role in {"canon", "setting"}
            else "review"
            if role in {"outline", "draft"}
            else "exclude"
        )
        ingest_policy = str(item.get("ingest_policy") or default_ingest).strip().lower()
        if ingest_policy not in INGEST_POLICIES:
            raise PlotRagError(
                f"config.authority_sources[{index}].ingest_policy must be one of: "
                + ", ".join(sorted(INGEST_POLICIES))
            )
        priority = item.get("priority", 50)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 1000:
            raise PlotRagError(
                f"config.authority_sources[{index}].priority must be an integer from 0 to 1000"
            )
        key = (pattern.casefold(), role)
        if key in seen:
            raise PlotRagError(
                f"duplicate authority source for role {role!r}: {pattern!r}"
            )
        seen.add(key)
        result.append(
            {
                "glob": pattern,
                "role": role,
                "scope_policy": scope_policy,
                "ingest_policy": ingest_policy,
                "priority": priority,
            }
        )
    return result


def _validate_relative_glob_array(
    value: Any,
    *,
    field: str,
    defaults: Sequence[str] = (),
) -> list[str]:
    raw = list(defaults) if value is None else value
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise PlotRagError(f"config.{field} must be a string array")
    result: list[str] = []
    for item in raw:
        pattern = item.strip().replace("\\", "/")
        if not pattern:
            continue
        pure = PurePosixPath(pattern)
        if pure.is_absolute() or ".." in pure.parts:
            raise PlotRagError(f"config.{field} entries must stay inside the project")
        if pattern not in result:
            result.append(pattern)
    return result


def _choice(
    value: Any,
    *,
    field: str,
    default: str,
    allowed: set[str],
) -> str:
    selected = str(default if value is None else value).strip().lower()
    if selected not in allowed:
        raise PlotRagError(
            f"config.{field} must be one of: " + ", ".join(sorted(allowed))
        )
    return selected


def _bounded_number(
    config: dict[str, Any], key: str, default: float, minimum: float, maximum: float
) -> float:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlotRagError(f"config.{key} must be a number")
    number = float(value)
    if not minimum <= number <= maximum:
        raise PlotRagError(f"config.{key} must be between {minimum} and {maximum}")
    return number


def _project_path(root: Path, value: Any, default: str, field: str) -> Path:
    raw = default if value is None else value
    if not isinstance(raw, str) or not raw.strip():
        raise PlotRagError(f"config.{field} must be a non-empty relative path")
    relative = Path(raw.strip())
    if relative.is_absolute() or ".." in relative.parts:
        raise PlotRagError(f"config.{field} must stay inside the project")
    resolved = (root / relative).resolve()
    if not _is_relative_to(resolved, root):
        raise PlotRagError(f"config.{field} resolves outside the project")
    return resolved


def _paths_share_identity(left: Path, right: Path) -> bool:
    if left == right:
        return True
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except OSError:
        return False


def _validate_runtime_path_layout(
    root: Path,
    config_path: Path,
    config: Mapping[str, Any],
) -> None:
    runtime_files = {
        "config": config_path.resolve(),
        "index_path": Path(config["index_path"]).resolve(),
        "grill.database_path": Path(
            config["grill"]["database_path"]
        ).resolve(),
        "state.db_path": Path(config["state"]["db_path"]).resolve(),
        "state.snapshot_path": Path(
            config["state"]["snapshot_path"]
        ).resolve(),
        "initialization.database_path": Path(
            config["initialization"]["database_path"]
        ).resolve(),
        "authority.v1": (root / DEFAULT_AUTHORITY_INDEX_V1_PATH).resolve(),
        "longform.v1": (root / DEFAULT_LONGFORM_V1_PATH).resolve(),
        "projection-runs.v1": (
            root / DEFAULT_PROJECTION_RUNS_V1_PATH
        ).resolve(),
    }
    items = list(runtime_files.items())
    for index, (left_name, left_path) in enumerate(items):
        for right_name, right_path in items[index + 1 :]:
            if _paths_share_identity(left_path, right_path):
                raise PlotRagError(
                    "config runtime paths must be distinct: "
                    f"{left_name} and {right_name}"
                )

    commit_dir = Path(config["state"]["commit_dir"]).resolve()
    for name, path in runtime_files.items():
        if _paths_share_identity(commit_dir, path) or _is_relative_to(
            path,
            commit_dir,
        ):
            raise PlotRagError(
                "config.state.commit_dir must not contain runtime file "
                f"{name}: {path}"
            )


def _nested_number(
    value: Any, field: str, default: float, minimum: float, maximum: float
) -> float:
    number = default if value is None else value
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        raise PlotRagError(f"config.{field} must be a number")
    result = float(number)
    if not minimum <= result <= maximum:
        raise PlotRagError(f"config.{field} must be between {minimum} and {maximum}")
    return result


def _nested_integer(
    value: Any, field: str, default: int, minimum: int, maximum: int
) -> int:
    number = default if value is None else value
    if type(number) is not int:
        raise PlotRagError(f"config.{field} must be an integer")
    if not minimum <= number <= maximum:
        raise PlotRagError(f"config.{field} must be between {minimum} and {maximum}")
    return number


def _bounded_integer(
    config: dict[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    return _nested_integer(config.get(key), key, default, minimum, maximum)


def _strict_boolean_fields(
    raw: Mapping[str, Any],
    *,
    field: str,
    defaults: Mapping[str, bool],
) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for key, default in defaults.items():
        value = raw[key] if key in raw else default
        if type(value) is not bool:
            raise PlotRagError(f"config.{field}.{key} must be a boolean")
        result[key] = value
    return result


def _strict_integer_field(
    raw: Mapping[str, Any],
    key: str,
    *,
    field: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = raw[key] if key in raw else default
    if type(value) is not int:
        raise PlotRagError(f"config.{field}.{key} must be an integer")
    if not minimum <= value <= maximum:
        raise PlotRagError(
            f"config.{field}.{key} must be between {minimum} and {maximum}"
        )
    return value


def _strict_choice_field(
    raw: Mapping[str, Any],
    key: str,
    *,
    field: str,
    default: str,
    allowed: set[str],
) -> str:
    value = raw[key] if key in raw else default
    if not isinstance(value, str) or not value.strip():
        raise PlotRagError(f"config.{field}.{key} must be a non-empty string")
    selected = value.strip().lower()
    if selected not in allowed:
        raise PlotRagError(
            f"config.{field}.{key} must be one of: "
            + ", ".join(sorted(allowed))
        )
    return selected


def _strict_string_enum_array(
    raw: Mapping[str, Any],
    key: str,
    *,
    field: str,
    default: Sequence[str],
    allowed: set[str],
    maximum_items: int,
) -> list[str]:
    value = raw[key] if key in raw else list(default)
    if not isinstance(value, list):
        raise PlotRagError(f"config.{field}.{key} must be a string array")
    if len(value) > maximum_items:
        raise PlotRagError(
            f"config.{field}.{key} must contain at most {maximum_items} entries"
        )
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise PlotRagError(
                f"config.{field}.{key}[{index}] must be a non-empty string"
            )
        normalized = item.strip().lower()
        if normalized not in allowed:
            raise PlotRagError(
                f"config.{field}.{key}[{index}] must be one of: "
                + ", ".join(sorted(allowed))
            )
        if normalized not in result:
            result.append(normalized)
    return result


def _remote_section(raw: Any, name: str, defaults: dict[str, Any]) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PlotRagError(f"config.remote.{name} must be an object")
    if "api_key" in raw:
        raise PlotRagError(
            f"config.remote.{name}.api_key is not allowed; use api_key_env instead"
        )
    # Unknown project fields are intentionally not propagated into the
    # normalized runtime config.  In particular, a project cannot add trusted
    # hosts, request headers, credential aliases, or other future security
    # surface by placing them beside the documented service fields.
    section = {
        field: raw[field] if field in raw else default
        for field, default in defaults.items()
    }
    for field in ("enabled", "api_key_required"):
        if not isinstance(section.get(field), bool):
            raise PlotRagError(f"config.remote.{name}.{field} must be a boolean")
    for field in ("base_url", "base_url_env", "model", "model_env", "api_key_env"):
        value = section.get(field, "")
        if not isinstance(value, str):
            raise PlotRagError(f"config.remote.{name}.{field} must be a string")
        section[field] = value.strip()
    for field, allowed in REMOTE_ENV_ALLOWLIST[name].items():
        if section[field] not in allowed:
            choices = ", ".join(sorted(allowed))
            raise PlotRagError(
                f"config.remote.{name}.{field} must be one of: {choices}"
            )
    return section


def load_config(project_root: Path | str) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise PlotRagError(f"project root does not exist or is not a directory: {root}")
    path = root / CONFIG_RELATIVE_PATH
    if not path.is_file():
        raise PlotRagError(f"missing project config: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise PlotRagError(f"invalid JSON in {path}: line {exc.lineno}, column {exc.colno}") from exc
    except OSError as exc:
        raise PlotRagError(f"cannot read project config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PlotRagError("project config root must be an object")
    legacy_version = raw.get("version")
    if (
        legacy_version is not None
        and (
            type(legacy_version) is not int
            or legacy_version not in {1, 2, 3}
        )
    ):
        raise PlotRagError(f"unsupported legacy config version: {legacy_version!r}")
    config_version = raw.get(
        "config_version",
        1 if legacy_version is None else legacy_version,
    )
    if type(config_version) is not int or config_version not in {1, 2, 3}:
        raise PlotRagError(f"unsupported config version: {config_version!r}")

    legacy_globs = (
        _validate_globs(raw.get("authority_globs"))
        if raw.get("authority_globs") is not None
        else None
    )
    authority_sources = _validate_authority_sources(
        raw.get("authority_sources"),
        legacy_globs=legacy_globs,
    )
    authority_globs = list(
        dict.fromkeys(source["glob"] for source in authority_sources)
    )
    ignore_value = raw.get("ignore_globs", [])
    if not isinstance(ignore_value, list) or any(not isinstance(v, str) for v in ignore_value):
        raise PlotRagError("config.ignore_globs must be a string array")
    ignore_globs = [v.strip().replace("\\", "/") for v in ignore_value if v.strip()]
    ignore_globs = list(dict.fromkeys([*ignore_globs, *INIT_IGNORE_GLOBS]))

    index_path = _project_path(root, raw.get("index_path"), DEFAULT_INDEX_PATH, "index_path")

    enabled = raw.get("enabled", True)
    trigger_short = raw.get("trigger_short_continue", True)
    if not isinstance(enabled, bool) or not isinstance(trigger_short, bool):
        raise PlotRagError("config.enabled and config.trigger_short_continue must be booleans")

    config = {
        "version": config_version,
        "config_version": config_version,
        "config_schema_version": config_version,
        "enabled": enabled,
        "trigger_short_continue": trigger_short,
        "authority_globs": authority_globs,
        "authority_sources": authority_sources,
        "ignore_globs": ignore_globs,
        "legacy_index_schema_version": SCHEMA_VERSION,
        "index_schema_version": SCHEMA_VERSION,
        "state_schema_version": (
            CONTINUITY_STATE_SCHEMA_VERSION
            if config_version >= 3
            else LEGACY_STATE_SCHEMA_VERSION
        ),
        "authority_index_schema_version": AUTHORITY_INDEX_SCHEMA_VERSION,
        "index_path": str(index_path),
        "max_chunk_chars": _bounded_integer(
            raw,
            "max_chunk_chars",
            DEFAULT_MAX_CHUNK_CHARS,
            200,
            8000,
        ),
        "max_file_bytes": _bounded_integer(
            raw,
            "max_file_bytes",
            DEFAULT_MAX_FILE_BYTES,
            1024,
            100 * 1024 * 1024,
        ),
        "candidate_limit": _bounded_integer(
            raw,
            "candidate_limit",
            DEFAULT_CANDIDATE_LIMIT,
            1,
            20,
        ),
        "reliable_coverage": _bounded_number(
            raw, "reliable_coverage", DEFAULT_RELIABLE_COVERAGE, 0.15, 0.95
        ),
        "weak_coverage": _bounded_number(
            raw, "weak_coverage", DEFAULT_WEAK_COVERAGE, 0.05, 0.8
        ),
    }
    if config["weak_coverage"] >= config["reliable_coverage"]:
        raise PlotRagError("config.weak_coverage must be lower than reliable_coverage")

    grill_raw = raw.get("grill", {})
    if not isinstance(grill_raw, dict):
        raise PlotRagError("config.grill must be an object")
    grill_bools: dict[str, bool] = {}
    for key, default in (
        ("enabled", True),
        ("one_question_per_turn", True),
        ("recommend_answer", True),
        ("explore_project_first", True),
    ):
        value = grill_raw.get(key, default)
        if not isinstance(value, bool):
            raise PlotRagError(f"config.grill.{key} must be a boolean")
        grill_bools[key] = value
    if not grill_bools["one_question_per_turn"]:
        raise PlotRagError(
            "config.grill.one_question_per_turn must stay true for deterministic handoff"
        )
    if "schema_version" not in grill_raw:
        grill_schema = "plot-rag-intent/v1"
    else:
        grill_schema_value = grill_raw["schema_version"]
        if (
            not isinstance(grill_schema_value, str)
            or not grill_schema_value.strip()
        ):
            raise PlotRagError(
                "config.grill.schema_version must be plot-rag-intent/v1"
            )
        grill_schema = grill_schema_value.strip()
    if grill_schema != "plot-rag-intent/v1":
        raise PlotRagError(
            "config.grill.schema_version must be plot-rag-intent/v1"
        )
    required_fields = grill_raw.get(
        "required_fields",
        list(GRILL_INTENT_FIELDS),
    )
    if (
        not isinstance(required_fields, list)
        or not required_fields
        or any(
            not isinstance(item, str) or item not in GRILL_INTENT_FIELDS
            for item in required_fields
        )
    ):
        raise PlotRagError(
            "config.grill.required_fields must be a non-empty array of supported intent fields"
        )

    def grill_phrases(key: str, defaults: Sequence[str]) -> list[str]:
        value = grill_raw.get(key, list(defaults))
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) or not item.strip() for item in value)
        ):
            raise PlotRagError(
                f"config.grill.{key} must be a non-empty string array"
            )
        return list(dict.fromkeys(item.strip() for item in value))

    config["grill"] = {
        **grill_bools,
        "schema_version": grill_schema,
        "database_path": str(
            _project_path(
                root,
                grill_raw.get("database_path"),
                DEFAULT_GRILL_DB_PATH,
                "grill.database_path",
            )
        ),
        "max_questions": _nested_integer(
            grill_raw.get("max_questions"),
            "grill.max_questions",
            6,
            1,
            12,
        ),
        "session_ttl_seconds": _nested_integer(
            grill_raw.get("session_ttl_seconds"),
            "grill.session_ttl_seconds",
            21600,
            300,
            86400,
        ),
        "required_fields": list(dict.fromkeys(required_fields)),
        "skip_phrases": grill_phrases(
            "skip_phrases",
            DEFAULT_GRILL_SKIP_PHRASES,
        ),
        "cancel_phrases": grill_phrases(
            "cancel_phrases",
            DEFAULT_GRILL_CANCEL_PHRASES,
        ),
    }

    state_raw = raw.get("state", {})
    if not isinstance(state_raw, dict):
        raise PlotRagError("config.state must be an object")
    state_bools: dict[str, bool] = {}
    for key, default in (
        ("enabled", True),
        ("auto_retrieve", True),
        ("auto_record", True),
        ("fail_closed", False),
    ):
        value = state_raw.get(key, default)
        if not isinstance(value, bool):
            raise PlotRagError(f"config.state.{key} must be a boolean")
        state_bools[key] = value
    categories = state_raw.get(
        "categories",
        list(
            STATE_CATEGORIES
            if config_version >= 3
            else LEGACY_STATE_CATEGORIES
        ),
    )
    if (
        not isinstance(categories, list)
        or not categories
        or any(not isinstance(item, str) or item not in STATE_CATEGORIES for item in categories)
    ):
        raise PlotRagError(
            "config.state.categories must be a non-empty array of supported category names"
        )
    config["state"] = {
        **state_bools,
        "db_path": str(
            _project_path(
                root,
                state_raw.get("db_path"),
                DEFAULT_STATE_DB_PATH,
                "state.db_path",
            )
        ),
        "snapshot_path": str(
            _project_path(
                root,
                state_raw.get("snapshot_path"),
                DEFAULT_STATE_SNAPSHOT_PATH,
                "state.snapshot_path",
            )
        ),
        "commit_dir": str(
            _project_path(
                root,
                state_raw.get("commit_dir"),
                DEFAULT_STATE_COMMIT_DIR,
                "state.commit_dir",
            )
        ),
        "categories": list(dict.fromkeys(categories)),
        "top_k": _nested_integer(
            state_raw.get("top_k"),
            "state.top_k",
            12,
            1,
            50,
        ),
        "max_context_chars": _nested_integer(
            state_raw.get("max_context_chars"),
            "state.max_context_chars",
            12000,
            1000,
            100000,
        ),
        "min_confidence": _nested_number(
            state_raw.get("min_confidence"),
            "state.min_confidence",
            0.72,
            0.0,
            1.0,
        ),
    }

    lifecycle_raw = raw.get("lifecycle", {})
    if not isinstance(lifecycle_raw, dict):
        raise PlotRagError("config.lifecycle must be an object")
    strict_lifecycle = lifecycle_raw.get("strict", config_version >= 3)
    index_embeddings = lifecycle_raw.get("index_embeddings_on_prepare", False)
    if not isinstance(strict_lifecycle, bool):
        raise PlotRagError("config.lifecycle.strict must be a boolean")
    if not isinstance(index_embeddings, bool):
        raise PlotRagError(
            "config.lifecycle.index_embeddings_on_prepare must be a boolean"
        )
    if config_version >= 3 and not strict_lifecycle:
        raise PlotRagError(
            "config v3 requires lifecycle.strict=true; generation hooks are proposal-only"
        )
    config["lifecycle"] = {
        "strict": bool(strict_lifecycle and config_version >= 3),
        "longform_context_chars": _nested_integer(
            lifecycle_raw.get("longform_context_chars"),
            "lifecycle.longform_context_chars",
            7000,
            1200,
            100000,
        ),
        "index_embeddings_on_prepare": index_embeddings,
        "approval_ttl_seconds": _nested_integer(
            lifecycle_raw.get("approval_ttl_seconds"),
            "lifecycle.approval_ttl_seconds",
            300,
            30,
            3600,
        ),
        "candidate_cache_limit": _nested_integer(
            lifecycle_raw.get("candidate_cache_limit"),
            "lifecycle.candidate_cache_limit",
            5000,
            0,
            100000,
        ),
        "projection_history_limit": _nested_integer(
            lifecycle_raw.get("projection_history_limit"),
            "lifecycle.projection_history_limit",
            20,
            1,
            1000,
        ),
    }

    performance_raw = raw.get("performance", {})
    if not isinstance(performance_raw, dict):
        raise PlotRagError("config.performance must be an object")
    prepare_v2_raw = performance_raw.get("prepare_v2", {})
    if not isinstance(prepare_v2_raw, dict):
        raise PlotRagError("config.performance.prepare_v2 must be an object")
    prepare_v2 = {
        **_strict_boolean_fields(
            prepare_v2_raw,
            field="performance.prepare_v2",
            defaults=PREPARE_V2_BOOLEAN_DEFAULTS,
        ),
        "rerank_max_concurrency": _strict_integer_field(
            prepare_v2_raw,
            "rerank_max_concurrency",
            field="performance.prepare_v2",
            default=4,
            minimum=1,
            maximum=32,
        ),
        "remote_total_concurrency": _strict_integer_field(
            prepare_v2_raw,
            "remote_total_concurrency",
            field="performance.prepare_v2",
            default=6,
            minimum=1,
            maximum=64,
        ),
    }
    if (
        prepare_v2["enabled"]
        and not prepare_v2["single_read_snapshot"]
    ):
        raise PlotRagError(
            "config.performance.prepare_v2.single_read_snapshot must stay true "
            "when prepare_v2 is enabled"
        )
    if (
        prepare_v2["batch_embedding"]
        and not prepare_v2["batch_failure_fallback_single"]
    ):
        raise PlotRagError(
            "config.performance.prepare_v2.batch_failure_fallback_single must "
            "stay true when batch_embedding is enabled"
        )

    extraction_raw = performance_raw.get("extraction", {})
    if not isinstance(extraction_raw, dict):
        raise PlotRagError("config.performance.extraction must be an object")
    extraction = {
        "mode": _strict_choice_field(
            extraction_raw,
            "mode",
            field="performance.extraction",
            default="sync",
            allowed=EXTRACTION_MODES,
        ),
        **_strict_boolean_fields(
            extraction_raw,
            field="performance.extraction",
            defaults=EXTRACTION_BOOLEAN_DEFAULTS,
        ),
        "deterministic_repairs": _strict_string_enum_array(
            extraction_raw,
            "deterministic_repairs",
            field="performance.extraction",
            default=("single_action_event_type_echo",),
            allowed=DETERMINISTIC_REPAIRS,
            maximum_items=16,
        ),
    }
    if extraction["mode"] == "async" and (
        not extraction["next_plot_turn_barrier"]
        or not extraction["barrier_requires_proposal_resolution"]
    ):
        raise PlotRagError(
            "config.performance.extraction async mode requires "
            "next_plot_turn_barrier=true and "
            "barrier_requires_proposal_resolution=true"
        )
    config["performance"] = {
        "prepare_v2": prepare_v2,
        "extraction": extraction,
    }

    event_experience_raw = raw.get("event_experience", {})
    if not isinstance(event_experience_raw, dict):
        raise PlotRagError("config.event_experience must be an object")
    event_experience = {
        **_strict_boolean_fields(
            event_experience_raw,
            field="event_experience",
            defaults=EVENT_EXPERIENCE_BOOLEAN_DEFAULTS,
        ),
        "max_questions_per_chain": _strict_integer_field(
            event_experience_raw,
            "max_questions_per_chain",
            field="event_experience",
            default=1,
            minimum=1,
            maximum=1,
        ),
        "repeat_same_question_limit": _strict_integer_field(
            event_experience_raw,
            "repeat_same_question_limit",
            field="event_experience",
            default=1,
            minimum=1,
            maximum=1,
        ),
        "session_ttl_seconds": _strict_integer_field(
            event_experience_raw,
            "session_ttl_seconds",
            field="event_experience",
            default=21600,
            minimum=60,
            maximum=604800,
        ),
    }
    if not event_experience["one_question_per_turn"]:
        raise PlotRagError(
            "config.event_experience.one_question_per_turn must stay true"
        )
    config["event_experience"] = event_experience

    items_raw = raw.get("items", {})
    if not isinstance(items_raw, dict):
        raise PlotRagError("config.items must be an object")
    config["items"] = {
        "schema_version": _strict_choice_field(
            items_raw,
            "schema_version",
            field="items",
            default="plot-rag-item/v1",
            allowed=ITEM_SCHEMA_VERSIONS,
        ),
        "delta_version": _strict_choice_field(
            items_raw,
            "delta_version",
            field="items",
            default="plot-rag-delta/v4",
            allowed=ITEM_DELTA_VERSIONS,
        ),
        **_strict_boolean_fields(
            items_raw,
            field="items",
            defaults=ITEM_BOOLEAN_DEFAULTS,
        ),
    }

    advantage_raw = raw.get("advantage", {})
    if not isinstance(advantage_raw, dict):
        raise PlotRagError("config.advantage must be an object")
    advantage_bools = _strict_boolean_fields(
        advantage_raw,
        field="advantage",
        defaults=ADVANTAGE_BOOLEAN_DEFAULTS,
    )
    config["advantage"] = {
        "enabled": advantage_bools["enabled"],
        "shadow": advantage_bools["shadow"],
        "schema_version": _strict_choice_field(
            advantage_raw,
            "schema_version",
            field="advantage",
            default="plot-rag-advantage/v1",
            allowed=ADVANTAGE_SCHEMA_VERSIONS,
        ),
        "strict_runtime_validation": advantage_bools[
            "strict_runtime_validation"
        ],
        "readable_projection": advantage_bools["readable_projection"],
        "mandatory_context": advantage_bools["mandatory_context"],
    }

    initialization_raw = raw.get("initialization", {})
    if not isinstance(initialization_raw, dict):
        raise PlotRagError("config.initialization must be an object")
    initialization_enabled = initialization_raw.get("enabled", True)
    proposal_only = initialization_raw.get("proposal_only", True)
    if not isinstance(initialization_enabled, bool):
        raise PlotRagError("config.initialization.enabled must be a boolean")
    if not isinstance(proposal_only, bool):
        raise PlotRagError("config.initialization.proposal_only must be a boolean")
    if config_version >= 3 and not proposal_only:
        raise PlotRagError(
            "config v3 requires initialization.proposal_only=true before approval"
        )
    init_schema = str(
        initialization_raw.get("schema_version")
        or ("auto" if config_version >= 3 else "plot-rag-init/v1")
    ).strip()
    if init_schema not in {
        "auto",
        "plot-rag-init/v1",
        "plot-rag-init/v2",
    }:
        raise PlotRagError(
            "config.initialization.schema_version must be one of: "
            "auto, plot-rag-init/v1, plot-rag-init/v2"
        )
    config["initialization"] = {
        "enabled": initialization_enabled,
        "schema_version": init_schema,
        "database_path": str(
            _project_path(
                root,
                initialization_raw.get("database_path"),
                DEFAULT_INIT_DB_PATH,
                "initialization.database_path",
            )
        ),
        "proposal_only": proposal_only,
        "default_mode": _choice(
            initialization_raw.get("default_mode"),
            field="initialization.default_mode",
            default="auto",
            allowed=INITIALIZATION_MODES,
        ),
        "default_target_profile": _choice(
            initialization_raw.get("default_target_profile"),
            field="initialization.default_target_profile",
            default="plot_ready",
            allowed=INITIALIZATION_TARGET_PROFILES,
        ),
        "default_interaction_profile": _choice(
            initialization_raw.get("default_interaction_profile"),
            field="initialization.default_interaction_profile",
            default="balanced",
            allowed=INITIALIZATION_INTERACTION_PROFILES,
        ),
        "source_max_bytes": _nested_integer(
            initialization_raw.get("source_max_bytes"),
            "initialization.source_max_bytes",
            16 * 1024 * 1024,
            1024,
            1024 * 1024 * 1024,
        ),
        "exclude_globs": _validate_relative_glob_array(
            initialization_raw.get("exclude_globs"),
            field="initialization.exclude_globs",
            defaults=INIT_IGNORE_GLOBS,
        ),
    }

    power_raw = raw.get("power_system", {})
    if not isinstance(power_raw, dict):
        raise PlotRagError("config.power_system must be an object")
    strict_progression = power_raw.get("strict_progression", True)
    if not isinstance(strict_progression, bool):
        raise PlotRagError(
            "config.power_system.strict_progression must be a boolean"
        )
    profiles = power_raw.get("profiles", [])
    if (
        not isinstance(profiles, list)
        or any(not isinstance(item, str) or not item.strip() for item in profiles)
    ):
        raise PlotRagError(
            "config.power_system.profiles must be an array of non-empty strings"
        )
    power_schema = str(
        power_raw.get("schema_version") or "plot-rag-power/v1"
    ).strip()
    if power_schema != "plot-rag-power/v1":
        raise PlotRagError(
            "config.power_system.schema_version must be plot-rag-power/v1"
        )
    config["power_system"] = {
        "mode": _choice(
            power_raw.get("mode"),
            field="power_system.mode",
            default="auto",
            allowed=POWER_SYSTEM_MODES,
        ),
        "schema_version": power_schema,
        "strict_progression": strict_progression,
        "comparison_mode": _choice(
            power_raw.get("comparison_mode"),
            field="power_system.comparison_mode",
            default="conditional",
            allowed=POWER_COMPARISON_MODES,
        ),
        "unknown_policy": _choice(
            power_raw.get("unknown_policy"),
            field="power_system.unknown_policy",
            default="quarantine",
            allowed=POWER_UNKNOWN_POLICIES,
        ),
        "profiles": list(dict.fromkeys(item.strip() for item in profiles)),
    }

    craft_raw = raw.get("craft", {})
    if not isinstance(craft_raw, dict):
        raise PlotRagError("config.craft must be an object")
    craft_bools: dict[str, bool] = {}
    for key, default in (
        ("enabled", True),
        ("auto_retrieve", True),
        ("use_embedding", True),
        ("use_rerank", True),
    ):
        value = craft_raw.get(key, default)
        if not isinstance(value, bool):
            raise PlotRagError(f"config.craft.{key} must be a boolean")
        craft_bools[key] = value
    craft_top_k = _nested_integer(
        craft_raw.get("top_k"),
        "craft.top_k",
        4,
        1,
        8,
    )
    craft_candidate_pool = _nested_integer(
        craft_raw.get("candidate_pool"),
        "craft.candidate_pool",
        10,
        2,
        50,
    )
    if craft_candidate_pool < craft_top_k:
        raise PlotRagError("config.craft.candidate_pool must be greater than or equal to craft.top_k")
    config["craft"] = {
        **craft_bools,
        "top_k": craft_top_k,
        "candidate_pool": craft_candidate_pool,
        "max_context_chars": _nested_integer(
            craft_raw.get("max_context_chars"),
            "craft.max_context_chars",
            6500,
            1200,
            20000,
        ),
    }

    remote_raw = raw.get("remote", {})
    if not isinstance(remote_raw, dict):
        raise PlotRagError("config.remote must be an object")
    config["remote"] = {
        "timeout_seconds": _nested_number(
            remote_raw.get("timeout_seconds"),
            "remote.timeout_seconds",
            15,
            1,
            120,
        ),
        "embedding": _remote_section(
            remote_raw.get("embedding"),
            "embedding",
            {
                "enabled": True,
                "base_url": "https://api.siliconflow.cn/v1",
                "base_url_env": "EMBED_BASE_URL",
                "model": "BAAI/bge-m3",
                "model_env": "EMBED_MODEL",
                "api_key_env": "SILICONFLOW_API_KEY",
                "api_key_required": True,
            },
        ),
        "rerank": _remote_section(
            remote_raw.get("rerank"),
            "rerank",
            {
                "enabled": True,
                "base_url": "https://api.siliconflow.cn/v1",
                "base_url_env": "RERANK_BASE_URL",
                "model": "BAAI/bge-reranker-v2-m3",
                "model_env": "RERANK_MODEL",
                "api_key_env": "SILICONFLOW_API_KEY",
                "api_key_required": True,
            },
        ),
        "extract": _remote_section(
            remote_raw.get("extract"),
            "extract",
            {
                "enabled": True,
                "base_url": "https://api.siliconflow.cn/v1",
                "base_url_env": "PLOT_RAG_LLM_BASE_URL",
                "model": "Qwen/Qwen3-30B-A3B-Instruct-2507",
                "model_env": "PLOT_RAG_LLM_MODEL",
                "api_key_env": "SILICONFLOW_API_KEY",
                "api_key_required": True,
            },
        ),
    }
    _validate_runtime_path_layout(root, path, config)
    return config


def _matches_any(relative_path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(relative_path, pattern) for pattern in patterns)


def _source_files(root: Path, config: dict[str, Any]) -> list[Path]:
    found: dict[str, Path] = {}
    excluded: set[str] = set()
    index_path = Path(config["index_path"]).resolve()
    for source in config["authority_sources"]:
        pattern = str(source["glob"])
        ingest_policy = str(source["ingest_policy"])
        try:
            matches = root.glob(pattern)
        except (OSError, ValueError) as exc:
            raise PlotRagError(f"cannot expand authority glob {pattern!r}: {exc}") from exc
        for path in matches:
            try:
                resolved = path.resolve()
            except OSError as exc:
                raise PlotRagError(f"cannot resolve authority source {path}: {exc}") from exc
            if not resolved.is_file() or resolved == index_path:
                continue
            if not _is_relative_to(resolved, root):
                raise PlotRagError(f"authority source escapes project root: {resolved}")
            relative = resolved.relative_to(root).as_posix()
            if _matches_any(relative, config["ignore_globs"]):
                continue
            if ingest_policy == "exclude":
                excluded.add(relative)
                found.pop(relative, None)
                continue
            if relative in excluded:
                continue
            found[relative] = resolved
    return [found[key] for key in sorted(found, key=str.casefold)]


def _split_long_block(
    block: list[tuple[int, str]], max_chars: int
) -> Iterable[list[tuple[int, str]]]:
    current: list[tuple[int, str]] = []
    size = 0
    for line_number, line in block:
        if len(line) > max_chars:
            if current:
                yield current
                current = []
                size = 0
            for offset in range(0, len(line), max_chars):
                yield [(line_number, line[offset : offset + max_chars])]
            continue
        added = len(line) + (1 if current else 0)
        if current and size + added > max_chars:
            yield current
            current = []
            size = 0
        current.append((line_number, line))
        size += len(line) + (1 if len(current) > 1 else 0)
    if current:
        yield current


def chunk_markdown(text: str, max_chars: int = DEFAULT_MAX_CHUNK_CHARS) -> list[Chunk]:
    lines = text.splitlines()
    chunks: list[Chunk] = []
    headings: list[str] = []
    pending: list[tuple[int, str]] = []

    def heading_path() -> str:
        return " > ".join(headings)

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        for part in _split_long_block(pending, max_chars):
            body = "\n".join(line for _, line in part).strip()
            if not body:
                continue
            heading = heading_path()
            search_text = f"{heading}\n{body}" if heading else body
            chunks.append(
                Chunk(
                    ordinal=len(chunks),
                    heading=heading,
                    start_line=part[0][0],
                    end_line=part[-1][0],
                    text=body,
                    search_text=search_text,
                )
            )
        pending = []

    for line_number, line in enumerate(lines, start=1):
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            flush()
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            headings[:] = headings[: level - 1]
            while len(headings) < level - 1:
                headings.append("")
            headings.append(title)
            continue
        if not line.strip():
            flush()
            continue
        pending.append((line_number, line))
    flush()
    return chunks


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _index_fingerprint(config: dict[str, Any]) -> str:
    material = {
        "schema": SCHEMA_VERSION,
        "authority_globs": config["authority_globs"],
        "authority_sources": [
            {
                "glob": source["glob"],
                "ingest_policy": source["ingest_policy"],
            }
            for source in config["authority_sources"]
        ],
        "ignore_globs": config["ignore_globs"],
        "max_chunk_chars": config["max_chunk_chars"],
        "max_file_bytes": config["max_file_bytes"],
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return _sha256(encoded)


def _open_database(index_path: Path) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(index_path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("BEGIN IMMEDIATE")
        try:
            validate_sqlite_component_schema(
                connection,
                component="legacy authority index",
                meta_table="meta",
                version_key="schema_version",
                supported_version=SCHEMA_VERSION,
                owned_tables=LEGACY_INDEX_TABLES,
                allowed_tables=LEGACY_INDEX_TABLES,
            )
        except SQLiteComponentSchemaError as exc:
            raise PlotRagError(str(exc)) from exc
        execute_sqlite_script_in_transaction(
            connection,
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                chunk_count INTEGER NOT NULL,
                indexed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL
                    REFERENCES files(path) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                heading TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                text TEXT NOT NULL,
                search_text TEXT NOT NULL,
                UNIQUE(path, ordinal)
            );
            CREATE INDEX IF NOT EXISTS chunks_path_idx ON chunks(path);
            """,
        )
        _meta_set(connection, "schema_version", SCHEMA_VERSION)
        connection.commit()
        return connection
    except BaseException as exc:
        if connection is not None:
            try:
                if connection.in_transaction:
                    connection.rollback()
            except sqlite3.Error:
                pass
            try:
                connection.close()
            except sqlite3.Error:
                pass
        if isinstance(exc, (OSError, sqlite3.Error)):
            raise PlotRagError(f"cannot open index {index_path}: {exc}") from exc
        raise


def _meta_get(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else None


def _meta_set(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def refresh_index(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    sources = _source_files(root, config)
    index_path = Path(config["index_path"])
    fingerprint = _index_fingerprint(config)
    connection = _open_database(index_path)
    changed = 0
    removed = 0
    unchanged = 0
    try:
        integrity = connection.execute("PRAGMA quick_check").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            raise PlotRagError(f"SQLite quick_check failed for {index_path}")
        previous_schema = _meta_get(connection, "schema_version")
        if previous_schema not in (None, str(SCHEMA_VERSION)):
            raise PlotRagError(
                f"index schema {previous_schema} is incompatible with {SCHEMA_VERSION}; rebuild required"
            )
        force_refresh = _meta_get(connection, "index_fingerprint") != fingerprint
        existing_rows = {
            str(row["path"]): row
            for row in connection.execute(
                "SELECT path, sha256, size, mtime_ns, chunk_count FROM files"
            )
        }
        current_paths: set[str] = set()

        with connection:
            for path in sources:
                relative = path.relative_to(root).as_posix()
                current_paths.add(relative)
                try:
                    stat = path.stat()
                    if stat.st_size > config["max_file_bytes"]:
                        raise PlotRagError(
                            f"authority source exceeds max_file_bytes ({stat.st_size}): {relative}"
                        )
                    data = path.read_bytes()
                    digest = _sha256(data)
                    text = data.decode("utf-8-sig")
                except (OSError, UnicodeError) as exc:
                    raise PlotRagError(f"cannot index authority source {relative}: {exc}") from exc

                previous = existing_rows.get(relative)
                if previous is not None and not force_refresh and previous["sha256"] == digest:
                    unchanged += 1
                    continue

                chunks = chunk_markdown(text, config["max_chunk_chars"])
                connection.execute("DELETE FROM files WHERE path = ?", (relative,))
                connection.execute(
                    "INSERT INTO files(path, sha256, size, mtime_ns, chunk_count, indexed_at) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (relative, digest, len(data), stat.st_mtime_ns, len(chunks), _utc_now()),
                )
                connection.executemany(
                    "INSERT INTO chunks(path, ordinal, heading, start_line, end_line, text, search_text) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            relative,
                            chunk.ordinal,
                            chunk.heading,
                            chunk.start_line,
                            chunk.end_line,
                            chunk.text,
                            chunk.search_text,
                        )
                        for chunk in chunks
                    ],
                )
                changed += 1

            stale = sorted(set(existing_rows) - current_paths)
            for relative in stale:
                connection.execute("DELETE FROM files WHERE path = ?", (relative,))
                removed += 1

            source_count = int(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0])
            chunk_count = int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            _meta_set(connection, "schema_version", SCHEMA_VERSION)
            _meta_set(connection, "index_fingerprint", fingerprint)
            _meta_set(connection, "refreshed_at", _utc_now())
            _meta_set(connection, "source_count", source_count)
            _meta_set(connection, "chunk_count", chunk_count)
        return {
            "healthy": True,
            "index_path": str(index_path),
            "source_count": source_count,
            "chunk_count": chunk_count,
            "changed_files": changed,
            "unchanged_files": unchanged,
            "removed_files": removed,
        }
    except sqlite3.Error as exc:
        raise PlotRagError(f"index refresh failed for {index_path}: {exc}") from exc
    finally:
        connection.close()


def _compact(text: str) -> str:
    return _COMPACT_RE.sub("", text.lower())


def _tokens(text: str) -> list[str]:
    lowered = text.lower()
    tokens = [f"w:{word}" for word in _ASCII_WORD_RE.findall(lowered)]
    for run in _CJK_RE.findall(lowered):
        if len(run) == 1:
            tokens.append(f"c1:{run}")
            continue
        tokens.extend(f"c2:{run[i:i + 2]}" for i in range(len(run) - 1))
        if len(run) >= 3:
            tokens.extend(f"c3:{run[i:i + 3]}" for i in range(len(run) - 2))
    return tokens


def _meaningful_anchor_tokens(text: str) -> set[str]:
    anchors: set[str] = set()
    for token in _tokens(text):
        kind, _, value = token.partition(":")
        if kind == "w":
            if len(value) >= 2 and value not in _CORE_ASCII_STOPWORDS:
                anchors.add(token)
        elif kind == "c2" and value not in _CORE_CJK_STOPWORDS:
            anchors.add(token)
    return anchors


def _primary_focus_anchors(query: str) -> set[str]:
    cjk = "".join(_CJK_RE.findall(query.lower()))
    if cjk:
        tail_match = re.search(r"什么([\u3400-\u4dbf\u4e00-\u9fff]{1,4})$", cjk)
        if tail_match:
            tail = tail_match.group(1)
            anchors = {tail}
            prefix = cjk[: tail_match.start()].rstrip("的了过着是有在")
            if len(tail) == 1 and prefix:
                anchors.add(prefix[-1] + tail)
            return anchors

        stem = cjk
        for suffix in _CJK_QUESTION_SUFFIXES:
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        stem = stem.rstrip("的了过着是有在")
        if stem:
            focus = stem[-4:]
            for modifier in _FOCUS_MODIFIERS:
                if focus.startswith(modifier) and len(focus) > len(modifier):
                    focus = focus[len(modifier) :]
                    break
            if focus and focus not in _CORE_CJK_STOPWORDS:
                return {focus}

    words = [
        word
        for word in _ASCII_WORD_RE.findall(query.lower())
        if len(word) >= 2 and word not in _CORE_ASCII_STOPWORDS
    ]
    if not words:
        return set()
    return {words[0] if query.lstrip().lower().startswith(("what ", "which ")) else words[-1]}


def _shared_query_anchors(variants: Sequence[str]) -> set[str]:
    if len(variants) < 2:
        return set()
    per_variant = [_meaningful_anchor_tokens(variant) for variant in variants]
    if not per_variant:
        return set()
    return set.intersection(*per_variant)


def _independent_anchor_span_count(query: str, passage: str) -> int:
    query_cjk = "".join(_CJK_RE.findall(query.lower()))
    passage_cjk = "".join(_CJK_RE.findall(passage.lower()))
    cjk_spans = 0
    if query_cjk and passage_cjk:
        for block in SequenceMatcher(
            None,
            query_cjk,
            passage_cjk,
            autojunk=False,
        ).get_matching_blocks():
            if block.size < 2:
                continue
            fragment = query_cjk[block.a : block.a + block.size]
            if fragment not in _CORE_CJK_STOPWORDS:
                cjk_spans += 1

    query_words = {
        token[2:]
        for token in _meaningful_anchor_tokens(query)
        if token.startswith("w:")
    }
    passage_words = {
        token[2:]
        for token in _meaningful_anchor_tokens(passage)
        if token.startswith("w:")
    }
    return cjk_spans + len(query_words & passage_words)


def _bm25_candidates(
    rows: Sequence[sqlite3.Row], query: str, config: dict[str, Any]
) -> list[dict[str, Any]]:
    if not rows:
        return []
    document_tokens = [_tokens(str(row["search_text"])) for row in rows]
    frequencies = [Counter(tokens) for tokens in document_tokens]
    lengths = [len(tokens) for tokens in document_tokens]
    average_length = sum(lengths) / max(len(lengths), 1)
    query_tokens = list(dict.fromkeys(_tokens(query)))
    if not query_tokens:
        return []
    document_frequency = {
        token: sum(1 for frequency in frequencies if token in frequency) for token in query_tokens
    }
    corpus_size = len(rows)
    idf = {
        token: math.log(1.0 + (corpus_size - count + 0.5) / (count + 0.5))
        for token, count in document_frequency.items()
    }
    total_query_weight = sum(idf.values()) or 1.0
    compact_query = _compact(query)
    candidates: list[dict[str, Any]] = []
    k1 = 1.5
    b = 0.75

    for row, frequency, length in zip(rows, frequencies, lengths):
        score = 0.0
        matched: list[str] = []
        for token in query_tokens:
            term_frequency = frequency.get(token, 0)
            if not term_frequency:
                continue
            matched.append(token)
            denominator = term_frequency + k1 * (
                1 - b + b * (length / average_length if average_length else 1.0)
            )
            score += idf[token] * ((term_frequency * (k1 + 1)) / denominator)
        coverage = sum(idf[token] for token in matched) / total_query_weight
        compact_document = _compact(str(row["search_text"]))
        exact = bool(len(compact_query) >= 3 and compact_query in compact_document)
        if exact:
            score += 25.0
            coverage = 1.0
        if not matched and not exact:
            continue
        score += coverage * 8.0
        candidates.append(
            {
                "path": str(row["path"]),
                "heading": str(row["heading"]),
                "start_line": int(row["start_line"]),
                "end_line": int(row["end_line"]),
                "excerpt": str(row["text"]),
                "score": round(score, 4),
                "coverage": round(coverage, 4),
                "matched_terms": len(matched),
                "matched_trigrams": sum(token.startswith("c3:") for token in matched),
                "exact": exact,
                "_query": query,
                "_tokens": set(frequency),
            }
        )
    candidates.sort(
        key=lambda item: (
            bool(item["exact"]),
            float(item["score"]),
            float(item["coverage"]),
            -len(str(item["excerpt"])),
        ),
        reverse=True,
    )
    return candidates[: max(config["candidate_limit"] * 3, 10)]


def _localized_candidate_excerpt(
    candidate: dict[str, Any],
) -> tuple[int, int, str]:
    start_line = int(candidate["start_line"])
    end_line = int(candidate["end_line"])
    excerpt = str(candidate["excerpt"])
    query_tokens = set(_tokens(str(candidate.get("_query", ""))))
    excerpt_lines = excerpt.splitlines()
    if query_tokens and len(excerpt_lines) > 1:
        compact_query = _compact(str(candidate.get("_query", "")))
        spans: list[tuple[bool, int, int, int, str]] = []
        max_window = min(3, len(excerpt_lines))
        for window_size in range(1, max_window + 1):
            for window_start in range(len(excerpt_lines) - window_size + 1):
                window_text = "\n".join(
                    excerpt_lines[window_start : window_start + window_size]
                )
                token_score = len(query_tokens & set(_tokens(window_text)))
                exact = bool(
                    compact_query
                    and compact_query in _compact(window_text)
                )
                spans.append(
                    (
                        exact,
                        token_score,
                        -window_size,
                        -window_start,
                        window_text,
                    )
                )
        best = max(spans, key=lambda item: item[:4])
        if best[1] > 0:
            window_size = -best[2]
            window_start = -best[3]
            start_line += window_start
            end_line = start_line + window_size - 1
            excerpt = best[4]
    return start_line, end_line, excerpt


def _covers_atomic_core_anchors(
    candidate: dict[str, Any],
    variants: Sequence[str],
) -> bool:
    if not variants:
        return False
    _, _, excerpt = _localized_candidate_excerpt(candidate)
    primary = str(variants[0])
    compact_primary = _compact(primary)
    compact_excerpt = _compact(excerpt)
    if _NONCONFIRMING_EVIDENCE_RE.search(compact_excerpt):
        return False
    if compact_primary and compact_primary in compact_excerpt:
        return True

    focus_anchors = _primary_focus_anchors(primary)
    if focus_anchors and not any(
        _compact(anchor) in compact_excerpt for anchor in focus_anchors
    ):
        return False

    passage_anchors = _meaningful_anchor_tokens(excerpt)
    shared_anchors = _shared_query_anchors(variants)
    focus_tokens = set().union(
        *(_meaningful_anchor_tokens(anchor) for anchor in focus_anchors)
    )
    shared_context = shared_anchors - focus_tokens
    if shared_context and not (shared_context & passage_anchors):
        return False

    if focus_anchors and shared_context:
        return True
    return _independent_anchor_span_count(primary, excerpt) >= 2


def _public_candidate(candidate: dict[str, Any], root: Path) -> dict[str, Any]:
    start_line, end_line, excerpt = _localized_candidate_excerpt(candidate)
    return {
        key: value
        for key, value in {
            "path": candidate["path"],
            "absolute_path": str(root / candidate["path"]),
            "heading": candidate["heading"],
            "start_line": start_line,
            "end_line": end_line,
            "excerpt": excerpt,
            "score": candidate["score"],
            "coverage": candidate["coverage"],
            "exact": candidate["exact"],
        }.items()
    }


def _same_fact(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left["path"] == right["path"]:
        if left["heading"] and left["heading"] == right["heading"]:
            return True
        if abs(int(left["start_line"]) - int(right["start_line"])) <= 40:
            return True
    left_tokens = left.get("_tokens", set())
    right_tokens = right.get("_tokens", set())
    union = left_tokens | right_tokens
    return bool(union and len(left_tokens & right_tokens) / len(union) >= 0.28)


def _deduplicate_candidates(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[str, int, int], dict[str, Any]] = {}
    for candidate in candidates:
        key = (candidate["path"], candidate["start_line"], candidate["end_line"])
        if key not in best or candidate["score"] > best[key]["score"]:
            best[key] = candidate
    return sorted(best.values(), key=lambda item: item["score"], reverse=True)


def _exact_passage(candidate: dict[str, Any]) -> str:
    query = _compact(str(candidate.get("_query", "")))
    for line in str(candidate["excerpt"]).splitlines():
        compact_line = _compact(line)
        if query and query in compact_line:
            return compact_line
    return _compact(str(candidate["excerpt"]))


def _classify(
    variants: Sequence[str],
    ranked: Sequence[list[dict[str, Any]]],
    config: dict[str, Any],
) -> tuple[str, str, list[dict[str, Any]]]:
    reliable_per_query: list[list[dict[str, Any]]] = []
    anchored_reliable_per_query: list[list[dict[str, Any]]] = []
    weak_per_query: list[list[dict[str, Any]]] = []
    for candidates in ranked:
        reliable = [
            candidate
            for candidate in candidates
            if candidate["exact"]
            or (
                candidate["coverage"] >= config["reliable_coverage"]
                and candidate["matched_terms"] >= 3
            )
            or (
                candidate["coverage"] >= config["weak_coverage"]
                and (
                    candidate["matched_terms"] >= 6
                    or (
                        candidate["matched_terms"] >= 5
                        and candidate["matched_trigrams"] >= 2
                    )
                )
            )
        ]
        weak = [
            candidate
            for candidate in candidates
            if candidate["coverage"] >= config["weak_coverage"]
            and candidate["matched_terms"] >= 2
        ]
        reliable_per_query.append(reliable)
        anchored_reliable_per_query.append(
            [
                candidate
                for candidate in reliable
                if _covers_atomic_core_anchors(candidate, variants)
            ]
        )
        weak_per_query.append(weak)

    exact = _deduplicate_candidates(
        candidate
        for candidates in anchored_reliable_per_query
        for candidate in candidates
        if candidate["exact"]
    )
    if exact:
        passages = {_exact_passage(candidate) for candidate in exact}
        if len(passages) > 1:
            return (
                STATUS_AMBIGUOUS,
                "multiple exact authoritative passages differ; inspect them before proceeding",
                exact,
            )
        return STATUS_HIT, "at least one query exactly matches authoritative text", exact

    top_reliable = [
        candidates[0]
        for candidates in anchored_reliable_per_query
        if candidates
    ]
    if top_reliable:
        if len(top_reliable) == 1:
            candidate = top_reliable[0]
            if (
                candidate["coverage"] >= max(config["reliable_coverage"] + 0.12, 0.48)
                or candidate["matched_terms"] >= 6
                or (candidate["matched_terms"] >= 5 and candidate["matched_trigrams"] >= 2)
            ):
                return STATUS_HIT, "one query has a strong multi-anchor authoritative match", [candidate]
            return (
                STATUS_AMBIGUOUS,
                "only one query produced a moderate match; inspect the candidate or refine the alias",
                _deduplicate_candidates(top_reliable),
            )
        anchor = top_reliable[0]
        if all(_same_fact(anchor, candidate) for candidate in top_reliable[1:]):
            evidence = _deduplicate_candidates(
                candidate
                for candidates in anchored_reliable_per_query
                for candidate in candidates
            )
            return STATUS_HIT, "independent query variants converge on the same fact", evidence
        return (
            STATUS_AMBIGUOUS,
            "query variants point to different authoritative passages",
            _deduplicate_candidates(top_reliable),
        )

    mismatched = _deduplicate_candidates(
        candidate
        for candidates in reliable_per_query
        for candidate in candidates
    )
    if mismatched:
        return (
            STATUS_AMBIGUOUS,
            "retrieval candidates do not cover the atomic query's core anchors "
            "in the same evidence span",
            mismatched,
        )

    weak = _deduplicate_candidates(candidate for candidates in weak_per_query for candidate in candidates)
    if weak:
        return (
            STATUS_AMBIGUOUS,
            "only weak candidates were found; weak evidence cannot confirm a hit or a miss",
            weak,
        )
    if len(variants) < 2:
        return (
            STATUS_AMBIGUOUS,
            "one empty query is insufficient; provide at least one independent alias",
            [],
        )
    return (
        STATUS_MISS,
        "a healthy refreshed index returned no reliable or weak candidate for two or more variants",
        [],
    )


def query_project(
    project_root: Path | str,
    need: str,
    aliases: Sequence[str] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    variants: list[str] = []
    seen: set[str] = set()
    for value in (need, *(aliases or [])):
        cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
        normalized = _compact(cleaned)
        if cleaned and normalized and normalized not in seen:
            variants.append(cleaned)
            seen.add(normalized)
    if not variants:
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": "query need is empty",
            "project_root": str(root),
            "request_id": request_id,
            "queries": [],
            "evidence": [],
        }

    try:
        config = load_config(root)
        if not config["enabled"]:
            raise PlotRagError("plot RAG is disabled by project config")
        health = refresh_index(root, config)
        connection = _open_database(Path(config["index_path"]))
        try:
            rows = list(
                connection.execute(
                    "SELECT path, heading, start_line, end_line, text, search_text "
                    "FROM chunks ORDER BY path, ordinal"
                )
            )
        finally:
            connection.close()
        ranked = [_bm25_candidates(rows, variant, config) for variant in variants]
        status, reason, selected = _classify(variants, ranked, config)
        limit = config["candidate_limit"]
        result: dict[str, Any] = {
            "status": status,
            "reason": reason,
            "project_root": str(root),
            "request_id": request_id,
            "queries": variants,
            "index": health,
        }
        if status == STATUS_HIT:
            result["evidence"] = [_public_candidate(item, root) for item in selected[:limit]]
        elif status == STATUS_AMBIGUOUS:
            result["candidates"] = [_public_candidate(item, root) for item in selected[:limit]]
            result["evidence"] = []
        else:
            result["evidence"] = []
        return result
    except (PlotRagError, OSError, sqlite3.Error) as exc:
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": str(exc),
            "project_root": str(root),
            "request_id": request_id,
            "queries": variants,
            "evidence": [],
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Query project-local authoritative files through an independent SQLite index."
    )
    parser.add_argument("--project-root", help="Project root containing .plot-rag/config.json")
    parser.add_argument("--need", required=True, help="One atomic fact need")
    parser.add_argument(
        "--alias", action="append", default=[], help="Independent rephrasing; repeat as needed"
    )
    parser.add_argument("--request-id", help="Receipt identifier injected by the prompt hook")
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.project_root).expanduser().resolve() if args.project_root else locate_project_root()
    if root is None:
        result = {
            "status": STATUS_UNAVAILABLE,
            "reason": (
                "cannot locate a project; pass --project-root or create "
                f"{CONFIG_RELATIVE_PATH.as_posix()} / {POINTER_FILE}"
            ),
            "request_id": args.request_id,
            "queries": [args.need, *args.alias],
            "evidence": [],
        }
    else:
        result = query_project(root, args.need, args.alias, args.request_id)
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=None if args.compact else 2,
            sort_keys=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
