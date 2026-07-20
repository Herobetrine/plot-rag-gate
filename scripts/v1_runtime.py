#!/usr/bin/env python3
"""Integrated v1 runtime for the webnovel continuity engine.

The legacy ``state_rag`` module remains the compatibility surface for config
versions 1 and 2.  Config version 3 uses this module to keep generation-time
extraction proposal-only, bind every proposal to the canon revision observed
during prepare, and project accepted commits into the long-form retrieval
layers.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import weakref
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

if __package__:  # Package import, for example ``scripts.v1_runtime``.
    from . import state_rag
    from .continuity import (
        ContinuityError,
        ContinuityService,
        HostApprovalAuthority,
        ITEM_PROJECTION_META_HASH,
        SCHEMA_VERSION as CONTINUITY_SCHEMA_VERSION,
        SchemaVersionError,
        read_item_projection_metadata,
        validate_schema_versions,
    )
    from .continuity.advantages import (
        ADVANTAGE_META_HASH,
        read_advantage_projection_metadata,
    )
    from .continuity.power_spec import (
        PowerSpecImportError,
        preview_power_spec_import,
    )
    from .continuity.schema import STATE_DATABASE_TABLES
    from .continuity.source_manifest import (
        SOURCE_MANIFEST_ACCEPT_OPERATION,
        SOURCE_MANIFEST_PROPOSAL_KIND,
        current_manifest_snapshot,
        manifest_status as source_manifest_projection_status,
        normalize_source_path,
        preview_manifest_plan,
    )
    from .continuity.validators import (
        normalize_event,
        normalize_stage,
        validate_positive_int,
    )
    from .event_experience_runtime import verify_locked_manifest
    from .longform import (
        AUTHORITY_INDEX_SCHEMA_VERSION,
        AcceptedSummaryStore,
        AuthorityIndex,
        ContextContractBuilder,
        LayeredMemoryStore,
        ProjectPatternStore,
        ProjectionJournal,
        ProjectionRunError,
        WebnovelMethodPack,
        decompose_continuity_needs,
        load_annotation_manifest,
        run_annotation_benchmark,
        run_power_annotation_benchmark,
        validate_annotation_manifest,
        validate_power_annotation_manifest,
    )
    from .longform.authority import AUTHORITY_INDEX_TABLES
    from .longform.memory import (
        LONGFORM_SHARED_TABLES,
        MEMORY_SCHEMA_VERSION,
        SUMMARY_SCHEMA_VERSION,
    )
    from .longform.methods import (
        CRAFT_MEMORY_SCHEMA_VERSION,
        METHOD_PACK_SCHEMA_VERSION,
    )
    from .longform.projections import PROJECTION_SCHEMA_VERSION, PROJECTION_TABLES
    from .plot_init import (
        PlotInitError,
        PlotInitService,
        proposal_to_lifecycle_package,
    )
    from .plot_init.constants import (
        DATABASE_SCHEMA_VERSION as INIT_DB_SCHEMA_VERSION,
    )
    from .plot_init.remote_cache import REMOTE_CACHE_SHARED_TABLES
    from .plot_rag import (
        DEFAULT_AUTHORITY_INDEX_V1_PATH,
        DEFAULT_LONGFORM_V1_PATH,
        DEFAULT_PROJECTION_RUNS_V1_PATH,
        STATE_CATEGORIES,
        load_config,
    )
else:  # Direct CLI/runtime import with ``scripts`` on sys.path.
    import state_rag
    from continuity import (
        ContinuityError,
        ContinuityService,
        HostApprovalAuthority,
        ITEM_PROJECTION_META_HASH,
        SCHEMA_VERSION as CONTINUITY_SCHEMA_VERSION,
        SchemaVersionError,
        read_item_projection_metadata,
        validate_schema_versions,
    )
    from continuity.advantages import (
        ADVANTAGE_META_HASH,
        read_advantage_projection_metadata,
    )
    from continuity.power_spec import (
        PowerSpecImportError,
        preview_power_spec_import,
    )
    from continuity.schema import STATE_DATABASE_TABLES
    from continuity.source_manifest import (
        SOURCE_MANIFEST_ACCEPT_OPERATION,
        SOURCE_MANIFEST_PROPOSAL_KIND,
        current_manifest_snapshot,
        manifest_status as source_manifest_projection_status,
        normalize_source_path,
        preview_manifest_plan,
    )
    from continuity.validators import (
        normalize_event,
        normalize_stage,
        validate_positive_int,
    )
    from event_experience_runtime import verify_locked_manifest
    from longform import (
        AUTHORITY_INDEX_SCHEMA_VERSION,
        AcceptedSummaryStore,
        AuthorityIndex,
        ContextContractBuilder,
        LayeredMemoryStore,
        ProjectPatternStore,
        ProjectionJournal,
        ProjectionRunError,
        WebnovelMethodPack,
        decompose_continuity_needs,
        load_annotation_manifest,
        run_annotation_benchmark,
        run_power_annotation_benchmark,
        validate_annotation_manifest,
        validate_power_annotation_manifest,
    )
    from longform.authority import AUTHORITY_INDEX_TABLES
    from longform.memory import (
        LONGFORM_SHARED_TABLES,
        MEMORY_SCHEMA_VERSION,
        SUMMARY_SCHEMA_VERSION,
    )
    from longform.methods import (
        CRAFT_MEMORY_SCHEMA_VERSION,
        METHOD_PACK_SCHEMA_VERSION,
    )
    from longform.projections import PROJECTION_SCHEMA_VERSION, PROJECTION_TABLES
    from plot_init import (
        PlotInitError,
        PlotInitService,
        proposal_to_lifecycle_package,
    )
    from plot_init.constants import (
        DATABASE_SCHEMA_VERSION as INIT_DB_SCHEMA_VERSION,
    )
    from plot_init.remote_cache import REMOTE_CACHE_SHARED_TABLES
    from plot_rag import (
        DEFAULT_AUTHORITY_INDEX_V1_PATH,
        DEFAULT_LONGFORM_V1_PATH,
        DEFAULT_PROJECTION_RUNS_V1_PATH,
        STATE_CATEGORIES,
        load_config,
    )


RUNTIME_VERSION = 1
STRICT_CONFIG_VERSION = 3
_CHAPTER_RE = re.compile(r"第\s*([0-9一二三四五六七八九十百千]+)\s*章")
_SCENE_RE = re.compile(r"第\s*([0-9一二三四五六七八九十百千]+)\s*(?:场|幕)")
_INIT_PROPOSAL_PREFIX = "initp-"
_PREPARE_IDENTITY_MAX_ATTEMPTS = 3
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_STABLE_HASH_RE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9_]*_)?[0-9a-f]{64}$"
)
_LIFECYCLE_IDENTITY_FIELDS = frozenset(
    {
        "intent_contract_hash",
        "event_seed_manifest_hash",
        "experience_contract_hashes",
        "event_experience_control_revision",
        "event_seed_references",
    }
)
_EXTRACTION_PROPOSAL_BINDING_FIELDS = frozenset(
    {
        "extraction_job_id",
        "job_binding_hash",
        "receipt_id",
        "request_id",
        "assistant_sha256",
        "prompt_hash",
        "retrieved_context_digest",
        "prepared_canon_revision",
        "active_projection_hash",
        "intent_contract_hash",
        "event_seed_manifest_hash",
        "event_experience_control_revision",
        "event_seed_references",
        "experience_contract_hashes",
        "artifact_context",
    }
)
_EXACT_STATE_CACHE_SCHEMA_VERSION = 1
_EXACT_STATE_CACHE_ALGORITHM_VERSION = 1
_EXACT_STATE_CACHE_FILENAME = "exact-state-cache.v1.sqlite3"
_EXACT_STATE_EVIDENCE_MARKERS = (
    "原文",
    "逐字",
    "出处",
    "证据",
    "引用",
    "哪一章",
    "原句",
    "来源",
    "source",
    "quote",
    "citation",
    "evidence",
    "verbatim",
    "chapter",
)
_EXACT_STATE_UNRESOLVED_MARKERS = frozenset(
    {
        "unknown",
        "unresolved",
        "ambiguous",
        "conflicted",
        "conflict",
        "deferred",
        "pending",
        "expired",
        "待定",
        "未知",
        "冲突",
        "待裁决",
        "待确认",
        "已过期",
    }
)
_EXACT_STATE_CATEGORY_FACT_TYPES: Mapping[str, frozenset[str]] = {
    "current_state": frozenset(
        {
            "fact",
            "state",
            "world_rule",
            "time",
            "location",
            "inventory",
            "relation",
            "status",
        }
    ),
    "location": frozenset({"location"}),
    "inventory": frozenset({"inventory"}),
    "relationship": frozenset({"relation"}),
    "story_time": frozenset({"time"}),
    "open_loop": frozenset({"open_loop"}),
}
_EXACT_STATE_REQUIRED_COUNTS: Mapping[str, int] = {
    "current_state": 2,
    "open_loop": 2,
    "power_state": 2,
    "progression": 1,
    "ability": 1,
    "resource": 1,
    "power_binding": 1,
    "location": 1,
    "inventory": 1,
    "relationship": 1,
    "story_time": 1,
}
_POWER_NEED_SECTIONS: Mapping[str, tuple[str, ...]] = {
    "power_state": (
        "progression",
        "resources",
        "abilities",
        "statuses",
        "bindings",
        "qualifications",
    ),
    "progression": ("progression",),
    "ability": ("abilities",),
    "resource": ("resources",),
    "power_binding": ("bindings", "qualifications"),
}
_ITEM_CONTEXT_TRIGGER_MARKERS = (
    "战斗",
    "交战",
    "决斗",
    "袭击",
    "追杀",
    "解谜",
    "谜题",
    "机关",
    "逃生",
    "脱困",
    "逃亡",
    "追逐",
    "生产",
    "制造",
    "炼制",
    "锻造",
    "治疗",
    "疗伤",
    "救治",
    "交易",
    "买卖",
    "交换",
    "拍卖",
    "权限",
    "门禁",
    "通行",
    "授权",
    "身份",
    "证据",
    "证物",
    "线索",
    "物品",
    "道具",
    "装备",
    "法器",
    "武器",
    "库存",
    "持有",
    "携带",
    "使用",
    "消耗",
    "拾取",
    "combat",
    "battle",
    "puzzle",
    "escape",
    "craft",
    "produce",
    "heal",
    "trade",
    "permission",
    "evidence",
    "item",
    "inventory",
    "equipment",
    "weapon",
)
_ITEM_CONTEXT_MAX_SUBJECTS = 8
_ITEM_CONTEXT_MAX_FUNCTIONS_PER_SUBJECT = 6
_ITEM_CONTEXT_HISTORY_LIMIT = 8
_ADVANTAGE_SPECIAL_TERM_MARKERS = (
    "系统",
    "面板",
    "任务",
    "签到",
    "空间",
    "炉",
    "戒",
    "塔",
    "书",
    "器灵",
    "血脉",
    "重生",
    "回溯",
    "模拟",
    "复制",
    "契约",
    "演算点",
    "金手指",
    "外挂",
    "system",
    "panel",
    "quest",
    "sign-in",
    "relic",
    "bloodline",
    "rebirth",
    "rewind",
    "simulator",
    "contract",
    "advantage",
)
_ADVANTAGE_ACTION_MARKERS = (
    "获得",
    "认主",
    "激活",
    "鉴定",
    "抽取",
    "炼制",
    "兑换",
    "升级",
    "觉醒",
    "召唤",
    "回溯",
    "揭示",
    "启用",
    "使用",
    "触发",
    "奖励",
    "消耗",
    "解锁",
    "暴露",
    "acquire",
    "bind",
    "activate",
    "appraise",
    "extract",
    "refine",
    "exchange",
    "upgrade",
    "awaken",
    "summon",
    "rewind",
    "reveal",
    "trigger",
    "reward",
    "consume",
    "unlock",
    "expose",
)
_ADVANTAGE_CONTINUITY_MARKERS: Mapping[str, tuple[str, ...]] = {
    "ability": ("能力", "技能", "法术", "术式", "天赋", "ability", "skill"),
    "resource": ("资源", "能量", "演算点", "点数", "货币", "resource", "energy"),
    "knowledge": ("认知", "知识", "情报", "秘密", "已知", "knowledge", "secret"),
    "location": ("位置", "地点", "空间", "领域", "location", "position"),
    "inventory": ("持有物", "物品", "道具", "装备", "库存", "item", "inventory"),
    "relationship": ("关系", "契约", "信任", "债务", "relationship", "trust", "debt"),
    "story_time": ("故事时间", "时间", "时刻", "日期", "story time", "timeline"),
}
_ADVANTAGE_CONTINUITY_CHANGE_MARKERS = (
    "变化",
    "改变",
    "更新",
    "增加",
    "减少",
    "获得",
    "失去",
    "转移",
    "移动",
    "进入",
    "离开",
    "消耗",
    "恢复",
    "揭示",
    "暴露",
    "触发",
    "change",
    "update",
    "gain",
    "lose",
    "transfer",
    "move",
    "consume",
    "recover",
    "reveal",
    "expose",
)
_ADVANTAGE_ID_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?P<kind>advantage_id|module_id)\b\s*[:=]\s*"
    r"[\"']?(?P<value>[A-Za-z][A-Za-z0-9_.:-]{2,127})"
)
_ADVANTAGE_ID_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<value>(?:advantage|adv|module|mod)[_:-][A-Za-z0-9_.:-]{2,127})"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_ADVANTAGE_MAX_RECORDS = 4
_ADVANTAGE_MAX_MODULES_PER_RECORD = 8
_ADVANTAGE_LEDGER_LIMIT = 10
_ADVANTAGE_PROJECTION_HASH_RE = re.compile(
    r"^(?:advantage_projection_)?[0-9a-f]{64}$",
    re.IGNORECASE,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lifecycle_identity_error(message: str) -> ContinuityError:
    return ContinuityError(
        "PREPARED_LIFECYCLE_IDENTITY_INVALID",
        message,
    )


def _lifecycle_sha256(value: Any, field: str) -> str:
    text = str(value or "")
    if not _SHA256_HEX_RE.fullmatch(text):
        raise _lifecycle_identity_error(
            f"{field} must be a lowercase SHA-256 hex digest"
        )
    return text


def _normalize_lifecycle_identity(
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _lifecycle_identity_error(
            "lifecycle_identity must be a JSON object"
        )
    raw = dict(value)
    if not raw:
        return {}
    if any(not isinstance(key, str) for key in raw):
        raise _lifecycle_identity_error(
            "lifecycle_identity field names must be strings"
        )
    unknown = sorted(set(raw) - _LIFECYCLE_IDENTITY_FIELDS)
    missing = sorted(_LIFECYCLE_IDENTITY_FIELDS - set(raw))
    if unknown or missing:
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise _lifecycle_identity_error(
            "lifecycle_identity fields are invalid: " + "; ".join(details)
        )

    revision = raw["event_experience_control_revision"]
    if type(revision) is not int or revision < 1:
        raise _lifecycle_identity_error(
            "event_experience_control_revision must be an integer >= 1"
        )

    contract_values = raw["experience_contract_hashes"]
    if (
        not isinstance(contract_values, Sequence)
        or isinstance(contract_values, (str, bytes, bytearray))
        or not contract_values
    ):
        raise _lifecycle_identity_error(
            "experience_contract_hashes must be a non-empty array"
        )
    contract_hashes = sorted(
        {
            _lifecycle_sha256(item, "experience_contract_hashes[]")
            for item in contract_values
        }
    )

    reference_values = raw["event_seed_references"]
    if (
        not isinstance(reference_values, Sequence)
        or isinstance(reference_values, (str, bytes, bytearray))
        or not reference_values
    ):
        raise _lifecycle_identity_error(
            "event_seed_references must be a non-empty array"
        )
    references_by_id: dict[str, int] = {}
    for index, item in enumerate(reference_values):
        if not isinstance(item, Mapping):
            raise _lifecycle_identity_error(
                f"event_seed_references[{index}] must be an object"
            )
        item_raw = dict(item)
        if set(item_raw) != {"event_seed_id", "event_seed_revision"}:
            raise _lifecycle_identity_error(
                "event_seed_references entries must contain exactly "
                "event_seed_id and event_seed_revision"
            )
        seed_id = str(item_raw.get("event_seed_id") or "").strip()
        seed_revision = item_raw.get("event_seed_revision")
        if not seed_id or len(seed_id) > 256:
            raise _lifecycle_identity_error(
                f"event_seed_references[{index}].event_seed_id is invalid"
            )
        if type(seed_revision) is not int or seed_revision < 1:
            raise _lifecycle_identity_error(
                f"event_seed_references[{index}].event_seed_revision "
                "must be an integer >= 1"
            )
        existing_revision = references_by_id.get(seed_id)
        if (
            existing_revision is not None
            and existing_revision != seed_revision
        ):
            raise _lifecycle_identity_error(
                f"event seed {seed_id} has conflicting revisions"
            )
        references_by_id[seed_id] = seed_revision
    references = [
        {
            "event_seed_id": seed_id,
            "event_seed_revision": references_by_id[seed_id],
        }
        for seed_id in sorted(
            references_by_id,
            key=lambda item: (item, references_by_id[item]),
        )
    ]
    return {
        "intent_contract_hash": _lifecycle_sha256(
            raw["intent_contract_hash"],
            "intent_contract_hash",
        ),
        "event_seed_manifest_hash": _lifecycle_sha256(
            raw["event_seed_manifest_hash"],
            "event_seed_manifest_hash",
        ),
        "experience_contract_hashes": contract_hashes,
        "event_experience_control_revision": revision,
        "event_seed_references": references,
    }


def _decode_lifecycle_identity(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return _normalize_lifecycle_identity(value)
    if value is None or value == "":
        return {}
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _lifecycle_identity_error(
            "persisted lifecycle_identity_json is not valid JSON"
        ) from exc
    if not isinstance(decoded, Mapping):
        raise _lifecycle_identity_error(
            "persisted lifecycle_identity_json must be a JSON object"
        )
    return _normalize_lifecycle_identity(decoded)


def _proposal_binding_error(message: str) -> ContinuityError:
    return ContinuityError(
        "EXTRACTION_PROPOSAL_BINDING_INVALID",
        message,
    )


def _normalize_binding_artifact_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _proposal_binding_error(
            "proposal_binding.artifact_context must be a JSON object"
        )
    try:
        artifact = json.loads(_canonical_json(dict(value)))
    except (TypeError, ValueError) as exc:
        raise _proposal_binding_error(
            "proposal_binding.artifact_context must be canonical JSON"
        ) from exc
    for field in ("artifact_id", "artifact_stage", "branch_id"):
        raw_value = artifact.get(field)
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise _proposal_binding_error(
                f"proposal_binding.artifact_context.{field} is invalid"
            )
        artifact[field] = raw_value.strip()
    for field, minimum in (
        ("chapter_no", 1),
        ("scene_index", 0),
    ):
        raw_value = artifact.get(field)
        if raw_value is None:
            continue
        if type(raw_value) is not int or raw_value < minimum:
            raise _proposal_binding_error(
                f"proposal_binding.artifact_context.{field} is invalid"
            )
    revision = artifact.get("artifact_revision", 1)
    if type(revision) is not int or revision < 1:
        raise _proposal_binding_error(
            "proposal_binding.artifact_context.artifact_revision is invalid"
        )
    artifact["artifact_revision"] = revision
    return artifact


def _normalize_extraction_proposal_binding(
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _proposal_binding_error(
            "proposal_binding must be a JSON object"
        )
    raw = dict(value)
    if not raw:
        raise _proposal_binding_error(
            "proposal_binding must contain the complete reverse binding"
        )
    if any(not isinstance(key, str) for key in raw):
        raise _proposal_binding_error(
            "proposal_binding field names must be strings"
        )
    unknown = sorted(set(raw) - _EXTRACTION_PROPOSAL_BINDING_FIELDS)
    missing = sorted(_EXTRACTION_PROPOSAL_BINDING_FIELDS - set(raw))
    if unknown or missing:
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise _proposal_binding_error(
            "proposal_binding fields are invalid: " + "; ".join(details)
        )

    def required_text(field: str, *, maximum: int = 512) -> str:
        raw_value = raw[field]
        if not isinstance(raw_value, str):
            raise _proposal_binding_error(
                f"proposal_binding.{field} must be a string"
            )
        text = raw_value.strip()
        if not text or len(text) > maximum:
            raise _proposal_binding_error(
                f"proposal_binding.{field} is invalid"
            )
        return text

    def required_sha256(field: str) -> str:
        text = required_text(field, maximum=64)
        if not _SHA256_HEX_RE.fullmatch(text):
            raise _proposal_binding_error(
                f"proposal_binding.{field} must be a lowercase SHA-256 digest"
            )
        return text

    def required_stable_hash(field: str) -> str:
        text = required_text(field, maximum=256)
        if not _STABLE_HASH_RE.fullmatch(text):
            raise _proposal_binding_error(
                f"proposal_binding.{field} must be a stable SHA-256 hash"
            )
        return text

    prepared_revision = raw["prepared_canon_revision"]
    if type(prepared_revision) is not int or prepared_revision < 0:
        raise _proposal_binding_error(
            "proposal_binding.prepared_canon_revision must be an integer >= 0"
        )

    lifecycle_candidate = {
        "intent_contract_hash": raw["intent_contract_hash"],
        "event_seed_manifest_hash": raw["event_seed_manifest_hash"],
        "experience_contract_hashes": raw["experience_contract_hashes"],
        "event_experience_control_revision": raw[
            "event_experience_control_revision"
        ],
        "event_seed_references": raw["event_seed_references"],
    }
    lifecycle_present = any(
        (
            lifecycle_candidate["intent_contract_hash"],
            lifecycle_candidate["event_seed_manifest_hash"],
            lifecycle_candidate["experience_contract_hashes"],
            lifecycle_candidate["event_experience_control_revision"],
            lifecycle_candidate["event_seed_references"],
        )
    )
    if lifecycle_present:
        try:
            lifecycle_identity = _normalize_lifecycle_identity(
                lifecycle_candidate
            )
        except ContinuityError as exc:
            raise _proposal_binding_error(exc.message) from exc
    else:
        if (
            lifecycle_candidate["intent_contract_hash"] != ""
            or lifecycle_candidate["event_seed_manifest_hash"] != ""
            or lifecycle_candidate["experience_contract_hashes"] != []
            or lifecycle_candidate["event_experience_control_revision"] != 0
            or lifecycle_candidate["event_seed_references"] != []
        ):
            raise _proposal_binding_error(
                "proposal_binding lifecycle fields must be all empty or complete"
            )
        lifecycle_identity = {}

    return {
        "extraction_job_id": required_text(
            "extraction_job_id",
            maximum=256,
        ),
        "job_binding_hash": required_sha256("job_binding_hash"),
        "receipt_id": required_text("receipt_id", maximum=512),
        "request_id": required_text("request_id", maximum=512),
        "assistant_sha256": required_sha256("assistant_sha256"),
        "prompt_hash": required_sha256("prompt_hash"),
        "retrieved_context_digest": required_sha256(
            "retrieved_context_digest"
        ),
        "prepared_canon_revision": prepared_revision,
        "active_projection_hash": required_stable_hash(
            "active_projection_hash"
        ),
        "intent_contract_hash": str(
            lifecycle_identity.get("intent_contract_hash") or ""
        ),
        "event_seed_manifest_hash": str(
            lifecycle_identity.get("event_seed_manifest_hash") or ""
        ),
        "event_experience_control_revision": int(
            lifecycle_identity.get(
                "event_experience_control_revision",
                0,
            )
        ),
        "event_seed_references": list(
            lifecycle_identity.get("event_seed_references") or []
        ),
        "experience_contract_hashes": list(
            lifecycle_identity.get("experience_contract_hashes") or []
        ),
        "artifact_context": _normalize_binding_artifact_context(
            raw["artifact_context"]
        ),
    }


def _validate_extraction_proposal_binding(
    value: Mapping[str, Any] | None,
    *,
    turn: Mapping[str, Any],
    assistant_sha256: str,
    prepared_identity: Mapping[str, Any],
) -> dict[str, Any]:
    if not value:
        return {}
    normalized = _normalize_extraction_proposal_binding(value)
    lifecycle_identity = dict(
        prepared_identity.get("lifecycle_identity") or {}
    )
    prepared_artifact_raw = _json_load(
        turn.get("v1_context_json"),
        {},
    )
    if not isinstance(prepared_artifact_raw, Mapping):
        prepared_artifact_raw = {}
    prepared_artifact = _normalize_binding_artifact_context(
        prepared_artifact_raw
    )
    actual_artifact = dict(normalized["artifact_context"])
    expected = {
        "receipt_id": str(turn["receipt_id"]),
        "request_id": str(turn["request_id"]),
        "assistant_sha256": assistant_sha256,
        "prompt_hash": str(prepared_identity["prompt_hash"]),
        "retrieved_context_digest": str(
            prepared_identity["retrieved_context_digest"]
        ),
        "prepared_canon_revision": int(
            prepared_identity["prepared_canon_revision"]
        ),
        "active_projection_hash": str(
            prepared_identity["active_projection_hash"]
        ),
        "intent_contract_hash": str(
            lifecycle_identity.get("intent_contract_hash") or ""
        ),
        "event_seed_manifest_hash": str(
            lifecycle_identity.get("event_seed_manifest_hash") or ""
        ),
        "event_experience_control_revision": int(
            lifecycle_identity.get(
                "event_experience_control_revision",
                0,
            )
        ),
        "event_seed_references": list(
            lifecycle_identity.get("event_seed_references") or []
        ),
        "experience_contract_hashes": list(
            lifecycle_identity.get("experience_contract_hashes") or []
        ),
    }
    mismatches = {
        field: {
            "expected": expected_value,
            "actual": normalized.get(field),
        }
        for field, expected_value in expected.items()
        if normalized.get(field) != expected_value
    }
    artifact_mismatches = {
        field: {
            "expected": expected_value,
            "actual": actual_artifact.get(field),
        }
        for field, expected_value in prepared_artifact.items()
        if field != "artifact_revision"
        and actual_artifact.get(field) != expected_value
    }
    expected_revision = int(prepared_artifact.get("artifact_revision") or 1)
    if (
        "artifact_revision" in prepared_artifact_raw
        and int(actual_artifact.get("artifact_revision") or 0)
        != expected_revision
    ):
        artifact_mismatches["artifact_revision"] = {
            "expected": expected_revision,
            "actual": actual_artifact.get("artifact_revision"),
        }
    if artifact_mismatches:
        mismatches["artifact_context"] = artifact_mismatches
    if mismatches:
        raise ContinuityError(
            "EXTRACTION_PROPOSAL_BINDING_MISMATCH",
            "proposal_binding differs from the prepared turn identity",
            details=mismatches,
        )
    return normalized


def _validate_shadow_authoritative_proposal(
    service: ContinuityService,
    proposal_id: str,
    *,
    turn: Mapping[str, Any],
    assistant_sha256: str,
    prepared_identity: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(proposal_id, str):
        raise ContinuityError(
            "EXTRACTION_SHADOW_AUTHORITATIVE_INVALID",
            "authoritative_proposal_id must be a string",
        )
    normalized_id = proposal_id.strip()
    if not normalized_id or not _STABLE_HASH_RE.fullmatch(normalized_id):
        raise ContinuityError(
            "EXTRACTION_SHADOW_AUTHORITATIVE_INVALID",
            "authoritative_proposal_id must be a stable proposal hash",
        )
    with service.store.read_connection() as connection:
        row = connection.execute(
            """
            SELECT proposal_id, canon_status, payload_json, events_json
            FROM proposals
            WHERE proposal_id=?
            """,
            (normalized_id,),
        ).fetchone()
    if row is None:
        raise ContinuityError(
            "EXTRACTION_SHADOW_AUTHORITATIVE_INVALID",
            "authoritative proposal does not exist",
            details={"authoritative_proposal_id": normalized_id},
        )
    payload = _json_load(row["payload_json"], {})
    events = _json_load(row["events_json"], [])
    if not isinstance(payload, Mapping) or not isinstance(events, list):
        raise ContinuityError(
            "EXTRACTION_SHADOW_AUTHORITATIVE_INVALID",
            "authoritative proposal payload or events are invalid",
            details={"authoritative_proposal_id": normalized_id},
        )
    payload = dict(payload)
    if payload.get("extraction_shadow"):
        raise ContinuityError(
            "EXTRACTION_SHADOW_AUTHORITATIVE_INVALID",
            "a shadow proposal cannot be used as the authoritative result",
            details={"authoritative_proposal_id": normalized_id},
        )
    lifecycle_identity = dict(
        prepared_identity.get("lifecycle_identity") or {}
    )
    expected = {
        "receipt_id": str(turn["receipt_id"]),
        "request_id": str(turn["request_id"]),
        "assistant_sha256": assistant_sha256,
        "prompt_hash": str(prepared_identity["prompt_hash"]),
        "retrieved_context_digest": str(
            prepared_identity["retrieved_context_digest"]
        ),
        "prepared_canon_revision": int(
            prepared_identity["prepared_canon_revision"]
        ),
        "active_projection_hash": str(
            prepared_identity["active_projection_hash"]
        ),
        "lifecycle_identity": lifecycle_identity,
    }
    actual = {
        "receipt_id": str(payload.get("receipt_id") or ""),
        "request_id": str(payload.get("request_id") or ""),
        "assistant_sha256": str(
            payload.get("assistant_sha256") or ""
        ),
        "prompt_hash": str(payload.get("prompt_hash") or ""),
        "retrieved_context_digest": str(
            payload.get("retrieved_context_digest") or ""
        ),
        "prepared_canon_revision": payload.get(
            "prepared_canon_revision"
        ),
        "active_projection_hash": str(
            payload.get("active_projection_hash") or ""
        ),
        "lifecycle_identity": _normalize_lifecycle_identity(
            payload.get("lifecycle_identity")
        ),
    }
    mismatches = {
        field: {
            "expected": expected_value,
            "actual": actual.get(field),
        }
        for field, expected_value in expected.items()
        if actual.get(field) != expected_value
    }
    if mismatches:
        raise ContinuityError(
            "EXTRACTION_SHADOW_AUTHORITATIVE_MISMATCH",
            "authoritative proposal differs from the prepared turn",
            details=mismatches,
        )
    return {
        "proposal_id": normalized_id,
        "canon_status": str(row["canon_status"]),
        "payload": payload,
        "events": events,
        "events_sha256": _sha256(_canonical_json(events)),
    }


def _event_experience_required(root: Path) -> bool:
    config = load_config(root)
    settings = config.get("event_experience") or {}
    return bool(
        int(config.get("config_version") or config.get("version") or 1)
        >= STRICT_CONFIG_VERSION
        and isinstance(settings, Mapping)
        and settings.get("enabled") is True
        and settings.get("required_before_event_design") is True
    )


def _verify_lifecycle_identity(
    root: Path,
    lifecycle_identity: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_lifecycle_identity(lifecycle_identity)
    if not normalized:
        return {}
    try:
        verified = verify_locked_manifest(
            root,
            seed_references=normalized["event_seed_references"],
            expected_event_seed_manifest_hash=normalized[
                "event_seed_manifest_hash"
            ],
            expected_control_revision=normalized[
                "event_experience_control_revision"
            ],
        )
    except Exception as exc:
        raise ContinuityError(
            "PREPARED_LIFECYCLE_IDENTITY_STALE",
            f"event-experience manifest validation failed: {exc}",
        ) from exc
    if not isinstance(verified, Mapping):
        raise ContinuityError(
            "PREPARED_LIFECYCLE_IDENTITY_STALE",
            "event-experience manifest validation returned an invalid payload",
        )
    verified_payload = dict(verified)
    manifest = verified_payload.get("manifest", verified_payload)
    if not isinstance(manifest, Mapping):
        raise ContinuityError(
            "PREPARED_LIFECYCLE_IDENTITY_STALE",
            "event-experience verifier returned an invalid manifest",
        )
    manifest_payload = dict(manifest)
    intent_hash = str(
        manifest_payload.get("source_intent_contract_hash") or ""
    )
    if intent_hash != normalized["intent_contract_hash"]:
        raise ContinuityError(
            "PREPARED_LIFECYCLE_IDENTITY_STALE",
            "event-experience manifest intent contract changed",
        )
    contracts = manifest_payload.get("contracts")
    if not isinstance(contracts, Sequence) or isinstance(
        contracts, (str, bytes, bytearray)
    ):
        raise ContinuityError(
            "PREPARED_LIFECYCLE_IDENTITY_STALE",
            "event-experience manifest contracts are invalid",
        )
    actual_contract_hashes = sorted(
        {
            str(item.get("contract_hash") or "")
            for item in contracts
            if isinstance(item, Mapping)
        }
    )
    if actual_contract_hashes != normalized["experience_contract_hashes"]:
        raise ContinuityError(
            "PREPARED_LIFECYCLE_IDENTITY_STALE",
            "event-experience contract hashes changed",
        )
    return manifest_payload


def _json_load(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in {None, ""}:
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _zh_number(value: str) -> int | None:
    text = value.strip()
    if text.isdigit():
        number = int(text)
        return number if number > 0 else None
    digits = {
        "零": 0,
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1000}
    total = 0
    current = 0
    for char in text:
        if char in digits:
            current = digits[char]
        elif char in units:
            unit = units[char]
            total += (current or 1) * unit
            current = 0
        else:
            return None
    number = total + current
    return number if number > 0 else None


def is_strict_lifecycle(project_root: Path | str) -> bool:
    config = load_config(project_root)
    lifecycle = config.get("lifecycle") or {}
    return bool(
        int(config.get("config_version") or config.get("version") or 1)
        >= STRICT_CONFIG_VERSION
        and lifecycle.get("strict", True)
    )


def infer_artifact_context(
    prompt: str,
    *,
    artifact_stage: str | None = None,
    branch_id: str | None = None,
    chapter_no: int | None = None,
    scene_index: int | None = None,
    artifact_id: str | None = None,
    task: str | None = None,
) -> dict[str, Any]:
    """Fail-closed artifact classification for hook, CLI, and MCP callers."""

    text = str(prompt or "")
    compact = re.sub(r"\s+", "", text)
    stage = str(artifact_stage or "").strip().casefold()
    allowed = {"bootstrap", "brainstorm", "outline", "draft", "final", "published"}
    if stage not in allowed:
        if re.search(r"(?:已发布|发布版|正式发布|published)", compact, re.IGNORECASE):
            stage = "published"
        elif re.search(r"(?:终稿|定稿|最终稿|final)", compact, re.IGNORECASE):
            stage = "final"
        elif re.search(r"(?:章纲|卷纲|总纲|大纲|事件链|outline)", compact, re.IGNORECASE):
            stage = "outline"
        elif re.search(r"(?:正文|续写|写下一章|写第.+章|草稿|draft)", compact, re.IGNORECASE):
            stage = "draft"
        else:
            stage = "brainstorm"

    chapter = chapter_no
    if chapter is None:
        match = _CHAPTER_RE.search(text)
        if match:
            chapter = _zh_number(match.group(1))
    scene = scene_index
    if scene is None:
        match = _SCENE_RE.search(text)
        if match:
            parsed = _zh_number(match.group(1))
            scene = None if parsed is None else max(0, parsed - 1)

    selected_task = str(task or "").strip().casefold()
    if selected_task not in {"outline", "scene", "prose", "revision"}:
        if stage == "outline":
            selected_task = "outline"
        elif re.search(r"(?:审查|检查|修订|修改|重写|润色)", compact):
            selected_task = "revision"
        elif re.search(r"(?:场景|分场|对话)", compact):
            selected_task = "scene"
        else:
            selected_task = "prose"

    branch = str(branch_id or "").strip() or (
        "alternative"
        if re.search(r"(?:备选|方案|假设|如果|另一条线|what-if)", compact, re.IGNORECASE)
        else "main"
    )
    resolved_artifact_id = str(artifact_id or "").strip() or (
        "turn-" + _sha256(f"{branch}\n{chapter}\n{scene}\n{text}")[:24]
    )
    return {
        "artifact_stage": stage,
        "branch_id": branch,
        "chapter_no": chapter,
        "scene_index": scene,
        "artifact_id": resolved_artifact_id,
        "task": selected_task,
    }


def _ensure_turn_v1_columns(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(turns)")
    }
    if "prepared_canon_revision" not in columns:
        connection.execute(
            "ALTER TABLE turns "
            "ADD COLUMN prepared_canon_revision INTEGER NOT NULL DEFAULT 0"
        )
    if "v1_context_json" not in columns:
        connection.execute(
            "ALTER TABLE turns ADD COLUMN v1_context_json TEXT NOT NULL DEFAULT '{}'"
        )
    if "active_projection_hash" not in columns:
        connection.execute(
            "ALTER TABLE turns "
            "ADD COLUMN active_projection_hash TEXT NOT NULL DEFAULT ''"
        )
    if "retrieved_context_digest" not in columns:
        connection.execute(
            "ALTER TABLE turns "
            "ADD COLUMN retrieved_context_digest TEXT NOT NULL DEFAULT ''"
        )
    if "prepared_context_text" not in columns:
        connection.execute(
            "ALTER TABLE turns "
            "ADD COLUMN prepared_context_text TEXT NOT NULL DEFAULT ''"
        )
    if "prepare_telemetry_json" not in columns:
        connection.execute(
            "ALTER TABLE turns "
            "ADD COLUMN prepare_telemetry_json TEXT NOT NULL DEFAULT '{}'"
        )
    if "lifecycle_identity_json" not in columns:
        connection.execute(
            "ALTER TABLE turns "
            "ADD COLUMN lifecycle_identity_json TEXT NOT NULL DEFAULT '{}'"
        )


def _active_continuity_identity(
    service: ContinuityService,
) -> dict[str, Any]:
    """Read canon revisions and their completed projection in one snapshot."""

    for _attempt in range(2):
        with service.store.read_connection() as connection:
            connection.execute("BEGIN")
            head_revision = service.store.get_meta_int(
                connection,
                "head_canon_revision",
            )
            active_revision = service.store.get_meta_int(
                connection,
                "active_canon_revision",
            )
            row = connection.execute(
                """
                SELECT projection_hash
                FROM projection_runs
                WHERE projection_name='continuity'
                  AND run_status='completed'
                  AND source_active_revision=?
                ORDER BY created_at DESC, run_id DESC
                LIMIT 1
                """,
                (active_revision,),
            ).fetchone()
            connection.rollback()
        if row is not None:
            return {
                "head_canon_revision": int(head_revision),
                "active_canon_revision": int(active_revision),
                "active_projection_hash": str(row["projection_hash"]),
            }
        # A fresh or migrated store may not yet have a completed projection.
        # Replay establishes the projection, then the next loop reads the
        # revision and hash atomically.
        service.replay()
    raise ContinuityError(
        "ACTIVE_PROJECTION_UNAVAILABLE",
        "active continuity projection is unavailable",
    )


def _prepared_context_digest(
    row: Mapping[str, Any],
    prepared_context_text: str,
    artifact_context: Mapping[str, Any],
    lifecycle_identity: Mapping[str, Any] | None = None,
) -> str:
    normalized_lifecycle_identity = (
        _normalize_lifecycle_identity(lifecycle_identity)
        if lifecycle_identity is not None
        else _decode_lifecycle_identity(
            row.get("lifecycle_identity_json", "{}")
        )
    )
    return _sha256(
        _canonical_json(
            {
                "prompt_hash": str(row.get("prompt_hash") or ""),
                "retrieved": _json_load(row.get("retrieved_json"), []),
                "authority": _json_load(row.get("authority_json"), {}),
                "craft": _json_load(row.get("craft_json"), {}),
                "artifact_context": dict(artifact_context),
                "prepared_context": str(prepared_context_text or ""),
                "lifecycle_identity": normalized_lifecycle_identity,
            }
        )
    )


def _bind_prepared_context(
    service: ContinuityService,
    request_id: str,
    identity: Mapping[str, Any],
    artifact_context: Mapping[str, Any],
    prepared_context_text: str,
    telemetry: Mapping[str, Any],
    lifecycle_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_lifecycle_identity = _normalize_lifecycle_identity(
        lifecycle_identity
    )
    with service.store.transaction() as connection:
        _ensure_turn_v1_columns(connection)
        row = connection.execute(
            """
            SELECT * FROM turns
            WHERE request_id=? OR receipt_id=?
            """,
            (request_id, request_id),
        ).fetchone()
        if row is None:
            raise ContinuityError(
                "PREPARED_TURN_NOT_FOUND",
                "prepared turn disappeared before identity binding",
            )
        row_payload = dict(row)
        row_payload["lifecycle_identity_json"] = _canonical_json(
            normalized_lifecycle_identity
        )
        context_digest = _prepared_context_digest(
            row_payload,
            prepared_context_text,
            artifact_context,
            normalized_lifecycle_identity,
        )
        connection.execute(
            """
            UPDATE turns
            SET prepared_canon_revision=?,
                v1_context_json=?,
                active_projection_hash=?,
                retrieved_context_digest=?,
                prepared_context_text=?,
                prepare_telemetry_json=?,
                lifecycle_identity_json=?
            WHERE request_id=? OR receipt_id=?
            """,
            (
                int(identity["active_canon_revision"]),
                _canonical_json(dict(artifact_context)),
                str(identity["active_projection_hash"]),
                context_digest,
                str(prepared_context_text),
                _canonical_json(dict(telemetry)),
                _canonical_json(normalized_lifecycle_identity),
                request_id,
                request_id,
            ),
        )
    return {
        "prompt_hash": str(row_payload.get("prompt_hash") or ""),
        "retrieved_context_digest": context_digest,
        "active_projection_hash": str(identity["active_projection_hash"]),
        "lifecycle_identity": normalized_lifecycle_identity,
    }


def _turn_row(
    service: ContinuityService,
    *,
    request_id: str,
    session_id: str,
    turn_id: str,
) -> dict[str, Any] | None:
    with service.store.read_connection() as connection:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(turns)")
        }
        if "prepared_canon_revision" not in columns:
            return None
        if request_id:
            row = connection.execute(
                "SELECT * FROM turns WHERE request_id=? OR receipt_id=?",
                (request_id, request_id),
            ).fetchone()
        elif session_id and turn_id:
            row = connection.execute(
                """
                SELECT * FROM turns
                WHERE session_id=? AND turn_id=?
                  AND status IN ('pending', 'failed', 'proposed')
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (session_id, turn_id),
            ).fetchone()
        else:
            row = None
        return dict(row) if row is not None else None


def _mark_turn_identity_failed(
    service: ContinuityService,
    turn: Mapping[str, Any],
    reason: str,
) -> None:
    with service.store.transaction() as connection:
        _ensure_turn_v1_columns(connection)
        connection.execute(
            """
            UPDATE turns
            SET status='failed', error=?, completed_at=?
            WHERE request_id=?
            """,
            (
                str(reason),
                _utc_now(),
                str(turn.get("request_id") or ""),
            ),
        )


def _validate_prepared_turn_identity(
    service: ContinuityService,
    turn: Mapping[str, Any],
    *,
    effective_prompt: str,
    artifact_context: Mapping[str, Any],
) -> dict[str, Any]:
    stored_prompt_hash = str(turn.get("prompt_hash") or "")
    if not stored_prompt_hash or _sha256(effective_prompt) != stored_prompt_hash:
        raise ContinuityError(
            "PREPARED_PROMPT_MISMATCH",
            "prompt does not match the prepared receipt",
        )
    stored_projection_hash = str(
        turn.get("active_projection_hash") or ""
    )
    stored_context_digest = str(
        turn.get("retrieved_context_digest") or ""
    )
    prepared_context_text = str(
        turn.get("prepared_context_text") or ""
    )
    lifecycle_identity = _decode_lifecycle_identity(
        turn.get("lifecycle_identity_json", "{}")
    )
    if (
        not stored_projection_hash
        or not stored_context_digest
        or not prepared_context_text
    ):
        raise ContinuityError(
            "PREPARED_IDENTITY_MISSING",
            "prepared receipt is missing context or projection identity",
        )
    actual_context_digest = _prepared_context_digest(
        turn,
        prepared_context_text,
        artifact_context,
        lifecycle_identity,
    )
    if actual_context_digest != stored_context_digest:
        raise ContinuityError(
            "PREPARED_CONTEXT_MISMATCH",
            "prepared context digest does not match the receipt payload",
        )
    current = _active_continuity_identity(service)
    if (
        int(turn.get("prepared_canon_revision") or 0)
        != int(current["active_canon_revision"])
        or stored_projection_hash
        != str(current["active_projection_hash"])
    ):
        raise ContinuityError(
            "PREPARED_IDENTITY_STALE",
            "prepared canon revision or projection is stale",
        )
    return {
        "prompt_hash": stored_prompt_hash,
        "retrieved_context_digest": stored_context_digest,
        "active_projection_hash": stored_projection_hash,
        "prepared_canon_revision": int(
            current["active_canon_revision"]
        ),
        "lifecycle_identity": lifecycle_identity,
    }


def _authority_index_path(root: Path) -> Path:
    return root / DEFAULT_AUTHORITY_INDEX_V1_PATH


def _longform_database_path(root: Path) -> Path:
    return root / DEFAULT_LONGFORM_V1_PATH


def _projection_database_path(root: Path) -> Path:
    return root / DEFAULT_PROJECTION_RUNS_V1_PATH


def _embedding_batch_is_exact(service: Any) -> bool:
    """Return whether one batch preserves singleton embedding semantics.

    SiliconFlow's BAAI/bge-m3 endpoint currently produces deterministic but
    composition-dependent vectors when differently sized inputs share a
    request.  Prepare v2 must therefore use the existing per-query provider
    path for this exact service/model pair instead of silently changing the
    legacy candidate pool.
    """

    try:
        host = (
            (urlsplit(str(service.base_url)).hostname or "")
            .casefold()
            .rstrip(".")
        )
    except ValueError:
        return False
    model = str(service.model or "").strip().casefold()
    return not (
        host == "api.siliconflow.cn"
        and model == "baai/bge-m3"
    )


def _authority_index(
    root: Path,
    *,
    with_embeddings: bool = False,
    with_rerank: bool = False,
    prepare_v2_enabled: bool | None = None,
) -> AuthorityIndex:
    config = load_config(root)
    prepare_performance = dict(
        (config.get("performance") or {}).get("prepare_v2") or {}
    )
    raw_config_path = root / ".plot-rag" / "config.json"
    if raw_config_path.is_file():
        raw_config = _json_load(
            raw_config_path.read_text(encoding="utf-8-sig"),
            {},
        )
        if isinstance(raw_config, Mapping):
            raw_prepare_performance = (
                (raw_config.get("performance") or {}).get("prepare_v2")
                or {}
            )
            if isinstance(raw_prepare_performance, Mapping):
                prepare_performance.update(raw_prepare_performance)
    optimized_execution = (
        True
        if prepare_v2_enabled is None
        else bool(prepare_v2_enabled)
    )

    def performance_int(name: str, default: int) -> int:
        raw = prepare_performance.get(name, default)
        if isinstance(raw, bool):
            return default
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return default

    embedding_provider = None
    embedding_batch_provider = None
    embedding_model = "lexical-only"
    rerank_provider = None
    rerank_model = "disabled"
    runtime = None
    if with_embeddings or with_rerank:
        runtime = state_rag._load_runtime_config(root)
    if (
        with_embeddings
        and runtime is not None
        and runtime.embedding.enabled
    ):
        embedding_model = runtime.embedding.model or "remote-embedding"
        embedding_batch_exact = _embedding_batch_is_exact(
            runtime.embedding
        )

        def embedding_provider(text: str) -> Sequence[float]:
            vectors, _ = state_rag._embedding_call(runtime.embedding, [text])
            return vectors[0]

        setattr(
            embedding_provider,
            "cache_identity",
            _canonical_json(
                {
                    "service": runtime.embedding.name,
                    "base_url": runtime.embedding.base_url,
                    "endpoint": runtime.embedding.endpoint,
                    "model": runtime.embedding.model,
                    "input_semantics": (
                        "batch_independent"
                        if embedding_batch_exact
                        else "singleton_exact"
                    ),
                }
            ),
        )
        if (
            optimized_execution
            and bool(prepare_performance.get("batch_embedding", True))
            and embedding_batch_exact
        ):

            def embedding_batch_provider(
                texts: Sequence[str],
            ) -> Sequence[Sequence[float]]:
                vectors, _ = state_rag._embedding_call(
                    runtime.embedding,
                    list(texts),
                )
                return vectors

    if with_rerank and runtime is not None and runtime.rerank.enabled:
        rerank_model = runtime.rerank.model or "remote-rerank"

        def rerank_provider(
            query: str,
            documents: Sequence[str],
            top_n: int,
        ) -> Sequence[tuple[int, float]]:
            ranked, _ = state_rag._rerank_call(
                runtime.rerank,
                query,
                documents,
                top_n,
            )
            return ranked

        setattr(
            rerank_provider,
            "cache_identity",
            _canonical_json(
                {
                    "service": runtime.rerank.name,
                    "base_url": runtime.rerank.base_url,
                    "endpoint": runtime.rerank.endpoint,
                    "model": runtime.rerank.model,
                }
            ),
        )

    return AuthorityIndex(
        _authority_index_path(root),
        embedding_provider=embedding_provider,
        embedding_batch_provider=embedding_batch_provider,
        embedding_model=embedding_model,
        rerank_provider=rerank_provider,
        rerank_model=rerank_model,
        embedding_batch_size=performance_int(
            "embedding_batch_size",
            32,
        ),
        embedding_batch_max_chars=performance_int(
            "embedding_batch_max_chars",
            24_000,
        ),
        embedding_single_max_concurrency=(
            min(
                4,
                performance_int("remote_total_concurrency", 6),
            )
            if (
                optimized_execution
                and runtime is not None
                and runtime.embedding.enabled
                and not _embedding_batch_is_exact(runtime.embedding)
            )
            else 1
        ),
        rerank_max_concurrency=1
        if not optimized_execution
        else min(
            performance_int("rerank_max_concurrency", 4),
            performance_int("remote_total_concurrency", 6),
        ),
        query_embedding_cache_size=(
            performance_int("query_embedding_cache_size", 2048)
            if runtime is not None and runtime.embedding.enabled
            else 0
        ),
        singleflight_enabled=bool(
            optimized_execution
            and prepare_performance.get("singleflight", True)
        ),
    )


def refresh_longform_index(
    project_root: Path | str,
    *,
    with_embeddings: bool = False,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    config = load_config(root)
    service = ContinuityService(root)
    manifest_snapshot = service.get_current_source_manifest_snapshot()
    active_manifest = list(manifest_snapshot.get("entries") or [])
    accepted_hashes: dict[str, str] | None = (
        {} if manifest_snapshot.get("managed") else None
    )
    ignored_manifest_entries: list[dict[str, Any]] = []
    if manifest_snapshot.get("managed"):
        for entry in active_manifest:
            raw_path = str(
                entry.get("path") or entry.get("source_path") or ""
            ).strip()
            content_hash = str(entry.get("content_hash") or "").strip()
            metadata = dict(entry.get("metadata") or {})
            if metadata.get("indexable") is False:
                ignored_manifest_entries.append(
                    {
                        "source_path": raw_path,
                        "reason": "manifest_non_indexable",
                    }
                )
                continue
            if not raw_path or not content_hash:
                ignored_manifest_entries.append(
                    {
                        "source_path": raw_path,
                        "reason": "missing_path_or_hash",
                    }
                )
                continue
            candidate = Path(raw_path)
            resolved = (
                candidate.expanduser().resolve()
                if candidate.is_absolute()
                else (root / candidate).resolve()
            )
            if not _inside(resolved, root):
                ignored_manifest_entries.append(
                    {
                        "source_path": raw_path,
                        "reason": "outside_project_root",
                    }
                )
                continue
            relative = resolved.relative_to(root).as_posix()
            if relative in accepted_hashes:
                raise ContinuityError(
                    "SOURCE_MANIFEST_CURRENT_DUPLICATE_PATH",
                    "current accepted manifest contains a duplicate normalized path",
                    details={"path": relative},
                )
            accepted_hashes[relative] = content_hash
    index = _authority_index(root, with_embeddings=with_embeddings)
    refresh = index.refresh(
        root,
        config["authority_sources"],
        accepted_hashes=accepted_hashes,
    )
    retention = index.prune_derived_cache(
        keep_candidate_queries=int(
            (config.get("lifecycle") or {}).get("candidate_cache_limit", 5000)
        )
    )
    return {
        "status": "ready",
        "refresh": refresh,
        "retention": retention,
        "schema": index.schema_info(),
        "database_path": str(index.database_path.resolve()),
        "with_embeddings": bool(with_embeddings),
        "source_gate": {
            "mode": (
                "active_accepted_manifest"
                if manifest_snapshot.get("managed")
                else "legacy_config_compatibility"
            ),
            "active_manifest_entries": len(active_manifest),
            "manifest_history_entries": int(
                manifest_snapshot.get("history_count") or 0
            ),
            "indexable_manifest_entries": (
                len(accepted_hashes) if accepted_hashes is not None else None
            ),
            "ignored_manifest_entries": ignored_manifest_entries,
        },
    }


def _render_precise_facts(facts: Sequence[Mapping[str, Any]], limit: int = 40) -> str:
    lines: list[str] = []
    for fact in facts[:limit]:
        entity = (
            fact.get("entity_id")
            or fact.get("subject_entity_id")
            or fact.get("target_entity_id")
            or "world"
        )
        source_event_id = str(fact.get("source_event_id") or "unknown")
        lines.append(
            (
                "[accepted:{scope}:{kind}|event={source_event_id}] "
                "{entity}.{field} = {value}"
            ).format(
                scope=fact.get("scope") or "current",
                kind=fact.get("fact_type") or "fact",
                source_event_id=source_event_id,
                entity=entity,
                field=fact.get("field_name") or "value",
                value=json.dumps(
                    fact.get("value"), ensure_ascii=False, sort_keys=True
                ),
            )
        )
    return "\n".join(lines)


def _render_power_state(
    power_state: Mapping[str, Any],
    *,
    max_chars: int = 5200,
) -> str:
    lines: list[str] = []
    for section in (
        "progression",
        "resources",
        "abilities",
        "statuses",
        "bindings",
        "qualifications",
        "observations",
    ):
        for item in power_state.get(section) or []:
            line = (
                f"[accepted-power:{section}] "
                + _canonical_json(item)
            )
            projected = sum(len(value) + 1 for value in lines) + len(line)
            if projected > max_chars:
                if not lines:
                    return line[: max(1, max_chars - 1)] + "…"
                return "\n".join(lines)
            lines.append(line)
    return "\n".join(lines)


def _contains_exact_evidence_request(text: str) -> bool:
    folded = str(text or "").casefold()
    return any(marker.casefold() in folded for marker in _EXACT_STATE_EVIDENCE_MARKERS)


def _contains_unresolved_state(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            _contains_unresolved_state(key)
            or _contains_unresolved_state(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_unresolved_state(item) for item in value)
    if not isinstance(value, str):
        return False
    normalized = value.strip().casefold()
    if normalized in _EXACT_STATE_UNRESOLVED_MARKERS:
        return True
    return any(
        marker in normalized
        for marker in _EXACT_STATE_UNRESOLVED_MARKERS
        if any("\u4e00" <= character <= "\u9fff" for character in marker)
    )


def _accepted_entity_mentions(
    service: ContinuityService,
    text: str,
) -> dict[str, Any]:
    folded = " ".join(str(text or "").casefold().split())
    surfaces: dict[str, set[str]] = {}
    store = getattr(service, "store", None)
    if store is None or not hasattr(store, "read_connection"):
        return {
            "status": "none",
            "entity_ids": [],
            "mentions": {},
            "ambiguous": {},
        }
    with store.read_connection() as connection:
        rows = connection.execute(
            """
            SELECT entity_id, canonical_name AS surface
            FROM entities
            UNION ALL
            SELECT entity_id, alias_text AS surface
            FROM entity_aliases
            WHERE alias_status='confirmed'
            ORDER BY entity_id, surface
            """
        ).fetchall()
    for row in rows:
        surface = " ".join(str(row["surface"] or "").casefold().split())
        if not surface:
            continue
        if len(surface) < 2 and not surface.isascii():
            continue
        if surface.isascii() and len(surface) < 3:
            continue
        surfaces.setdefault(surface, set()).add(str(row["entity_id"]))
    matched = {
        surface: sorted(entity_ids)
        for surface, entity_ids in surfaces.items()
        if surface in folded
    }
    ambiguous = {
        surface: entity_ids
        for surface, entity_ids in matched.items()
        if len(entity_ids) != 1
    }
    if ambiguous:
        return {
            "status": "ambiguous",
            "entity_ids": [],
            "mentions": matched,
            "ambiguous": ambiguous,
        }
    entity_ids = sorted(
        {
            entity_ids[0]
            for entity_ids in matched.values()
            if len(entity_ids) == 1
        }
    )
    return {
        "status": "resolved" if entity_ids else "none",
        "entity_ids": entity_ids,
        "mentions": matched,
        "ambiguous": {},
    }


def _record_entity_ids(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = str(key).casefold()
            if (
                normalized_key == "entity_id"
                or normalized_key.endswith("_entity_id")
            ) and item is not None and str(item) != "":
                result.add(str(item))
            else:
                result.update(_record_entity_ids(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            result.update(_record_entity_ids(item))
    return result


def _record_matches_entities(
    value: Mapping[str, Any],
    entity_ids: set[str],
) -> bool:
    if not entity_ids:
        return True
    return bool(_record_entity_ids(value).intersection(entity_ids))


def _item_projection_identity(
    service: ContinuityService,
) -> dict[str, Any]:
    store = getattr(service, "store", None)
    if store is None or not hasattr(store, "read_connection"):
        return {
            "status": "degraded",
            "item_projection_hash": "",
            "metadata": {},
            "error_type": "ItemProjectionUnavailable",
            "reason": "continuity service has no item projection store",
        }
    try:
        with store.read_connection() as connection:
            metadata = read_item_projection_metadata(connection)
        return {
            "status": "ready",
            "item_projection_hash": str(
                metadata.get(ITEM_PROJECTION_META_HASH) or ""
            ),
            "metadata": dict(metadata),
        }
    except (sqlite3.Error, ValueError, TypeError) as exc:
        return {
            "status": "degraded",
            "item_projection_hash": "",
            "metadata": {},
            "error_type": type(exc).__name__,
            "reason": str(exc),
        }


def _item_context_triggers(prompt: str) -> list[str]:
    folded = str(prompt or "").casefold()
    return sorted(
        {
            marker
            for marker in _ITEM_CONTEXT_TRIGGER_MARKERS
            if marker.casefold() in folded
        },
        key=lambda value: (value.casefold(), value),
    )


def _advantage_trigger_signals(prompt: str) -> dict[str, Any]:
    """Classify deterministic Advantage recall signals without inventing IDs."""

    text = str(prompt or "")
    folded = text.casefold()
    special_terms = sorted(
        {
            marker
            for marker in _ADVANTAGE_SPECIAL_TERM_MARKERS
            if marker.casefold() in folded
        },
        key=lambda value: (value.casefold(), value),
    )
    actions = sorted(
        {
            marker
            for marker in _ADVANTAGE_ACTION_MARKERS
            if marker.casefold() in folded
        },
        key=lambda value: (value.casefold(), value),
    )
    has_continuity_change = any(
        marker.casefold() in folded
        for marker in _ADVANTAGE_CONTINUITY_CHANGE_MARKERS
    )
    continuity_signals = sorted(
        category
        for category, markers in _ADVANTAGE_CONTINUITY_MARKERS.items()
        if has_continuity_change
        and any(marker.casefold() in folded for marker in markers)
    )

    advantage_ids: set[str] = set()
    module_ids: set[str] = set()
    for match in _ADVANTAGE_ID_ASSIGNMENT_RE.finditer(text):
        value = str(match.group("value") or "").strip()
        if not value:
            continue
        if str(match.group("kind") or "").casefold() == "module_id":
            module_ids.add(value)
        else:
            advantage_ids.add(value)
    for match in _ADVANTAGE_ID_TOKEN_RE.finditer(text):
        value = str(match.group("value") or "").strip()
        if not value or value.casefold() in {"advantage_id", "module_id"}:
            continue
        if value.casefold().startswith("advantage_module_"):
            module_ids.add(value)
            continue
        prefix = value.split("_", 1)[0].split(":", 1)[0].split("-", 1)[0]
        if prefix.casefold() in {"module", "mod"}:
            module_ids.add(value)
        else:
            advantage_ids.add(value)

    layers: list[str] = []
    if advantage_ids or module_ids:
        layers.append("stable_id")
    if special_terms:
        layers.append("special_term")
    if actions:
        layers.append("action")
    if continuity_signals:
        layers.append("continuity")
    return {
        "required": bool(layers),
        "layers": layers,
        "stable_ids": {
            "advantage_ids": sorted(advantage_ids),
            "module_ids": sorted(module_ids),
        },
        "special_terms": special_terms,
        "actions": actions,
        "continuity_signals": continuity_signals,
    }


def _load_advantage_query_api() -> Any | None:
    """Load the optional Advantage core without making it a hard dependency."""

    for module_name in (
        "continuity.advantages",
        "scripts.continuity.advantages",
    ):
        try:
            return importlib.import_module(module_name)
        except (ImportError, ModuleNotFoundError):
            continue
    return None


def _advantage_call(
    function: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Call a rolling core API while ignoring only unsupported optional kwargs."""

    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(*args, **kwargs)
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return function(*args, **kwargs)
    supported = {
        key: value
        for key, value in kwargs.items()
        if key in signature.parameters
    }
    return function(*args, **supported)


def _advantage_result_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    if not isinstance(value, Mapping):
        return []
    raw = dict(value)
    for key in ("advantages", "contexts", "records", "results"):
        nested = raw.get(key)
        if isinstance(nested, Sequence) and not isinstance(
            nested,
            (str, bytes, bytearray),
        ):
            return [
                dict(item) for item in nested if isinstance(item, Mapping)
            ]
        if isinstance(nested, Mapping):
            return [
                dict(item)
                for _, item in sorted(
                    nested.items(),
                    key=lambda pair: str(pair[0]),
                )
                if isinstance(item, Mapping)
            ]
    if any(
        key in raw
        for key in (
            "advantage_id",
            "definition",
            "modules",
            "runtime",
            "ledger",
            "knowledge",
            "exposure",
        )
    ):
        return [raw]
    mapped = [
        dict(item)
        for _, item in sorted(raw.items(), key=lambda pair: str(pair[0]))
        if isinstance(item, Mapping)
    ]
    return mapped if mapped and len(mapped) == len(raw) else []


def _advantage_record_id(record: Mapping[str, Any]) -> str:
    definition = record.get("definition")
    definition_map = dict(definition) if isinstance(definition, Mapping) else {}
    return str(
        record.get("advantage_id")
        or definition_map.get("advantage_id")
        or ""
    )


def _advantage_modules(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = (
        record.get("modules")
        or record.get("module_definitions")
        or record.get("module")
        or []
    )
    if isinstance(value, Mapping):
        if "module_id" in value:
            return [dict(value)]
        return [
            dict(item)
            for _, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if isinstance(item, Mapping)
        ]
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def _advantage_module_id(module: Mapping[str, Any]) -> str:
    definition = module.get("definition")
    definition_map = dict(definition) if isinstance(definition, Mapping) else {}
    return str(
        module.get("module_id")
        or definition_map.get("module_id")
        or ""
    )


def _advantage_owner_ids(record: Mapping[str, Any]) -> set[str]:
    owners: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized = str(key).casefold()
                if normalized in {
                    "owner_entity_id",
                    "actor_entity_id",
                    "bound_owner_entity_id",
                } and item not in (None, ""):
                    owners.add(str(item))
                elif normalized in {
                    "definition",
                    "anchors",
                    "anchor",
                    "runtime",
                    "contracts",
                    "contract",
                }:
                    walk(item)
        elif isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            for item in value:
                walk(item)

    walk(record)
    return owners


def _advantage_projection_hash(value: Any) -> str:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) == "advantage_projection_hash" and isinstance(
                item,
                str,
            ):
                return item
            nested = _advantage_projection_hash(item)
            if nested:
                return nested
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for item in value:
            nested = _advantage_projection_hash(item)
            if nested:
                return nested
    return ""


def _advantage_record_search_text(record: Mapping[str, Any]) -> str:
    values: list[str] = []
    definition = record.get("definition")
    definition_map = dict(definition) if isinstance(definition, Mapping) else {}
    for key in (
        "advantage_id",
        "title",
        "name",
        "canonical_name",
        "profile",
        "profiles",
    ):
        value = record.get(key, definition_map.get(key))
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            values.extend(str(item) for item in value)
    for module in _advantage_modules(record):
        for key in ("module_id", "title", "name", "kind", "trigger"):
            value = module.get(key)
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, Mapping):
                values.append(_canonical_json(value))
            elif isinstance(value, Sequence) and not isinstance(
                value,
                (str, bytes, bytearray),
            ):
                values.extend(str(item) for item in value)
    return "\n".join(values).casefold()


def _advantage_runtime_enabled(record: Mapping[str, Any]) -> bool:
    runtime = record.get("runtime")
    if not isinstance(runtime, Mapping):
        return False
    enabled = runtime.get("enabled")
    if type(enabled) is bool:
        return enabled
    state = str(
        runtime.get("status")
        or runtime.get("binding_state")
        or runtime.get("state")
        or ""
    ).casefold()
    return state in {"active", "enabled", "available", "bound", "activated"}


def _compact_advantage_context_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        omitted = {
            "projection_hash",
            "advantage_projection_hash",
            "revisions",
        }
        return {
            str(key): _compact_advantage_context_value(item)
            for key, item in value.items()
            if str(key) not in omitted
        }
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_compact_advantage_context_value(item) for item in value]
    return value


def _advantage_records_value(value: Any, *, limit: int) -> list[Any]:
    if isinstance(value, Mapping):
        if any(
            key in value
            for key in (
                "entry_id",
                "claim",
                "stage",
                "exposure",
                "source_event_id",
            )
        ):
            return [dict(value)]
        return [
            dict(item)
            for _, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if isinstance(item, Mapping)
        ][:limit]
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return list(value)[:limit]
    return [] if value in (None, "") else [value]


def _selected_advantage_modules(
    record: Mapping[str, Any],
    prompt: str,
    *,
    explicit_module_ids: set[str],
) -> list[dict[str, Any]]:
    folded = str(prompt or "").casefold()
    modules = _advantage_modules(record)

    def module_rank(module: Mapping[str, Any]) -> tuple[Any, ...]:
        module_id = _advantage_module_id(module)
        search_text = _canonical_json(module).casefold()
        return (
            0 if module_id in explicit_module_ids else 1,
            0 if module_id and module_id.casefold() in folded else 1,
            0 if any(
                marker.casefold() in search_text
                for marker in (
                    *_ADVANTAGE_SPECIAL_TERM_MARKERS,
                    *_ADVANTAGE_ACTION_MARKERS,
                )
                if marker.casefold() in folded
            ) else 1,
            module_id,
            search_text,
        )

    return sorted(modules, key=module_rank)[:_ADVANTAGE_MAX_MODULES_PER_RECORD]


def _advantage_generation_knowledge(
    value: Any,
    *,
    observer_entity_id: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return only claims that are explicitly visible to the generator.

    The continuity query is a useful defence-in-depth boundary, but the
    renderer must not trust a provider to have removed every control-plane
    row.  In particular, ``objective``/``author_plan``/future rows must not be
    represented by a ``withheld`` marker: even a marker leaks the existence,
    identity, or reveal schedule of hidden knowledge to the model.  We keep a
    deterministic count/hash for diagnostics and return no other information
    about excluded rows.
    """

    rows = _advantage_records_value(value, limit=12)
    visible: list[dict[str, Any]] = []
    excluded_fingerprints: list[str] = []
    observer = str(observer_entity_id or "").strip()
    for raw in rows:
        if not isinstance(raw, Mapping):
            excluded_fingerprints.append(_canonical_json(raw))
            continue
        row = dict(raw)
        plane = str(row.get("knowledge_plane") or "")
        status = str(
            row.get("knowledge_status") or row.get("status") or "canon"
        )
        reveal_stage = str(row.get("reveal_stage") or "")
        folded_stage = reveal_stage.casefold()
        row_observer = str(row.get("observer_entity_id") or "").strip()
        hidden = any(
            marker in folded_stage
            for marker in (
                "author",
                "future",
                "hidden",
                "planned",
                "deferred",
                "unknown",
                "unrevealed",
            )
        )
        actor_scope_valid = bool(
            plane != "actor_belief"
            or (
                observer
                and row_observer
                and row_observer == observer
            )
        )
        claim_visible = bool(
            status == "canon"
            and not hidden
            and actor_scope_valid
            and plane
            in {
                "actor_belief",
                "public_narrative",
                "reader_disclosed",
            }
            and row.get("claim") not in (None, "", [], {})
        )
        if claim_visible:
            # A visible claim is the only knowledge payload the model needs.
            # Do not carry IDs, observer IDs, evidence, coordinates, reveal
            # stages, or other projection metadata into the prompt.
            visible.append(
                {
                    "claim": _compact_advantage_context_value(
                        row.get("claim")
                    )
                }
            )
            continue
        excluded_fingerprints.append(
            _canonical_json(_compact_advantage_context_value(row))
        )
    telemetry = {
        "source_count": len(rows),
        "visible_count": len(visible),
        "excluded_count": len(rows) - len(visible),
        "excluded_hash": (
            _sha256("\n".join(excluded_fingerprints))
            if excluded_fingerprints
            else ""
        ),
    }
    return visible, telemetry


def _normalize_advantage_story_cursor(
    value: Any,
) -> dict[str, Any]:
    """Keep only an accepted, comparable story coordinate.

    Chapter/scene numbers are not a substitute for a project calendar.  A
    missing or malformed coordinate therefore becomes an empty mapping and
    lets current-head queries use the provider's reveal-stage allow-list.
    """

    if not isinstance(value, Mapping):
        return {}
    calendar_id = str(value.get("calendar_id") or "").strip()
    ordinal = value.get("ordinal")
    if not calendar_id or type(ordinal) is not int:
        return {}
    cursor: dict[str, Any] = {
        "calendar_id": calendar_id,
        "ordinal": int(ordinal),
    }
    for key in ("label", "precision", "source_event_id"):
        if value.get(key) not in (None, ""):
            cursor[key] = str(value[key])
    return cursor


def _advantage_historical_query_requested(
    context: Mapping[str, Any],
) -> bool:
    """Return whether the caller explicitly asked for a historical view."""

    if bool(
        context.get("historical_query")
        or context.get("advantage_historical_query")
        or context.get("_historical_query")
    ):
        return True
    for key in (
        "advantage_query_mode",
        "temporal_query_mode",
        "query_mode",
        "time_scope",
        "scope",
    ):
        value = str(context.get(key) or "").strip().casefold()
        if value in {
            "historical",
            "history",
            "historical_query",
            "as_of",
            "past",
        }:
            return True
    return False


def _render_advantage_context(
    advantage_context: Mapping[str, Any],
) -> str:
    if not advantage_context.get("required"):
        return ""
    records = list(advantage_context.get("records") or [])
    if not records:
        status = str(advantage_context.get("status") or "empty")
        return (
            "accepted Advantage 投影"
            + ("当前不可读取" if status in {"unavailable", "degraded"} else "为空")
            + "，当前检索对象关联的金手指、模块、运行态、账本、知识与暴露均按未知处理；"
            "不得根据名称、题材套路或未来计划补造能力。"
        )
    lines = [
        (
            "以下内容只来自同一 accepted Advantage 投影；稳定 advantage_id/module_id "
            "优先于名称召回。未出现的模块、资源、冷却、代价、失败模式、契约与暴露一律未知。"
        ),
        (
            "knowledge 只包含当前视角明确可见的 claim；未通过可见性门禁的内容及其"
            "元数据已在进入模型前移除，禁止根据缺失项反推能力、揭示顺序或作者计划。"
        ),
    ]
    for record in records:
        advantage_id = _advantage_record_id(record) or "unknown"
        prefix = f"[accepted-advantage:{advantage_id}] "
        selected_modules = list(record.get("selected_modules") or [])
        values: list[tuple[str, Any]] = [
            ("definition", record.get("definition")),
            ("anchors", record.get("anchors") or record.get("anchor")),
            ("modules", selected_modules),
            ("runtime", record.get("runtime")),
            (
                "module_runtime",
                record.get("module_runtime")
                or record.get("module_runtimes"),
            ),
            (
                "ledger",
                _advantage_records_value(
                    record.get("ledger"),
                    limit=_ADVANTAGE_LEDGER_LIMIT,
                ),
            ),
            (
                "knowledge",
                record.get("knowledge"),
            ),
            (
                "contracts",
                record.get("contracts") or record.get("contract"),
            ),
            (
                "narrative_contract",
                record.get("narrative_contract")
                or record.get("narrative"),
            ),
            (
                "progression",
                _advantage_records_value(
                    record.get("progression"),
                    limit=8,
                ),
            ),
            ("exposure", record.get("exposure")),
        ]
        for key, value in values:
            if value in (None, "", [], {}):
                continue
            lines.append(
                prefix
                + key
                + "="
                + _canonical_json(_compact_advantage_context_value(value))
            )
    return "\n".join(lines)


def _build_advantage_context(
    service: ContinuityService,
    prompt: str,
    *,
    entity_resolution: Mapping[str, Any],
    branch_id: str = "main",
    story_cursor: Mapping[str, Any] | None = None,
    chapter_no: int | None = None,
    scene_index: int | None = None,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build deterministic mandatory Advantage context from accepted projection."""

    effective_policy = {
        # Match the rollout defaults in plot_rag.py.  A caller that does not
        # supply the Advantage config must not silently enable a mandatory
        # database dependency.
        "enabled": False,
        "shadow": True,
        "mandatory_context": True,
        **dict(policy or {}),
    }
    historical_query = _advantage_historical_query_requested(
        dict(effective_policy)
    )
    require_story_cursor = bool(
        historical_query
        and effective_policy.get("_require_story_cursor", True)
    )
    signals = _advantage_trigger_signals(prompt)
    enabled = bool(effective_policy.get("enabled", False))
    mandatory = bool(effective_policy.get("mandatory_context", True))
    required = enabled and (mandatory or bool(signals["required"]))
    stable_ids = dict(signals.get("stable_ids") or {})
    explicit_advantage_ids = {
        str(value)
        for value in stable_ids.get("advantage_ids") or []
        if str(value)
    }
    explicit_module_ids = {
        str(value)
        for value in stable_ids.get("module_ids") or []
        if str(value)
    }
    selected_owner_ids = [
        str(value)
        for value in entity_resolution.get("entity_ids") or []
        if str(value)
    ]
    explicit_owner_entity_id = str(
        entity_resolution.get("owner_entity_id") or ""
    ).strip() or None
    observer_entity_id = str(
        entity_resolution.get("pov_entity_id")
        or entity_resolution.get("observer_entity_id")
        or ""
    ).strip() or None
    # A real project coordinate is the only valid historical cursor.  Do not
    # synthesize a ``chapter_scene`` calendar from convenience chapter fields:
    # projects may use ``main``, ``generic`` or another independent calendar.
    normalized_story_cursor = _normalize_advantage_story_cursor(story_cursor)
    base: dict[str, Any] = {
        "required": required,
        "enabled": enabled,
        "shadow": bool(effective_policy.get("shadow", True)),
        "mandatory_context": mandatory,
        "status": (
            "not_required"
            if enabled
            else (
                "shadow_disabled"
                if bool(effective_policy.get("shadow", True))
                and signals["required"]
                else "disabled"
            )
        ),
        "triggers": signals,
        "stable_ids": stable_ids,
        "selected_owner_entity_ids": selected_owner_ids,
        "owner_entity_id": explicit_owner_entity_id,
        "observer_entity_id": observer_entity_id,
        "story_cursor": normalized_story_cursor,
        "query_mode": "historical" if historical_query else "current_head",
        "cursor_policy": (
            "required_comparable"
            if require_story_cursor
            else "accepted_coordinate_or_current_head"
        ),
        "selected_advantage_ids": [],
        "selected_module_ids": [],
        "advantage_projection_hash": "",
        "records": [],
        "errors": [],
        "knowledge_filter_telemetry": {
            "source_count": 0,
            "visible_count": 0,
            "excluded_count": 0,
            "excluded_hash": "",
        },
    }
    if not required:
        base["context_text"] = ""
        return base
    if (
        require_story_cursor
        and not normalized_story_cursor
    ):
        base["status"] = "degraded"
        base["errors"].append(
            {
                "surface": "story_cursor",
                "error_type": "AdvantageStoryCursorRequired",
                "reason": (
                    "historical Advantage visibility requires an accepted "
                    "comparable story cursor"
                ),
            }
        )
        base["context_text"] = _render_advantage_context(base)
        return base

    store = getattr(service, "store", None)
    read_connection = getattr(store, "read_connection", None)
    if not callable(read_connection):
        base["status"] = "not_configured"
        base["errors"].append(
            {
                "surface": "advantage_store",
                "error_type": "AdvantageStoreUnavailable",
                "reason": (
                    "service exposes no accepted Advantage projection store"
                ),
            }
        )
        base["context_text"] = _render_advantage_context(base)
        return base

    api = _load_advantage_query_api()
    if api is None:
        base["status"] = "unavailable"
        base["errors"].append(
            {
                "surface": "advantage_core",
                "error_type": "AdvantageCoreUnavailable",
                "reason": "optional continuity.advantages module is not installed",
            }
        )
        base["context_text"] = _render_advantage_context(base)
        return base

    query_many = getattr(api, "query_advantage_contexts", None)
    query_one = getattr(api, "query_advantage_context", None)
    if not callable(query_many) and not callable(query_one):
        base["status"] = "unavailable"
        base["errors"].append(
            {
                "surface": "advantage_query",
                "error_type": "AdvantageQueryUnavailable",
                "reason": "Advantage core exposes no supported context query",
            }
        )
        base["context_text"] = _render_advantage_context(base)
        return base

    raw_result: Any = []
    projection_metadata: dict[str, Any] = {}
    try:
        with read_connection() as connection:
            # Pin definition/runtime/ledger/knowledge plus projection metadata
            # to one SQLite read snapshot.
            if not bool(getattr(connection, "in_transaction", False)):
                connection.execute("BEGIN")
            if callable(query_many):
                raw_result = _advantage_call(
                    query_many,
                    connection,
                    advantage_ids=(
                        sorted(explicit_advantage_ids)
                        if explicit_advantage_ids
                        else None
                    ),
                    owner_entity_id=(
                        explicit_owner_entity_id
                        if explicit_owner_entity_id
                        and not explicit_advantage_ids
                        else None
                    ),
                    branch_id=str(branch_id or "main"),
                    knowledge_plane=None,
                    observer_entity_id=observer_entity_id,
                    ledger_limit=_ADVANTAGE_LEDGER_LIMIT,
                    visibility="generation",
                    story_cursor=(
                        normalized_story_cursor or None
                    ),
                    # Never let a compatibility fallback infer a synthetic
                    # chapter_scene calendar for a current-head query.
                    chapter_no=None,
                    scene_index=None,
                    limit=_ADVANTAGE_MAX_RECORDS,
                )
            else:
                advantage_ids = sorted(explicit_advantage_ids)
                if not advantage_ids:
                    table_names = {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    if "advantage_definitions" in table_names:
                        advantage_ids = [
                            str(row[0])
                            for row in connection.execute(
                                "SELECT advantage_id "
                                "FROM advantage_definitions "
                                "ORDER BY advantage_id"
                            )
                        ]
                raw_result = [
                    _advantage_call(
                        query_one,
                        connection,
                        advantage_id,
                        branch_id=str(branch_id or "main"),
                        knowledge_plane=None,
                        observer_entity_id=observer_entity_id,
                        ledger_limit=_ADVANTAGE_LEDGER_LIMIT,
                        visibility="generation",
                        story_cursor=(
                            normalized_story_cursor or None
                        ),
                        chapter_no=None,
                        scene_index=None,
                    )
                    for advantage_id in advantage_ids
                ]
            read_metadata = getattr(
                api,
                "read_advantage_projection_metadata",
                None,
            )
            if callable(read_metadata):
                metadata_value = read_metadata(connection)
                if isinstance(metadata_value, Mapping):
                    projection_metadata = dict(metadata_value)
    except (
        ContinuityError,
        sqlite3.Error,
        ValueError,
        TypeError,
        RuntimeError,
    ) as exc:
        base["status"] = "degraded"
        base["errors"].append(
            {
                "surface": "advantage_query",
                "error_type": type(exc).__name__,
                "reason": str(exc),
            }
        )
        base["context_text"] = _render_advantage_context(base)
        return base

    records = _advantage_result_records(raw_result)
    # Sanitize every returned card before ranking/selection so hidden
    # knowledge cannot influence the model-facing record or leak through a
    # later renderer.  Only aggregate count/hash telemetry survives.
    knowledge_source_count = 0
    knowledge_visible_count = 0
    knowledge_excluded_count = 0
    knowledge_excluded_hash_parts: list[str] = []
    sanitized_records: list[dict[str, Any]] = []
    for raw_record in records:
        record = dict(raw_record)
        (
            visible_knowledge,
            knowledge_telemetry,
        ) = _advantage_generation_knowledge(
            record.get("knowledge"),
            observer_entity_id=observer_entity_id,
        )
        record["knowledge"] = visible_knowledge
        sanitized_records.append(record)
        knowledge_source_count += int(
            knowledge_telemetry.get("source_count") or 0
        )
        knowledge_visible_count += int(
            knowledge_telemetry.get("visible_count") or 0
        )
        knowledge_excluded_count += int(
            knowledge_telemetry.get("excluded_count") or 0
        )
        excluded_hash = str(
            knowledge_telemetry.get("excluded_hash") or ""
        ).strip()
        if excluded_hash:
            knowledge_excluded_hash_parts.append(excluded_hash)
    records = sanitized_records
    base["knowledge_filter_telemetry"] = {
        "source_count": knowledge_source_count,
        "visible_count": knowledge_visible_count,
        "excluded_count": knowledge_excluded_count,
        "excluded_hash": (
            _sha256("\n".join(knowledge_excluded_hash_parts))
            if knowledge_excluded_hash_parts
            else ""
        ),
    }
    folded = str(prompt or "").casefold()
    mentioned_owner_ids = set(selected_owner_ids)

    def record_rank(record: Mapping[str, Any]) -> tuple[Any, ...]:
        advantage_id = _advantage_record_id(record)
        module_ids = {
            _advantage_module_id(module)
            for module in _advantage_modules(record)
            if _advantage_module_id(module)
        }
        owners = _advantage_owner_ids(record)
        search_text = _advantage_record_search_text(record)
        return (
            0 if advantage_id in explicit_advantage_ids else 1,
            0 if module_ids.intersection(explicit_module_ids) else 1,
            0 if owners.intersection(mentioned_owner_ids) else 1,
            0 if advantage_id and advantage_id.casefold() in folded else 1,
            0 if search_text and any(
                token in folded
                for token in search_text.splitlines()
                if len(token) >= 2
            ) else 1,
            0 if _advantage_runtime_enabled(record) else 1,
            advantage_id,
        )

    selected_records: list[dict[str, Any]] = []
    for raw_record in sorted(records, key=record_rank)[:_ADVANTAGE_MAX_RECORDS]:
        record = dict(raw_record)
        record["selected_modules"] = _selected_advantage_modules(
            record,
            prompt,
            explicit_module_ids=explicit_module_ids,
        )
        selected_records.append(record)
    base["records"] = selected_records
    base["selected_advantage_ids"] = [
        value
        for value in (
            _advantage_record_id(record) for record in selected_records
        )
        if value
    ]
    base["selected_module_ids"] = list(
        dict.fromkeys(
            module_id
            for record in selected_records
            for module_id in (
                _advantage_module_id(module)
                for module in record.get("selected_modules") or []
            )
            if module_id
        )
    )
    base["selected_owner_entity_ids"] = sorted(
        {
            *selected_owner_ids,
            *(
                owner_id
                for record in selected_records
                for owner_id in _advantage_owner_ids(record)
            ),
        }
    )
    base["advantage_projection_hash"] = (
        str(
            projection_metadata.get("projection_hash")
            or projection_metadata.get("advantage_projection_hash")
            or ""
        )
        or _advantage_projection_hash(raw_result)
    )
    projection_hash = str(base["advantage_projection_hash"] or "").strip()
    if not projection_hash or not _ADVANTAGE_PROJECTION_HASH_RE.fullmatch(
        projection_hash
    ):
        base["errors"].append(
            {
                "surface": "advantage_projection_identity",
                "error_type": "AdvantageProjectionHashUnavailable",
                "reason": (
                    "generation context is not bound to a valid Advantage "
                    "projection hash"
                ),
                "details": {"projection_hash": projection_hash},
            }
        )
    missing_advantage_ids = sorted(
        explicit_advantage_ids - set(base["selected_advantage_ids"])
    )
    missing_module_ids = sorted(
        explicit_module_ids - set(base["selected_module_ids"])
    )
    if missing_advantage_ids or missing_module_ids:
        base["status"] = "stable_id_missing"
        base["errors"].append(
            {
                "surface": "stable_id_resolution",
                "error_type": "AdvantageStableIdNotFound",
                "reason": "explicit stable Advantage identifiers were not accepted",
                "details": {
                    "advantage_ids": missing_advantage_ids,
                    "module_ids": missing_module_ids,
                },
            }
        )
    if base["status"] == "stable_id_missing":
        pass
    elif base["errors"]:
        base["status"] = "degraded"
    elif selected_records:
        base["status"] = "ready"
    else:
        base["status"] = "empty"
    base["context_text"] = _render_advantage_context(base)
    return base


def _mandatory_advantage_failure(
    advantage_context: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(advantage_context, Mapping):
        return None
    if not bool(advantage_context.get("required")):
        return None
    status = str(advantage_context.get("status") or "unknown").casefold()
    if status == "ready":
        return None
    errors = [
        dict(item)
        for item in advantage_context.get("errors") or []
        if isinstance(item, Mapping)
    ]
    return {
        "code": "ADVANTAGE_MANDATORY_CONTEXT_BLOCKED",
        "status": status,
        "reason": (
            "mandatory accepted Advantage context is not ready"
            f" (status={status})"
        ),
        "errors": errors,
    }


def _item_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _item_catalog(
    service: ContinuityService,
) -> dict[str, Any]:
    with service.store.read_connection() as connection:
        entity_names = {
            str(row["entity_id"]): str(row["canonical_name"])
            for row in connection.execute(
                "SELECT entity_id, canonical_name FROM entities"
            )
        }
        definitions = []
        for row in connection.execute(
            """
            SELECT item_definition_id, item_entity_id, definition_json,
                   item_status
            FROM item_definitions
            ORDER BY item_definition_id
            """
        ):
            payload = _item_json_object(row["definition_json"])
            names = {
                str(row["item_definition_id"]),
                str(payload.get("canonical_name") or ""),
                str(payload.get("name") or ""),
                str(entity_names.get(str(row["item_entity_id"])) or ""),
            }
            definitions.append(
                {
                    "subject_type": "item_definition",
                    "subject_id": str(row["item_definition_id"]),
                    "definition_id": str(row["item_definition_id"]),
                    "status": str(row["item_status"] or ""),
                    "names": sorted(name for name in names if name),
                }
            )
        instances = []
        for row in connection.execute(
            """
            SELECT item_instance_id, item_definition_id, item_entity_id,
                   instance_json, instance_status
            FROM item_instances
            ORDER BY item_instance_id
            """
        ):
            payload = _item_json_object(row["instance_json"])
            names = {
                str(row["item_instance_id"]),
                str(payload.get("canonical_name") or ""),
                str(payload.get("instance_name") or ""),
                str(payload.get("name") or ""),
                str(entity_names.get(str(row["item_entity_id"])) or ""),
            }
            instances.append(
                {
                    "subject_type": "item_instance",
                    "subject_id": str(row["item_instance_id"]),
                    "definition_id": str(row["item_definition_id"]),
                    "status": str(row["instance_status"] or ""),
                    "names": sorted(name for name in names if name),
                }
            )
        stacks = []
        for row in connection.execute(
            """
            SELECT stack_id, item_definition_id, batch_json, stack_status
            FROM item_stacks
            ORDER BY stack_id
            """
        ):
            payload = _item_json_object(row["batch_json"])
            names = {
                str(row["stack_id"]),
                str(payload.get("canonical_name") or ""),
                str(payload.get("name") or ""),
                str(payload.get("batch_name") or ""),
            }
            stacks.append(
                {
                    "subject_type": "item_stack",
                    "subject_id": str(row["stack_id"]),
                    "definition_id": str(row["item_definition_id"]),
                    "status": str(row["stack_status"] or ""),
                    "names": sorted(name for name in names if name),
                }
            )
    return {
        "definitions": definitions,
        "instances": instances,
        "stacks": stacks,
    }


def _item_subjects_from_inventory(
    inventory: Mapping[str, Any],
) -> list[tuple[str, str]]:
    subjects: set[tuple[str, str]] = set()
    for facet in ("owned", "custodied", "carried", "stored", "equipped"):
        for item in inventory.get(facet) or []:
            if not isinstance(item, Mapping):
                continue
            subject_type = str(item.get("subject_type") or "")
            subject_id = str(item.get("subject_id") or "")
            if (
                subject_type in {"item_instance", "item_stack"}
                and subject_id
            ):
                subjects.add((subject_type, subject_id))
    return sorted(subjects)


def _compact_item_context_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        omitted = {
            "revisions",
            "projection_hash",
            "item_projection_hash",
            "item_projection_schema_version",
            "item_projection_source_revisions",
        }
        return {
            str(key): _compact_item_context_value(item)
            for key, item in value.items()
            if str(key) not in omitted
        }
    if isinstance(value, list):
        return [_compact_item_context_value(item) for item in value]
    return value


def _render_item_context(item_context: Mapping[str, Any]) -> str:
    if not item_context.get("required"):
        return ""
    records = list(item_context.get("records") or [])
    inventories = list(item_context.get("inventories") or [])
    if not records and not inventories:
        return (
            "accepted 物品投影为空，或没有稳定解析到与本轮相关的物品；"
            "物品定义、功能、运行态、保管关系和库存状态均按未知处理。"
            "不得从名称、类型或空 attributes 推断功能。"
        )
    lines = [
        (
            "以下内容只来自 accepted item projection；未出现的功能、"
            "持有人、位置、数量、耐久、充能与冷却一律未知，不得推断。"
        )
    ]
    for inventory in inventories:
        subjects: dict[tuple[str, str], set[str]] = {}
        for facet in ("owned", "custodied", "carried", "stored", "equipped"):
            for item in inventory.get(facet) or []:
                if not isinstance(item, Mapping):
                    continue
                subject_type = str(item.get("subject_type") or "")
                subject_id = str(item.get("subject_id") or "")
                if subject_type and subject_id:
                    subjects.setdefault(
                        (subject_type, subject_id),
                        set(),
                    ).add(facet)
        compact_inventory = {
            "actor_entity_id": inventory.get("actor_entity_id"),
            "subjects": [
                {
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "facets": sorted(facets),
                }
                for (subject_type, subject_id), facets in sorted(
                    subjects.items()
                )
            ],
            "legacy_inventory": [
                {
                    key: item.get(key)
                    for key in (
                        "item_entity_id",
                        "owner_entity_id",
                        "quantity",
                        "item_status",
                        "source_event_id",
                    )
                }
                for item in inventory.get("legacy_inventory") or []
                if isinstance(item, Mapping)
            ],
        }
        lines.append(
            "[accepted-item:inventory] "
            + _canonical_json(compact_inventory)
        )
    for record in records:
        subject_type = str(record.get("subject_type") or "item")
        subject_id = str(record.get("subject_id") or "")
        prefix = f"[accepted-item:{subject_type}:{subject_id}] "
        for key in (
            "definition",
            "functions",
            "runtime",
            "custody",
            "instance",
            "stack",
            "history",
            "observations",
        ):
            value = record.get(key)
            if key in {"history", "observations"}:
                value = list(value or [])[:2]
            if (
                value is None
                or value == ""
                or value == []
                or value == {}
            ):
                continue
            lines.append(
                prefix
                + key
                + "="
                + _canonical_json(_compact_item_context_value(value))
            )
    return "\n".join(lines)


def _build_item_context(
    service: ContinuityService,
    prompt: str,
    *,
    entity_resolution: Mapping[str, Any],
) -> dict[str, Any]:
    triggers = _item_context_triggers(prompt)
    identity = _item_projection_identity(service)
    selected_actor_ids = [
        str(value)
        for value in entity_resolution.get("entity_ids") or []
        if str(value)
    ]
    observer_entity_id = str(
        entity_resolution.get("pov_entity_id")
        or entity_resolution.get("observer_entity_id")
        or ""
    ).strip()
    if (
        not observer_entity_id
        and str(entity_resolution.get("status") or "") == "resolved"
        and len(selected_actor_ids) == 1
    ):
        # A single, accepted, unambiguous actor mention is the narrowest
        # defensible generation viewpoint for that actor's item context.  Do
        # not make the same inference for ambiguous or multi-actor prompts:
        # actor_belief observations must remain observer-scoped.
        observer_entity_id = selected_actor_ids[0]
    base = {
        "required": bool(triggers),
        "triggers": triggers,
        "status": "not_required" if not triggers else "ready",
        "item_projection_hash": identity["item_projection_hash"],
        "item_projection_status": identity["status"],
        "selected_actor_ids": selected_actor_ids,
        "observer_entity_id": observer_entity_id or None,
        "selected_subjects": [],
        "inventories": [],
        "records": [],
        "errors": [],
    }
    if not triggers:
        base["context_text"] = ""
        return base
    if identity["status"] != "ready":
        base["status"] = "degraded"
        base["errors"].append(
            {
                "surface": "item_projection",
                "error_type": identity.get("error_type"),
                "reason": identity.get("reason"),
            }
        )
        base["context_text"] = _render_item_context(base)
        return base
    try:
        catalog = _item_catalog(service)
    except (sqlite3.Error, ValueError, TypeError) as exc:
        base["status"] = "degraded"
        base["errors"].append(
            {
                "surface": "item_catalog",
                "error_type": type(exc).__name__,
                "reason": str(exc),
            }
        )
        base["context_text"] = _render_item_context(base)
        return base

    actor_subjects: set[tuple[str, str]] = set()
    for actor_id in base["selected_actor_ids"]:
        try:
            inventory = service.query_actor_inventory(str(actor_id))
        except ContinuityError as exc:
            base["errors"].append(
                {
                    "surface": "query_actor_inventory",
                    "actor_entity_id": actor_id,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                }
            )
            continue
        projected = {
            key: inventory.get(key)
            for key in (
                "actor_entity_id",
                "owned",
                "custodied",
                "carried",
                "stored",
                "equipped",
                "legacy_inventory",
            )
        }
        base["inventories"].append(projected)
        actor_subjects.update(_item_subjects_from_inventory(inventory))

    folded = str(prompt or "").casefold()
    direct_definitions: set[str] = set()
    direct_subjects: set[tuple[str, str]] = set()
    for group in ("definitions", "instances", "stacks"):
        for item in catalog[group]:
            if any(
                str(name).casefold() in folded
                for name in item.get("names") or []
            ):
                if group == "definitions":
                    direct_definitions.add(str(item["definition_id"]))
                else:
                    direct_subjects.add(
                        (
                            str(item["subject_type"]),
                            str(item["subject_id"]),
                        )
                    )
                    direct_definitions.add(str(item["definition_id"]))
    for item in (*catalog["instances"], *catalog["stacks"]):
        if str(item["definition_id"]) in direct_definitions:
            direct_subjects.add(
                (str(item["subject_type"]), str(item["subject_id"]))
            )

    selected = sorted(actor_subjects | direct_subjects)
    if not selected:
        selected = [
            (str(item["subject_type"]), str(item["subject_id"]))
            for item in (*catalog["instances"], *catalog["stacks"])
            if str(item.get("status") or "").casefold()
            not in {
                "destroyed",
                "consumed",
                "retired",
                "lost",
                "deprecated",
            }
        ]
    selected = selected[:_ITEM_CONTEXT_MAX_SUBJECTS]
    selected_definitions = set(direct_definitions)
    selected_definitions.update(
        str(item["definition_id"])
        for item in (*catalog["instances"], *catalog["stacks"])
        if (str(item["subject_type"]), str(item["subject_id"])) in selected
    )
    if not selected and not selected_definitions:
        selected_definitions.update(
            str(item["definition_id"])
            for item in catalog["definitions"][:_ITEM_CONTEXT_MAX_SUBJECTS]
        )

    for subject_type, subject_id in selected:
        record: dict[str, Any] = {
            "subject_type": subject_type,
            "subject_id": subject_id,
        }
        try:
            custody = service.query_item_custody(
                subject_type=subject_type,
                subject_id=subject_id,
            )
            record.update(
                {
                    key: custody.get(key)
                    for key in (
                        "definition",
                        "instance",
                        "stack",
                        "custody",
                        "runtime",
                    )
                }
            )
            definition = custody.get("definition") or {}
            definition_id = str(
                definition.get("item_definition_id") or ""
            )
            if definition_id:
                selected_definitions.add(definition_id)
            if subject_type == "item_instance":
                instance = service.query_item_instance(subject_id)
                runtime = service.query_item_runtime(subject_id)
                record["bindings"] = instance.get("bindings") or []
                record["function_runtime"] = (
                    runtime.get("function_runtime") or []
                )
                record["runtime"] = runtime.get("runtime")
                history = service.query_item_history(
                    item_instance_id=subject_id,
                    limit=_ITEM_CONTEXT_HISTORY_LIMIT,
                )
                observations = service.query_item_observations(
                    item_instance_id=subject_id,
                    observer_entity_id=observer_entity_id or None,
                    limit=_ITEM_CONTEXT_HISTORY_LIMIT,
                )
            else:
                history = service.query_item_history(
                    stack_id=subject_id,
                    limit=_ITEM_CONTEXT_HISTORY_LIMIT,
                )
                observations = service.query_item_observations(
                    stack_id=subject_id,
                    observer_entity_id=observer_entity_id or None,
                    limit=_ITEM_CONTEXT_HISTORY_LIMIT,
                )
            record["history"] = history.get("history") or []
            record["observations"] = (
                observations.get("observations") or []
            )
        except ContinuityError as exc:
            base["errors"].append(
                {
                    "surface": "item_subject",
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                }
            )
            continue
        base["records"].append(record)

    definition_records: dict[str, dict[str, Any]] = {}
    for definition_id in sorted(selected_definitions):
        try:
            definition_payload = service.query_item_definition(
                definition_id
            )
        except ContinuityError as exc:
            base["errors"].append(
                {
                    "surface": "query_item_definition",
                    "item_definition_id": definition_id,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                }
            )
            continue
        functions = []
        for function in (
            definition_payload.get("functions") or []
        )[:_ITEM_CONTEXT_MAX_FUNCTIONS_PER_SUBJECT]:
            function_id = str(function.get("function_id") or "")
            if not function_id:
                continue
            matching_subject = next(
                (
                    (
                        str(record.get("subject_type") or ""),
                        str(record.get("subject_id") or ""),
                    )
                    for record in base["records"]
                    if str(
                        (record.get("definition") or {}).get(
                            "item_definition_id"
                        )
                        or ""
                    )
                    == definition_id
                    and str(record.get("subject_type") or "")
                    in {"item_instance", "item_stack"}
                ),
                None,
            )
            query_kwargs: dict[str, str] = {}
            if matching_subject is not None:
                if matching_subject[0] == "item_instance":
                    query_kwargs["item_instance_id"] = matching_subject[1]
                elif matching_subject[0] == "item_stack":
                    query_kwargs["stack_id"] = matching_subject[1]
            try:
                functions.append(
                    service.query_item_function(
                        function_id,
                        **query_kwargs,
                    )
                )
            except ContinuityError as exc:
                base["errors"].append(
                    {
                        "surface": "query_item_function",
                        "function_id": function_id,
                        "error_type": type(exc).__name__,
                        "reason": str(exc),
                    }
                )
        definition_records[definition_id] = {
            "definition": definition_payload.get("definition"),
            "bindings": definition_payload.get("bindings") or [],
            "functions": functions,
        }

    for record in base["records"]:
        definition = record.get("definition") or {}
        definition_id = str(
            definition.get("item_definition_id") or ""
        )
        extra = definition_records.get(definition_id)
        if extra:
            record["definition"] = extra["definition"]
            record["bindings"] = extra["bindings"]
            record["functions"] = extra["functions"]
    existing_definition_ids = {
        str((record.get("definition") or {}).get("item_definition_id") or "")
        for record in base["records"]
    }
    for definition_id, record in sorted(definition_records.items()):
        if definition_id in existing_definition_ids:
            continue
        base["records"].append(
            {
                "subject_type": "item_definition",
                "subject_id": definition_id,
                **record,
            }
        )

    base["selected_subjects"] = [
        {
            "subject_type": str(record.get("subject_type") or ""),
            "subject_id": str(record.get("subject_id") or ""),
        }
        for record in base["records"]
    ]
    if base["errors"]:
        base["status"] = "degraded"
    elif not base["records"] and not base["inventories"]:
        base["status"] = "empty"
    base["accepted_record_count"] = len(base["records"])
    base["accepted_inventory_count"] = sum(
        len(inventory.get(facet) or [])
        for inventory in base["inventories"]
        for facet in (
            "owned",
            "custodied",
            "carried",
            "stored",
            "equipped",
            "legacy_inventory",
        )
    )
    base["context_text"] = _render_item_context(base)
    return base


def _exact_state_cache_path(root: Path) -> Path:
    return root / ".plot-rag" / _EXACT_STATE_CACHE_FILENAME


def _ensure_exact_state_cache(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS exact_state_cache (
            cache_key TEXT PRIMARY KEY,
            active_canon_revision INTEGER NOT NULL,
            active_projection_hash TEXT NOT NULL,
            item_projection_hash TEXT NOT NULL,
            branch_id TEXT NOT NULL,
            chapter_no INTEGER,
            scene_index INTEGER,
            atomic_needs_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_accessed_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_exact_state_cache_access
        ON exact_state_cache(last_accessed_at, cache_key)
        """
    )
    connection.execute(
        f"PRAGMA user_version={_EXACT_STATE_CACHE_SCHEMA_VERSION}"
    )


def _load_exact_state_cache(
    root: Path,
    cache_key: str,
) -> tuple[dict[str, Any] | None, str]:
    path = _exact_state_cache_path(root)
    if not path.is_file():
        return None, "miss"
    try:
        with closing(sqlite3.connect(path, timeout=5.0)) as connection:
            connection.row_factory = sqlite3.Row
            _ensure_exact_state_cache(connection)
            row = connection.execute(
                """
                SELECT payload_json
                FROM exact_state_cache
                WHERE cache_key=?
                """,
                (cache_key,),
            ).fetchone()
            if row is None:
                return None, "miss"
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, Mapping):
                return None, "corrupt"
            connection.execute(
                """
                UPDATE exact_state_cache
                SET last_accessed_at=?
                WHERE cache_key=?
                """,
                (_utc_now(), cache_key),
            )
            connection.commit()
            return dict(payload), "hit"
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return None, "error"


def _store_exact_state_cache(
    root: Path,
    cache_key: str,
    identity: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> str:
    path = _exact_state_cache_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        now = _utc_now()
        with closing(sqlite3.connect(path, timeout=5.0)) as connection:
            _ensure_exact_state_cache(connection)
            connection.execute(
                """
                INSERT INTO exact_state_cache(
                    cache_key, active_canon_revision,
                    active_projection_hash, item_projection_hash,
                    branch_id, chapter_no, scene_index,
                    atomic_needs_hash, payload_json,
                    created_at, last_accessed_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    last_accessed_at=excluded.last_accessed_at
                """,
                (
                    cache_key,
                    int(identity["active_canon_revision"]),
                    str(identity["active_projection_hash"]),
                    str(identity["item_projection_hash"]),
                    str(identity["branch_id"]),
                    identity.get("chapter_no"),
                    identity.get("scene_index"),
                    str(identity["atomic_needs_hash"]),
                    _canonical_json(payload),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                DELETE FROM exact_state_cache
                WHERE cache_key IN (
                    SELECT cache_key
                    FROM exact_state_cache
                    ORDER BY last_accessed_at DESC, cache_key DESC
                    LIMIT -1 OFFSET 512
                )
                """
            )
            connection.commit()
        return "stored"
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return "error"


def _exact_state_authority_health(
    index_result: Mapping[str, Any],
) -> str:
    if str(index_result.get("status") or "") != "ready":
        return "degraded"
    schema = dict(index_result.get("schema") or {})
    if int(schema.get("chunk_count") or 0) <= 0:
        return "empty"
    return "ready"


def _exact_state_decision(
    root: Path,
    prompt: str,
    *,
    context: Mapping[str, Any],
    accepted_identity: Mapping[str, Any],
    index_result: Mapping[str, Any],
    precise: Mapping[str, Any],
    open_loops: Mapping[str, Any],
    power_state: Mapping[str, Any],
    item_context: Mapping[str, Any],
    entity_resolution: Mapping[str, Any],
    enabled: bool,
    persistent_cache: bool,
) -> dict[str, Any]:
    needs = decompose_continuity_needs(prompt)
    atomic_needs = [
        {
            "category": need.category,
            "query": need.query,
            "mandatory": need.mandatory,
        }
        for need in needs
    ]
    atomic_needs_hash = _sha256(_canonical_json(atomic_needs))
    authority_health = _exact_state_authority_health(index_result)
    base = {
        "enabled": enabled,
        "persistent_cache": persistent_cache,
        "cache_status": "disabled",
        "decision": "MISS_UNCONFIRMED",
        "miss_confirmed": False,
        "authority_health": authority_health,
        "atomic_needs_hash": atomic_needs_hash,
        "skipped_need_indices": [],
        "satisfied_counts": {},
        "evidence_request": False,
        "entity_resolution": dict(entity_resolution),
    }
    if not enabled:
        base["reason"] = "exact_state_short_circuit_disabled"
        return base
    if _contains_exact_evidence_request(prompt):
        base["evidence_request"] = True
        base["cache_status"] = "bypassed"
        base["reason"] = "source_evidence_requires_authority"
        return base
    if entity_resolution.get("status") == "ambiguous":
        base["cache_status"] = "bypassed"
        base["reason"] = "entity_resolution_ambiguous"
        return base

    item_projection_hash = str(
        item_context.get("item_projection_hash") or ""
    )
    cache_identity = {
        "algorithm_version": _EXACT_STATE_CACHE_ALGORITHM_VERSION,
        "head_canon_revision": int(
            accepted_identity["head_canon_revision"]
        ),
        "active_canon_revision": int(
            accepted_identity["active_canon_revision"]
        ),
        "active_projection_hash": str(
            accepted_identity["active_projection_hash"]
        ),
        "item_projection_hash": item_projection_hash,
        "branch_id": str(context.get("branch_id") or "main"),
        "chapter_no": context.get("chapter_no"),
        "scene_index": context.get("scene_index"),
        "atomic_needs_hash": atomic_needs_hash,
    }
    cache_key = _sha256(_canonical_json(cache_identity))
    base["cache_key"] = cache_key
    if persistent_cache:
        cached, cache_status = _load_exact_state_cache(root, cache_key)
        base["cache_status"] = cache_status
        if cached is not None:
            skipped = [
                int(index)
                for index in cached.get("skipped_need_indices") or []
                if 0 <= int(index) < len(needs)
            ]
            satisfied = {
                str(key): max(0, int(value))
                for key, value in dict(
                    cached.get("satisfied_counts") or {}
                ).items()
            }
            if skipped:
                return {
                    **base,
                    "decision": "HIT_CONFIRMED",
                    "skipped_need_indices": skipped,
                    "satisfied_counts": satisfied,
                    "cache_status": "hit",
                    "reason": "projection_bound_exact_cache_hit",
                }

    entity_ids = set(entity_resolution.get("entity_ids") or [])
    accepted_facts = [
        dict(fact)
        for fact in precise.get("facts") or []
        if isinstance(fact, Mapping)
        and not _contains_unresolved_state(fact)
        and _record_matches_entities(fact, entity_ids)
    ]
    accepted_loops = [
        dict(fact)
        for fact in open_loops.get("facts") or []
        if isinstance(fact, Mapping)
        and not _contains_unresolved_state(fact)
        and _record_matches_entities(fact, entity_ids)
    ]
    counts: dict[str, int] = {}
    for category, fact_types in _EXACT_STATE_CATEGORY_FACT_TYPES.items():
        source = accepted_loops if category == "open_loop" else accepted_facts
        matched = []
        for fact in source:
            fact_type = str(fact.get("fact_type") or "")
            if category == "story_time":
                if fact_type != "time" and fact.get("story_time") in {
                    None,
                    "",
                }:
                    continue
            elif fact_type not in fact_types:
                continue
            matched.append(
                str(fact.get("source_event_id") or fact.get("fact_key") or "")
            )
        counts[category] = len({value for value in matched if value})

    power_healthy = (
        str(power_state.get("status") or "ready").casefold()
        not in {
            "degraded",
            "failed",
            "uninitialized",
            "unresolved",
            "ambiguous",
        }
        and not power_state.get("unknown_or_conflicted")
        and not _contains_unresolved_state(
            power_state.get("unknown_or_conflicted") or []
        )
    )
    for category, sections in _POWER_NEED_SECTIONS.items():
        records = [
            dict(item)
            for section in sections
            for item in power_state.get(section) or []
            if isinstance(item, Mapping)
            and not _contains_unresolved_state(item)
            and _record_matches_entities(item, entity_ids)
        ]
        counts[category] = len(records) if power_healthy else 0

    if item_context.get("status") == "ready":
        typed_item_count = int(
            item_context.get("accepted_record_count") or 0
        )
        inventory_count = int(
            item_context.get("accepted_inventory_count") or 0
        )
        counts["inventory"] = max(
            int(counts.get("inventory") or 0),
            typed_item_count,
            inventory_count,
        )

    skipped: list[int] = []
    satisfied: dict[str, int] = {}
    for need_index, need in enumerate(needs):
        required = int(
            _EXACT_STATE_REQUIRED_COUNTS.get(need.category, 1)
        )
        available = int(counts.get(need.category) or 0)
        if available < required:
            continue
        skipped.append(need_index)
        satisfied[need.category] = max(
            int(satisfied.get(need.category) or 0),
            min(available, required),
        )
    if skipped:
        satisfied["accepted_authority"] = 1
        payload = {
            "skipped_need_indices": skipped,
            "satisfied_counts": satisfied,
        }
        store_status = (
            _store_exact_state_cache(
                root,
                cache_key,
                cache_identity,
                payload,
            )
            if persistent_cache
            else "disabled"
        )
        return {
            **base,
            "decision": "HIT_CONFIRMED",
            "cache_status": (
                "stored" if store_status == "stored" else store_status
            ),
            "skipped_need_indices": skipped,
            "satisfied_counts": satisfied,
            "reason": "accepted_projection_exact_match",
        }
    if persistent_cache and base["cache_status"] == "disabled":
        base["cache_status"] = "miss"
    base["reason"] = (
        "exact_state_insufficient_authority_empty"
        if authority_health == "empty"
        else "exact_state_insufficient_authority_degraded"
        if authority_health == "degraded"
        else "exact_state_insufficient"
    )
    return base


def _truncate_context_text(text: str, max_chars: int) -> tuple[str, bool]:
    """Fit human-readable context without exceeding a hard character limit."""

    limit = max(0, int(max_chars))
    if len(text) <= limit:
        return text, False
    if limit <= 0:
        return "", bool(text)
    if limit == 1:
        return "…", True
    lines: list[str] = []
    used = 0
    for raw_line in text.splitlines():
        separator = 1 if lines else 0
        remaining = limit - used - separator
        if remaining <= 0:
            break
        if len(raw_line) <= remaining:
            lines.append(raw_line)
            used += separator + len(raw_line)
            continue
        if remaining == 1:
            lines.append("…")
        else:
            lines.append(raw_line[: remaining - 1].rstrip() + "…")
        used += separator + len(lines[-1])
        break
    return "\n".join(lines), True


def _context_section_text(
    heading: str,
    body: str,
    *,
    intro: str = "",
    max_chars: int | None = None,
) -> tuple[str, dict[str, Any]]:
    components = [heading]
    if intro:
        components.append(intro)
    prefix = "\n".join(components)
    full = prefix + "\n" + body
    requested = len(full)
    if max_chars is None or requested <= max_chars:
        return full, {
            "requested_chars": requested,
            "included_chars": requested,
            "included": True,
            "truncated": False,
        }
    available = max(0, int(max_chars))
    body_budget = available - len(prefix) - 1
    if body_budget <= 0:
        return "", {
            "requested_chars": requested,
            "included_chars": 0,
            "included": False,
            "truncated": True,
        }
    fitted_body, truncated = _truncate_context_text(body, body_budget)
    fitted = prefix + "\n" + fitted_body
    return fitted, {
        "requested_chars": requested,
        "included_chars": len(fitted),
        "included": True,
        "truncated": truncated,
    }


def _append_context_section(
    parts: list[str],
    section_budget: dict[str, dict[str, Any]],
    *,
    total_budget: int,
    key: str,
    heading: str,
    body: str,
    intro: str = "",
    protected_chars: int = 0,
) -> None:
    if not body:
        section_budget[key] = {
            "requested_chars": 0,
            "included_chars": 0,
            "included": False,
            "truncated": False,
        }
        return
    used = sum(len(part) for part in parts) + max(0, len(parts) - 1)
    separator = 1 if parts else 0
    available = max(
        0,
        int(total_budget)
        - used
        - separator
        - max(0, int(protected_chars)),
    )
    fitted, metadata = _context_section_text(
        heading,
        body,
        intro=intro,
        max_chars=available,
    )
    section_budget[key] = metadata
    if fitted:
        parts.append(fitted)


def _context_section_reserve(
    heading: str,
    body: str,
    *,
    intro: str = "",
    target_chars: int,
) -> int:
    if not body:
        return 0
    full, _ = _context_section_text(heading, body, intro=intro)
    prefix_chars = len(heading) + (len(intro) + 1 if intro else 0) + 2
    return min(len(full), max(prefix_chars, int(target_chars)))


def _longform_query_observation(
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    authority_candidates = [
        item
        for section in (contract.get("sections") or {}).values()
        for item in section
        if isinstance(item, Mapping) and item.get("retrieval_mode")
    ]

    def distinct(key: str) -> list[str]:
        return sorted(
            {
                str(item[key])
                for item in authority_candidates
                if item.get(key) not in {None, ""}
            }
        )

    return {
        "authority_candidate_count": len(authority_candidates),
        "vector_candidate_count": sum(
            item.get("vector_score") is not None
            for item in authority_candidates
        ),
        "reranked_candidate_count": sum(
            str(item.get("retrieval_mode") or "").startswith("reranked_")
            for item in authority_candidates
        ),
        "retrieval_modes": distinct("retrieval_mode"),
        "embedding_statuses": distinct("embedding_status"),
        "rerank_statuses": distinct("rerank_status"),
    }


_PREPARE_V2_SEMANTIC_EXCLUDED_FIELDS = frozenset(
    {
        "base_score",
        "candidate_cache_hit",
        "score",
        "retrieval_mode",
        "embedding_status",
        "semantic_score",
        "vector_score",
        "rerank_status",
        "rerank_score",
        "rerank_rank",
        "bm25_score",
        "lexical_score",
        "combined_score",
    }
)


def _prepare_v2_semantic_sections(
    contract: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    sections = contract.get("sections") or {}
    if not isinstance(sections, Mapping):
        return {}
    return {
        str(category): [
            {
                str(key): value
                for key, value in dict(item).items()
                if str(key) not in _PREPARE_V2_SEMANTIC_EXCLUDED_FIELDS
            }
            for item in values
            if isinstance(item, Mapping)
        ]
        for category, values in sections.items()
        if isinstance(values, Sequence)
        and not isinstance(values, (str, bytes, bytearray))
    }


def _prepare_v2_selected_chunks(
    contract: Mapping[str, Any],
) -> list[list[dict[str, Any]]]:
    needs = list(contract.get("needs") or [])
    selected: list[list[dict[str, Any]]] = [
        [] for _need in needs
    ]
    for values in (contract.get("sections") or {}).values():
        if not isinstance(values, Sequence) or isinstance(
            values,
            (str, bytes, bytearray),
        ):
            continue
        for item in values:
            if not isinstance(item, Mapping) or not item.get("chunk_id"):
                continue
            try:
                need_index = int(item.get("need_index"))
            except (TypeError, ValueError):
                continue
            if need_index < 0 or need_index >= len(selected):
                continue
            selected[need_index].append(
                {
                    "chunk_id": str(item.get("chunk_id") or ""),
                    "path": str(item.get("path") or ""),
                    "ordinal": int(item.get("ordinal") or 0),
                    "start_line": int(item.get("start_line") or 0),
                    "end_line": int(item.get("end_line") or 0),
                    "content_sha256": str(
                        item.get("content_sha256") or ""
                    ),
                    "role": str(item.get("role") or ""),
                    "scope_policy": str(
                        item.get("scope_policy") or ""
                    ),
                }
            )
    return selected


def _prepare_v2_contract_observation(
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    needs = list(contract.get("needs") or [])
    selected_chunks = _prepare_v2_selected_chunks(contract)
    sections = _prepare_v2_semantic_sections(contract)
    missing_mandatory = sorted(
        str(item)
        for item in (contract.get("missing_mandatory") or [])
    )
    mandatory_shortfall = {
        str(key): int(value)
        for key, value in dict(
            contract.get("mandatory_shortfall") or {}
        ).items()
    }
    semantic_payload = {
        "contract_version": contract.get("contract_version"),
        "task": contract.get("task"),
        "needs": needs,
        "mandatory_quotas": dict(
            contract.get("mandatory_quotas") or {}
        ),
        "missing_mandatory": missing_mandatory,
        "mandatory_shortfall": mandatory_shortfall,
        "accepted_authority_selected": int(
            contract.get("accepted_authority_selected") or 0
        ),
        "sections": sections,
        "context_text": str(contract.get("context_text") or ""),
    }
    return {
        "needs": needs,
        "selected_chunks": selected_chunks,
        "sections": sections,
        "missing_mandatory": missing_mandatory,
        "mandatory_shortfall": mandatory_shortfall,
        "semantic_hash": _sha256(_canonical_json(semantic_payload)),
        "context_hash": _sha256(
            str(contract.get("context_text") or "")
        ),
        "needs_hash": _sha256(_canonical_json(needs)),
        "selected_chunks_hash": _sha256(
            _canonical_json(selected_chunks)
        ),
    }


def _prepare_v2_path_record(
    contract: Mapping[str, Any],
    *,
    wall_ms: float,
) -> dict[str, Any]:
    observation = _prepare_v2_contract_observation(contract)
    return {
        "status": "ok",
        "wall_ms": round(float(wall_ms), 3),
        "semantic_hash": observation["semantic_hash"],
        "context_hash": observation["context_hash"],
        "needs_hash": observation["needs_hash"],
        "selected_chunks_hash": observation[
            "selected_chunks_hash"
        ],
        "need_count": len(observation["needs"]),
        "selected_chunk_count": sum(
            len(items)
            for items in observation["selected_chunks"]
        ),
    }


def _prepare_v2_comparison(
    v1_contract: Mapping[str, Any],
    v2_contract: Mapping[str, Any],
) -> dict[str, Any]:
    v1 = _prepare_v2_contract_observation(v1_contract)
    v2 = _prepare_v2_contract_observation(v2_contract)
    max_need_count = max(
        len(v1["selected_chunks"]),
        len(v2["selected_chunks"]),
    )
    mismatched_need_indices = [
        index
        for index in range(max_need_count)
        if (
            (
                v1["selected_chunks"][index]
                if index < len(v1["selected_chunks"])
                else []
            )
            != (
                v2["selected_chunks"][index]
                if index < len(v2["selected_chunks"])
                else []
            )
        )
    ]
    mismatch_categories = sorted(
        {
            str(need.get("category") or "unknown")
            for index in mismatched_need_indices
            for need in (
                (
                    v1["needs"][index]
                    if index < len(v1["needs"])
                    else None
                ),
                (
                    v2["needs"][index]
                    if index < len(v2["needs"])
                    else None
                ),
            )
            if isinstance(need, Mapping)
        }
    )
    needs_equivalent = v1["needs"] == v2["needs"]
    selected_chunks_equivalent = (
        v1["selected_chunks"] == v2["selected_chunks"]
    )
    sections_equivalent = v1["sections"] == v2["sections"]
    context_text_equivalent = (
        str(v1_contract.get("context_text") or "")
        == str(v2_contract.get("context_text") or "")
    )
    missing_mandatory_equivalent = (
        v1["missing_mandatory"] == v2["missing_mandatory"]
    )
    mandatory_shortfall_equivalent = (
        v1["mandatory_shortfall"] == v2["mandatory_shortfall"]
    )
    semantic_equivalent = (
        v1["semantic_hash"] == v2["semantic_hash"]
    )
    context_hash_equivalent = (
        v1["context_hash"] == v2["context_hash"]
    )
    if not needs_equivalent:
        mismatch_categories.append("needs")
    if not sections_equivalent:
        mismatch_categories.append("sections")
    if not context_text_equivalent:
        mismatch_categories.append("context_text")
    if not missing_mandatory_equivalent:
        mismatch_categories.append("missing_mandatory")
    if not mandatory_shortfall_equivalent:
        mismatch_categories.append("mandatory_shortfall")
    mismatch_categories = sorted(set(mismatch_categories))
    equivalent = all(
        (
            needs_equivalent,
            selected_chunks_equivalent,
            sections_equivalent,
            context_text_equivalent,
            missing_mandatory_equivalent,
            mandatory_shortfall_equivalent,
            semantic_equivalent,
            context_hash_equivalent,
        )
    )
    return {
        "status": "equivalent" if equivalent else "mismatch",
        "equivalent": equivalent,
        "needs_equivalent": needs_equivalent,
        "selected_chunks_equivalent": selected_chunks_equivalent,
        "sections_equivalent": sections_equivalent,
        "context_text_equivalent": context_text_equivalent,
        "missing_mandatory_equivalent": (
            missing_mandatory_equivalent
        ),
        "mandatory_shortfall_equivalent": (
            mandatory_shortfall_equivalent
        ),
        "semantic_equivalent": semantic_equivalent,
        "context_hash_equivalent": context_hash_equivalent,
        "mismatched_need_indices": mismatched_need_indices,
        "mismatch_categories": mismatch_categories,
    }


def _build_longform_context_once(
    project_root: Path | str,
    prompt: str,
    *,
    artifact_context: Mapping[str, Any] | None = None,
    max_context_chars: int | None = None,
    accepted_identity: Mapping[str, Any],
) -> dict[str, Any]:
    longform_started = time.perf_counter()
    root = Path(project_root).expanduser().resolve()
    config = load_config(root)
    context = dict(artifact_context or infer_artifact_context(prompt))
    lifecycle = dict(config.get("lifecycle") or {})
    prepare_performance = dict(
        (config.get("performance") or {}).get("prepare_v2") or {}
    )
    prepare_v2_enabled = bool(
        prepare_performance.get("enabled", False)
    )
    prepare_v2_shadow = bool(
        prepare_performance.get("shadow", True)
    )
    run_prepare_v1 = prepare_v2_shadow or not prepare_v2_enabled
    run_prepare_v2 = prepare_v2_shadow or prepare_v2_enabled
    chosen_prepare_path = (
        "v1"
        if prepare_v2_shadow or not prepare_v2_enabled
        else "v2"
    )
    configured_budget = (
        max_context_chars
        if max_context_chars is not None
        else lifecycle.get("longform_context_chars")
        or min(7000, max(2200, int(config["state"]["max_context_chars"]) // 2))
    )
    budget = max(1, int(configured_budget))
    service = ContinuityService(root)
    index_embeddings = bool(
        lifecycle.get("index_embeddings_on_prepare", False)
    )
    query_embeddings = bool(
        (config.get("craft") or {}).get("use_embedding", True)
    )
    use_rerank = bool(
        (config.get("craft") or {}).get("use_rerank", True)
    )
    refresh_started = time.perf_counter()
    index_result = refresh_longform_index(
        root,
        with_embeddings=index_embeddings,
    )
    authority_refresh_ms = round(
        (time.perf_counter() - refresh_started) * 1000.0,
        3,
    )
    authority_indexes: dict[str, AuthorityIndex] = {}
    if run_prepare_v1:
        authority_indexes["v1"] = _authority_index(
            root,
            with_embeddings=query_embeddings,
            with_rerank=use_rerank,
            prepare_v2_enabled=False,
        )
    if run_prepare_v2:
        authority_indexes["v2"] = _authority_index(
            root,
            with_embeddings=query_embeddings,
            with_rerank=use_rerank,
            prepare_v2_enabled=True,
        )
    index = authority_indexes[chosen_prepare_path]
    longform_database = _longform_database_path(root)
    memory = LayeredMemoryStore(longform_database)
    summaries = AcceptedSummaryStore(longform_database)
    exact_state_started = time.perf_counter()
    precise = service.query_facts(
        chapter_no=context.get("chapter_no"),
        scene_index=context.get("scene_index"),
        include_timeless=True,
        include_historical=False,
        branch_id=(
            str(context.get("branch_id"))
            if str(context.get("branch_id") or "main") != "main"
            else None
        ),
        include_provisional=str(context.get("branch_id") or "main") != "main",
    )
    planned = service.query_facts(
        scope="planned",
        chapter_no=context.get("chapter_no"),
        scene_index=context.get("scene_index"),
        include_timeless=False,
        include_historical=False,
    )
    historical = service.query_facts(
        scope="historical",
        chapter_no=context.get("chapter_no"),
        scene_index=context.get("scene_index"),
        include_timeless=False,
        include_historical=True,
    )
    open_loops = service.query_facts(
        fact_type="open_loop",
        chapter_no=context.get("chapter_no"),
        scene_index=context.get("scene_index"),
        include_timeless=False,
    )
    power_state = query_power_state(
        root,
        chapter_no=context.get("chapter_no"),
        scene_index=context.get("scene_index"),
        branch_id=(
            str(context.get("branch_id"))
            if str(context.get("branch_id") or "main") != "main"
            else None
        ),
        include_provisional=str(context.get("branch_id") or "main") != "main",
    )
    entity_resolution = _accepted_entity_mentions(service, prompt)
    if context.get("owner_entity_id") is not None:
        entity_resolution["owner_entity_id"] = str(
            context.get("owner_entity_id") or ""
        )
    if context.get("pov_entity_id") is not None:
        entity_resolution["pov_entity_id"] = str(
            context.get("pov_entity_id") or ""
        )
    # Use only an accepted project coordinate.  Chapter/scene fields are
    # presentation hints and do not define a comparable timeline by
    # themselves; falling back to ``chapter_scene`` would hide valid runtime
    # rows from projects whose calendars are named ``main``/``generic``.
    advantage_story_cursor = _normalize_advantage_story_cursor(
        context.get("story_coordinate")
    )
    if not advantage_story_cursor:
        advantage_story_cursor = _normalize_advantage_story_cursor(
            context.get("story_cursor")
        )
    historical_advantage_query = _advantage_historical_query_requested(context)
    advantage_context_started = time.perf_counter()
    advantage_context = _build_advantage_context(
        service,
        prompt,
        entity_resolution=entity_resolution,
        branch_id=str(context.get("branch_id") or "main"),
        story_cursor=advantage_story_cursor,
        chapter_no=(
            int(context["chapter_no"])
            if context.get("chapter_no") is not None
            else None
        ),
        scene_index=(
            int(context["scene_index"])
            if context.get("scene_index") is not None
            else None
        ),
        policy={
            **dict(config.get("advantage") or {}),
            "_historical_query": historical_advantage_query,
            "_require_story_cursor": historical_advantage_query,
        },
    )
    advantage_context_ms = round(
        (time.perf_counter() - advantage_context_started) * 1000.0,
        3,
    )
    item_context_started = time.perf_counter()
    item_context = _build_item_context(
        service,
        prompt,
        entity_resolution=entity_resolution,
    )
    item_context_ms = round(
        (time.perf_counter() - item_context_started) * 1000.0,
        3,
    )
    exact_state = _exact_state_decision(
        root,
        prompt,
        context=context,
        accepted_identity=accepted_identity,
        index_result=index_result,
        precise=precise,
        open_loops=open_loops,
        power_state=power_state,
        item_context=item_context,
        entity_resolution=entity_resolution,
        enabled=bool(
            prepare_performance.get(
                "exact_state_short_circuit",
                True,
            )
        ),
        persistent_cache=bool(
            prepare_performance.get(
                "persistent_exact_cache",
                True,
            )
        ),
    )
    exact_state_ms = round(
        (time.perf_counter() - exact_state_started) * 1000.0,
        3,
    )
    context_assembly_started = time.perf_counter()
    precise_text = _render_precise_facts(precise.get("facts") or [])
    planned_text = _render_precise_facts(planned.get("facts") or [], limit=18)
    historical_text = _render_precise_facts(
        historical.get("facts") or [],
        limit=18,
    )
    loop_text = _render_precise_facts(open_loops.get("facts") or [], limit=12)
    power_text = _render_power_state(power_state)
    advantage_text = str(advantage_context.get("context_text") or "")
    item_text = str(item_context.get("context_text") or "")
    envelope_head = "\n".join(
        [
            "[WEBNOVEL_CONTINUITY_CONTRACT]",
            (
                "prepared_canon_revision: "
                f"{int(accepted_identity['active_canon_revision'])}"
            ),
            (
                "active_projection_hash: "
                f"{accepted_identity['active_projection_hash']}"
            ),
            f"artifact_stage: {context.get('artifact_stage')}",
            f"branch_id: {context.get('branch_id')}",
            f"chapter_no: {context.get('chapter_no')}",
            f"scene_index: {context.get('scene_index')}",
            (
                "必须优先服从 accepted 精确投影；branch provisional、planned、"
                "historical 与 current 不得混写。"
            ),
        ]
    )
    envelope_footer = "\n".join(
        [
            "本轮生成完成后只形成 proposal；任何 current/timeless 晋升都需要一次性 approval grant 与 canon CAS。",
            "[/WEBNOVEL_CONTINUITY_CONTRACT]",
        ]
    )
    opening_marker = "[WEBNOVEL_CONTINUITY_CONTRACT]"
    closing_marker = "[/WEBNOVEL_CONTINUITY_CONTRACT]"
    compact_envelope = len(envelope_head) + len(envelope_footer) + 1 > budget
    if compact_envelope:
        envelope_head = opening_marker
        envelope_footer = closing_marker
    middle_budget = max(
        0,
        budget - len(envelope_head) - len(envelope_footer) - 2,
    )

    contract_heading = "[LONGFORM_AUTHORITY_AND_MEMORY]"
    contract_section_target = min(3000, max(0, middle_budget // 4))
    contract_content_budget = max(
        1,
        contract_section_target - len(contract_heading) - 1,
    )
    contract_kwargs = {
        "task": str(context.get("task") or "prose"),
        "max_context_chars": contract_content_budget,
        "branch_id": str(context.get("branch_id") or "main"),
        "chapter_no": (
            int(context["chapter_no"])
            if context.get("chapter_no") is not None
            else None
        ),
        "arc_id": (
            str(context["arc_id"])
            if context.get("arc_id") is not None
            else None
        ),
        "volume_id": (
            str(context["volume_id"])
            if context.get("volume_id") is not None
            else None
        ),
        "skip_authority_need_indices": list(
            exact_state.get("skipped_need_indices") or []
        ),
        "exact_state_satisfied_counts": dict(
            exact_state.get("satisfied_counts") or {}
        ),
    }
    prepare_contracts: dict[str, dict[str, Any]] = {}
    prepare_path_records: dict[str, dict[str, Any]] = {}

    def build_prepare_contract(path: str) -> None:
        started = time.perf_counter()
        try:
            built = ContextContractBuilder(
                authority_indexes[path],
                memory_store=memory,
                summary_store=summaries,
            ).build(
                prompt,
                **contract_kwargs,
                search_mode=("legacy" if path == "v1" else "v2"),
                use_candidate_cache=(
                    True
                    if path == "v1"
                    else not prepare_v2_shadow
                ),
            )
        except Exception as exc:
            prepare_path_records[path] = {
                "status": "error",
                "wall_ms": round(
                    (time.perf_counter() - started) * 1000.0,
                    3,
                ),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            if path == chosen_prepare_path:
                raise
            return
        wall_ms = (time.perf_counter() - started) * 1000.0
        prepare_contracts[path] = built
        prepare_path_records[path] = _prepare_v2_path_record(
            built,
            wall_ms=wall_ms,
        )

    if run_prepare_v1:
        build_prepare_contract("v1")
    if run_prepare_v2:
        build_prepare_contract("v2")
    contract = prepare_contracts[chosen_prepare_path]
    contract["exact_state_short_circuit"] = {
        **dict(contract.get("exact_state_short_circuit") or {}),
        **exact_state,
    }
    authority_contract_ms = float(
        prepare_path_records[chosen_prepare_path]["wall_ms"]
    )

    if "v1" in prepare_contracts and "v2" in prepare_contracts:
        prepare_comparison = _prepare_v2_comparison(
            prepare_contracts["v1"],
            prepare_contracts["v2"],
        )
    elif run_prepare_v1 and run_prepare_v2:
        failed_path = next(
            (
                path
                for path in ("v1", "v2")
                if prepare_path_records.get(path, {}).get("status")
                == "error"
            ),
            "unknown",
        )
        prepare_comparison = {
            "status": f"{failed_path}_error",
            "equivalent": False,
            "needs_equivalent": None,
            "selected_chunks_equivalent": None,
            "sections_equivalent": None,
            "context_text_equivalent": None,
            "missing_mandatory_equivalent": None,
            "mandatory_shortfall_equivalent": None,
            "semantic_equivalent": None,
            "context_hash_equivalent": None,
            "mismatched_need_indices": list(
                range(len(contract.get("needs") or []))
            ),
            "mismatch_categories": ["execution_error"],
        }
    else:
        prepare_comparison = {
            "status": "not_compared",
            "equivalent": None,
            "needs_equivalent": None,
            "selected_chunks_equivalent": None,
            "sections_equivalent": None,
            "context_text_equivalent": None,
            "missing_mandatory_equivalent": None,
            "mandatory_shortfall_equivalent": None,
            "semantic_equivalent": None,
            "context_hash_equivalent": None,
            "mismatched_need_indices": [],
            "mismatch_categories": [],
        }
    prepare_v2_rollout = {
        "enabled": prepare_v2_enabled,
        "shadow": prepare_v2_shadow,
        "chosen_path": chosen_prepare_path,
        "executed_paths": [
            path
            for path in ("v1", "v2")
            if path in prepare_path_records
        ],
        "v1": prepare_path_records.get(
            "v1",
            {"status": "not_run", "wall_ms": 0.0},
        ),
        "v2": prepare_path_records.get(
            "v2",
            {"status": "not_run", "wall_ms": 0.0},
        ),
        "v1_wall_ms": float(
            prepare_path_records.get("v1", {}).get("wall_ms") or 0.0
        ),
        "v2_wall_ms": float(
            prepare_path_records.get("v2", {}).get("wall_ms") or 0.0
        ),
        "comparison": prepare_comparison,
    }
    contract["prepare_v2_rollout"] = prepare_v2_rollout
    query_schema = index.schema_info()
    index_result["prepare_refresh"] = {
        "embedding_generation_requested": index_embeddings,
        "schema": dict(index_result.get("schema") or {}),
    }
    index_result["query_policy"] = {
        "embedding_requested": query_embeddings,
        "embedding_enabled": bool(query_schema.get("embedding_enabled")),
        "embedding_model": query_schema.get("embedding_model"),
        "rerank_requested": use_rerank,
        "rerank_enabled": bool(query_schema.get("rerank_enabled")),
        "rerank_model": query_schema.get("rerank_model"),
        "chosen_path": chosen_prepare_path,
    }
    index_result["query_schema"] = query_schema
    index_result["query_observation"] = _longform_query_observation(contract)
    index_result["prepare_v2_rollout"] = prepare_v2_rollout
    index_result["exact_state_short_circuit"] = exact_state
    method_pack = WebnovelMethodPack()
    cards = method_pack.retrieve(
        prompt,
        artifact_stage=str(context.get("artifact_stage") or "brainstorm"),
        task=str(context.get("task") or "prose"),
        continuity_risks=[
            need["category"]
            for need in contract.get("needs") or []
            if need.get("mandatory")
        ],
        limit=4,
    )
    patterns = ProjectPatternStore(_longform_database_path(root)).query(
        prompt,
        task=str(context.get("task") or "prose"),
        limit=3,
    )

    power_intro = (
        "以下力量定义与运行态优先于相似正文片段；knowledge_plane "
        "决定角色是否可据此行动："
    )
    planned_intro = "以下内容只代表已验收的未来计划，不代表当前已经发生："
    historical_intro = (
        "以下内容只代表目标故事点之前的历史事实，不得覆盖当前状态："
    )
    contract_text = str(contract.get("context_text") or "")
    contract_section, _ = _context_section_text(
        contract_heading,
        contract_text,
    ) if contract_text else ("", {})
    advantage_reserve = _context_section_reserve(
        "[ACCEPTED_ADVANTAGE_CONTEXT]",
        advantage_text,
        target_chars=max(1024, middle_budget * 30 // 100),
    )
    has_advantage_context = bool(advantage_reserve)
    item_reserve = _context_section_reserve(
        "[ACCEPTED_ITEM_CONTEXT]",
        item_text,
        target_chars=(
            max(512, middle_budget * 16 // 100)
            if has_advantage_context
            else max(768, middle_budget * 28 // 100)
        ),
    )
    open_reserve = _context_section_reserve(
        "[ACTIVE_OPEN_LOOPS]",
        loop_text,
        target_chars=(
            max(192, middle_budget * 8 // 100)
            if has_advantage_context
            else max(256, middle_budget * 12 // 100)
        ),
    )
    power_reserve = _context_section_reserve(
        "[ACCEPTED_POWER_STATE]",
        power_text,
        intro=power_intro,
        target_chars=(
            max(384, middle_budget * 14 // 100)
            if has_advantage_context
            else max(512, middle_budget * 22 // 100)
        ),
    )
    contract_reserve = (
        min(
            len(contract_section),
            max(512, middle_budget * 20 // 100),
        )
        if has_advantage_context
        else len(contract_section)
    )
    middle_parts: list[str] = []
    section_budget: dict[str, dict[str, Any]] = {}

    future = [
        value
        for value in (
            advantage_reserve,
            item_reserve,
            open_reserve,
            power_reserve,
            contract_reserve,
        )
        if value
    ]
    _append_context_section(
        middle_parts,
        section_budget,
        total_budget=middle_budget,
        key="accepted_precise_state",
        heading="[ACCEPTED_PRECISE_STATE]",
        body=precise_text,
        protected_chars=sum(future) + len(future),
    )
    future = [
        value
        for value in (
            item_reserve,
            open_reserve,
            power_reserve,
            contract_reserve,
        )
        if value
    ]
    _append_context_section(
        middle_parts,
        section_budget,
        total_budget=middle_budget,
        key="accepted_advantage_context",
        heading="[ACCEPTED_ADVANTAGE_CONTEXT]",
        body=advantage_text,
        protected_chars=sum(future) + len(future),
    )
    future = [
        value
        for value in (open_reserve, power_reserve, contract_reserve)
        if value
    ]
    _append_context_section(
        middle_parts,
        section_budget,
        total_budget=middle_budget,
        key="accepted_item_context",
        heading="[ACCEPTED_ITEM_CONTEXT]",
        body=item_text,
        protected_chars=sum(future) + len(future),
    )
    future = [value for value in (power_reserve, contract_reserve) if value]
    _append_context_section(
        middle_parts,
        section_budget,
        total_budget=middle_budget,
        key="active_open_loops",
        heading="[ACTIVE_OPEN_LOOPS]",
        body=loop_text,
        protected_chars=sum(future) + len(future),
    )
    future = [value for value in (contract_reserve,) if value]
    _append_context_section(
        middle_parts,
        section_budget,
        total_budget=middle_budget,
        key="accepted_power_state",
        heading="[ACCEPTED_POWER_STATE]",
        body=power_text,
        intro=power_intro,
        protected_chars=sum(future) + len(future),
    )
    _append_context_section(
        middle_parts,
        section_budget,
        total_budget=middle_budget,
        key="authority_and_memory",
        heading=contract_heading,
        body=contract_text,
    )
    _append_context_section(
        middle_parts,
        section_budget,
        total_budget=middle_budget,
        key="accepted_planned_facts",
        heading="[ACCEPTED_PLANNED_FACTS]",
        body=planned_text,
        intro=planned_intro,
    )
    _append_context_section(
        middle_parts,
        section_budget,
        total_budget=middle_budget,
        key="accepted_historical_facts",
        heading="[ACCEPTED_HISTORICAL_FACTS]",
        body=historical_text,
        intro=historical_intro,
    )
    _append_context_section(
        middle_parts,
        section_budget,
        total_budget=middle_budget,
        key="method_guidance",
        heading="[WEBNOVEL_METHOD_GUIDANCE]",
        body=(
            method_pack.render_guidance(cards, expose_internal_checks=False)
            if cards
            else ""
        ),
    )
    _append_context_section(
        middle_parts,
        section_budget,
        total_budget=middle_budget,
        key="project_patterns",
        heading="[ACCEPTED_PROJECT_PATTERNS]",
        body="\n".join(
            str(item.get("pattern_text") or "") for item in patterns
        ),
    )

    boundary_only = opening_marker + "\n" + closing_marker
    if len(boundary_only) > budget:
        final_context, _ = _truncate_context_text(boundary_only, budget)
        boundary_complete = False
    else:
        final_context = "\n".join(
            [envelope_head, *middle_parts, envelope_footer]
        )
        boundary_complete = (
            final_context.startswith(opening_marker)
            and final_context.endswith(closing_marker)
        )
    context_chars = len(final_context)
    budget_metadata = {
        "max_context_chars": budget,
        "context_chars": context_chars,
        "remaining_chars": max(0, budget - context_chars),
        "within_budget": context_chars <= budget,
        "hard_limit_applied": True,
        "boundary_complete": boundary_complete,
        "compact_envelope": compact_envelope,
        "contract_content_quota": contract_content_budget,
        "advantage_context": {
            "required": bool(advantage_context.get("required")),
            "status": str(advantage_context.get("status") or ""),
            "reserved_chars": advantage_reserve,
            **dict(
                section_budget.get("accepted_advantage_context") or {}
            ),
        },
        "item_context": {
            "required": bool(item_context.get("required")),
            "status": str(item_context.get("status") or ""),
            "reserved_chars": item_reserve,
            **dict(section_budget.get("accepted_item_context") or {}),
        },
        "sections": section_budget,
    }
    advantage_context["budget"] = dict(
        budget_metadata["advantage_context"]
    )
    item_context["budget"] = dict(budget_metadata["item_context"])
    context_assembly_ms = round(
        (time.perf_counter() - context_assembly_started) * 1000.0,
        3,
    )
    retrieval_telemetry = dict(
        contract.get("retrieval_telemetry") or {}
    )
    telemetry = {
        "authority_refresh_ms": authority_refresh_ms,
        "exact_state_ms": exact_state_ms,
        "advantage_context_ms": advantage_context_ms,
        "item_context_ms": item_context_ms,
        "exact_state_cache_status": str(
            exact_state.get("cache_status") or ""
        ),
        "exact_state_skipped_need_count": len(
            exact_state.get("skipped_need_indices") or []
        ),
        "authority_contract_ms": authority_contract_ms,
        "embedding_batch_ms": float(
            retrieval_telemetry.get("embedding_batch_ms") or 0.0
        ),
        "rerank_wall_ms": float(
            retrieval_telemetry.get("rerank_wall_ms") or 0.0
        ),
        "rerank_sum_ms": float(
            retrieval_telemetry.get("rerank_sum_ms") or 0.0
        ),
        "context_assembly_ms": context_assembly_ms,
        "cache_hit_count": int(
            retrieval_telemetry.get("cache_hit_count") or 0
        ),
        "cache_miss_count": int(
            retrieval_telemetry.get("cache_miss_count") or 0
        ),
        "prepare_v2": prepare_v2_rollout,
        "longform_total_ms": round(
            (time.perf_counter() - longform_started) * 1000.0,
            3,
        ),
    }
    advantage_failure = _mandatory_advantage_failure(advantage_context)
    longform_status = "degraded" if advantage_failure else "ready"
    longform_result: dict[str, Any] = {
        "status": longform_status,
        "artifact_context": context,
        "canon_revisions": {
            "head": int(accepted_identity["head_canon_revision"]),
            "active": int(accepted_identity["active_canon_revision"]),
        },
        "active_projection_hash": str(
            accepted_identity["active_projection_hash"]
        ),
        "precise": precise,
        "planned": planned,
        "historical": historical,
        "open_loops": open_loops,
        "power_state": power_state,
        "advantage_context": advantage_context,
        "advantage_context_ms": advantage_context_ms,
        "item_context": item_context,
        "item_context_ms": item_context_ms,
        "exact_state": exact_state,
        "contract": contract,
        "method_cards": cards,
        "project_patterns": patterns,
        "index": index_result,
        "max_context_chars": budget,
        "context_chars": context_chars,
        "within_budget": context_chars <= budget,
        "context_budget": budget_metadata,
        "context": final_context,
        "telemetry": telemetry,
    }
    if advantage_failure:
        longform_result["reason"] = advantage_failure["reason"]
        longform_result["gate"] = {
            "action": "block",
            **advantage_failure,
        }
    return longform_result


def build_longform_context(
    project_root: Path | str,
    prompt: str,
    *,
    artifact_context: Mapping[str, Any] | None = None,
    max_context_chars: int | None = None,
    _accepted_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build accepted context and reject a mixed-revision assembly."""

    root = Path(project_root).expanduser().resolve()
    if _accepted_identity is not None:
        result = _build_longform_context_once(
            root,
            prompt,
            artifact_context=artifact_context,
            max_context_chars=max_context_chars,
            accepted_identity=_accepted_identity,
        )
        result.setdefault("telemetry", {})["identity_retries"] = 0
        return result

    service = ContinuityService(root)
    for attempt in range(_PREPARE_IDENTITY_MAX_ATTEMPTS):
        before = _active_continuity_identity(service)
        result = _build_longform_context_once(
            root,
            prompt,
            artifact_context=artifact_context,
            max_context_chars=max_context_chars,
            accepted_identity=before,
        )
        after = _active_continuity_identity(service)
        if (
            int(before["active_canon_revision"])
            == int(after["active_canon_revision"])
            and str(before["active_projection_hash"])
            == str(after["active_projection_hash"])
        ):
            result.setdefault("telemetry", {})[
                "identity_retries"
            ] = attempt
            return result
    raise ContinuityError(
        "PREPARE_IDENTITY_DRIFT",
        "accepted canon revision or projection changed during context assembly",
    )


def prepare_plot_turn(
    project_root: Path | str,
    prompt: str,
    *,
    request_id: str = "",
    session_id: str = "",
    turn_id: str = "",
    artifact_stage: str | None = None,
    branch_id: str | None = None,
    chapter_no: int | None = None,
    scene_index: int | None = None,
    artifact_id: str | None = None,
    task: str | None = None,
    lifecycle_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    normalized_lifecycle_identity = _normalize_lifecycle_identity(
        lifecycle_identity
    )
    if not is_strict_lifecycle(root):
        result = state_rag.prepare_turn(
            root,
            prompt,
            request_id=request_id,
            session_id=session_id,
            turn_id=turn_id,
        )
        result["lifecycle_mode"] = "legacy_commit"
        return result
    if (
        _event_experience_required(root)
        and not normalized_lifecycle_identity
    ):
        return {
            "status": "failed",
            "reason": (
                "EVENT_EXPERIENCE_BINDING_REQUIRED: a locked event "
                "experience manifest is required before Prepare"
            ),
            "recorded_events": [],
            "proposal_events": [],
            "receipt_created": False,
            "remote_called": False,
            "lifecycle_identity": {},
            "lifecycle_mode": "strict_proposal",
            "turn_status": "failed",
        }

    prepare_started = time.perf_counter()
    service = ContinuityService(root)
    artifact_context = infer_artifact_context(
        prompt,
        artifact_stage=artifact_stage,
        branch_id=branch_id,
                        chapter_no=None,
                        scene_index=None,
        artifact_id=artifact_id,
        task=task,
    )
    legacy_prepare_ms = 0.0
    longform_attempt_ms = 0.0
    identity_check_ms = 0.0
    last_legacy: dict[str, Any] | None = None
    last_bound_id = ""
    for attempt in range(_PREPARE_IDENTITY_MAX_ATTEMPTS):
        identity_started = time.perf_counter()
        before = _active_continuity_identity(service)
        identity_check_ms += (
            time.perf_counter() - identity_started
        ) * 1000.0
        _verify_lifecycle_identity(
            root,
            normalized_lifecycle_identity,
        )

        legacy_started = time.perf_counter()
        legacy = state_rag.prepare_turn(
            root,
            prompt,
            request_id=request_id,
            session_id=session_id,
            turn_id=turn_id,
            authority_preflight=False,
        )
        legacy_prepare_ms += (
            time.perf_counter() - legacy_started
        ) * 1000.0
        last_legacy = legacy
        if str(legacy.get("status") or "") in {
            "failed",
            "error",
            "disabled",
        }:
            legacy["lifecycle_mode"] = "strict_proposal"
            legacy["telemetry"] = {
                "state_prepare_ms": round(legacy_prepare_ms, 3),
                "identity_check_ms": round(identity_check_ms, 3),
                "identity_retries": attempt,
                "prepare_total_ms": round(
                    (time.perf_counter() - prepare_started) * 1000.0,
                    3,
                ),
            }
            return legacy
        bound_id = str(
            legacy.get("request_id")
            or legacy.get("receipt_id")
            or request_id
        )
        last_bound_id = bound_id
        longform_started = time.perf_counter()
        try:
            longform = build_longform_context(
                root,
                prompt,
                artifact_context=artifact_context,
                _accepted_identity=before,
            )
        except Exception as exc:
            longform = {
                "status": "degraded",
                "reason": f"longform context failed: {exc}",
                "context": (
                    "[WEBNOVEL_CONTINUITY_CONTRACT]\n"
                    "prepared_canon_revision: "
                    f"{before['active_canon_revision']}\n"
                    "active_projection_hash: "
                    f"{before['active_projection_hash']}\n"
                    "长篇派生召回故障不代表事实不存在；继续服从精确 accepted 投影与原文证据。\n"
                    "[/WEBNOVEL_CONTINUITY_CONTRACT]"
                ),
                "telemetry": {},
            }
        longform_attempt_ms += (
            time.perf_counter() - longform_started
        ) * 1000.0
        combined_context = (
            str(legacy.get("context") or "")
            + "\n\n"
            + str(longform.get("context") or "")
        ).strip()

        identity_started = time.perf_counter()
        after = _active_continuity_identity(service)
        identity_check_ms += (
            time.perf_counter() - identity_started
        ) * 1000.0
        _verify_lifecycle_identity(
            root,
            normalized_lifecycle_identity,
        )
        identity_stable = (
            int(before["active_canon_revision"])
            == int(after["active_canon_revision"])
            and str(before["active_projection_hash"])
            == str(after["active_projection_hash"])
        )
        if not identity_stable:
            continue

        telemetry = {
            **dict(longform.get("telemetry") or {}),
            "state_prepare_ms": round(legacy_prepare_ms, 3),
            "longform_attempt_ms": round(longform_attempt_ms, 3),
            "identity_check_ms": round(identity_check_ms, 3),
            "identity_retries": attempt,
            "prepare_total_ms": round(
                (time.perf_counter() - prepare_started) * 1000.0,
                3,
            ),
        }
        advantage_failure = _mandatory_advantage_failure(
            longform.get("advantage_context")
            if isinstance(longform, Mapping)
            else None
        )
        if advantage_failure:
            reason = str(advantage_failure["reason"])
            try:
                with service.store.transaction() as connection:
                    _ensure_turn_v1_columns(connection)
                    connection.execute(
                        """
                        UPDATE turns
                        SET status='failed', error=?, completed_at=?
                        WHERE request_id=? OR receipt_id=?
                        """,
                        (reason, _utc_now(), bound_id, bound_id),
                    )
            except (AttributeError, sqlite3.Error):
                pass
            legacy.update(
                {
                    "status": "failed",
                    "reason": reason,
                    "lifecycle_mode": "strict_proposal",
                    "artifact_context": artifact_context,
                    "longform": longform,
                    "advantage_context": dict(
                        longform.get("advantage_context") or {}
                    ),
                    "context": combined_context,
                    "turn_status": "failed",
                    "telemetry": telemetry,
                    "recorded_events": [],
                    "proposal_events": [],
                }
            )
            return legacy
        binding = _bind_prepared_context(
            service,
            bound_id,
            before,
            artifact_context,
            combined_context,
            telemetry,
            normalized_lifecycle_identity,
        )
        public_identity = {
            "prompt_hash": binding["prompt_hash"],
            "retrieved_context_digest": binding[
                "retrieved_context_digest"
            ],
            "prepared_canon_revision": int(
                before["active_canon_revision"]
            ),
            "active_projection_hash": str(
                before["active_projection_hash"]
            ),
            "lifecycle_identity": dict(
                binding["lifecycle_identity"]
            ),
        }
        legacy["context"] = combined_context
        legacy.update(
            {
                "lifecycle_mode": "strict_proposal",
                "prepared_canon_revision": int(
                    before["active_canon_revision"]
                ),
                "active_projection_hash": str(
                    before["active_projection_hash"]
                ),
                "prompt_hash": binding["prompt_hash"],
                "retrieved_context_digest": binding[
                    "retrieved_context_digest"
                ],
                "context_digest": binding["retrieved_context_digest"],
                "lifecycle_identity": dict(
                    binding["lifecycle_identity"]
                ),
                "identity": public_identity,
                "artifact_context": artifact_context,
                "longform": longform,
                "turn_status": "pending_proposal",
                "telemetry": telemetry,
            }
        )
        if (
            longform.get("status") == "degraded"
            and legacy.get("status") == "ready"
        ):
            legacy["status"] = "degraded"
        return legacy

    reason = (
        "accepted canon revision or projection changed during context "
        "assembly; prepare must be retried"
    )
    if last_bound_id:
        with service.store.transaction() as connection:
            _ensure_turn_v1_columns(connection)
            connection.execute(
                """
                UPDATE turns
                SET status='failed', error=?, completed_at=?
                WHERE request_id=? OR receipt_id=?
                """,
                (reason, _utc_now(), last_bound_id, last_bound_id),
            )
    failed = dict(last_legacy or {})
    failed.update(
        {
            "status": "failed",
            "reason": reason,
            "lifecycle_mode": "strict_proposal",
            "turn_status": "failed",
            "telemetry": {
                "state_prepare_ms": round(legacy_prepare_ms, 3),
                "longform_attempt_ms": round(longform_attempt_ms, 3),
                "identity_check_ms": round(identity_check_ms, 3),
                "identity_retries": _PREPARE_IDENTITY_MAX_ATTEMPTS,
                "prepare_total_ms": round(
                    (time.perf_counter() - prepare_started) * 1000.0,
                    3,
                ),
            },
        }
    )
    return failed


def _entity_type(
    service: ContinuityService,
    entity_id: str,
    *,
    connection: sqlite3.Connection | None = None,
) -> str | None:
    """Read an entity type without crossing an active write transaction.

    Stop proposal conversion runs inside ``ContinuityService.atomic_write``.
    A proposal-local entity is visible on that transaction connection before
    commit, but not from a separate read-only connection.  Callers that
    already hold the transaction connection must pass it through.
    """

    if connection is not None:
        row = connection.execute(
            "SELECT entity_type FROM entities WHERE entity_id=?",
            (entity_id,),
        ).fetchone()
        return str(row["entity_type"]) if row is not None else None
    with service.store.read_connection() as read_connection:
        row = read_connection.execute(
            "SELECT entity_type FROM entities WHERE entity_id=?",
            (entity_id,),
        ).fetchone()
        return str(row["entity_type"]) if row is not None else None


def _resolve_or_register(
    service: ContinuityService,
    name: Any,
    entity_type: str,
    *,
    artifact_id: str,
    issues: list[dict[str, Any]],
) -> str | None:
    text = str(name or "").strip()
    if not text:
        issues.append(
            {
                "code": "ENTITY_NAME_MISSING",
                "severity": "error",
                "message": f"{entity_type} name is missing",
            }
        )
        return None
    resolution = service.resolve_mention(
        text,
        artifact_id=artifact_id,
        persist=True,
    )
    if resolution["status"] == "RESOLVED":
        resolved = str(resolution["entity_id"])
        if _entity_type(service, resolved) == entity_type:
            return resolved
    if resolution["status"] == "AMBIGUOUS":
        issues.append(
            {
                "code": "ENTITY_MENTION_AMBIGUOUS",
                "severity": "error",
                "message": f"ambiguous entity mention: {text}",
                "details": {"candidates": resolution["candidates"]},
            }
        )
        return None
    return str(
        service.register_entity(entity_type, text)["entity_id"]
    )


def _evidence(
    delta: Mapping[str, Any],
    *,
    receipt_id: str,
    assistant_hash: str,
) -> dict[str, Any]:
    return {
        "quote": str(delta.get("evidence") or ""),
        "source": "assistant_text",
        "receipt_id": receipt_id,
        "assistant_sha256": assistant_hash,
        "confidence": float(delta.get("confidence") or 0.0),
    }


def _current_location(
    service: ContinuityService,
    actor_entity_id: str,
) -> str | None:
    facts = service.query_facts(
        entity_id=actor_entity_id,
        fact_type="location",
        include_timeless=False,
    ).get("facts") or []
    if not facts:
        return None
    value = facts[0]
    return (
        str(value.get("target_entity_id"))
        if value.get("target_entity_id")
        else None
    )


def _current_item_owner(
    service: ContinuityService,
    item_entity_id: str,
) -> str | None:
    facts = service.query_facts(
        entity_id=item_entity_id,
        fact_type="inventory",
        include_timeless=False,
    ).get("facts") or []
    if not facts:
        return None
    value = facts[0].get("value") or {}
    owner = (
        value.get("owner_entity_id")
        if isinstance(value, Mapping)
        else facts[0].get("subject_entity_id")
    )
    return str(owner) if owner else None


def _resolve_event_reference(
    service: ContinuityService,
    value: Any,
    entity_type: str,
    *,
    artifact_id: str,
    issues: list[dict[str, Any]],
    optional: bool = False,
) -> str | None:
    text = str(value or "").strip()
    if not text:
        if optional:
            return None
        return _resolve_or_register(
            service,
            value,
            entity_type,
            artifact_id=artifact_id,
            issues=issues,
        )
    if _entity_type(service, text) == entity_type:
        return text
    return _resolve_or_register(
        service,
        text,
        entity_type,
        artifact_id=artifact_id,
        issues=issues,
    )


def _typed_delta_to_event(
    service: ContinuityService,
    delta: Mapping[str, Any],
    *,
    artifact_id: str,
    common: Mapping[str, Any],
    issues: list[dict[str, Any]],
) -> dict[str, Any] | None:
    event_type = str(delta.get("event_type") or "").strip()
    action = str(delta.get("action") or "set").strip().casefold()
    subject = delta.get("subject")
    object_value = delta.get("object")
    field = str(delta.get("field") or "").strip()
    raw_value = delta.get("value")
    value = dict(raw_value) if isinstance(raw_value, Mapping) else {}
    typed_common = dict(common)
    if delta.get("story_coordinate") is not None:
        typed_common["story_coordinate"] = dict(
            delta.get("story_coordinate") or {}
        )
    if delta.get("knowledge_plane"):
        typed_common["knowledge_plane"] = str(delta["knowledge_plane"])

    if event_type == "state":
        actor = _resolve_event_reference(
            service,
            subject,
            "character",
            artifact_id=artifact_id,
            issues=issues,
        )
        return (
            {
                "event_type": "state",
                "entity_id": actor,
                "field": field or "state",
                "value": raw_value,
                **typed_common,
            }
            if actor
            else None
        )

    if event_type == "movement":
        actor = _resolve_event_reference(
            service,
            subject,
            "character",
            artifact_id=artifact_id,
            issues=issues,
        )
        destination = _resolve_event_reference(
            service,
            object_value,
            "location",
            artifact_id=artifact_id,
            issues=issues,
            optional=action in {"leave", "depart"},
        )
        origin_ref = value.get("from_location")
        origin = (
            _resolve_event_reference(
                service,
                origin_ref,
                "location",
                artifact_id=artifact_id,
                issues=issues,
                optional=True,
            )
            if origin_ref
            else (
                _current_location(service, actor)
                if actor and action in {"leave", "depart"}
                else None
            )
        )
        if not actor:
            return None
        return {
            "event_type": "movement",
            "actor_entity_id": actor,
            "from_location_entity_id": origin,
            "to_location_entity_id": destination,
            "action": action,
            **typed_common,
        }

    if event_type == "inventory":
        owner = _resolve_event_reference(
            service,
            subject,
            "character",
            artifact_id=artifact_id,
            issues=issues,
        )
        item = _resolve_event_reference(
            service,
            object_value or value.get("item") or field,
            "item",
            artifact_id=artifact_id,
            issues=issues,
        )
        if not owner or not item:
            return None
        previous_owner = _current_item_owner(service, item)
        from_owner = value.get("from_owner")
        if from_owner:
            previous_owner = _resolve_event_reference(
                service,
                from_owner,
                "character",
                artifact_id=artifact_id,
                issues=issues,
                optional=True,
            )
        event = {
            "event_type": "inventory",
            "item_entity_id": item,
            "from_owner_entity_id": previous_owner,
            "action": action,
            "quantity": value.get("quantity", 1),
            "unique": bool(value.get("unique", False)),
            **typed_common,
        }
        if action not in {"consume", "lose"}:
            event["to_owner_entity_id"] = owner
        return event

    if event_type == "relation":
        source = _resolve_event_reference(
            service,
            subject,
            "character",
            artifact_id=artifact_id,
            issues=issues,
        )
        target = _resolve_event_reference(
            service,
            object_value or value.get("target"),
            "character",
            artifact_id=artifact_id,
            issues=issues,
        )
        if not source or not target:
            return None
        relation_value = {
            key: item
            for key, item in value.items()
            if key != "target"
        } or {"status": "removed" if action == "remove" else "established"}
        return {
            "event_type": "relation",
            "source_entity_id": source,
            "target_entity_id": target,
            "dimension": field or str(value.get("dimension") or "relationship"),
            "value": relation_value,
            **typed_common,
        }

    if event_type == "time":
        story = _resolve_event_reference(
            service,
            subject or "故事",
            "world",
            artifact_id=artifact_id,
            issues=issues,
        )
        return (
            {
                "event_type": "time",
                "entity_id": story,
                "field": field or "current_time",
                "value": raw_value,
                **typed_common,
            }
            if story
            else None
        )

    if event_type == "world_rule":
        return {
            "event_type": "world_rule",
            "field": field or str(value.get("rule_id") or "world_state"),
            "value": raw_value,
            **typed_common,
        }

    if event_type == "ability":
        owner = _resolve_event_reference(
            service,
            subject,
            "character",
            artifact_id=artifact_id,
            issues=issues,
        )
        ability = _resolve_event_reference(
            service,
            object_value or value.get("ability") or field,
            "ability",
            artifact_id=artifact_id,
            issues=issues,
        )
        if not owner or not ability:
            return None
        state = dict(value.get("state") or {})
        for key, item in value.items():
            if key not in {"ability", "state", "cooldown_until"}:
                state.setdefault(key, item)
        event = {
            "event_type": "ability",
            "owner_entity_id": owner,
            "ability_entity_id": ability,
            "action": action,
            "state": state,
            **typed_common,
        }
        if value.get("cooldown_until") is not None:
            event["cooldown_until"] = value["cooldown_until"]
        return event

    if event_type == "progression":
        actor = _resolve_event_reference(
            service,
            subject,
            "character",
            artifact_id=artifact_id,
            issues=issues,
        )
        track = _resolve_event_reference(
            service,
            object_value or value.get("track") or field,
            "progression_track",
            artifact_id=artifact_id,
            issues=issues,
        )
        if not actor or not track:
            return None
        event = {
            "event_type": "progression",
            "actor_entity_id": actor,
            "track_entity_id": track,
            "action": action,
            **typed_common,
        }
        for source_key, target_key, entity_type in (
            ("from_rank", "from_rank_entity_id", "rank_node"),
            ("to_rank", "to_rank_entity_id", "rank_node"),
            ("rank_edge", "rank_edge_entity_id", "rank_edge"),
        ):
            reference = value.get(source_key)
            if reference:
                resolved = _resolve_event_reference(
                    service,
                    reference,
                    entity_type,
                    artifact_id=artifact_id,
                    issues=issues,
                    optional=True,
                )
                if resolved:
                    event[target_key] = resolved
        return event

    if event_type == "resource":
        actor = _resolve_event_reference(
            service,
            subject,
            "character",
            artifact_id=artifact_id,
            issues=issues,
        )
        resource = _resolve_event_reference(
            service,
            object_value or value.get("resource") or field,
            "resource_pool",
            artifact_id=artifact_id,
            issues=issues,
        )
        if not actor or not resource:
            return None
        event = {
            "event_type": "resource",
            "actor_entity_id": actor,
            "resource_entity_id": resource,
            "action": action,
            "amount": value.get("amount", raw_value if not value else None),
            **typed_common,
        }
        for source_key, target_key, entity_type in (
            ("target_resource", "target_resource_entity_id", "resource_pool"),
            ("conversion_rule", "conversion_rule_entity_id", "conversion_rule"),
        ):
            reference = value.get(source_key)
            if reference:
                resolved = _resolve_event_reference(
                    service,
                    reference,
                    entity_type,
                    artifact_id=artifact_id,
                    issues=issues,
                    optional=True,
                )
                if resolved:
                    event[target_key] = resolved
        if value.get("target_amount") is not None:
            event["target_amount"] = value["target_amount"]
        if value.get("source") is not None:
            event["source"] = value["source"]
        return event

    if event_type == "status_effect":
        actor = _resolve_event_reference(
            service,
            subject,
            "character",
            artifact_id=artifact_id,
            issues=issues,
        )
        status = _resolve_event_reference(
            service,
            object_value or value.get("status") or field,
            "status_effect",
            artifact_id=artifact_id,
            issues=issues,
        )
        if not actor or not status:
            return None
        event = {
            "event_type": "status_effect",
            "actor_entity_id": actor,
            "status_entity_id": status,
            "action": action,
            **typed_common,
        }
        for key in ("stacks", "expires_coordinate"):
            if value.get(key) is not None:
                event[key] = value[key]
        if value.get("source"):
            source_type = str(value.get("source_type") or "ability")
            source = _resolve_event_reference(
                service,
                value["source"],
                source_type,
                artifact_id=artifact_id,
                issues=issues,
                optional=True,
            )
            if source:
                event["source_entity_id"] = source
        return event

    if event_type == "power_binding":
        actor = _resolve_event_reference(
            service,
            subject,
            "character",
            artifact_id=artifact_id,
            issues=issues,
        )
        source_type = str(value.get("source_type") or "item")
        source = _resolve_event_reference(
            service,
            object_value or value.get("source") or field,
            source_type,
            artifact_id=artifact_id,
            issues=issues,
        )
        if not actor or not source:
            return None
        binding_id = str(value.get("binding_id") or "").strip()
        if not binding_id:
            binding_id = "binding_" + _sha256(
                _canonical_json([actor, source, value.get("slot_key")])
            )[:32]
        ability_ids: list[str] = []
        for ability_ref in value.get("ability_ids") or []:
            resolved = _resolve_event_reference(
                service,
                ability_ref,
                "ability",
                artifact_id=artifact_id,
                issues=issues,
                optional=True,
            )
            if resolved:
                ability_ids.append(resolved)
        event = {
            "event_type": "power_binding",
            "actor_entity_id": actor,
            "binding_id": binding_id,
            "source_entity_id": source,
            "action": action,
            "ability_entity_ids": ability_ids,
            "unique": bool(value.get("unique", False)),
            **typed_common,
        }
        if value.get("slot_key") is not None:
            event["slot_key"] = str(value["slot_key"])
        return event

    if event_type == "qualification":
        actor = _resolve_event_reference(
            service,
            subject,
            "character",
            artifact_id=artifact_id,
            issues=issues,
        )
        qualification = _resolve_event_reference(
            service,
            object_value or value.get("qualification") or field,
            "qualification",
            artifact_id=artifact_id,
            issues=issues,
        )
        if not actor or not qualification:
            return None
        event = {
            "event_type": "qualification",
            "actor_entity_id": actor,
            "qualification_entity_id": qualification,
            "action": action,
            "quantity": value.get("quantity", 1),
            **typed_common,
        }
        if value.get("source"):
            source_type = str(value.get("source_type") or "role")
            source = _resolve_event_reference(
                service,
                value["source"],
                source_type,
                artifact_id=artifact_id,
                issues=issues,
                optional=True,
            )
            if source:
                event["source_entity_id"] = source
        if value.get("expires_coordinate") is not None:
            event["expires_coordinate"] = value["expires_coordinate"]
        return event

    if event_type == "power_observation":
        observer = _resolve_event_reference(
            service,
            subject,
            "character",
            artifact_id=artifact_id,
            issues=issues,
        )
        observed = _resolve_event_reference(
            service,
            object_value or value.get("subject"),
            "character",
            artifact_id=artifact_id,
            issues=issues,
            optional=True,
        )
        ability = _resolve_event_reference(
            service,
            value.get("ability"),
            "ability",
            artifact_id=artifact_id,
            issues=issues,
            optional=True,
        )
        if not observer:
            return None
        event = {
            "event_type": "power_observation",
            "observer_entity_id": observer,
            "action": action,
            "knowledge_plane": str(
                delta.get("knowledge_plane") or "actor_belief"
            ),
            "confidence": float(delta.get("confidence") or 0.0),
            "observed_fields": dict(value.get("observed_fields") or {}),
            **typed_common,
        }
        if observed:
            event["subject_entity_id"] = observed
        if ability:
            event["ability_entity_id"] = ability
        return event

    issues.append(
        {
            "code": "UNSUPPORTED_TYPED_EVENT",
            "severity": "error",
            "message": f"unsupported typed extracted event: {event_type}",
            "details": {"delta": dict(delta)},
        }
    )
    return None


def legacy_deltas_to_events(
    service: ContinuityService,
    deltas: Sequence[Mapping[str, Any]],
    *,
    artifact_context: Mapping[str, Any],
    receipt_id: str,
    assistant_hash: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert validated SiliconFlow deltas into typed continuity events."""

    events: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    artifact_id = str(artifact_context["artifact_id"])
    for index, delta in enumerate(deltas):
        category = str(delta.get("category") or "")
        operation = str(delta.get("operation") or "set")
        scope = str(delta.get("scope") or "current")
        common = {
            "scope": scope,
            "chapter_no": artifact_context.get("chapter_no"),
            "scene_index": artifact_context.get("scene_index"),
            "story_time": delta.get("effective_at"),
            "evidence": _evidence(
                delta,
                receipt_id=receipt_id,
                assistant_hash=assistant_hash,
            ),
        }
        if delta.get("event_type"):
            event = _typed_delta_to_event(
                service,
                delta,
                artifact_id=artifact_id,
                common=common,
                issues=issues,
            )
            if event is not None:
                events.append(event)
            continue

        if category in {
            "ability",
            "progression",
            "resource",
            "status",
            "binding",
            "qualification",
            "observation",
        }:
            value = (
                dict(delta.get("value") or {})
                if isinstance(delta.get("value"), Mapping)
                else {"value": delta.get("value")}
            )
            event_type = {
                "status": "status_effect",
                "binding": "power_binding",
                "observation": "power_observation",
            }.get(category, category)
            default_actions = {
                "ability": "set",
                "progression": "initialize",
                "resource": "set",
                "status": "apply",
                "binding": "bind",
                "qualification": "grant",
                "observation": "observe",
            }
            object_key = {
                "ability": "ability",
                "progression": "track",
                "resource": "resource",
                "status": "status",
                "binding": "source",
                "qualification": "qualification",
                "observation": "subject",
            }[category]
            typed = {
                **dict(delta),
                "event_type": event_type,
                "action": str(
                    value.get("action")
                    or default_actions[category]
                ),
                "object": value.get(object_key) or delta.get("field"),
                "value": value,
            }
            event = _typed_delta_to_event(
                service,
                typed,
                artifact_id=artifact_id,
                common=common,
                issues=issues,
            )
            if event is not None:
                events.append(event)
            continue
        if operation == "delete" and category not in {"inventory"}:
            issues.append(
                {
                    "code": "DELETE_REQUIRES_EXPLICIT_RETRACTION",
                    "severity": "error",
                    "message": (
                        f"delta {index} uses delete without an accepted event target"
                    ),
                    "details": {"delta": dict(delta)},
                }
            )
            continue

        if category == "character_state":
            actor = _resolve_or_register(
                service,
                delta.get("subject"),
                "character",
                artifact_id=artifact_id,
                issues=issues,
            )
            if actor:
                events.append(
                    {
                        "event_type": "state",
                        "entity_id": actor,
                        "field": str(delta.get("field") or "state"),
                        "value": delta.get("value"),
                        **common,
                    }
                )
            continue

        if category == "location":
            actor = _resolve_or_register(
                service,
                delta.get("subject"),
                "character",
                artifact_id=artifact_id,
                issues=issues,
            )
            destination = _resolve_or_register(
                service,
                delta.get("value"),
                "location",
                artifact_id=artifact_id,
                issues=issues,
            )
            if actor and destination:
                origin = _current_location(service, actor)
                events.append(
                    {
                        "event_type": "movement",
                        "actor_entity_id": actor,
                        "from_location_entity_id": origin,
                        "to_location_entity_id": destination,
                        "action": "move" if origin else "arrive",
                        **common,
                    }
                )
            continue

        if category == "inventory":
            value = delta.get("value")
            value_map = dict(value) if isinstance(value, Mapping) else {}
            field = str(delta.get("field") or "")
            item_name = (
                value_map.get("item")
                or value_map.get("name")
                or (field[5:] if field.startswith("item:") else field)
            )
            owner = _resolve_or_register(
                service,
                delta.get("subject"),
                "character",
                artifact_id=artifact_id,
                issues=issues,
            )
            item = _resolve_or_register(
                service,
                item_name,
                "item",
                artifact_id=artifact_id,
                issues=issues,
            )
            if owner and item:
                status = str(
                    value_map.get("status")
                    or value_map.get("action")
                    or ("lost" if operation == "delete" else "held")
                ).casefold()
                if status in {"consumed", "consume", "used", "消耗", "已消耗"}:
                    action = "consume"
                elif status in {"lost", "lose", "遗失", "丢失", "失去"}:
                    action = "lose"
                elif status in {"transfer", "transferred", "转移", "交给"}:
                    action = "transfer"
                else:
                    action = "set"
                previous_owner = _current_item_owner(service, item)
                event = {
                    "event_type": "inventory",
                    "item_entity_id": item,
                    "from_owner_entity_id": previous_owner,
                    "action": action,
                    "quantity": value_map.get("quantity", 1),
                    "unique": bool(value_map.get("unique", False)),
                    **common,
                }
                if action not in {"consume", "lose"}:
                    event["to_owner_entity_id"] = owner
                events.append(event)
            continue

        if category == "relationship":
            value = delta.get("value")
            value_map = dict(value) if isinstance(value, Mapping) else {}
            source = _resolve_or_register(
                service,
                delta.get("subject"),
                "character",
                artifact_id=artifact_id,
                issues=issues,
            )
            target = _resolve_or_register(
                service,
                value_map.get("target"),
                "character",
                artifact_id=artifact_id,
                issues=issues,
            )
            if source and target:
                dimension = str(
                    value_map.get("dimension")
                    or value_map.get("type")
                    or "relationship"
                )
                events.append(
                    {
                        "event_type": "relation",
                        "source_entity_id": source,
                        "target_entity_id": target,
                        "dimension": dimension,
                        "value": {
                            key: item
                            for key, item in value_map.items()
                            if key != "target"
                        }
                        or {"status": "established"},
                        **common,
                    }
                )
            continue

        if category == "story_time":
            story = _resolve_or_register(
                service,
                delta.get("subject") or "故事",
                "world",
                artifact_id=artifact_id,
                issues=issues,
            )
            if story:
                events.append(
                    {
                        "event_type": "time",
                        "entity_id": story,
                        "field": "current_time",
                        "value": delta.get("value"),
                        **common,
                    }
                )
            continue

        if category == "world_state":
            world = _resolve_or_register(
                service,
                delta.get("subject") or "世界",
                "world",
                artifact_id=artifact_id,
                issues=issues,
            )
            if world:
                events.append(
                    {
                        "event_type": "state",
                        "entity_id": world,
                        "field": str(delta.get("field") or "world_state"),
                        "value": delta.get("value"),
                        **common,
                    }
                )
            continue

        issues.append(
            {
                "code": "UNSUPPORTED_LEGACY_CATEGORY",
                "severity": "error",
                "message": f"unsupported extracted category: {category}",
                "details": {"delta": dict(delta)},
            }
        )
    return events, issues


def _item_candidate_normalized_mention(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _item_candidate_stable_id(
    reference_type: str,
    mention: str,
    artifact_context: Mapping[str, Any],
) -> str:
    prefix = {
        "item_definition": "item_definition_",
        "item_function": "item_function_",
        "item_function_binding": "item_function_binding_",
        "item_instance": "item_instance_",
        "item_stack": "item_stack_",
    }[reference_type]
    payload = {
        "artifact_id": str(artifact_context.get("artifact_id") or ""),
        "reference_type": reference_type,
        "normalized_mention": _item_candidate_normalized_mention(mention),
    }
    return prefix + _sha256(_canonical_json(payload))


def _item_candidate_creatable_ids(
    candidates: Sequence[Mapping[str, Any]],
    artifact_context: Mapping[str, Any],
    *,
    assistant_text: str = "",
) -> dict[tuple[str, str], str]:
    """Predeclare only IDs that the current v4 batch is allowed to create."""

    creatable: dict[tuple[str, str], str] = {}

    def declare(reference_type: str, mention: Any) -> None:
        normalized = _item_candidate_normalized_mention(mention)
        if not normalized:
            return
        creatable.setdefault(
            (reference_type, normalized),
            _item_candidate_stable_id(
                reference_type,
                str(mention),
                artifact_context,
            ),
        )

    def inspect_candidate(candidate: Mapping[str, Any]) -> None:
        event_type = str(candidate.get("event_type") or "").casefold()
        action = str(candidate.get("action") or "").casefold()
        # A creator mention is visible to dependent candidates only after the
        # neutral v4 candidate itself passes the same closed schema,
        # confidence, evidence, and action checks as the production adapter.
        # This prevents an invalid creator from manufacturing a deterministic
        # ID that later candidates can resolve.
        if assistant_text and event_type in {
            "item_spec",
            "item_instance",
        }:
            try:
                state_rag.normalize_item_extraction_candidate(
                    candidate,
                    assistant_text,
                )
            except Exception:
                return
        subject = (
            dict(candidate.get("subject") or {})
            if isinstance(candidate.get("subject"), Mapping)
            else {}
        )
        subject_kind = str(subject.get("kind") or "")
        subject_mention = subject.get("mention")
        if event_type == "item_spec" and action in {"define", "supersede"}:
            reference_type = {
                "item_definition": "item_definition",
                "function_definition": "item_function",
                "function_binding": "item_function_binding",
            }.get(subject_kind)
            if reference_type:
                declare(reference_type, subject_mention)
        elif event_type == "item_instance" and action == "instantiate":
            reference_type = {
                "item_instance": "item_instance",
                "item_stack": "item_stack",
            }.get(subject_kind)
            if reference_type:
                declare(reference_type, subject_mention)
        if event_type == "item_instance" and action == "split":
            for value in candidate.get("objects") or []:
                if (
                    isinstance(value, Mapping)
                    and str(value.get("role") or "") == "target_stack"
                ):
                    declare("item_stack", value.get("mention"))
        if event_type == "item_correction":
            changes = candidate.get("changes")
            replacement = (
                changes.get("replacement")
                if isinstance(changes, Mapping)
                else None
            )
            if isinstance(replacement, Mapping):
                inspect_candidate(replacement)

    for candidate in candidates:
        if isinstance(candidate, Mapping):
            inspect_candidate(candidate)
    return creatable


def _item_candidate_json_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = str(key).casefold()
            if (
                isinstance(item, str)
                and (
                    "name" in normalized_key
                    or normalized_key
                    in {
                        "title",
                        "canonical",
                        "label",
                        "serial_or_mark",
                    }
                )
            ):
                normalized = _item_candidate_normalized_mention(item)
                if normalized:
                    names.add(normalized)
            else:
                names.update(_item_candidate_json_names(item))
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for item in value:
            names.update(_item_candidate_json_names(item))
    return names


def _item_candidate_table_matches(
    connection: sqlite3.Connection,
    mention: str,
    reference_type: str,
) -> list[str]:
    normalized = _item_candidate_normalized_mention(mention)
    table_specs: Mapping[str, tuple[tuple[str, str, str | None], ...]] = {
        "item_definition": (
            ("item_definitions", "item_definition_id", "definition_json"),
        ),
        "item_instance": (
            ("item_instances", "item_instance_id", "instance_json"),
        ),
        "item_stack": (
            ("item_stacks", "stack_id", "batch_json"),
        ),
        "item_function": (
            (
                "item_function_definitions",
                "function_id",
                "definition_json",
            ),
        ),
        "item_function_binding": (
            ("item_function_bindings", "binding_id", "binding_json"),
        ),
        "item_subject": (
            ("item_instances", "item_instance_id", "instance_json"),
            ("item_stacks", "stack_id", "batch_json"),
        ),
        "item_spec": (
            ("item_definitions", "item_definition_id", "definition_json"),
            (
                "item_function_definitions",
                "function_id",
                "definition_json",
            ),
            ("item_function_bindings", "binding_id", "binding_json"),
        ),
        "item_event": (
            ("continuity_events", "event_id", None),
        ),
    }
    matches: set[str] = set()
    existing_tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for table, id_column, json_column in table_specs.get(
        reference_type,
        (),
    ):
        if table not in existing_tables:
            continue
        columns = id_column if json_column is None else f"{id_column}, {json_column}"
        for row in connection.execute(
            f"SELECT {columns} FROM {table} ORDER BY {id_column}"
        ):
            stable_id = str(row[id_column])
            if _item_candidate_normalized_mention(stable_id) == normalized:
                matches.add(stable_id)
                continue
            if json_column is None:
                continue
            payload = _item_json_object(row[json_column])
            if normalized in _item_candidate_json_names(payload):
                matches.add(stable_id)
    return sorted(matches)


def _item_candidate_entity_resolution(
    service: ContinuityService,
    mention: str,
    reference_type: str,
    *,
    artifact_id: str,
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    resolution = service.resolve_mention(
        mention,
        artifact_id=artifact_id,
        persist=False,
    )
    status = str(resolution.get("status") or "")
    if status != "RESOLVED":
        return {
            "status": status or "UNRESOLVED",
            "candidates": list(resolution.get("candidates") or []),
        }
    entity_id = str(resolution.get("entity_id") or "")
    expected_types = {
        "item": {"item"},
        "location": {"location"},
        "ability": {"ability"},
        "resource": {"resource"},
        "entity": set(),
    }.get(reference_type, set())
    entity_type = _entity_type(
        service,
        entity_id,
        connection=connection,
    )
    if expected_types and entity_type not in expected_types:
        return {
            "status": "UNRESOLVED",
            "candidates": [
                {
                    "entity_id": entity_id,
                    "entity_type": entity_type,
                }
            ],
        }
    return {"status": "RESOLVED", "reference_id": entity_id}


def _item_candidate_resolver(
    service: ContinuityService,
    connection: sqlite3.Connection,
    candidates: Sequence[Mapping[str, Any]],
    artifact_context: Mapping[str, Any],
    *,
    assistant_text: str = "",
) -> Callable[[str, str, str], Any]:
    creatable = _item_candidate_creatable_ids(
        candidates,
        artifact_context,
        assistant_text=assistant_text,
    )
    artifact_id = str(artifact_context.get("artifact_id") or "")

    def resolve(
        mention: str,
        reference_type: str,
        role: str,
    ) -> dict[str, Any]:
        normalized = _item_candidate_normalized_mention(mention)
        if reference_type in {
            "item_definition",
            "item_instance",
            "item_stack",
            "item_function",
            "item_function_binding",
            "item_subject",
            "item_spec",
            "item_event",
        }:
            matches = _item_candidate_table_matches(
                connection,
                mention,
                reference_type,
            )
            if len(matches) == 1:
                return {
                    "status": "RESOLVED",
                    "reference_id": matches[0],
                }
            if len(matches) > 1:
                return {
                    "status": "AMBIGUOUS",
                    "candidates": matches,
                }
            creatable_types = {
                "item_subject": ("item_instance", "item_stack"),
                "item_spec": (
                    "item_definition",
                    "item_function",
                    "item_function_binding",
                ),
            }.get(reference_type, (reference_type,))
            created_candidates = sorted(
                {
                    creatable[(candidate_type, normalized)]
                    for candidate_type in creatable_types
                    if (candidate_type, normalized) in creatable
                }
            )
            if len(created_candidates) == 1:
                return {
                    "status": "RESOLVED",
                    "reference_id": created_candidates[0],
                }
            if len(created_candidates) > 1:
                return {
                    "status": "AMBIGUOUS",
                    "candidates": created_candidates,
                }
            return {
                "status": "UNRESOLVED",
                "mention": mention,
                "reference_type": reference_type,
                "role": role,
            }
        if reference_type in {
            "entity",
            "item",
            "location",
            "ability",
            "resource",
        }:
            return _item_candidate_entity_resolution(
                service,
                mention,
                reference_type,
                artifact_id=artifact_id,
                connection=connection,
            )
        return {
            "status": "UNRESOLVED",
            "mention": mention,
            "reference_type": reference_type,
            "role": role,
        }

    return resolve


def _split_item_extraction_candidates(
    deltas: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    item_event_types = set(getattr(state_rag, "ITEM_DELTA_EVENT_TYPES", ()))
    contains_item = any(
        isinstance(delta, Mapping)
        and str(delta.get("event_type") or "").casefold()
        in item_event_types
        for delta in deltas
    )
    if not contains_item:
        return [dict(delta) for delta in deltas], []
    splitter = getattr(state_rag, "split_delta_v4_results", None)
    if not callable(splitter):
        return (
            [
                dict(delta)
                for delta in deltas
                if str(delta.get("event_type") or "").casefold()
                not in item_event_types
            ],
            [
                dict(delta)
                for delta in deltas
                if str(delta.get("event_type") or "").casefold()
                in item_event_types
            ],
        )
    legacy, items = splitter(deltas)
    return list(legacy), list(items)


def _adapt_item_extraction_candidates(
    service: ContinuityService,
    connection: sqlite3.Connection,
    candidates: Sequence[Mapping[str, Any]],
    *,
    assistant_text: str,
    artifact_context: Mapping[str, Any],
) -> dict[str, Any]:
    if not candidates:
        return {
            "ok": True,
            "events": [],
            "issues": [],
            "candidate_count": 0,
            "adapted_count": 0,
        }
    adapter = getattr(state_rag, "adapt_item_extraction_candidates", None)
    if not callable(adapter):
        return {
            "ok": False,
            "events": [],
            "issues": [
                {
                    "code": "ITEM_V4_ADAPTER_UNAVAILABLE",
                    "severity": "error",
                    "message": (
                        "plot-rag-delta/v4 item candidates require the "
                        "production batch adapter"
                    ),
                }
            ],
            "candidate_count": len(candidates),
            "adapted_count": 0,
        }
    resolver = _item_candidate_resolver(
        service,
        connection,
        candidates,
        artifact_context,
        assistant_text=assistant_text,
    )
    return dict(
        adapter(
            candidates,
            assistant_text,
            artifact_context,
            resolver,
        )
    )


def _advantage_candidate_normalized_mention(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _advantage_candidate_stable_id(
    reference_type: str,
    mention: str,
    artifact_context: Mapping[str, Any],
) -> str:
    prefix = {
        "advantage": "advantage_",
        "advantage_anchor": "advantage_anchor_",
        "advantage_module": "advantage_module_",
        "advantage_knowledge": "advantage_knowledge_",
        "advantage_contract": "advantage_contract_",
        "narrative_contract": "advantage_narrative_contract_",
    }[reference_type]
    payload = {
        "artifact_id": str(artifact_context.get("artifact_id") or ""),
        "reference_type": reference_type,
        "normalized_mention": _advantage_candidate_normalized_mention(
            mention
        ),
    }
    return prefix + _sha256(_canonical_json(payload))


def _advantage_candidate_creatable_ids(
    candidates: Sequence[Mapping[str, Any]],
    artifact_context: Mapping[str, Any],
    *,
    assistant_text: str = "",
) -> dict[tuple[str, str], str]:
    """Predeclare deterministic IDs only for valid creator candidates."""

    creatable: dict[tuple[str, str], str] = {}

    def declare(reference_type: str, mention: Any) -> None:
        normalized = _advantage_candidate_normalized_mention(mention)
        if not normalized:
            return
        creatable.setdefault(
            (reference_type, normalized),
            _advantage_candidate_stable_id(
                reference_type,
                str(mention),
                artifact_context,
            ),
        )

    def inspect_candidate(candidate: Mapping[str, Any]) -> None:
        event_type = str(candidate.get("event_type") or "").casefold()
        action = str(candidate.get("action") or "").casefold()
        if assistant_text:
            normalizer = getattr(
                state_rag,
                "normalize_advantage_extraction_candidate",
                None,
            )
            if callable(normalizer):
                try:
                    normalizer(candidate, assistant_text)
                except Exception:
                    return
        subject = (
            dict(candidate.get("subject") or {})
            if isinstance(candidate.get("subject"), Mapping)
            else {}
        )
        subject_kind = str(subject.get("kind") or "").casefold()
        subject_mention = subject.get("mention")
        creator_types: dict[tuple[str, str], str] = {
            ("advantage_spec", "advantage_definition"): "advantage",
            ("advantage_anchor", "advantage_anchor"): "advantage_anchor",
            ("advantage_module", "advantage_module"): "advantage_module",
            ("advantage_reveal", "advantage_knowledge"): (
                "advantage_knowledge"
            ),
            ("advantage_contract", "advantage_contract"): (
                "advantage_contract"
            ),
            ("advantage_contract", "narrative_contract"): (
                "narrative_contract"
            ),
        }
        reference_type = creator_types.get((event_type, subject_kind))
        creator_actions = {
            "advantage_spec": {"define", "supersede"},
            "advantage_anchor": {"define", "supersede"},
            "advantage_module": {"define", "supersede"},
            "advantage_reveal": {"reveal"},
            "advantage_contract": {"define", "narrative"},
        }
        if (
            reference_type
            and action in creator_actions.get(event_type, set())
        ):
            declare(reference_type, subject_mention)
        if event_type == "advantage_correction":
            changes = candidate.get("changes")
            replacement = (
                changes.get("replacement")
                if isinstance(changes, Mapping)
                else None
            )
            if isinstance(replacement, Mapping):
                inspect_candidate(replacement)

    for candidate in candidates:
        if isinstance(candidate, Mapping):
            inspect_candidate(candidate)
    return creatable


def _advantage_candidate_json_strings(value: Any) -> set[str]:
    strings: set[str] = set()
    if isinstance(value, Mapping):
        for item in value.values():
            strings.update(_advantage_candidate_json_strings(item))
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for item in value:
            strings.update(_advantage_candidate_json_strings(item))
    elif isinstance(value, str):
        normalized = _advantage_candidate_normalized_mention(value)
        if normalized:
            strings.add(normalized)
    return strings


def _advantage_candidate_table_matches(
    connection: sqlite3.Connection,
    mention: str,
    reference_type: str,
) -> list[str]:
    normalized = _advantage_candidate_normalized_mention(mention)
    table_specs: Mapping[
        str,
        tuple[str, str, tuple[str, ...]],
    ] = {
        "advantage": (
            "advantage_definitions",
            "advantage_id",
            (
                "title",
                "profiles_json",
                "promise_json",
                "counterplay_json",
                "definition_json",
            ),
        ),
        "advantage_anchor": (
            "advantage_anchors",
            "anchor_id",
            (
                "anchor_ref_id",
                "attributes_json",
                "transfer_rule_json",
            ),
        ),
        "advantage_module": (
            "advantage_module_definitions",
            "module_id",
            (
                "title",
                "experience_contract_json",
                "trigger_json",
                "effects_json",
            ),
        ),
        "advantage_knowledge": (
            "advantage_knowledge",
            "knowledge_id",
            ("claim_json", "evidence_json"),
        ),
        "advantage_contract": (
            "advantage_contracts",
            "contract_id",
            ("terms_json", "agency_json", "breach_effect_json"),
        ),
        "narrative_contract": (
            "advantage_narrative_contracts",
            "narrative_contract_id",
            (
                "reading_promise_json",
                "reward_loop_json",
                "risk_loop_json",
                "reveal_ladder_json",
            ),
        ),
        "advantage_event": (
            "continuity_events",
            "event_id",
            ("payload_json", "evidence_json"),
        ),
    }
    spec = table_specs.get(reference_type)
    if spec is None:
        return []
    table, id_column, searchable_columns = spec
    existing_tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if table not in existing_tables:
        return []
    available_columns = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})")
    }
    selected = [
        column
        for column in searchable_columns
        if column in available_columns
    ]
    projection = ", ".join([id_column, *selected])
    matches: set[str] = set()
    for row in connection.execute(
        f"SELECT {projection} FROM {table} ORDER BY {id_column}"
    ):
        stable_id = str(row[id_column])
        if _advantage_candidate_normalized_mention(stable_id) == normalized:
            matches.add(stable_id)
            continue
        for column in selected:
            raw = row[column]
            if raw is None:
                continue
            if isinstance(raw, str):
                if (
                    _advantage_candidate_normalized_mention(raw)
                    == normalized
                ):
                    matches.add(stable_id)
                    break
                try:
                    decoded = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if (
                    normalized
                    in _advantage_candidate_json_strings(decoded)
                ):
                    matches.add(stable_id)
                    break
    return sorted(matches)


def _advantage_candidate_resolver(
    service: ContinuityService,
    connection: sqlite3.Connection,
    candidates: Sequence[Mapping[str, Any]],
    artifact_context: Mapping[str, Any],
    *,
    assistant_text: str = "",
) -> Callable[[str, str, str], Any]:
    creatable = _advantage_candidate_creatable_ids(
        candidates,
        artifact_context,
        assistant_text=assistant_text,
    )
    artifact_id = str(artifact_context.get("artifact_id") or "")

    def resolve(
        mention: str,
        reference_type: str,
        role: str,
    ) -> dict[str, Any]:
        normalized = _advantage_candidate_normalized_mention(mention)
        if reference_type in {
            "advantage",
            "advantage_anchor",
            "advantage_module",
            "advantage_knowledge",
            "advantage_contract",
            "narrative_contract",
            "advantage_event",
        }:
            matches = _advantage_candidate_table_matches(
                connection,
                mention,
                reference_type,
            )
            if len(matches) == 1:
                return {
                    "status": "RESOLVED",
                    "reference_id": matches[0],
                }
            if len(matches) > 1:
                return {
                    "status": "AMBIGUOUS",
                    "candidates": matches,
                }
            created = creatable.get((reference_type, normalized))
            if created:
                return {
                    "status": "RESOLVED",
                    "reference_id": created,
                }
            return {
                "status": "UNRESOLVED",
                "mention": mention,
                "reference_type": reference_type,
                "role": role,
            }
        if reference_type in {"item_instance", "item_stack"}:
            matches = _item_candidate_table_matches(
                connection,
                mention,
                reference_type,
            )
            if len(matches) == 1:
                return {
                    "status": "RESOLVED",
                    "reference_id": matches[0],
                }
            if len(matches) > 1:
                return {
                    "status": "AMBIGUOUS",
                    "candidates": matches,
                }
            return {
                "status": "UNRESOLVED",
                "mention": mention,
                "reference_type": reference_type,
                "role": role,
            }
        if reference_type in {
            "entity",
            "location",
            "ability",
            "anchor_ref",
        }:
            entity_type = (
                "entity"
                if reference_type == "anchor_ref"
                else reference_type
            )
            return _item_candidate_entity_resolution(
                service,
                mention,
                entity_type,
                artifact_id=artifact_id,
                connection=connection,
            )
        return {
            "status": "UNRESOLVED",
            "mention": mention,
            "reference_type": reference_type,
            "role": role,
        }

    return resolve


def _advantage_experience_bindings(
    candidates: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> dict[int, dict[str, Any]]:
    """Bind neutral candidates to locked contracts without trusting IDs."""

    if not candidates or not manifest:
        return {}
    raw_contracts = manifest.get("contracts")
    if (
        not isinstance(raw_contracts, Sequence)
        or isinstance(raw_contracts, (str, bytes, bytearray))
        or not raw_contracts
    ):
        raise ContinuityError(
            "ADVANTAGE_EXPERIENCE_BINDING_UNRESOLVED",
            "locked event-experience manifest has no contracts",
        )
    contracts: list[dict[str, Any]] = []
    orders: set[int] = set()
    for index, raw in enumerate(raw_contracts):
        if not isinstance(raw, Mapping):
            raise ContinuityError(
                "ADVANTAGE_EXPERIENCE_BINDING_UNRESOLVED",
                "locked event-experience contract identity is invalid",
                details={"contract_index": index},
            )
        item = dict(raw)
        dependency_order = item.get("dependency_order")
        if type(dependency_order) is not int or dependency_order < 1:
            raise ContinuityError(
                "ADVANTAGE_EXPERIENCE_BINDING_UNRESOLVED",
                "locked contract dependency_order is invalid",
                details={"contract_index": index},
            )
        if dependency_order in orders:
            raise ContinuityError(
                "ADVANTAGE_EXPERIENCE_BINDING_AMBIGUOUS",
                "locked contracts share one dependency_order",
                details={"dependency_order": dependency_order},
            )
        orders.add(dependency_order)
        contract_id = str(item.get("contract_id") or "").strip()
        contract_hash = str(item.get("contract_hash") or "").strip()
        event_seed_id = str(item.get("event_seed_id") or "").strip()
        event_seed_revision = item.get("event_seed_revision")
        if (
            not contract_id
            or not _SHA256_HEX_RE.fullmatch(contract_hash)
            or not event_seed_id
            or type(event_seed_revision) is not int
            or event_seed_revision < 1
        ):
            raise ContinuityError(
                "ADVANTAGE_EXPERIENCE_BINDING_UNRESOLVED",
                "locked contract identity is incomplete",
                details={"contract_index": index},
            )
        contracts.append(
            {
                "dependency_order": dependency_order,
                "experience_contract_id": contract_id,
                "experience_contract_hash": contract_hash,
                "event_seed_id": event_seed_id,
                "event_seed_revision": event_seed_revision,
            }
        )
    contracts.sort(
        key=lambda item: (
            int(item["dependency_order"]),
            str(item["event_seed_id"]),
            int(item["event_seed_revision"]),
        )
    )
    if len(contracts) == 1:
        return {
            index: dict(contracts[0])
            for index in range(len(candidates))
        }

    coordinate_groups: dict[str, list[int]] = {}
    group_order: list[str] = []
    for index, candidate in enumerate(candidates):
        coordinate = (
            candidate.get("story_coordinate")
            if isinstance(candidate, Mapping)
            else None
        )
        if not isinstance(coordinate, Mapping):
            raise ContinuityError(
                "ADVANTAGE_EXPERIENCE_BINDING_COORDINATE_REQUIRED",
                "multi-event Advantage candidates require story_coordinate",
                details={"candidate_index": index},
            )
        calendar_id = str(coordinate.get("calendar_id") or "").strip()
        ordinal = coordinate.get("ordinal")
        if (
            not calendar_id
            or isinstance(ordinal, bool)
            or not isinstance(ordinal, (int, float))
            or (
                isinstance(ordinal, float)
                and not ordinal.is_integer()
            )
        ):
            raise ContinuityError(
                "ADVANTAGE_EXPERIENCE_BINDING_COORDINATE_REQUIRED",
                "multi-event Advantage candidate coordinate is invalid",
                details={"candidate_index": index},
            )
        coordinate_key = _canonical_json(
            {
                "calendar_id": calendar_id,
                "ordinal": int(ordinal),
            }
        )
        if coordinate_key not in coordinate_groups:
            coordinate_groups[coordinate_key] = []
            group_order.append(coordinate_key)
        coordinate_groups[coordinate_key].append(index)
    if len(group_order) != len(contracts):
        raise ContinuityError(
            "ADVANTAGE_EXPERIENCE_BINDING_CARDINALITY_MISMATCH",
            "Advantage coordinate groups do not match locked contracts",
            details={
                "candidate_indexes": list(range(len(candidates))),
                "coordinate_groups": [
                    {
                        "coordinate": json.loads(key),
                        "candidate_indexes": coordinate_groups[key],
                    }
                    for key in group_order
                ],
                "manifest_contract_count": len(contracts),
            },
        )
    bindings: dict[int, dict[str, Any]] = {}
    for coordinate_key, contract in zip(group_order, contracts):
        for candidate_index in coordinate_groups[coordinate_key]:
            bindings[candidate_index] = dict(contract)
    return bindings


def _split_extraction_candidates_by_family(
    deltas: Sequence[Mapping[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    item_types = set(getattr(state_rag, "ITEM_DELTA_EVENT_TYPES", ()))
    advantage_types = set(
        getattr(state_rag, "ADVANTAGE_DELTA_EVENT_TYPES", ())
    )
    contains_typed_candidate = any(
        isinstance(delta, Mapping)
        and str(delta.get("event_type") or "").casefold()
        in (item_types | advantage_types)
        for delta in deltas
    )
    if not contains_typed_candidate:
        return [dict(delta) for delta in deltas], [], []
    splitter = getattr(
        state_rag,
        "split_delta_v4_results_by_family",
        None,
    )
    if callable(splitter):
        legacy, items, advantages = splitter(deltas)
        return list(legacy), list(items), list(advantages)
    legacy: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    advantages: list[dict[str, Any]] = []
    for delta in deltas:
        event_type = str(delta.get("event_type") or "").casefold()
        if event_type in item_types:
            items.append(dict(delta))
        elif event_type in advantage_types:
            advantages.append(dict(delta))
        else:
            legacy.append(dict(delta))
    return legacy, items, advantages


def _adapt_advantage_extraction_candidates(
    service: ContinuityService,
    connection: sqlite3.Connection,
    candidates: Sequence[Mapping[str, Any]],
    *,
    assistant_text: str,
    artifact_context: Mapping[str, Any],
    experience_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    if not candidates:
        return {
            "ok": True,
            "events": [],
            "issues": [],
            "candidate_count": 0,
            "adapted_count": 0,
        }
    adapter = getattr(
        state_rag,
        "adapt_advantage_extraction_candidates",
        None,
    )
    if not callable(adapter):
        return {
            "ok": False,
            "events": [],
            "issues": [
                {
                    "code": "ADVANTAGE_V1_ADAPTER_UNAVAILABLE",
                    "severity": "error",
                    "message": (
                        "plot-rag-delta/v4 Advantage candidates require "
                        "the production batch adapter"
                    ),
                }
            ],
            "candidate_count": len(candidates),
            "adapted_count": 0,
        }
    bindings = _advantage_experience_bindings(
        candidates,
        experience_manifest,
    )
    adapter_context = {
        **dict(artifact_context),
        "advantage_experience_required": bool(experience_manifest),
        "advantage_experience_bindings": bindings,
    }
    resolver = _advantage_candidate_resolver(
        service,
        connection,
        candidates,
        adapter_context,
        assistant_text=assistant_text,
    )
    return dict(
        adapter(
            candidates,
            assistant_text,
            adapter_context,
            resolver,
        )
    )


def propose_plot_turn(
    project_root: Path | str,
    assistant_text: str,
    *,
    request_id: str = "",
    session_id: str = "",
    turn_id: str = "",
    prompt: str = "",
    proposal_binding: Mapping[str, Any] | None = None,
    no_delta_without_proposal: bool = False,
    shadow_only: bool = False,
    authoritative_proposal_id: str = "",
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    if not is_strict_lifecycle(root):
        result = state_rag.commit_turn(
            root,
            assistant_text,
            request_id=request_id,
            session_id=session_id,
            turn_id=turn_id,
            prompt=prompt,
        )
        result["lifecycle_mode"] = "legacy_commit"
        return result
    if type(no_delta_without_proposal) is not bool:
        return {
            "status": "failed",
            "reason": (
                "EXTRACTION_PROPOSAL_BINDING_INVALID: "
                "no_delta_without_proposal must be boolean"
            ),
            "recorded_events": [],
            "proposal_events": [],
            "lifecycle_mode": "strict_proposal",
        }
    if type(shadow_only) is not bool:
        return {
            "status": "failed",
            "reason": (
                "EXTRACTION_PROPOSAL_BINDING_INVALID: "
                "shadow_only must be boolean"
            ),
            "recorded_events": [],
            "proposal_events": [],
            "lifecycle_mode": "strict_proposal",
        }
    if not isinstance(authoritative_proposal_id, str):
        return {
            "status": "failed",
            "reason": (
                "EXTRACTION_SHADOW_AUTHORITATIVE_INVALID: "
                "authoritative_proposal_id must be a string"
            ),
            "recorded_events": [],
            "proposal_events": [],
            "lifecycle_mode": "strict_proposal",
        }
    try:
        normalized_proposal_binding = (
            _normalize_extraction_proposal_binding(proposal_binding)
        )
    except ContinuityError as exc:
        return {
            "status": "failed",
            "reason": f"{exc.code}: {exc.message}",
            "recorded_events": [],
            "proposal_events": [],
            "identity_check": {
                "status": "failed",
                "code": exc.code,
                "remote_called": False,
            },
            "lifecycle_mode": "strict_proposal",
        }
    if shadow_only and not normalized_proposal_binding:
        return {
            "status": "failed",
            "reason": (
                "EXTRACTION_PROPOSAL_BINDING_INVALID: shadow_only "
                "requires a complete proposal_binding"
            ),
            "recorded_events": [],
            "proposal_events": [],
            "identity_check": {
                "status": "failed",
                "code": "EXTRACTION_PROPOSAL_BINDING_INVALID",
                "remote_called": False,
            },
            "lifecycle_mode": "strict_proposal",
        }
    if not shadow_only and authoritative_proposal_id.strip():
        return {
            "status": "failed",
            "reason": (
                "EXTRACTION_SHADOW_AUTHORITATIVE_INVALID: "
                "authoritative_proposal_id requires shadow_only=true"
            ),
            "recorded_events": [],
            "proposal_events": [],
            "identity_check": {
                "status": "failed",
                "code": "EXTRACTION_SHADOW_AUTHORITATIVE_INVALID",
                "remote_called": False,
            },
            "lifecycle_mode": "strict_proposal",
        }

    service = ContinuityService(root)
    turn = _turn_row(
        service,
        request_id=str(request_id or ""),
        session_id=str(session_id or ""),
        turn_id=str(turn_id or ""),
    )
    if turn is None:
        return {
            "status": "skipped",
            "reason": "no_prepared_turn",
            "recorded_events": [],
            "proposal_events": [],
            "lifecycle_mode": "strict_proposal",
        }
    assistant_text = str(assistant_text or "")
    if not assistant_text.strip():
        return {
            "status": "failed",
            "reason": "assistant_text is empty",
            "recorded_events": [],
            "proposal_events": [],
            "lifecycle_mode": "strict_proposal",
        }
    assistant_hash = _sha256(assistant_text)
    if (
        not shadow_only
        and str(turn.get("status") or "") in {"proposed", "no_delta"}
    ):
        if str(turn.get("assistant_hash") or "") != assistant_hash:
            return {
                "status": "failed",
                "reason": (
                    "receipt is already finalized with different "
                    "assistant_text"
                ),
                "receipt_id": str(turn["receipt_id"]),
                "recorded_events": [],
                "proposal_events": [],
                "lifecycle_mode": "strict_proposal",
            }
        stored = _json_load(turn.get("result_json"), {})
        if isinstance(stored, dict):
            stored["idempotent"] = True
            return stored

    effective_prompt = str(prompt or turn.get("prompt") or "")
    artifact_context = _json_load(turn.get("v1_context_json"), {})
    if not isinstance(artifact_context, dict) or not artifact_context:
        artifact_context = infer_artifact_context(effective_prompt)
    identity_started = time.perf_counter()
    authoritative_shadow: dict[str, Any] | None = None
    experience_manifest: dict[str, Any] = {}
    try:
        prepared_identity = _validate_prepared_turn_identity(
            service,
            turn,
            effective_prompt=effective_prompt,
            artifact_context=artifact_context,
        )
        if (
            _event_experience_required(root)
            and not prepared_identity["lifecycle_identity"]
        ):
            raise ContinuityError(
                "EVENT_EXPERIENCE_BINDING_REQUIRED",
                "prepared receipt lacks a locked event-experience manifest",
            )
        experience_manifest = dict(
            _verify_lifecycle_identity(
                root,
                prepared_identity["lifecycle_identity"],
            )
        )
        normalized_proposal_binding = (
            _validate_extraction_proposal_binding(
                normalized_proposal_binding,
                turn=turn,
                assistant_sha256=assistant_hash,
                prepared_identity=prepared_identity,
            )
        )
        if normalized_proposal_binding:
            artifact_context = dict(
                normalized_proposal_binding["artifact_context"]
            )
        if shadow_only:
            authoritative_shadow = (
                _validate_shadow_authoritative_proposal(
                    service,
                    authoritative_proposal_id,
                    turn=turn,
                    assistant_sha256=assistant_hash,
                    prepared_identity=prepared_identity,
                )
            )
    except ContinuityError as exc:
        reason = f"{exc.code}: {exc.message}"
        if not shadow_only:
            _mark_turn_identity_failed(service, turn, reason)
        return {
            "status": "failed",
            "reason": reason,
            "request_id": str(turn["request_id"]),
            "receipt_id": str(turn["receipt_id"]),
            "recorded_events": [],
            "proposal_events": [],
            "identity_check": {
                "status": "failed",
                "code": exc.code,
                "latency_ms": round(
                    (time.perf_counter() - identity_started) * 1000.0,
                    3,
                ),
                "remote_called": False,
            },
            "lifecycle_mode": "strict_proposal",
        }
    identity_validation_ms = round(
        (time.perf_counter() - identity_started) * 1000.0,
        3,
    )

    runtime = state_rag._load_runtime_config(root)
    retrieved = _json_load(turn.get("retrieved_json"), [])
    remote = state_rag._default_remote_status(runtime)
    extract_started = time.perf_counter()
    try:
        deltas, extraction_skipped, extract_status = state_rag._chat_extract(
            runtime,
            assistant_text,
            effective_prompt,
            retrieved if isinstance(retrieved, list) else [],
        )
        remote["extract"] = extract_status
        remote["status"] = state_rag._remote_overall(remote)
    except Exception as exc:
        if not shadow_only:
            try:
                state_rag._mark_turn_failed(
                    runtime,
                    str(turn["request_id"]),
                    str(turn["receipt_id"]),
                    str(exc),
                    remote,
                )
            except Exception:
                pass
        return {
            "status": "failed",
            "reason": str(exc),
            "request_id": str(turn["request_id"]),
            "receipt_id": str(turn["receipt_id"]),
            "recorded_events": [],
            "proposal_events": [],
            "remote": remote,
            "lifecycle_mode": "strict_proposal",
        }

    extract_remote_ms = round(
        (time.perf_counter() - extract_started) * 1000.0,
        3,
    )
    try:
        experience_manifest = dict(
            _verify_lifecycle_identity(
                root,
                prepared_identity["lifecycle_identity"],
            )
        )
    except ContinuityError as exc:
        reason = f"{exc.code}: {exc.message}"
        if not shadow_only:
            _mark_turn_identity_failed(service, turn, reason)
        return {
            "status": "failed",
            "reason": reason,
            "request_id": str(turn["request_id"]),
            "receipt_id": str(turn["receipt_id"]),
            "recorded_events": [],
            "proposal_events": [],
            "remote": remote,
            "identity_check": {
                "status": "stale_after_extract",
                "code": exc.code,
                "remote_called": True,
            },
            "telemetry": {
                "identity_validation_ms": identity_validation_ms,
                "extract_remote_ms": extract_remote_ms,
            },
            "lifecycle_mode": "strict_proposal",
        }
    post_extract_identity = _active_continuity_identity(service)
    if (
        int(post_extract_identity["active_canon_revision"])
        != int(prepared_identity["prepared_canon_revision"])
        or str(post_extract_identity["active_projection_hash"])
        != str(prepared_identity["active_projection_hash"])
    ):
        reason = (
            "PREPARED_IDENTITY_STALE: accepted canon revision or "
            "projection changed during extraction"
        )
        if not shadow_only:
            _mark_turn_identity_failed(service, turn, reason)
        return {
            "status": "failed",
            "reason": reason,
            "request_id": str(turn["request_id"]),
            "receipt_id": str(turn["receipt_id"]),
            "recorded_events": [],
            "proposal_events": [],
            "remote": remote,
            "identity_check": {
                "status": "stale_after_extract",
                "remote_called": True,
            },
            "telemetry": {
                "identity_validation_ms": identity_validation_ms,
                "extract_remote_ms": extract_remote_ms,
            },
            "lifecycle_mode": "strict_proposal",
        }

    prepared_revision = int(
        prepared_identity["prepared_canon_revision"]
    )
    validation_ms = 0.0
    proposal_persist_ms = 0.0
    item_adapter_result: dict[str, Any] = {
        "ok": True,
        "events": [],
        "issues": [],
        "candidate_count": 0,
        "adapted_count": 0,
    }
    advantage_adapter_result: dict[str, Any] = {
        "ok": True,
        "events": [],
        "issues": [],
        "candidate_count": 0,
        "adapted_count": 0,
    }
    try:
        with service.atomic_write() as connection:
            validation_started = time.perf_counter()
            (
                legacy_deltas,
                item_candidates,
                advantage_candidates,
            ) = _split_extraction_candidates_by_family(deltas)
            events, issues = legacy_deltas_to_events(
                service,
                legacy_deltas,
                artifact_context=artifact_context,
                receipt_id=str(turn["receipt_id"]),
                assistant_hash=assistant_hash,
            )
            item_artifact_context = {
                **artifact_context,
                "receipt_id": str(turn["receipt_id"]),
                "assistant_sha256": assistant_hash,
            }
            item_adapter_result = _adapt_item_extraction_candidates(
                service,
                connection,
                item_candidates,
                assistant_text=assistant_text,
                artifact_context=item_artifact_context,
            )
            events.extend(
                dict(event)
                for event in item_adapter_result.get("events") or []
                if isinstance(event, Mapping)
            )
            issues.extend(
                dict(issue)
                    for issue in item_adapter_result.get("issues") or []
                    if isinstance(issue, Mapping)
                )
            advantage_adapter_result = (
                _adapt_advantage_extraction_candidates(
                    service,
                    connection,
                    advantage_candidates,
                    assistant_text=assistant_text,
                    artifact_context=item_artifact_context,
                    experience_manifest=experience_manifest,
                )
            )
            events.extend(
                dict(event)
                for event in advantage_adapter_result.get("events") or []
                if isinstance(event, Mapping)
            )
            issues.extend(
                dict(issue)
                for issue in advantage_adapter_result.get("issues") or []
                if isinstance(issue, Mapping)
            )
            for skipped in extraction_skipped:
                issues.append(
                    {
                        "code": "EXTRACTION_DELTA_SKIPPED",
                        "severity": "warning",
                        "message": str(
                            skipped.get("reason") or "delta skipped"
                        ),
                        "details": dict(skipped),
                    }
                )
            validation_ms = round(
                (time.perf_counter() - validation_started) * 1000.0,
                3,
            )
            error_issues = [
                issue
                for issue in issues
                if str(issue.get("severity") or "error").casefold()
                in {"error", "critical"}
            ]
            events_for_proposal = events
            if shadow_only:
                normalized_stage = normalize_stage(
                    str(
                        artifact_context.get("artifact_stage")
                        or "brainstorm"
                    )
                )
                normalized_branch = str(
                    artifact_context.get("branch_id") or "main"
                )
                events_for_proposal = [
                    normalize_event(
                        event,
                        artifact_stage=normalized_stage,
                        branch_id=normalized_branch,
                        chapter_no=artifact_context.get("chapter_no"),
                        scene_index=artifact_context.get("scene_index"),
                    )
                    for event in events
                ]
            shadow_comparison: dict[str, Any] | None = None
            if shadow_only:
                assert authoritative_shadow is not None
                shadow_events_sha256 = _sha256(
                    _canonical_json(events_for_proposal)
                )
                exact_match = (
                    authoritative_shadow["events"]
                    == events_for_proposal
                )
                shadow_comparison = {
                    "status": (
                        "exact_match" if exact_match else "mismatch"
                    ),
                    "exact_match": exact_match,
                    "authoritative_events_sha256": (
                        authoritative_shadow["events_sha256"]
                    ),
                    "shadow_events_sha256": shadow_events_sha256,
                }
            no_delta = (
                no_delta_without_proposal
                and not deltas
                and not extraction_skipped
                and not events
                and not error_issues
            )
            if no_delta:
                result = {
                    "status": "no_delta",
                    "result_kind": "no_delta",
                    "request_id": str(turn["request_id"]),
                    "receipt_id": str(turn["receipt_id"]),
                    "receipt": str(turn["receipt_id"]),
                    "proposal_id": "",
                    "prepared_canon_revision": prepared_revision,
                    "active_projection_hash": prepared_identity[
                        "active_projection_hash"
                    ],
                    "prompt_hash": prepared_identity["prompt_hash"],
                    "retrieved_context_digest": prepared_identity[
                        "retrieved_context_digest"
                    ],
                    "context_digest": prepared_identity[
                        "retrieved_context_digest"
                    ],
                    "lifecycle_identity": dict(
                        prepared_identity["lifecycle_identity"]
                    ),
                    "identity": {
                        "prompt_hash": prepared_identity["prompt_hash"],
                        "retrieved_context_digest": prepared_identity[
                            "retrieved_context_digest"
                        ],
                        "prepared_canon_revision": prepared_revision,
                        "active_projection_hash": prepared_identity[
                            "active_projection_hash"
                        ],
                        "lifecycle_identity": dict(
                            prepared_identity["lifecycle_identity"]
                        ),
                    },
                    "canon_revision_unchanged": prepared_revision,
                    "recorded_events": [],
                    "proposal_events": [],
                    "issues": issues,
                    "item_candidate_adapter": {
                        key: item_adapter_result.get(key)
                        for key in (
                            "ok",
                            "candidate_count",
                            "adapted_count",
                        )
                    },
                    "advantage_candidate_adapter": {
                        key: advantage_adapter_result.get(key)
                        for key in (
                            "ok",
                            "candidate_count",
                            "adapted_count",
                        )
                    },
                    "remote": remote,
                    "lifecycle_mode": "strict_proposal",
                    "shadow_only": shadow_only,
                    "authoritative_proposal_id": (
                        str(
                            authoritative_shadow["proposal_id"]
                        )
                        if authoritative_shadow is not None
                        else ""
                    ),
                    "comparison": shadow_comparison,
                    "idempotent": False,
                    "identity_check": {
                        "status": "ok",
                        "remote_called": True,
                    },
                    "telemetry": {
                        "identity_validation_ms": identity_validation_ms,
                        "extract_remote_ms": extract_remote_ms,
                        "validation_ms": validation_ms,
                        "proposal_persist_ms": 0.0,
                    },
                }
                if not shadow_only:
                    _ensure_turn_v1_columns(connection)
                    connection.execute(
                        """
                        UPDATE turns
                        SET assistant_hash=?, status='no_delta',
                            remote_json=?, result_json=?, error='',
                            completed_at=?
                        WHERE request_id=?
                        """,
                        (
                            assistant_hash,
                            _canonical_json(remote),
                            _canonical_json(result),
                            _utc_now(),
                            str(turn["request_id"]),
                        ),
                    )
                return result
            proposal_started = time.perf_counter()
            proposal_payload = {
                "runtime_version": RUNTIME_VERSION,
                "request_id": str(turn["request_id"]),
                "receipt_id": str(turn["receipt_id"]),
                "prompt": effective_prompt,
                "assistant_text": assistant_text,
                "assistant_sha256": assistant_hash,
                "extracted_deltas": deltas,
                "extraction_skipped": extraction_skipped,
                "item_candidate_adapter": {
                    key: item_adapter_result.get(key)
                    for key in (
                        "ok",
                        "candidate_count",
                        "adapted_count",
                    )
                },
                "advantage_candidate_adapter": {
                    key: advantage_adapter_result.get(key)
                    for key in (
                        "ok",
                        "candidate_count",
                        "adapted_count",
                    )
                },
                "extract_model": runtime.extract.model,
                "artifact_context": artifact_context,
                "prompt_hash": prepared_identity["prompt_hash"],
                "retrieved_context_digest": prepared_identity[
                    "retrieved_context_digest"
                ],
                "active_projection_hash": prepared_identity[
                    "active_projection_hash"
                ],
                "prepared_canon_revision": prepared_revision,
                "lifecycle_identity": dict(
                    prepared_identity["lifecycle_identity"]
                ),
            }
            proposal_payload.update(normalized_proposal_binding)
            if shadow_only:
                assert authoritative_shadow is not None
                assert shadow_comparison is not None
                proposal_payload["extraction_shadow"] = {
                    "mode": "async_shadow",
                    "authoritative_proposal_id": str(
                        authoritative_shadow["proposal_id"]
                    ),
                    "acceptable": False,
                    "barrier_blocking": False,
                    "comparison": shadow_comparison,
                }
            proposal_idempotency_key = (
                (
                    "stop-shadow:"
                    + str(
                        normalized_proposal_binding[
                            "extraction_job_id"
                        ]
                    )
                    + ":"
                    + assistant_hash
                )
                if shadow_only
                else (
                    "stop:"
                    + str(turn["request_id"])
                    + ":"
                    + assistant_hash
                )
            )
            proposal = service.save_proposal(
                events=events_for_proposal,
                payload=proposal_payload,
                artifact_id=str(artifact_context.get("artifact_id") or ""),
                artifact_stage=str(
                    artifact_context.get("artifact_stage") or "brainstorm"
                ),
                branch_id=str(artifact_context.get("branch_id") or "main"),
                chapter_no=artifact_context.get("chapter_no"),
                scene_index=artifact_context.get("scene_index"),
                artifact_revision=artifact_context.get(
                    "artifact_revision"
                ),
                prepared_canon_revision=prepared_revision,
                issues=issues,
                proposal_kind="story_delta",
                idempotency_key=proposal_idempotency_key,
            )
            if shadow_only:
                rejected_at = _utc_now()
                connection.execute(
                    """
                    UPDATE proposals
                    SET canon_status='rejected',
                        status_reason='async_shadow_non_accepting',
                        updated_at=?
                    WHERE proposal_id=?
                    """,
                    (rejected_at, str(proposal["proposal_id"])),
                )
                connection.execute(
                    """
                    UPDATE artifacts
                    SET canon_status='rejected', active=0, updated_at=?
                    WHERE artifact_version_id=(
                        SELECT artifact_version_id
                        FROM proposals
                        WHERE proposal_id=?
                    )
                    """,
                    (rejected_at, str(proposal["proposal_id"])),
                )
                proposal = {
                    **proposal,
                    "canon_status": "rejected",
                    "status_reason": "async_shadow_non_accepting",
                }
            proposal_persist_ms = round(
                (time.perf_counter() - proposal_started) * 1000.0,
                3,
            )
            result = {
                "status": (
                    "quarantined"
                    if proposal.get("validation_status") == "quarantined"
                    else "proposed"
                ),
                "result_kind": "proposal",
                "request_id": str(turn["request_id"]),
                "receipt_id": str(turn["receipt_id"]),
                "receipt": str(turn["receipt_id"]),
                "proposal_id": proposal["proposal_id"],
                "prepared_canon_revision": prepared_revision,
                "active_projection_hash": prepared_identity[
                    "active_projection_hash"
                ],
                "prompt_hash": prepared_identity["prompt_hash"],
                "retrieved_context_digest": prepared_identity[
                    "retrieved_context_digest"
                ],
                "context_digest": prepared_identity[
                    "retrieved_context_digest"
                ],
                "lifecycle_identity": dict(
                    prepared_identity["lifecycle_identity"]
                ),
                "identity": {
                    "prompt_hash": prepared_identity["prompt_hash"],
                    "retrieved_context_digest": prepared_identity[
                        "retrieved_context_digest"
                    ],
                    "prepared_canon_revision": prepared_revision,
                    "active_projection_hash": prepared_identity[
                        "active_projection_hash"
                    ],
                    "lifecycle_identity": dict(
                        prepared_identity["lifecycle_identity"]
                    ),
                },
                "canon_revision_unchanged": prepared_revision,
                "recorded_events": [],
                "proposal_events": proposal["events"],
                "issues": proposal["issues"],
                "item_candidate_adapter": {
                    key: item_adapter_result.get(key)
                    for key in (
                        "ok",
                        "candidate_count",
                        "adapted_count",
                    )
                },
                "advantage_candidate_adapter": {
                    key: advantage_adapter_result.get(key)
                    for key in (
                        "ok",
                        "candidate_count",
                        "adapted_count",
                    )
                },
                "remote": remote,
                "lifecycle_mode": "strict_proposal",
                "shadow_only": shadow_only,
                "authoritative_proposal_id": (
                    str(authoritative_shadow["proposal_id"])
                    if authoritative_shadow is not None
                    else ""
                ),
                "comparison": shadow_comparison,
                "canon_status": str(
                    proposal.get("canon_status") or "proposed"
                ),
                "status_reason": str(
                    proposal.get("status_reason") or ""
                ),
                "idempotent": False,
                "identity_check": {
                    "status": "ok",
                    "remote_called": True,
                },
                "telemetry": {
                    "identity_validation_ms": identity_validation_ms,
                    "extract_remote_ms": extract_remote_ms,
                    "validation_ms": validation_ms,
                    "proposal_persist_ms": proposal_persist_ms,
                },
            }
            if not shadow_only:
                _ensure_turn_v1_columns(connection)
                connection.execute(
                    """
                    UPDATE turns
                    SET assistant_hash=?, status='proposed', remote_json=?,
                        result_json=?, error='', completed_at=?
                    WHERE request_id=?
                    """,
                    (
                        assistant_hash,
                        _canonical_json(remote),
                        _canonical_json(result),
                        _utc_now(),
                        str(turn["request_id"]),
                    ),
                )
    except Exception as exc:
        if not shadow_only:
            try:
                state_rag._mark_turn_failed(
                    runtime,
                    str(turn["request_id"]),
                    str(turn["receipt_id"]),
                    str(exc),
                    remote,
                )
            except Exception:
                pass
        return {
            "status": "failed",
            "reason": str(exc),
            "request_id": str(turn["request_id"]),
            "receipt_id": str(turn["receipt_id"]),
            "recorded_events": [],
            "proposal_events": [],
            "remote": remote,
            "lifecycle_mode": "strict_proposal",
        }
    return result


def query_continuity(
    project_root: Path | str,
    *,
    mention: str | None = None,
    entity_id: str | None = None,
    system_id: str | None = None,
    track_id: str | None = None,
    ability_id: str | None = None,
    resource_id: str | None = None,
    fact_type: str | None = None,
    scope: str | None = None,
    chapter_no: int | None = None,
    scene_index: int | None = None,
    branch_id: str | None = None,
    include_historical: bool = False,
    include_provisional: bool = False,
    include_relations: bool = True,
) -> dict[str, Any]:
    service = ContinuityService(Path(project_root).expanduser().resolve())
    resolution = None
    resolved_entity = entity_id
    if mention:
        resolution = service.resolve_mention(mention, persist=False)
        if resolution["status"] == "RESOLVED":
            resolved_entity = str(resolution["entity_id"])
    facts = service.query_facts(
        entity_id=resolved_entity,
        fact_type=fact_type,
        scope=scope,
        chapter_no=chapter_no,
        scene_index=scene_index,
        include_timeless=True,
        include_historical=include_historical,
        branch_id=branch_id,
        include_provisional=include_provisional,
    )
    relations: dict[str, Any] | None = None
    if include_relations and resolved_entity:
        relations = service.query_relations(
            resolved_entity,
            chapter_no=chapter_no,
            scene_index=scene_index,
            scope=scope,
            branch_id=branch_id,
            include_provisional=include_provisional,
        )
    power_state: dict[str, Any] | None = None
    resolved_type = (
        _entity_type(service, resolved_entity) if resolved_entity else None
    )
    power_filters = {
        "actor_entity_id": (
            resolved_entity
            if resolved_type in {"character", "group", "summon"}
            else None
        ),
        "system_entity_id": (
            system_id
            or (
                resolved_entity
                if resolved_type == "power_system"
                else None
            )
        ),
        "track_entity_id": (
            track_id
            or (
                resolved_entity
                if resolved_type == "progression_track"
                else None
            )
        ),
        "ability_entity_id": (
            ability_id
            or (
                resolved_entity
                if resolved_type == "ability"
                else None
            )
        ),
        "resource_entity_id": (
            resource_id
            or (
                resolved_entity
                if resolved_type == "resource_pool"
                else None
            )
        ),
    }
    if any(power_filters.values()):
        power_state = query_power_state(
            project_root,
            entity_id=power_filters["actor_entity_id"],
            system_id=power_filters["system_entity_id"],
            track_id=power_filters["track_entity_id"],
            ability_id=power_filters["ability_entity_id"],
            resource_id=power_filters["resource_entity_id"],
            chapter_no=chapter_no,
            scene_index=scene_index,
            branch_id=branch_id,
            include_historical=include_historical,
            include_provisional=include_provisional,
        )
    return {
        "status": "ready",
        "canon_revisions": service.get_canon_revisions(),
        "projection_hash": service.projection_hash(),
        "resolution": resolution,
        "facts": facts["facts"],
        "relations": (relations or {}).get("relations", []),
        "power_state": power_state,
    }


_COMPAT_CATEGORY_FACT_TYPES = {
    "character_state": {"state", "goal", "injury", "commitment"},
    "relationship": {"relation"},
    "location": {"location"},
    "inventory": {"inventory"},
    "story_time": {"time"},
    "world_state": {"world_rule", "faction", "open_loop", "ability", "belief"},
    "ability": {"ability"},
    "progression": {"progression"},
    "resource": {"resource"},
    "status": {"status_effect"},
    "binding": {"power_binding"},
    "qualification": {"qualification"},
    "observation": {"power_observation"},
}
_QUERY_FACT_HINTS = {
    "location": ("位置", "哪里", "哪儿", "地点", "所在", "抵达", "离开"),
    "inventory": ("道具", "持有", "拥有", "物品", "库存", "交给", "失去"),
    "relation": ("关系", "敌友", "盟友", "仇敌", "信任", "好感"),
    "time": ("时间", "日期", "时刻", "第几天", "多久"),
    "ability": ("能力", "技能", "境界", "功法", "冷却", "代价"),
    "progression": ("境界", "等级", "晋升", "突破", "职业", "成长轨"),
    "resource": ("法力", "灵力", "体力", "经验", "资源", "余额", "消耗"),
    "status_effect": ("状态", "增益", "减益", "伤势", "污染", "封印"),
    "power_binding": ("绑定", "装备", "契约", "召唤", "来源", "同调"),
    "qualification": ("资格", "权限", "职业", "执照", "准备位"),
    "power_observation": ("观察", "已知能力", "情报", "弱点", "传闻"),
    "belief": ("知道", "认知", "相信", "误解", "情报"),
    "open_loop": ("伏笔", "悬念", "承诺", "债务", "未决"),
}


def query_continuity_text(
    project_root: Path | str,
    query: str = "",
    *,
    categories: Sequence[str] | None = None,
    top_k: int | None = None,
    subject: str | None = None,
    category: str | None = None,
    include_historical: bool = False,
) -> dict[str, Any]:
    """Compatibility query over accepted v1 projections.

    The legacy public tools accept a free-text question instead of stable
    entity IDs.  For config v3 this adapter resolves every canonical name or
    confirmed alias mentioned in the question, applies category hints, and
    returns accepted facts only.
    """

    root = Path(project_root).expanduser().resolve()
    service = ContinuityService(root)
    raw_query = str(query or subject or "").strip()
    normalized_query = re.sub(r"\s+", "", raw_query).casefold()
    requested_categories = [
        str(item).strip()
        for item in [*(categories or ()), *([category] if category else [])]
        if str(item or "").strip()
    ]
    requested_fact_types: set[str] = set()
    for item in requested_categories:
        requested_fact_types.update(
            _COMPAT_CATEGORY_FACT_TYPES.get(item, {item})
        )

    with service.store.read_connection() as connection:
        entity_rows = connection.execute(
            """
            SELECT entity_id, canonical_name
            FROM entities
            ORDER BY canonical_name, entity_id
            """
        ).fetchall()
        alias_rows = connection.execute(
            """
            SELECT entity_id, alias_text
            FROM entity_aliases
            WHERE alias_status='confirmed'
            ORDER BY alias_text, entity_id
            """
        ).fetchall()
    names: dict[str, str] = {
        str(row["entity_id"]): str(row["canonical_name"])
        for row in entity_rows
    }
    mentions: list[tuple[int, str, str]] = []
    for row in entity_rows:
        name = str(row["canonical_name"]).strip()
        normalized = re.sub(r"\s+", "", name).casefold()
        if normalized and normalized in normalized_query:
            mentions.append((len(normalized), str(row["entity_id"]), name))
    for row in alias_rows:
        alias = str(row["alias_text"]).strip()
        normalized = re.sub(r"\s+", "", alias).casefold()
        if normalized and normalized in normalized_query:
            mentions.append((len(normalized), str(row["entity_id"]), alias))
    mentions.sort(key=lambda item: (-item[0], item[1], item[2]))
    matched_entity_ids = list(dict.fromkeys(item[1] for item in mentions))

    if subject and not matched_entity_ids:
        resolution = service.resolve_mention(subject, persist=False)
        if resolution.get("status") == "RESOLVED":
            matched_entity_ids = [str(resolution["entity_id"])]
    else:
        resolution = (
            {
                "status": "RESOLVED",
                "entity_ids": matched_entity_ids,
                "mentions": [
                    {
                        "entity_id": entity_id,
                        "matched_text": matched_text,
                    }
                    for _length, entity_id, matched_text in mentions
                ],
            }
            if matched_entity_ids
            else {"status": "UNRESOLVED", "entity_ids": [], "mentions": []}
        )

    queried = service.query_facts(
        include_timeless=True,
        include_historical=include_historical,
    )
    ranked: list[tuple[float, dict[str, Any]]] = []
    query_terms = {
        token.casefold()
        for token in re.findall(
            r"[A-Za-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]{2,}",
            raw_query,
        )
    }
    for fact in queried.get("facts") or []:
        fact_type = str(fact.get("fact_type") or "")
        endpoints = {
            str(value)
            for value in (
                fact.get("entity_id"),
                fact.get("subject_entity_id"),
                fact.get("target_entity_id"),
            )
            if value
        }
        if matched_entity_ids and not endpoints.intersection(
            matched_entity_ids
        ):
            continue
        if requested_fact_types and fact_type not in requested_fact_types:
            continue
        decorated = dict(fact)
        decorated["entity_name"] = names.get(
            str(fact.get("entity_id") or "")
        )
        decorated["subject_name"] = names.get(
            str(fact.get("subject_entity_id") or "")
        )
        decorated["target_name"] = names.get(
            str(fact.get("target_entity_id") or "")
        )
        score = 0.0
        if endpoints.intersection(matched_entity_ids):
            score += 100.0
        if fact_type in requested_fact_types:
            score += 40.0
        if any(
            hint in raw_query
            for hint in _QUERY_FACT_HINTS.get(fact_type, ())
        ):
            score += 30.0
        document = _canonical_json(
            {
                "entity": decorated.get("entity_name"),
                "subject": decorated.get("subject_name"),
                "target": decorated.get("target_name"),
                "field": fact.get("field"),
                "value": fact.get("value"),
                "fact_type": fact_type,
            }
        ).casefold()
        score += 2.0 * sum(term in document for term in query_terms)
        decorated["score"] = score
        ranked.append((score, decorated))
    ranked.sort(
        key=lambda item: (
            -item[0],
            str(item[1].get("scope") or ""),
            str(item[1].get("fact_type") or ""),
            str(item[1].get("fact_key") or ""),
        )
    )
    limit = max(1, min(200, int(top_k or 20)))
    facts = [item for _score, item in ranked[:limit]]

    relations: list[dict[str, Any]] = []
    seen_relations: set[str] = set()
    if not requested_fact_types or "relation" in requested_fact_types:
        for entity_id in matched_entity_ids:
            result = service.query_relations(entity_id)
            for relation in result.get("relations") or []:
                key = _canonical_json(relation)
                if key in seen_relations:
                    continue
                seen_relations.add(key)
                decorated = dict(relation)
                decorated["subject_name"] = names.get(
                    str(relation.get("subject_entity_id") or "")
                )
                decorated["target_name"] = names.get(
                    str(relation.get("target_entity_id") or "")
                )
                relations.append(decorated)
    return {
        "status": "ready",
        "lifecycle_mode": "strict_proposal",
        "query": raw_query,
        "categories": requested_categories,
        "canon_revisions": service.get_canon_revisions(),
        "projection_hash": service.projection_hash(),
        "resolution": resolution,
        "facts_count": len(facts),
        "facts": facts,
        "relations": relations,
        "absence_is_confirmed": False,
    }


_DEFAULT_POWER_KNOWLEDGE_PLANES = (
    "objective",
    "actor_belief",
    "public_narrative",
    "reader_disclosed",
)
_POWER_FACT_SECTIONS = {
    "progression": "progression",
    "resource": "resources",
    "ability": "abilities",
    "status_effect": "statuses",
    "power_binding": "bindings",
    "qualification": "qualifications",
    "power_observation": "observations",
    "power_spec": "specifications",
}
_POWER_ENDPOINT_TYPES = {
    "actor": {"character", "group", "summon"},
    "system": {"power_system"},
    "track": {"progression_track"},
    "ability": {"ability"},
    "resource": {"resource_pool"},
}


def _power_empty_metadata(
    knowledge_planes: Sequence[str],
) -> dict[str, Any]:
    return {
        "canon_revision": 0,
        "canon_revisions": {"head": 0, "active": 0},
        "projection_hash": None,
        "knowledge_planes": list(knowledge_planes),
    }


def _validated_power_story_position(
    chapter_no: int | None,
    scene_index: int | None,
) -> tuple[int | None, int | None]:
    return (
        validate_positive_int(
            chapter_no,
            "chapter_no",
            allow_none=True,
            minimum=1,
        ),
        validate_positive_int(
            scene_index,
            "scene_index",
            allow_none=True,
            minimum=0,
        ),
    )


def _power_readonly_service(
    project_root: Path,
) -> ContinuityService | None:
    """Open a disposable continuity snapshot without touching project files.

    The normal ContinuityService is intentionally a write-path service:
    ``read_connection`` first creates or migrates the schema.  Power query
    surfaces, however, promise source-tree zero writes.  Copying the SQLite
    database and recovery journal into a temporary project keeps legacy
    migration/replay compatibility while making every mutation disposable.
    Source files are fingerprinted before and after the copy; a concurrently
    changing database is retried rather than queried through a torn snapshot.
    """

    source_db = project_root / ".plot-rag" / "state.sqlite3"
    if not source_db.is_file():
        return None
    temporary = tempfile.TemporaryDirectory(
        prefix="plot-rag-power-readonly-"
    )
    snapshot_root = Path(temporary.name) / "project"
    snapshot_db = snapshot_root / ".plot-rag" / "state.sqlite3"
    snapshot_db.parent.mkdir(parents=True, exist_ok=True)
    try:
        suffixes = ("", "-wal", "-journal")
        copied = False
        for _attempt in range(3):
            sources = {
                suffix: source
                for suffix in suffixes
                if (source := Path(str(source_db) + suffix)).is_file()
            }
            before = {
                suffix: (source.stat().st_size, source.stat().st_mtime_ns)
                for suffix, source in sources.items()
            }
            for suffix in suffixes:
                target = Path(str(snapshot_db) + suffix)
                if suffix in sources:
                    shutil.copy2(sources[suffix], target)
                elif target.exists():
                    target.unlink()
            after = {
                suffix: (source.stat().st_size, source.stat().st_mtime_ns)
                for suffix in suffixes
                if (source := Path(str(source_db) + suffix)).is_file()
            }
            if before == after:
                copied = True
                break
        if not copied:
            raise RuntimeError(
                "continuity database changed while creating a read-only snapshot"
            )
        service = ContinuityService(snapshot_root, db_path=snapshot_db)
    except Exception:
        temporary.cleanup()
        raise
    # Keep the snapshot alive for exactly as long as the service and invoke
    # explicit cleanup so ResourceWarning-as-error test runs stay clean.
    weakref.finalize(service, temporary.cleanup)
    return service


def _service_query_call(
    service: ContinuityService,
    method_name: str,
    **kwargs: Any,
) -> dict[str, Any] | None:
    method = getattr(service, method_name, None)
    if not callable(method):
        return None
    signature = inspect.signature(method)
    supported = {
        key: value
        for key, value in kwargs.items()
        if key in signature.parameters
    }
    result = method(**supported)
    return dict(result) if isinstance(result, Mapping) else {"result": result}


def _power_resolution(
    service: ContinuityService,
    value: str | None,
    *,
    label: str,
) -> tuple[str | None, dict[str, Any] | None]:
    text = str(value or "").strip()
    if not text:
        return None, None
    entity_type = _entity_type(service, text)
    if entity_type is not None:
        if entity_type not in _POWER_ENDPOINT_TYPES[label]:
            return None, {
                "status": "TYPE_MISMATCH",
                "entity_id": text,
                "entity_type": entity_type,
                "expected_types": sorted(_POWER_ENDPOINT_TYPES[label]),
            }
        return text, {
            "status": "RESOLVED",
            "entity_id": text,
            "entity_type": entity_type,
            "match": "entity_id",
        }
    resolution = service.resolve_mention(text, persist=False)
    if resolution.get("status") != "RESOLVED":
        return None, dict(resolution)
    entity_id = str(resolution["entity_id"])
    entity_type = _entity_type(service, entity_id)
    if entity_type not in _POWER_ENDPOINT_TYPES[label]:
        return None, {
            **dict(resolution),
            "status": "TYPE_MISMATCH",
            "entity_type": entity_type,
            "expected_types": sorted(_POWER_ENDPOINT_TYPES[label]),
        }
    return entity_id, {
        **dict(resolution),
        "entity_type": entity_type,
    }


def _knowledge_planes(
    values: Sequence[str] | None,
) -> tuple[str, ...]:
    selected = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in (values or _DEFAULT_POWER_KNOWLEDGE_PLANES)
            if str(value or "").strip()
        )
    )
    allowed = {
        "objective",
        "actor_belief",
        "public_narrative",
        "reader_disclosed",
        "author_plan",
    }
    invalid = sorted(set(selected) - allowed)
    if invalid:
        raise ValueError(
            "unsupported knowledge plane(s): " + ", ".join(invalid)
        )
    return selected


def _filter_power_planes(
    value: Any,
    allowed_planes: set[str],
) -> Any:
    if isinstance(value, list):
        filtered: list[Any] = []
        for item in value:
            if isinstance(item, Mapping):
                plane = str(
                    item.get("knowledge_plane")
                    or (item.get("value") or {}).get("knowledge_plane")
                    if isinstance(item.get("value"), Mapping)
                    else item.get("knowledge_plane")
                    or ""
                )
                if plane and plane not in allowed_planes:
                    continue
            filtered.append(_filter_power_planes(item, allowed_planes))
        return filtered
    if isinstance(value, Mapping):
        return {
            str(key): _filter_power_planes(item, allowed_planes)
            for key, item in value.items()
        }
    return value


def _power_query_metadata(
    service: ContinuityService,
    *,
    knowledge_planes: Sequence[str],
) -> dict[str, Any]:
    revisions = service.get_canon_revisions()
    return {
        "canon_revision": revisions["active"],
        "canon_revisions": revisions,
        "projection_hash": service.projection_hash(),
        "knowledge_planes": list(knowledge_planes),
    }


def _fallback_power_state(
    service: ContinuityService,
    *,
    actor_entity_id: str | None,
    system_entity_id: str | None,
    track_entity_id: str | None,
    ability_entity_id: str | None,
    resource_entity_id: str | None,
    chapter_no: int | None,
    scene_index: int | None,
    branch_id: str | None,
    knowledge_planes: Sequence[str],
    include_historical: bool,
    include_provisional: bool,
) -> dict[str, Any]:
    queried = service.query_facts(
        chapter_no=chapter_no,
        scene_index=scene_index,
        include_timeless=True,
        include_historical=include_historical,
        branch_id=branch_id,
        include_provisional=include_provisional,
    )
    endpoints = {
        value
        for value in (
            actor_entity_id,
            system_entity_id,
            track_entity_id,
            ability_entity_id,
            resource_entity_id,
        )
        if value
    }
    sections: dict[str, list[dict[str, Any]]] = {
        section: [] for section in _POWER_FACT_SECTIONS.values()
    }
    source_event_ids: set[str] = set()
    for fact in queried.get("facts") or []:
        fact_type = str(fact.get("fact_type") or "")
        section = _POWER_FACT_SECTIONS.get(fact_type)
        if section is None:
            continue
        fact_endpoints = {
            str(value)
            for value in (
                fact.get("entity_id"),
                fact.get("subject_entity_id"),
                fact.get("target_entity_id"),
                (fact.get("value") or {}).get("system_entity_id")
                if isinstance(fact.get("value"), Mapping)
                else None,
                (fact.get("value") or {}).get("track_entity_id")
                if isinstance(fact.get("value"), Mapping)
                else None,
                (fact.get("value") or {}).get("resource_entity_id")
                if isinstance(fact.get("value"), Mapping)
                else None,
            )
            if value
        }
        if endpoints and not endpoints.intersection(fact_endpoints):
            continue
        sections[section].append(dict(fact))
        if fact.get("source_event_id"):
            source_event_ids.add(str(fact["source_event_id"]))
    return {
        "status": "ready",
        **sections,
        "source_event_ids": sorted(source_event_ids),
        "unknown_or_conflicted": [],
    }


def list_power_systems(
    project_root: Path | str,
    *,
    chapter_no: int | None = None,
    scene_index: int | None = None,
    branch_id: str | None = None,
    include_provisional: bool = False,
    knowledge_planes: Sequence[str] | None = None,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    chapter_no, scene_index = _validated_power_story_position(
        chapter_no,
        scene_index,
    )
    planes = _knowledge_planes(knowledge_planes)
    service = _power_readonly_service(root)
    if service is None:
        return {
            "status": "uninitialized",
            "systems": [],
            "count": 0,
            "source_event_ids": [],
            "unknown_or_conflicted": [],
            **_power_empty_metadata(planes),
        }
    direct = _service_query_call(
        service,
        "list_power_systems",
        chapter_no=chapter_no,
        scene_index=scene_index,
        branch_id=branch_id,
        include_provisional=include_provisional,
        knowledge_planes=planes,
    )
    if direct is None:
        with service.store.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT entity_id, canonical_name, attributes_json
                FROM entities
                WHERE entity_type='power_system'
                ORDER BY canonical_name, entity_id
                """
            ).fetchall()
        systems = [
            {
                "system_id": str(row["entity_id"]),
                "name": str(row["canonical_name"]),
                "profile": (
                    _json_load(row["attributes_json"], {}).get("profile")
                ),
                "namespace": (
                    _json_load(row["attributes_json"], {}).get("namespace")
                ),
                "modeling_status": (
                    _json_load(row["attributes_json"], {}).get(
                        "modeling_status", "partial"
                    )
                ),
            }
            for row in rows
        ]
        direct = {
            "status": "ready",
            "systems": systems,
            "count": len(systems),
        }
    filtered = _filter_power_planes(direct, set(planes))
    return {
        **filtered,
        **_power_query_metadata(service, knowledge_planes=planes),
    }


def query_power_state(
    project_root: Path | str,
    *,
    mention: str | None = None,
    entity_id: str | None = None,
    system_id: str | None = None,
    track_id: str | None = None,
    ability_id: str | None = None,
    resource_id: str | None = None,
    chapter_no: int | None = None,
    scene_index: int | None = None,
    branch_id: str | None = None,
    knowledge_planes: Sequence[str] | None = None,
    include_historical: bool = False,
    include_provisional: bool = False,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    chapter_no, scene_index = _validated_power_story_position(
        chapter_no,
        scene_index,
    )
    planes = _knowledge_planes(knowledge_planes)
    service = _power_readonly_service(root)
    if service is None:
        return {
            "status": "uninitialized",
            **{
                section: []
                for section in _POWER_FACT_SECTIONS.values()
            },
            "source_event_ids": [],
            "unknown_or_conflicted": [],
            "resolution": {},
            "filters": {},
            **_power_empty_metadata(planes),
        }
    resolved: dict[str, str | None] = {}
    resolutions: dict[str, Any] = {}
    endpoint_values = {
        "actor": entity_id or mention,
        "system": system_id,
        "track": track_id,
        "ability": ability_id,
        "resource": resource_id,
    }
    for label, value in endpoint_values.items():
        resolved_id, resolution = _power_resolution(
            service,
            value,
            label=label,
        )
        resolved[label] = resolved_id
        if resolution is not None:
            resolutions[label] = resolution
            if resolution.get("status") != "RESOLVED":
                return {
                    "status": str(resolution.get("status") or "UNRESOLVED").lower(),
                    "resolution": resolutions,
                    **_power_query_metadata(
                        service,
                        knowledge_planes=planes,
                    ),
                }
    kwargs = {
        "entity_id": resolved["actor"],
        "actor_entity_id": resolved["actor"],
        "system_entity_id": resolved["system"],
        "track_entity_id": resolved["track"],
        "ability_entity_id": resolved["ability"],
        "resource_entity_id": resolved["resource"],
        "chapter_no": chapter_no,
        "scene_index": scene_index,
        "branch_id": branch_id,
        "knowledge_planes": planes,
        "include_historical": include_historical,
        "include_history": include_historical,
        "include_provisional": include_provisional,
    }
    direct = _service_query_call(service, "query_power_state", **kwargs)
    if direct is None:
        direct = _fallback_power_state(
            service,
            actor_entity_id=resolved["actor"],
            system_entity_id=resolved["system"],
            track_entity_id=resolved["track"],
            ability_entity_id=resolved["ability"],
            resource_entity_id=resolved["resource"],
            chapter_no=chapter_no,
            scene_index=scene_index,
            branch_id=branch_id,
            knowledge_planes=planes,
            include_historical=include_historical,
            include_provisional=include_provisional,
        )
    filtered = _filter_power_planes(direct, set(planes))
    source_event_ids = sorted(
        {
            str(item.get("source_event_id"))
            for section in _POWER_FACT_SECTIONS.values()
            for item in filtered.get(section, [])
            if isinstance(item, Mapping) and item.get("source_event_id")
        }
        | {
            str(value)
            for value in filtered.get("source_event_ids", [])
            if value
        }
    )
    return {
        **filtered,
        **_power_query_metadata(service, knowledge_planes=planes),
        "resolution": resolutions,
        "filters": {
            key: value for key, value in resolved.items() if value
        },
        "source_event_ids": source_event_ids,
    }


def query_progression_path(
    project_root: Path | str,
    *,
    mention: str | None = None,
    entity_id: str | None = None,
    system_id: str | None = None,
    track_id: str | None = None,
    target_rank_id: str | None = None,
    chapter_no: int | None = None,
    scene_index: int | None = None,
    branch_id: str | None = None,
    knowledge_planes: Sequence[str] | None = None,
    include_provisional: bool = False,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    chapter_no, scene_index = _validated_power_story_position(
        chapter_no,
        scene_index,
    )
    planes = _knowledge_planes(knowledge_planes)
    service = _power_readonly_service(root)
    if service is None:
        return {
            "status": "uninitialized",
            "current_progression": [],
            "legal_edges": [],
            "satisfied_prerequisites": [],
            "blocking_requirements": [
                {
                    "code": "POWER_STATE_UNINITIALIZED",
                    "message": "accepted continuity database is missing",
                }
            ],
            "unknown_or_conflicted": [],
            "source_event_ids": [],
            "resolution": {"actor": None, "track": None},
            **_power_empty_metadata(planes),
        }
    actor, actor_resolution = _power_resolution(
        service, entity_id or mention, label="actor"
    )
    track, track_resolution = _power_resolution(
        service, track_id, label="track"
    )
    if actor_resolution and actor_resolution.get("status") != "RESOLVED":
        return {
            "status": str(actor_resolution["status"]).lower(),
            "resolution": {"actor": actor_resolution},
            **_power_query_metadata(service, knowledge_planes=planes),
        }
    if track_resolution and track_resolution.get("status") != "RESOLVED":
        return {
            "status": str(track_resolution["status"]).lower(),
            "resolution": {"track": track_resolution},
            **_power_query_metadata(service, knowledge_planes=planes),
        }
    direct = _service_query_call(
        service,
        "query_progression_path",
        entity_id=actor,
        actor_entity_id=actor,
        system_entity_id=system_id,
        track_entity_id=track,
        target_rank_entity_id=target_rank_id,
        chapter_no=chapter_no,
        scene_index=scene_index,
        branch_id=branch_id,
        knowledge_planes=planes,
        include_provisional=include_provisional,
    )
    if direct is None:
        state = query_power_state(
            root,
            entity_id=actor,
            system_id=system_id,
            track_id=track,
            chapter_no=chapter_no,
            scene_index=scene_index,
            branch_id=branch_id,
            knowledge_planes=planes,
            include_provisional=include_provisional,
        )
        direct = {
            "status": "insufficient_model",
            "current_progression": state.get("progression", []),
            "legal_edges": [],
            "satisfied_prerequisites": [],
            "blocking_requirements": [
                {
                    "code": "POWER_GRAPH_UNAVAILABLE",
                    "message": "accepted progression edges are unavailable",
                }
            ],
            "unknown_or_conflicted": state.get("unknown_or_conflicted", []),
            "source_event_ids": state.get("source_event_ids", []),
        }
    elif "legal_edges" not in direct:
        tracks = [
            dict(item)
            for item in direct.get("tracks", [])
            if isinstance(item, Mapping)
        ]
        legal_edges: list[dict[str, Any]] = []
        source_event_ids: set[str] = {
            str(value)
            for value in direct.get("source_event_ids", [])
            if value
        }
        for track_item in tracks:
            current_rank = track_item.get("current_rank_entity_id")
            if track_item.get("source_event_id"):
                source_event_ids.add(str(track_item["source_event_id"]))
            for edge in track_item.get("edges", []):
                if not isinstance(edge, Mapping):
                    continue
                edge_item = dict(edge)
                if edge_item.get("source_event_id"):
                    source_event_ids.add(
                        str(edge_item["source_event_id"])
                    )
                from_ranks = {
                    str(value)
                    for value in edge_item.get(
                        "from_rank_entity_ids", []
                    )
                    if value is not None
                }
                if current_rank is None or str(current_rank) in from_ranks:
                    legal_edges.append(edge_item)
        direct = {
            **direct,
            "current_progression": [
                {
                    "track_entity_id": item.get("track_entity_id"),
                    "current_rank_entity_id": item.get(
                        "current_rank_entity_id"
                    ),
                    "target_rank_entity_id": item.get(
                        "target_rank_entity_id"
                    ),
                    "status": item.get("status"),
                    "path": item.get("path"),
                    "source_event_id": item.get("source_event_id"),
                }
                for item in tracks
            ],
            "legal_edges": legal_edges,
            "satisfied_prerequisites": list(
                direct.get("satisfied_prerequisites", [])
            ),
            "blocking_requirements": list(
                direct.get("blocking_requirements", [])
            ),
            "unknown_or_conflicted": list(
                direct.get("unknown_or_conflicted", [])
            ),
            "source_event_ids": sorted(source_event_ids),
        }
    return {
        **_filter_power_planes(direct, set(planes)),
        **_power_query_metadata(service, knowledge_planes=planes),
        "resolution": {
            "actor": actor_resolution,
            "track": track_resolution,
        },
    }


def explain_power_action(
    project_root: Path | str,
    *,
    action_id: str,
    mention: str | None = None,
    entity_id: str | None = None,
    system_id: str | None = None,
    track_id: str | None = None,
    ability_id: str | None = None,
    resource_id: str | None = None,
    target_rank_id: str | None = None,
    chapter_no: int | None = None,
    scene_index: int | None = None,
    branch_id: str | None = None,
    knowledge_planes: Sequence[str] | None = None,
    include_provisional: bool = False,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    chapter_no, scene_index = _validated_power_story_position(
        chapter_no,
        scene_index,
    )
    planes = _knowledge_planes(knowledge_planes)
    service = _power_readonly_service(root)
    if service is None:
        return {
            "status": "uninitialized",
            "decision": "indeterminate",
            "allowed": False,
            "reason_codes": ["POWER_STATE_UNINITIALIZED"],
            "reasons": [
                {
                    "code": "POWER_STATE_UNINITIALIZED",
                    "message": "accepted continuity database is missing",
                }
            ],
            "unknown_or_conflicted": [],
            "source_event_ids": [],
            "action_id": action_id,
            "resolution": {"actor": None, "ability": None},
            **_power_empty_metadata(planes),
        }
    actor, actor_resolution = _power_resolution(
        service, entity_id or mention, label="actor"
    )
    ability, ability_resolution = _power_resolution(
        service, ability_id, label="ability"
    )
    direct = (
        _service_query_call(
            service,
            "explain_power_action",
            entity_id=actor,
            actor_entity_id=actor,
            action=action_id,
            action_id=action_id,
            system_entity_id=system_id,
            track_entity_id=track_id,
            ability_id=ability,
            ability_entity_id=ability,
            resource_entity_id=resource_id,
            target_rank_entity_id=target_rank_id,
            story_coordinate=(
                {
                    "calendar_id": "chapter_scene",
                    "ordinal": (
                        int(chapter_no or 0) * 1_000_000
                        + int(scene_index or 0)
                    ),
                    "label": (
                        f"chapter={chapter_no},scene={scene_index}"
                    ),
                    "precision": "scene",
                }
                if chapter_no is not None
                else None
            ),
            chapter_no=chapter_no,
            scene_index=scene_index,
            branch_id=branch_id,
            knowledge_planes=planes,
            include_provisional=include_provisional,
        )
        if actor and ability
        else None
    )
    if direct is None:
        state = query_power_state(
            root,
            entity_id=actor,
            system_id=system_id,
            track_id=track_id,
            ability_id=ability,
            resource_id=resource_id,
            chapter_no=chapter_no,
            scene_index=scene_index,
            branch_id=branch_id,
            knowledge_planes=planes,
            include_provisional=include_provisional,
        )
        reasons: list[dict[str, Any]] = []
        decision = "indeterminate"
        if ability:
            abilities = list(state.get("abilities") or [])
            if not abilities:
                decision = "denied"
                reasons.append(
                    {
                        "code": "POWER_ABILITY_NOT_OWNED",
                        "message": "no accepted active ownership was found",
                    }
                )
            elif any(
                str((item.get("value") or {}).get("status") or "").casefold()
                in {"lost", "revoked", "inactive"}
                for item in abilities
                if isinstance(item, Mapping)
            ):
                decision = "denied"
                reasons.append(
                    {
                        "code": "POWER_SOURCE_INACTIVE",
                        "message": "the accepted ability state is inactive",
                    }
                )
        if decision == "indeterminate":
            reasons.append(
                {
                    "code": "POWER_PREREQUISITES_INCOMPLETE",
                    "message": (
                        "ownership may be known, but accepted cost, cooldown, "
                        "qualification, target, or environment rules are incomplete"
                    ),
                }
            )
        direct = {
            "status": "ready",
            "decision": decision,
            "allowed": decision == "allowed",
            "reason_codes": [item["code"] for item in reasons],
            "reasons": reasons,
            "unknown_or_conflicted": state.get("unknown_or_conflicted", []),
            "source_event_ids": state.get("source_event_ids", []),
        }
    elif "decision" not in direct and "executable" in direct:
        executable = bool(direct.get("executable"))
        reasons = [
            dict(item)
            for item in direct.get("reasons", [])
            if isinstance(item, Mapping)
        ]
        direct = {
            **direct,
            "decision": "allowed" if executable else "denied",
            "allowed": executable,
            "reason_codes": [
                str(item.get("code"))
                for item in reasons
                if item.get("code")
            ],
        }
    return {
        **_filter_power_planes(direct, set(planes)),
        **_power_query_metadata(service, knowledge_planes=planes),
        "action_id": action_id,
        "resolution": {
            "actor": actor_resolution,
            "ability": ability_resolution,
        },
    }


def compare_power_conditions(
    project_root: Path | str,
    *,
    left_mention: str | None = None,
    left_entity_id: str | None = None,
    right_mention: str | None = None,
    right_entity_id: str | None = None,
    system_id: str | None = None,
    chapter_no: int | None = None,
    scene_index: int | None = None,
    branch_id: str | None = None,
    knowledge_planes: Sequence[str] | None = None,
    include_provisional: bool = False,
    conditions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    chapter_no, scene_index = _validated_power_story_position(
        chapter_no,
        scene_index,
    )
    planes = _knowledge_planes(knowledge_planes)
    service = _power_readonly_service(root)
    if service is None:
        return {
            "status": "uninitialized",
            "baseline": "conditional",
            "advantages": [],
            "disadvantages": [],
            "decisive_conditions": list((conditions or {}).keys()),
            "known_to_actor": [],
            "unknown_or_conflicted": [
                {
                    "code": "POWER_STATE_UNINITIALIZED",
                    "message": "accepted continuity database is missing",
                }
            ],
            "counter_evidence": [],
            "confidence": 0.0,
            "source_event_ids": [],
            "winner": None,
            "resolution": {"left": None, "right": None},
            **_power_empty_metadata(planes),
        }
    left, left_resolution = _power_resolution(
        service, left_entity_id or left_mention, label="actor"
    )
    right, right_resolution = _power_resolution(
        service, right_entity_id or right_mention, label="actor"
    )
    direct = (
        _service_query_call(
            service,
            "compare_power_conditions",
            left_id=left,
            right_id=right,
            left_actor_entity_id=left,
            right_actor_entity_id=right,
            system_entity_id=system_id,
            chapter_no=chapter_no,
            scene_index=scene_index,
            branch_id=branch_id,
            knowledge_plane=planes[0] if planes else "objective",
            knowledge_planes=planes,
            include_provisional=include_provisional,
            conditions=dict(conditions or {}),
        )
        if left and right
        else None
    )
    if direct is None:
        left_state = query_power_state(
            root,
            entity_id=left,
            system_id=system_id,
            chapter_no=chapter_no,
            scene_index=scene_index,
            branch_id=branch_id,
            knowledge_planes=planes,
            include_provisional=include_provisional,
        )
        right_state = query_power_state(
            root,
            entity_id=right,
            system_id=system_id,
            chapter_no=chapter_no,
            scene_index=scene_index,
            branch_id=branch_id,
            knowledge_planes=planes,
            include_provisional=include_provisional,
        )
        source_event_ids = sorted(
            set(left_state.get("source_event_ids", []))
            | set(right_state.get("source_event_ids", []))
        )
        direct = {
            "status": "insufficient_model",
            "baseline": "conditional",
            "advantages": [],
            "disadvantages": [],
            "decisive_conditions": list((conditions or {}).keys()),
            "known_to_actor": [],
            "unknown_or_conflicted": [
                {
                    "code": "POWER_COMPARISON_RULES_INCOMPLETE",
                    "message": (
                        "no accepted conditional comparison or bridge rule "
                        "covers every relevant dimension"
                    ),
                }
            ],
            "counter_evidence": [],
            "confidence": 0.0,
            "source_event_ids": source_event_ids,
        }
    return {
        **_filter_power_planes(direct, set(planes)),
        **_power_query_metadata(service, knowledge_planes=planes),
        "resolution": {
            "left": left_resolution,
            "right": right_resolution,
        },
    }


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=str(path.parent),
            prefix="." + path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(path.parent),
            prefix="." + path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _commit_payload(
    service: ContinuityService,
    commit: Mapping[str, Any],
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    facts = service.query_facts(
        include_timeless=True,
        include_historical=False,
    ).get("facts") or []
    power_fact_types = {
        "progression",
        "resource",
        "ability",
        "ability_ownership",
        "ability_runtime",
        "status_effect",
        "power_binding",
        "qualification",
        "power_observation",
        "power_spec",
    }
    current = [
        f"{item.get('entity_id') or item.get('subject_entity_id')}:"
        f"{item.get('field') or item.get('field_name')}="
        f"{json.dumps(item.get('value'), ensure_ascii=False)}"
        for item in facts
        if item.get("scope") == "current"
        and item.get("fact_type") not in power_fact_types
    ]
    semantic = [
        f"{item.get('entity_id') or item.get('subject_entity_id')}:"
        f"{item.get('field') or item.get('field_name')}="
        f"{json.dumps(item.get('value'), ensure_ascii=False)}"
        for item in facts
        if item.get("scope") == "timeless"
        and item.get("fact_type") not in power_fact_types
    ]
    open_loops = [
        json.dumps(item.get("value"), ensure_ascii=False, sort_keys=True)
        for item in facts
        if item.get("fact_type") == "open_loop"
    ]
    power_facts = [
        item for item in facts if item.get("fact_type") in power_fact_types
    ]
    event_metadata: dict[str, dict[str, Any]] = {}
    for event in commit.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        event_id = str(event.get("event_id") or "")
        if not event_id:
            continue
        event_payload = dict(event.get("payload") or {})
        event_metadata[event_id] = {
            "knowledge_plane": str(
                event_payload.get("knowledge_plane") or "objective"
            ),
            "scope": str(
                event.get("scope")
                or event_payload.get("scope")
                or "current"
            ),
            "event_type": str(
                event.get("event_type")
                or event_payload.get("event_type")
                or ""
            ),
        }
    missing_event_ids = sorted(
        {
            str(item.get("source_event_id") or "")
            for item in power_facts
            if item.get("source_event_id")
        }
        - set(event_metadata)
    )
    if missing_event_ids:
        with service.store.read_connection() as connection:
            for offset in range(0, len(missing_event_ids), 500):
                current_ids = missing_event_ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in current_ids)
                rows = connection.execute(
                    "SELECT event_id, scope, event_type, payload_json "
                    f"FROM continuity_events WHERE event_id IN ({placeholders})",
                    current_ids,
                ).fetchall()
                for row in rows:
                    event_payload = _json_load(row["payload_json"], {})
                    event_metadata[str(row["event_id"])] = {
                        "knowledge_plane": str(
                            event_payload.get("knowledge_plane")
                            or "objective"
                        ),
                        "scope": str(
                            row["scope"]
                            or event_payload.get("scope")
                            or "current"
                        ),
                        "event_type": str(
                            row["event_type"]
                            or event_payload.get("event_type")
                            or ""
                        ),
                    }

    def render_power(item: Mapping[str, Any]) -> str:
        return (
            f"[{item.get('fact_type')}] "
            f"{item.get('entity_id') or item.get('subject_entity_id')}:"
            f"{item.get('field') or item.get('field_name')}="
            f"{json.dumps(item.get('value'), ensure_ascii=False, sort_keys=True)}"
        )

    def structured_power(item: Mapping[str, Any]) -> dict[str, Any]:
        source_event_id = str(item.get("source_event_id") or "")
        source = event_metadata.get(source_event_id, {})
        value = item.get("value")
        embedded_plane = (
            str(value.get("knowledge_plane") or "")
            if isinstance(value, Mapping)
            else ""
        )
        fact_type = str(item.get("fact_type") or "")
        semantic_key = str(item.get("fact_key") or "").strip()
        if not semantic_key:
            semantic_key = hashlib.sha256(
                _canonical_json(
                    {
                        "fact_type": fact_type,
                        "entity_id": (
                            item.get("entity_id")
                            or item.get("subject_entity_id")
                        ),
                        "target_entity_id": item.get("target_entity_id"),
                        "field": item.get("field") or item.get("field_name"),
                        "value": value,
                    }
                ).encode("utf-8")
            ).hexdigest()
        return {
            "content": render_power(item),
            "fact_type": fact_type,
            "semantic_key": semantic_key,
            "knowledge_plane": str(
                item.get("knowledge_plane")
                or embedded_plane
                or source.get("knowledge_plane")
                or "objective"
            ),
            "scope": str(
                item.get("scope") or source.get("scope") or "current"
            ),
            "source_event_id": source_event_id or None,
            "entity_id": (
                item.get("entity_id") or item.get("subject_entity_id")
            ),
            "subject_entity_id": item.get("subject_entity_id"),
            "target_entity_id": item.get("target_entity_id"),
            "field": item.get("field") or item.get("field_name"),
        }

    structured_power_facts = [
        structured_power(item) for item in power_facts
    ]
    power_state = [
        item
        for item in structured_power_facts
        if item.get("scope") == "current"
    ]
    power_progression = [
        item
        for item in structured_power_facts
        if item.get("fact_type") == "progression"
        and item.get("scope") == "current"
    ]
    power_abilities = [
        item
        for item in structured_power_facts
        if str(item.get("fact_type") or "").startswith("ability")
        and item.get("scope") == "current"
    ]
    power_resources = [
        item
        for item in structured_power_facts
        if item.get("fact_type") == "resource"
        and item.get("scope") == "current"
    ]
    power_bindings = [
        item
        for item in structured_power_facts
        if item.get("fact_type") == "power_binding"
        and item.get("scope") == "current"
    ]
    power_definitions = [
        item
        for item in structured_power_facts
        if item.get("scope") == "timeless"
    ]

    def has_debt_value(item: Mapping[str, Any]) -> bool:
        value = item.get("value")
        if not isinstance(value, Mapping):
            return False
        for key in (
            "cooldown_until",
            "reserved",
            "debt",
            "expires_coordinate",
            "blocking_requirements",
        ):
            current_value = value.get(key)
            if (
                current_value is not None
                and current_value != ""
                and current_value != 0
                and current_value is not False
            ):
                return True
        return False

    power_debts = [
        structured_power(item)
        for item in power_facts
        if item.get("scope") == "current"
        and has_debt_value(item)
    ]
    payload = dict(proposal.get("payload") or {})
    artifact_context = dict(payload.get("artifact_context") or {})
    operation = str(commit.get("operation") or "accept")
    return {
        **dict(commit),
        "canon_status": (
            "accepted" if operation == "accept" else "retracted"
        ),
        "text": str(payload.get("assistant_text") or ""),
        "summary": str(payload.get("summary") or ""),
        "current_state": current,
        "semantic_facts": semantic,
        "open_loops": open_loops,
        "power_state": power_state,
        "power_progression": power_progression,
        "power_abilities": power_abilities,
        "power_resources": power_resources,
        "power_bindings": power_bindings,
        "power_definitions": power_definitions,
        "power_debts": power_debts,
        "events": list(commit.get("events") or []),
        "genre": payload.get("genre"),
        "task": artifact_context.get("task"),
        "arc_id": payload.get("arc_id"),
        "volume_id": payload.get("volume_id"),
        "success_pattern": payload.get("success_pattern"),
        "craft_signals": payload.get("craft_signals") or {},
    }


def _active_authority_payloads(
    service: ContinuityService,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for active_commit in service.list_active_accepted_commits():
        active_proposal = service.inspect_proposal(
            str(active_commit["proposal_id"])
        )
        payloads.append(
            _commit_payload(service, active_commit, active_proposal)
        )
    return payloads


def _rebuild_longform_content_projections(
    service: ContinuityService,
    memory: LayeredMemoryStore,
    summaries: AcceptedSummaryStore,
    patterns: ProjectPatternStore,
) -> dict[str, Any]:
    payloads = _active_authority_payloads(service)
    cleared = {
        "memory": memory.clear(),
        "summaries": summaries.clear(),
        "patterns": patterns.clear(),
    }
    memory_counts = {"working": 0, "episodic": 0, "semantic": 0}
    summary_results: list[dict[str, Any]] = []
    pattern_results: list[dict[str, Any]] = []
    for active_payload in payloads:
        episodic = memory.project_accepted_commit(
            active_payload,
            layers=("episodic",),
        )
        memory_counts["episodic"] += int(episodic["episodic"])
        summary_results.append(summaries.project_commit(active_payload))
        pattern_results.append(patterns.learn(active_payload))
    if payloads:
        current = memory.project_accepted_commit(
            payloads[-1],
            layers=("working", "semantic"),
        )
        memory_counts["working"] += int(current["working"])
        memory_counts["semantic"] += int(current["semantic"])
    return {
        "active_commit_ids": [
            str(payload["commit_id"]) for payload in payloads
        ],
        "cleared": cleared,
        "memory_counts": memory_counts,
        "summaries": summary_results,
        "patterns": pattern_results,
    }


def _project_longform_vectors(
    root: Path,
    _payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Refresh the vector projection for an accepted long-form payload."""

    runtime = state_rag._load_runtime_config(root)
    embedding_readiness = dict(
        state_rag._service_readiness(runtime.embedding)
    )
    indexed = refresh_longform_index(root, with_embeddings=True)
    refresh = dict(indexed.get("refresh") or {})
    info = dict(indexed.get("schema") or {})
    embedding_attempts = int(refresh.get("embedding_attempts") or 0)
    embedding_calls = int(refresh.get("embedding_calls") or 0)
    embedding_failures = int(refresh.get("embedding_failures") or 0)
    chunk_count = int(info.get("chunk_count") or 0)
    vector_count = int(info.get("vector_count") or 0)
    embedding_enabled = bool(info.get("embedding_enabled"))
    lexical_ready = chunk_count > 0
    projected = embedding_enabled and vector_count > 0
    service_ready = embedding_readiness.get("status") == "not_called"
    semantic_ready = (
        projected
        and service_ready
        and embedding_failures == 0
    )
    status = (
        "success"
        if semantic_ready
        else "failed"
        if embedding_failures > 0
        else "degraded"
    )
    embedding_readiness.update(
        {
            "attempts": embedding_attempts,
            "calls": embedding_calls,
            "failures": embedding_failures,
            "vector_count": vector_count,
        }
    )
    if semantic_ready:
        embedding_readiness.update(
            {
                "status": "ok",
                "configured": True,
                "result": (
                    "refreshed"
                    if embedding_calls > 0
                    else "cached"
                ),
            }
        )
        embedding_readiness.pop("reason", None)
    elif (
        embedding_failures > 0
        and embedding_readiness.get("status") == "not_called"
    ):
        embedding_readiness.update(
            {
                "status": "failed",
                "reason": (
                    f"{embedding_failures} of "
                    f"{embedding_attempts} embedding attempts failed"
                ),
            }
        )
    return {
        "status": status,
        "projected": projected,
        "lexical_ready": lexical_ready,
        "semantic_ready": semantic_ready,
        "lexical_backend": (
            "fts5_bm25"
            if info.get("fts5_available")
            else "lexical_fallback"
        ),
        "embedding_readiness": embedding_readiness,
        "refresh": refresh,
        "authority_index_schema_version": info[
            "authority_index_schema_version"
        ],
        "embedding_enabled": embedding_enabled,
        "embedding_model": info.get("embedding_model"),
        "chunk_count": chunk_count,
        "vector_count": vector_count,
        "index_digest": info["index_digest"],
    }


def _project_after_commit(
    root: Path,
    service: ContinuityService,
    commit: Mapping[str, Any],
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    payload = _commit_payload(service, commit, proposal)
    journal = ProjectionJournal(_projection_database_path(root))
    longform_db = _longform_database_path(root)
    memory = LayeredMemoryStore(longform_db)
    summaries = AcceptedSummaryStore(longform_db)
    patterns = ProjectPatternStore(longform_db)
    operation = str(commit.get("operation") or "accept")
    active_commits = (
        service.list_active_accepted_commits()
        if bool(commit.get("changes_authority"))
        else []
    )
    active_commit_ids = [
        str(active_commit["commit_id"])
        for active_commit in active_commits
    ]
    rebuilt_content: dict[str, Any] | None = None

    def ensure_rebuilt_content() -> dict[str, Any]:
        nonlocal rebuilt_content
        if rebuilt_content is None:
            rebuilt_content = _rebuild_longform_content_projections(
                service,
                memory,
                summaries,
                patterns,
            )
        return rebuilt_content

    def snapshot_projector(_payload: Mapping[str, Any]) -> dict[str, Any]:
        snapshot = {
            "schema_version": RUNTIME_VERSION,
            "canon_revisions": service.get_canon_revisions(),
            "projection_hash": service.projection_hash(),
            "facts": service.query_facts(
                include_timeless=True,
                include_historical=False,
            )["facts"],
            "updated_at": _utc_now(),
        }
        target = root / ".plot-rag" / "continuity_snapshot.json"
        _atomic_json(target, snapshot)
        return {
            "path": str(target.resolve()),
            "projection_hash": snapshot["projection_hash"],
            "fact_count": len(snapshot["facts"]),
            "updated_at": snapshot["updated_at"],
        }

    def index_projector(_payload: Mapping[str, Any]) -> dict[str, Any]:
        return refresh_longform_index(root, with_embeddings=False)

    def summary_projector(value: Mapping[str, Any]) -> dict[str, Any]:
        if not bool(commit.get("changes_authority")):
            return {"projected": False, "reason": "non_authority_commit"}
        if operation == "retract":
            rebuilt = ensure_rebuilt_content()
            return {
                "projected": True,
                "mode": "rebuild_after_retract",
                "active_commit_ids": rebuilt["active_commit_ids"],
                "summaries": rebuilt["summaries"],
            }
        projected = summaries.project_commit(value)
        projected["pruned"] = summaries.prune_to_source_commits(
            active_commit_ids
        )
        return projected

    def memory_projector(value: Mapping[str, Any]) -> dict[str, Any]:
        if not bool(commit.get("changes_authority")):
            return {"projected": False, "reason": "non_authority_commit"}
        if operation == "retract":
            rebuilt = ensure_rebuilt_content()
            return {
                "projected": True,
                "mode": "rebuild_after_retract",
                "active_commit_ids": rebuilt["active_commit_ids"],
                "counts": rebuilt["memory_counts"],
            }
        pruned = memory.prune_to_source_commits(active_commit_ids)
        cleared = memory.clear(layers=("working", "semantic"))
        return {
            "projected": True,
            "counts": memory.project_accepted_commit(value),
            "pruned": pruned,
            "cleared_current_layers": cleared,
        }

    projectors = {
        "snapshot": snapshot_projector,
        "index": index_projector,
        "summary": summary_projector,
        "memory": memory_projector,
        "vector": lambda value: _project_longform_vectors(root, value),
    }
    results: dict[str, Any] = {}
    failures: list[dict[str, Any]] = []
    degradations: list[dict[str, Any]] = []
    for name, projector in projectors.items():
        try:
            result = journal.run(name, payload, projector)
            results[name] = result
            if result.get("status") in {"degraded", "failed"}:
                degradations.append(
                    {
                        "projection": name,
                        "run_id": result.get("run_id"),
                        "status": result.get("status"),
                    }
                )
        except ProjectionRunError as exc:
            failures.append(
                {
                    "projection": name,
                    "run_id": exc.run_id,
                    "reason": str(exc),
                }
            )
    retention = {
        "projection_runs_removed": journal.prune_derived_runs(
            keep_successful_per_projection=int(
                (load_config(root).get("lifecycle") or {}).get(
                    "projection_history_limit", 20
                )
            )
        )
    }
    if bool(commit.get("changes_authority")) and operation == "retract":
        pattern = {
            "learned": False,
            "reason": "retraction_rebuilt_active_patterns",
            "rebuild": ensure_rebuilt_content()["patterns"],
        }
    else:
        if bool(commit.get("changes_authority")):
            patterns.prune_to_source_commits(active_commit_ids)
        pattern = patterns.learn(payload)
    return {
        "status": "degraded" if failures or degradations else "completed",
        "runs": results,
        "failures": failures,
        "degradations": degradations,
        "retention": retention,
        "project_pattern": pattern,
    }


def accept_plot_proposal(
    project_root: Path | str,
    proposal_id: str,
    *,
    approval_id: str,
    expected_canon_revision: int,
    workspace_root: Path | str | None = None,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    service = ContinuityService(root)
    proposal = service.inspect_proposal(proposal_id)
    commit = service.accept_proposal(
        proposal_id,
        approval_id=approval_id,
        expected_canon_revision=expected_canon_revision,
    )
    projections = _project_after_commit(root, service, commit, proposal)
    proposal_kind = str(proposal.get("proposal_kind") or "")
    initialization_rebase: dict[str, Any] | None = None
    if proposal_kind == "power_spec_change":
        payload = dict(proposal.get("payload") or {})
        parent_initialization = str(
            payload.get("parent_initialization_proposal_id") or ""
        ).strip()
        if parent_initialization:
            resolved_workspace = Path(
                workspace_root
                or payload.get("initialization_workspace_root")
                or root
            ).expanduser().resolve()
            try:
                initialization_rebase = register_initialization_proposal(
                    root,
                    parent_initialization,
                    workspace_root=resolved_workspace,
                )
            except (ContinuityError, PlotInitError, OSError, ValueError) as exc:
                initialization_rebase = {
                    "status": "rebase_failed",
                    "code": getattr(exc, "code", type(exc).__name__),
                    "reason": str(exc),
                    "init_proposal_id": parent_initialization,
                    "initialization_workspace_root": str(resolved_workspace),
                }
    return {
        "status": "accepted",
        "proposal_kind": proposal_kind,
        "required_operation": (
            "accept_power_spec"
            if proposal_kind == "power_spec_change"
            else SOURCE_MANIFEST_ACCEPT_OPERATION
            if proposal_kind == SOURCE_MANIFEST_PROPOSAL_KIND
            else "accept_initialization"
            if proposal_kind == "initialization_bundle"
            else "accept"
        ),
        "commit": commit,
        "projections": projections,
        "initialization_rebase": initialization_rebase,
    }


def validate_power_spec_change(
    power_spec: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and compile a standalone PowerSpec without project access."""

    try:
        return preview_power_spec_import(power_spec)
    except PowerSpecImportError as error:
        raise ContinuityError(
            str(error.code),
            str(error),
            details=dict(error.details),
        ) from error


def preview_power_spec_change(
    project_root: Path | str,
    power_spec: Mapping[str, Any],
    *,
    expected_canon_revision: int,
) -> dict[str, Any]:
    """Preview an independent PowerSpec proposal against accepted canon."""

    root = Path(project_root).expanduser().resolve()
    load_config(root)
    state_path = root / ".plot-rag" / "state.sqlite3"
    if not state_path.is_file():
        raise ContinuityError(
            "POWER_SPEC_STATE_NOT_CREATED",
            "power specification preview requires an existing continuity database",
            details={"path": str(state_path)},
        )
    return ContinuityService(root).preview_power_spec_change(
        power_spec,
        expected_canon_revision=expected_canon_revision,
    )


def propose_power_spec_change(
    project_root: Path | str,
    power_spec: Mapping[str, Any],
    *,
    expected_canon_revision: int,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Freeze an independent PowerSpec as a proposal-only artifact."""

    root = Path(project_root).expanduser().resolve()
    return ContinuityService(root).propose_power_spec_change(
        power_spec,
        expected_canon_revision=expected_canon_revision,
        idempotency_key=idempotency_key,
    )


def source_manifest_status(
    project_root: Path | str,
) -> dict[str, Any]:
    """Return the accepted source-manifest ledger and current projection."""

    root = Path(project_root).expanduser().resolve()
    load_config(root)
    state_path = root / ".plot-rag" / "state.sqlite3"
    if not state_path.is_file():
        raise ContinuityError(
            "SOURCE_MANIFEST_STATE_NOT_CREATED",
            "source manifest status requires an existing continuity database",
            details={"path": str(state_path)},
        )
    target = SimpleNamespace(db_path=state_path)
    with state_rag._open_diagnostic_database(target) as connection:
        response = source_manifest_projection_status(connection)
        revisions: dict[str, int] = {}
        for label, key in (
            ("head", "head_canon_revision"),
            ("active", "active_canon_revision"),
        ):
            row = connection.execute(
                "SELECT value FROM state_meta WHERE key=?",
                (key,),
            ).fetchone()
            revisions[label] = int(row[0]) if row is not None else 0
        response["canon_revisions"] = revisions
        return response


def preview_source_manifest_change(
    project_root: Path | str,
    plan: Mapping[str, Any],
    *,
    expected_canon_revision: int,
) -> dict[str, Any]:
    """Validate a source-manifest migration plan without saving a proposal."""

    root = Path(project_root).expanduser().resolve()
    load_config(root)
    expected = validate_positive_int(
        expected_canon_revision,
        "expected_canon_revision",
        allow_none=False,
        minimum=0,
    )
    state_path = root / ".plot-rag" / "state.sqlite3"
    if not state_path.is_file():
        raise ContinuityError(
            "SOURCE_MANIFEST_STATE_NOT_CREATED",
            "source manifest preview requires an existing continuity database",
            details={"path": str(state_path)},
        )
    target = SimpleNamespace(db_path=state_path)
    with state_rag._open_diagnostic_database(target) as connection:
        row = connection.execute(
            "SELECT value FROM state_meta WHERE key='active_canon_revision'"
        ).fetchone()
        active = int(row[0]) if row is not None else 0
        if active != expected:
            raise ContinuityError(
                "CANON_REVISION_CONFLICT",
                "source manifest preview must bind the active canon revision",
                details={"expected": expected, "actual": active},
            )
        response = preview_manifest_plan(
            connection,
            root,
            plan,
            expected_canon_revision=expected,
        )
        head_row = connection.execute(
            "SELECT value FROM state_meta WHERE key='head_canon_revision'"
        ).fetchone()
        response["canon_revisions"] = {
            "head": int(head_row[0]) if head_row is not None else 0,
            "active": active,
        }
        return response


def propose_source_manifest_change(
    project_root: Path | str,
    plan: Mapping[str, Any],
    *,
    expected_canon_revision: int,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Freeze a source-manifest change as an immutable lifecycle proposal."""

    root = Path(project_root).expanduser().resolve()
    return ContinuityService(root).propose_source_manifest_change(
        plan,
        expected_canon_revision=expected_canon_revision,
        idempotency_key=idempotency_key,
    )


def reject_plot_proposal(
    project_root: Path | str,
    proposal_id: str,
    *,
    reason: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    service = ContinuityService(Path(project_root).expanduser().resolve())
    return {
        "status": "rejected",
        "proposal": service.reject_proposal(
            proposal_id,
            reason=reason,
            idempotency_key=idempotency_key,
        ),
    }


def retract_plot_proposal(
    project_root: Path | str,
    proposal_id: str,
    *,
    approval_id: str,
    expected_canon_revision: int,
    reason: str,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    service = ContinuityService(root)
    commit = service.retract_proposal(
        proposal_id,
        approval_id=approval_id,
        expected_canon_revision=expected_canon_revision,
        reason=reason,
    )
    proposal = service.inspect_proposal(proposal_id)
    projections = _project_after_commit(root, service, commit, proposal)
    return {
        "status": "retracted",
        "commit": commit,
        "projections": projections,
    }


def replay_continuity(project_root: Path | str) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    service = ContinuityService(root)
    result = service.replay()
    longform_db = _longform_database_path(root)
    derived = _rebuild_longform_content_projections(
        service,
        LayeredMemoryStore(longform_db),
        AcceptedSummaryStore(longform_db),
        ProjectPatternStore(longform_db),
    )
    authority = service.query_facts(
        include_timeless=True,
        include_historical=False,
    )
    authority_facts = list(authority["facts"])
    with service.store.read_connection() as connection:
        entity_catalog = {
            str(row["entity_id"]): {
                "canonical_name": str(row["canonical_name"]),
                "entity_type": str(row["entity_type"]),
            }
            for row in connection.execute(
                """
                SELECT entity_id, canonical_name, entity_type
                FROM entities
                ORDER BY entity_id
                """
            )
        }
    snapshot_time = _utc_now()
    snapshot = {
        "schema_version": RUNTIME_VERSION,
        "canon_revisions": authority["revisions"],
        "projection_hash": result["projection_hash"],
        "facts": authority_facts,
        "updated_at": snapshot_time,
    }
    _atomic_json(root / ".plot-rag" / "continuity_snapshot.json", snapshot)
    state_snapshot = state_rag.rebuild_state_snapshot(
        root,
        continuity_facts=authority_facts,
        entity_catalog=entity_catalog,
        updated_at=snapshot_time,
    )
    return {
        "status": "completed",
        "replay": result,
        "derived_replay": derived,
        "snapshot_path": str(
            (root / ".plot-rag" / "continuity_snapshot.json").resolve()
        ),
        "state_snapshot": state_snapshot,
    }


def init_service(
    workspace_root: Path | str,
    *,
    project_root: Path | str | None = None,
) -> PlotInitService:
    workspace = Path(workspace_root).expanduser().resolve(strict=False)
    project = (
        Path(project_root).expanduser().resolve(strict=False)
        if project_root is not None
        else None
    )
    database: Path | None = None
    if project is not None:
        database = project / ".plot-rag" / "init.sqlite3"
        config_path = project / ".plot-rag" / "config.json"
        if config_path.is_file():
            raw = _json_load(config_path.read_text(encoding="utf-8-sig"), {})
            initialization = (
                dict(raw.get("initialization") or {})
                if isinstance(raw, Mapping)
                else {}
            )
            configured = str(
                initialization.get("database_path") or ".plot-rag/init.sqlite3"
            )
            candidate = (project / configured).resolve(strict=False)
            if not _inside(candidate, project):
                raise PlotInitError(
                    "UNSAFE_INIT_DATABASE",
                    "initialization database must stay inside the project",
                    database_path=str(candidate),
                )
            legacy_database = workspace / ".plot-rag-init" / "init.sqlite3"
            database = (
                candidate
                if candidate.is_file() or not legacy_database.is_file()
                else legacy_database
            )
        elif not database.exists():
            legacy_database = workspace / ".plot-rag-init" / "init.sqlite3"
            if legacy_database.is_file():
                database = legacy_database
    return PlotInitService(workspace, database_path=database)


def register_initialization_proposal(
    project_root: Path | str,
    init_proposal_id: str,
    *,
    workspace_root: Path | str | None = None,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    workspace = Path(workspace_root or root).expanduser().resolve()
    initializer = init_service(workspace, project_root=root)
    frozen = initializer.storage.load_proposal(init_proposal_id)
    bundle = dict(frozen.get("bundle") or {})
    service = ContinuityService(root)
    lifecycle_package = proposal_to_lifecycle_package(frozen)
    power_spec_package = dict(
        lifecycle_package.get("power_spec_package") or {}
    )
    all_proposals = service.list_proposals()
    initialization_package_hash = str(
        frozen.get("package_hash")
        or lifecycle_package.get("package_hash")
        or ""
    )
    accepted_initializations = sorted(
        (
            proposal
            for proposal in all_proposals
            if proposal.get("proposal_kind") == "initialization_bundle"
            and str(proposal.get("artifact_id") or "") == init_proposal_id
            and proposal.get("canon_status") == "accepted"
            and proposal.get("accepted_commit_id")
            and str(
                (proposal.get("payload") or {}).get("package_hash") or ""
            )
            == initialization_package_hash
        ),
        key=lambda proposal: int(proposal.get("artifact_revision") or 0),
        reverse=True,
    )
    if accepted_initializations:
        accepted_initialization = accepted_initializations[0]
        return {
            "status": "registered",
            "init_proposal_id": init_proposal_id,
            "canon_proposal_id": accepted_initialization["proposal_id"],
            "proposal": accepted_initialization,
            "bundle": bundle,
            "idempotent_retry": True,
        }
    if bool(lifecycle_package.get("requires_power_spec_acceptance")):
        for entity in power_spec_package.get("entities") or []:
            if not isinstance(entity, Mapping):
                continue
            service.register_entity(
                str(entity.get("entity_type") or "concept"),
                str(
                    entity.get("canonical_name")
                    or entity.get("entity_id")
                    or "power entity"
                ),
                entity_id=str(entity.get("entity_id") or "") or None,
                aliases=tuple(entity.get("aliases") or ()),
                attributes=dict(entity.get("attributes") or {}),
            )

        spec_artifact_id = str(
            power_spec_package.get("proposal_id") or ""
        )
        spec_package_hash = str(
            power_spec_package.get("package_hash") or ""
        )
        all_power_specs = [
            proposal
            for proposal in all_proposals
            if proposal.get("proposal_kind") == "power_spec_change"
        ]

        def proposal_spec_package(
            proposal: Mapping[str, Any],
        ) -> dict[str, Any]:
            payload = dict(proposal.get("payload") or {})
            return dict(
                payload.get("lifecycle_package")
                or payload.get("power_spec_package")
                or {}
            )

        matching_specs = [
            proposal
            for proposal in all_power_specs
            if (
                str(proposal.get("artifact_id") or "") == spec_artifact_id
                or str(
                    proposal_spec_package(proposal).get(
                        "parent_initialization_proposal_id"
                    )
                    or (proposal.get("payload") or {}).get(
                        "parent_initialization_proposal_id"
                    )
                    or ""
                )
                == init_proposal_id
            )
            and str(
                (
                    proposal_spec_package(proposal).get("package_hash")
                )
                or (proposal.get("payload") or {}).get("package_hash")
                or ""
            )
            == spec_package_hash
        ]
        active_spec_commit_ids = {
            str(commit.get("commit_id") or "")
            for commit in service.list_active_accepted_commits(
                authority_only=True
            )
            if commit.get("commit_id")
        }
        expected_power_package_hash = str(
            power_spec_package.get("power_package_hash") or ""
        )
        active_exact_specs = [
            proposal
            for proposal in all_power_specs
            if proposal.get("canon_status") == "accepted"
            and proposal.get("accepted_commit_id")
            and str(proposal.get("accepted_commit_id"))
            in active_spec_commit_ids
            and expected_power_package_hash
            and str(
                proposal_spec_package(proposal).get(
                    "power_package_hash"
                )
                or (proposal.get("payload") or {}).get(
                    "power_package_hash"
                )
                or ""
            )
            == expected_power_package_hash
        ]
        accepted_spec = next(
            (
                proposal
                for proposal in active_exact_specs
                if proposal.get("canon_status") == "accepted"
                and proposal.get("accepted_commit_id")
                and str(proposal.get("accepted_commit_id"))
                in active_spec_commit_ids
            ),
            None,
        )
        if accepted_spec is None:
            accepted_spec = next(
                (
                    proposal
                    for proposal in matching_specs
                    if proposal.get("canon_status") == "accepted"
                    and proposal.get("accepted_commit_id")
                    and str(proposal.get("accepted_commit_id"))
                    in active_spec_commit_ids
                ),
                None,
            )
        if accepted_spec is None:
            invalidated_spec = next(
                (
                    proposal
                    for proposal in matching_specs
                    if proposal.get("canon_status")
                    in {"rejected", "retracted"}
                    or (
                        proposal.get("accepted_commit_id")
                        and str(proposal.get("accepted_commit_id"))
                        not in active_spec_commit_ids
                    )
                ),
                None,
            )
            if invalidated_spec is not None:
                revisions = service.get_canon_revisions()
                raise ContinuityError(
                    "INITIALIZATION_POWER_SPEC_INVALIDATED",
                    "the initialization power specification is terminal or inactive; freeze a new initialization proposal before applying",
                    details={
                        "init_proposal_id": init_proposal_id,
                        "power_spec_proposal_id": str(
                            invalidated_spec.get("proposal_id") or ""
                        ),
                        "power_spec_canon_status": str(
                            invalidated_spec.get("canon_status") or ""
                        ),
                        "power_spec_commit_id": (
                            str(invalidated_spec.get("accepted_commit_id"))
                            if invalidated_spec.get("accepted_commit_id")
                            else None
                        ),
                        "current_canon_revision": int(
                            revisions.get("active", 0)
                        ),
                        "required_action": (
                            "freeze_new_initialization_proposal"
                        ),
                    },
                )
            proposed_spec = next(
                (
                    proposal
                    for proposal in matching_specs
                    if proposal.get("canon_status") == "proposed"
                ),
                None,
            )
            if proposed_spec is None:
                base_revision = validate_positive_int(
                    (bundle.get("meta") or {}).get(
                        "expected_canon_revision",
                        service.get_canon_revisions()["active"],
                    ),
                    "expected_canon_revision",
                    allow_none=False,
                    minimum=0,
                )
                proposed_spec = service.save_proposal(
                    events=list(power_spec_package.get("events") or []),
                    payload={
                        "lifecycle_package": power_spec_package,
                        "package_hash": spec_package_hash,
                        "power_package_hash": str(
                            power_spec_package.get(
                                "power_package_hash"
                            )
                            or ""
                        ),
                        "parent_initialization_proposal_id": str(
                            power_spec_package.get(
                                "parent_initialization_proposal_id"
                            )
                            or init_proposal_id
                        ),
                        "initialization_workspace_root": str(workspace),
                        "target_project_real_path": str(root),
                    },
                    artifact_id=spec_artifact_id,
                    artifact_kind="power_spec",
                    artifact_stage="bootstrap",
                    branch_id="main",
                    chapter_no=None,
                    scene_index=None,
                    prepared_canon_revision=base_revision,
                    source_role="setting",
                    proposal_kind="power_spec_change",
                    idempotency_key=(
                        "init-power-spec-register:"
                        + init_proposal_id
                        + ":"
                        + spec_package_hash
                    ),
                )
            return {
                "status": "POWER_SPEC_APPROVAL_REQUIRED",
                "code": "POWER_SPEC_APPROVAL_REQUIRED",
                "init_proposal_id": init_proposal_id,
                "initialization_workspace_root": str(workspace),
                "target_project_real_path": str(root),
                "power_spec_proposal_id": proposed_spec["proposal_id"],
                "power_spec_artifact_id": spec_artifact_id,
                "canon_proposal_id": proposed_spec["proposal_id"],
                "required_operation": "accept_power_spec",
                "expected_canon_revision": proposed_spec[
                    "prepared_canon_revision"
                ],
                "proposal": proposed_spec,
                "bundle": bundle,
            }

        spec_commit = service.inspect_commit(
            str(accepted_spec["accepted_commit_id"])
        )
        accepted_spec_package = proposal_spec_package(accepted_spec)
        accepted_spec_package_hash = str(
            accepted_spec_package.get("package_hash")
            or (accepted_spec.get("payload") or {}).get("package_hash")
            or ""
        )
        accepted_spec_parent = str(
            accepted_spec_package.get(
                "parent_initialization_proposal_id"
            )
            or (accepted_spec.get("payload") or {}).get(
                "parent_initialization_proposal_id"
            )
            or ""
        )
        current_active_revision = int(
            service.get_canon_revisions()["active"]
        )
        power_spec_reused = accepted_spec_parent != init_proposal_id
        power_spec_binding = {
            "proposal_id": accepted_spec["proposal_id"],
            "commit_id": spec_commit["commit_id"],
            "package_hash": accepted_spec_package_hash,
            "requested_package_hash": spec_package_hash,
            "power_package_hash": str(
                power_spec_package.get("power_package_hash") or ""
            ),
            "projection_hash": spec_commit["projection_hash"],
            "active_canon_revision": spec_commit[
                "active_canon_revision"
            ],
            "power_spec_reused": power_spec_reused,
            "power_spec_grant_consumed_in_this_saga": (
                not power_spec_reused
            ),
            "accepted_parent_initialization_proposal_id": (
                accepted_spec_parent
            ),
            "requested_parent_initialization_proposal_id": (
                init_proposal_id
            ),
        }
        canonical = service.save_initialization_bundle(
            frozen,
            artifact_id=init_proposal_id,
            prepared_canon_revision=current_active_revision,
            power_spec_binding=power_spec_binding,
            idempotency_key=(
                "init-register:"
                + init_proposal_id
                + ":"
                + str(frozen.get("package_hash") or "")
                + ":prepared:"
                + str(current_active_revision)
                + ":power-spec:"
                + str(spec_commit["commit_id"])
            ),
        )
        return {
            "status": "registered",
            "init_proposal_id": init_proposal_id,
            "canon_proposal_id": canonical["proposal_id"],
            "proposal": canonical,
            "bundle": bundle,
            "power_spec": {
                "proposal": accepted_spec,
                "commit": spec_commit,
                "binding": power_spec_binding,
            },
        }

    canonical = service.save_initialization_bundle(
        frozen,
        artifact_id=init_proposal_id,
        idempotency_key=(
            "init-register:"
            + init_proposal_id
            + ":"
            + str(frozen.get("package_hash") or "")
        ),
    )
    return {
        "status": "registered",
        "init_proposal_id": init_proposal_id,
        "canon_proposal_id": canonical["proposal_id"],
        "proposal": canonical,
        "bundle": bundle,
    }


def resolve_canon_proposal_id(
    project_root: Path | str,
    proposal_id: str,
    *,
    workspace_root: Path | str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    root = Path(project_root).expanduser().resolve()
    service = ContinuityService(root)
    try:
        inspected = service.inspect_proposal(proposal_id)
        return proposal_id, inspected
    except ContinuityError as exc:
        if exc.code != "PROPOSAL_NOT_FOUND" or not proposal_id.startswith(
            _INIT_PROPOSAL_PREFIX
        ):
            raise
    registered = register_initialization_proposal(
        root,
        proposal_id,
        workspace_root=workspace_root,
    )
    return str(registered["canon_proposal_id"]), registered["proposal"]


def _power_spec_approval_requirement(
    requested_proposal_id: str,
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(proposal.get("payload") or {})
    parent_initialization = str(
        payload.get("parent_initialization_proposal_id")
        or requested_proposal_id
    )
    return {
        "status": "POWER_SPEC_APPROVAL_REQUIRED",
        "code": "POWER_SPEC_APPROVAL_REQUIRED",
        "requested_proposal_id": requested_proposal_id,
        "init_proposal_id": parent_initialization,
        "power_spec_proposal_id": str(proposal["proposal_id"]),
        "canon_proposal_id": str(proposal["proposal_id"]),
        "required_operation": "accept_power_spec",
        "expected_canon_revision": int(
            proposal["prepared_canon_revision"]
        ),
        "approval_consumed": False,
        "proposal": dict(proposal),
    }


def prepare_initialization_apply(
    project_root: Path | str,
    proposal_id: str,
    *,
    workspace_root: Path | str | None = None,
) -> dict[str, Any]:
    """Register/rebase an init saga and report the next grant-bound stage."""

    root = Path(project_root).expanduser().resolve()
    workspace = Path(workspace_root or root).expanduser().resolve()
    canonical_id, proposal = resolve_canon_proposal_id(
        root,
        proposal_id,
        workspace_root=workspace,
    )
    service = ContinuityService(root)
    proposal = proposal or service.inspect_proposal(canonical_id)
    proposal_kind = str(proposal.get("proposal_kind") or "")
    if proposal_kind == "power_spec_change":
        return _power_spec_approval_requirement(proposal_id, proposal)
    if proposal_kind != "initialization_bundle":
        raise ContinuityError(
            "INITIALIZATION_PROPOSAL_REQUIRED",
            "story initialization apply requires an initialization proposal",
            details={
                "proposal_id": canonical_id,
                "proposal_kind": proposal_kind,
            },
        )
    return {
        "status": "ready",
        "requested_proposal_id": proposal_id,
        "canon_proposal_id": canonical_id,
        "required_operation": "accept_initialization",
        "expected_canon_revision": int(
            proposal["prepared_canon_revision"]
        ),
        "proposal": proposal,
    }


def issue_host_approval(
    project_root: Path | str,
    proposal_id: str,
    *,
    expected_canon_revision: int,
    issuer: str,
    channel: str = "interactive_cli",
    expires_in_seconds: int = 300,
    operations: Sequence[str] | None = None,
    workspace_root: Path | str | None = None,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    canonical_id, proposal = resolve_canon_proposal_id(
        root,
        proposal_id,
        workspace_root=workspace_root,
    )
    service = ContinuityService(root)
    proposal = proposal or service.inspect_proposal(canonical_id)
    payload = dict(proposal.get("payload") or {})
    bundle = dict(payload.get("bundle") or {})
    proposal_kind = str(proposal.get("proposal_kind") or "")
    selected_operations = tuple(
        operations
        or (
            ("accept_initialization", "materialize")
            if proposal_kind == "initialization_bundle"
            else ("accept_power_spec",)
            if proposal_kind == "power_spec_change"
            else (SOURCE_MANIFEST_ACCEPT_OPERATION,)
            if proposal_kind == SOURCE_MANIFEST_PROPOSAL_KIND
            else ("accept",)
        )
    )
    authorized_paths = [
        str(item.get("path"))
        for item in bundle.get("artifact_manifest") or []
        if item.get("path")
    ]
    target = (
        bundle.get("target_project_real_path")
        or payload.get("target_project_real_path")
        or root
    )
    authority = HostApprovalAuthority(
        service,
        issuer=issuer,
        channel=channel,
    )
    grant = authority.issue(
        canonical_id,
        expected_canon_revision=expected_canon_revision,
        operations=selected_operations,
        expires_in_seconds=expires_in_seconds,
        target_project_real_path=target,
        authorized_paths=authorized_paths,
    )
    return {
        "status": "approved",
        "requested_proposal_id": proposal_id,
        "canon_proposal_id": canonical_id,
        "grant": grant,
    }


def _materialization_files(bundle: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for item in bundle.get("artifact_manifest") or []:
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        files[path] = {
            "content": str(item.get("proposed_content") or ""),
            "expected_old_hash": item.get("expected_old_hash"),
        }
    return files


def apply_initialization_proposal(
    project_root: Path | str,
    proposal_id: str,
    *,
    approval_id: str,
    expected_canon_revision: int,
    idempotency_key: str,
    workspace_root: Path | str | None = None,
    materialize: bool = True,
) -> dict[str, Any]:
    if not materialize:
        raise ContinuityError(
            "MATERIALIZATION_REQUIRED",
            "initialization acceptance and materialization are one recoverable saga",
        )
    root = Path(project_root).expanduser().resolve()
    workspace = Path(workspace_root or root).expanduser().resolve()
    initializer: PlotInitService | None = None
    frozen_initialization: dict[str, Any] | None = None
    if str(proposal_id).startswith(_INIT_PROPOSAL_PREFIX):
        initializer = init_service(workspace, project_root=root)
        frozen_initialization = initializer.storage.load_proposal(proposal_id)
    prepared = prepare_initialization_apply(
        root,
        proposal_id,
        workspace_root=workspace,
    )
    if prepared.get("status") == "POWER_SPEC_APPROVAL_REQUIRED":
        return prepared
    canonical_id = str(prepared["canon_proposal_id"])
    proposal = dict(prepared["proposal"])
    service = ContinuityService(root)

    proposal_payload = dict(proposal.get("payload") or {})
    bundle = dict(proposal_payload.get("bundle") or {})
    if frozen_initialization is not None:
        commit = service.apply_initialization_bundle(
            frozen_initialization,
            proposal_id=canonical_id,
            approval_id=approval_id,
            expected_canon_revision=expected_canon_revision,
            prepared_canon_revision=int(
                proposal["prepared_canon_revision"]
            ),
            power_spec_binding=(
                dict(proposal_payload.get("power_spec_binding") or {})
                or None
            ),
            idempotency_key=f"{idempotency_key}:canon",
        )
    else:
        commit = service.accept_proposal(
            canonical_id,
            approval_id=approval_id,
            expected_canon_revision=expected_canon_revision,
        )
    materialization: dict[str, Any] | None = None
    if materialize:
        current_materialization = service.materialization_status(
            commit["commit_id"]
        )
        if current_materialization.get("status") == "completed":
            materialization = current_materialization
        elif current_materialization.get("status") in {
            "staged",
            "activating",
            "ready",
            "awaiting_manifest",
        }:
            materialization = service.activate_materialization(
                commit["commit_id"]
            )
        else:
            files = _materialization_files(bundle)
            materialization = service.stage_materialization(
                commit["commit_id"],
                files=files,
                target_root=root,
            )
            materialization = service.activate_materialization(
                commit["commit_id"]
            )
    projections = _project_after_commit(root, service, commit, proposal)
    verification: dict[str, Any] | None = None
    completion: dict[str, Any] | None = None
    replay_result: dict[str, Any] | None = None
    receipt_path: Path | None = None
    if (
        initializer is not None
        and frozen_initialization is not None
        and materialization is not None
        and materialization.get("status") == "completed"
        and projections.get("status") == "completed"
    ):
        replay_result = service.replay()
        verification = verify_initialization(root, commit["commit_id"])
        if verification.get("status") == "verified":
            receipt_path = root / ".plot-rag" / "completion-receipt.json"
            lifecycle_package = dict(
                proposal_payload.get("lifecycle_package") or {}
            )
            bundle_schema_version = str(
                lifecycle_package.get(
                    "initialization_bundle_schema_version"
                )
                or (bundle.get("meta") or {}).get(
                    "bundle_schema_version"
                )
                or (bundle.get("meta") or {}).get("protocol")
                or "plot-rag-init/v1"
            )
            power_spec_binding = dict(
                proposal_payload.get("power_spec_binding") or {}
            )
            is_v2 = bool(
                bundle_schema_version == "plot-rag-init/v2"
                or power_spec_binding
            )
            commit_projection_hash = str(
                commit.get("projection_hash") or ""
            )
            replay_projection_hash = str(
                verification.get("projection_hash")
                or (replay_result or {}).get("projection_hash")
                or commit_projection_hash
            )
            readiness_receipt = {
                "schema_version": (
                    "plot-rag-init/completion-v2"
                    if is_v2
                    else "plot-rag-init/completion-v1"
                ),
                "bundle_schema_version": bundle_schema_version,
                "bootstrap_ready": True,
                "proposal_id": proposal_id,
                "canon_proposal_id": canonical_id,
                "commit_id": commit["commit_id"],
                "canon_revisions": service.get_canon_revisions(),
                "package_hash": str(
                    proposal_payload.get("package_hash") or ""
                ),
                # Top-level projection_hash represents the fully replayed,
                # post-materialization bootstrap state.  The immutable commit
                # projection remains separately bound for audit/recovery.
                "projection_hash": replay_projection_hash,
                "commit_projection_hash": commit_projection_hash,
                "replay_projection_hash": replay_projection_hash,
                "accepted_event_hash": _sha256(
                    _canonical_json(commit.get("events") or [])
                ),
                "projection_runs": projections.get("runs") or {},
                "verified_files": [
                    {
                        "path": item["path"],
                        "sha256": item["actual"],
                    }
                    for item in verification.get("files") or []
                    if item.get("matches")
                ],
                "completed_at": _utc_now(),
            }
            if is_v2:
                spec_commit = service.inspect_commit(
                    str(power_spec_binding.get("commit_id") or "")
                )
                spec_proposal = service.inspect_proposal(
                    str(power_spec_binding.get("proposal_id") or "")
                )
                spec_revision = int(spec_commit["active_canon_revision"])
                spec_prepared_revision = int(
                    spec_proposal["prepared_canon_revision"]
                )
                initialization_prepared_revision = int(
                    proposal["prepared_canon_revision"]
                )
                initialization_revision = int(
                    commit["active_canon_revision"]
                )
                power_spec_reused = bool(
                    power_spec_binding.get("power_spec_reused")
                )
                power_spec_grant_consumed_in_this_saga = bool(
                    power_spec_binding.get(
                        "power_spec_grant_consumed_in_this_saga",
                        not power_spec_reused,
                    )
                )
                base_revision = initialization_prepared_revision
                initialization_binding = {
                    "source_proposal_id": proposal_id,
                    "proposal_id": canonical_id,
                    "commit_id": commit["commit_id"],
                    "package_hash": str(
                        proposal_payload.get("package_hash") or ""
                    ),
                    "prepared_canon_revision": int(
                        proposal["prepared_canon_revision"]
                    ),
                    "active_canon_revision": int(
                        commit["active_canon_revision"]
                    ),
                    "projection_hash": str(
                        commit["projection_hash"]
                    ),
                    "accepted_event_hash": readiness_receipt[
                        "accepted_event_hash"
                    ],
                }
                spec_binding = {
                    "proposal_id": str(
                        power_spec_binding.get("proposal_id") or ""
                    ),
                    "commit_id": str(
                        power_spec_binding.get("commit_id") or ""
                    ),
                    "package_hash": str(
                        power_spec_binding.get("package_hash") or ""
                    ),
                    "requested_package_hash": str(
                        power_spec_binding.get(
                            "requested_package_hash"
                        )
                        or power_spec_binding.get("package_hash")
                        or ""
                    ),
                    "power_package_hash": str(
                        power_spec_binding.get(
                            "power_package_hash"
                        )
                        or ""
                    ),
                    "prepared_canon_revision": (
                        spec_prepared_revision
                    ),
                    "active_canon_revision": spec_revision,
                    "projection_hash": str(
                        power_spec_binding.get("projection_hash") or ""
                    ),
                    "power_spec_reused": power_spec_reused,
                    "power_spec_grant_consumed_in_this_saga": (
                        power_spec_grant_consumed_in_this_saga
                    ),
                    "accepted_parent_initialization_proposal_id": str(
                        power_spec_binding.get(
                            "accepted_parent_initialization_proposal_id"
                        )
                        or ""
                    ),
                    "requested_parent_initialization_proposal_id": str(
                        power_spec_binding.get(
                            "requested_parent_initialization_proposal_id"
                        )
                        or proposal_id
                    ),
                }
                readiness_receipt.update(
                    {
                        "base_canon_revision": base_revision,
                        "initialization_base_canon_revision": (
                            initialization_prepared_revision
                        ),
                        "initialization_prepared_canon_revision": (
                            initialization_prepared_revision
                        ),
                        "power_spec_prepared_canon_revision": (
                            spec_prepared_revision
                        ),
                        "power_spec_proposal_id": spec_binding[
                            "proposal_id"
                        ],
                        "power_spec_commit_id": spec_binding["commit_id"],
                        "power_spec_package_hash": spec_binding[
                            "package_hash"
                        ],
                        "requested_power_spec_package_hash": (
                            spec_binding["requested_package_hash"]
                        ),
                        "power_package_hash": spec_binding[
                            "power_package_hash"
                        ],
                        "power_spec_canon_revision": spec_revision,
                        "initialization_proposal_id": canonical_id,
                        "initialization_commit_id": commit["commit_id"],
                        "initialization_package_hash": (
                            initialization_binding["package_hash"]
                        ),
                        "initialization_canon_revision": int(
                            commit["active_canon_revision"]
                        ),
                        "power_spec_reused": power_spec_reused,
                        "power_spec_grant_consumed_in_this_saga": (
                            power_spec_grant_consumed_in_this_saga
                        ),
                        "initialization_grant_consumed": True,
                        "power_spec": spec_binding,
                        "initialization": initialization_binding,
                        "saga": {
                            "base_canon_revision": base_revision,
                            "initialization_base_canon_revision": (
                                initialization_prepared_revision
                            ),
                            "initialization_prepared_canon_revision": (
                                initialization_prepared_revision
                            ),
                            "power_spec_prepared_canon_revision": (
                                spec_prepared_revision
                            ),
                            "power_spec_canon_revision": spec_revision,
                            "initialization_canon_revision": (
                                initialization_revision
                            ),
                            "power_spec_reused": power_spec_reused,
                            "power_spec_grant_consumed_in_this_saga": (
                                power_spec_grant_consumed_in_this_saga
                            ),
                            "initialization_grant_consumed": True,
                            "power_spec_commit_completed_in_this_saga": (
                                not power_spec_reused
                            ),
                            "initialization_commit_completed_in_this_saga": (
                                True
                            ),
                            "two_grants_consumed": True,
                            "two_grants_consumed_scope": (
                                "cumulative_acceptance_chain"
                            ),
                            "two_cas_commits_completed": True,
                            "two_cas_commits_completed_scope": (
                                "cumulative_acceptance_chain"
                            ),
                        },
                    }
                )
            if receipt_path.is_file():
                existing_receipt = _json_load(
                    receipt_path.read_text(encoding="utf-8-sig"),
                    {},
                )
                identity_fields = (
                    "schema_version",
                    "bundle_schema_version",
                    "commit_id",
                    "canon_proposal_id",
                    "package_hash",
                    "accepted_event_hash",
                    "power_spec_proposal_id",
                    "power_spec_commit_id",
                    "power_spec_package_hash",
                    "requested_power_spec_package_hash",
                    "power_package_hash",
                    "initialization_proposal_id",
                    "initialization_commit_id",
                    "initialization_package_hash",
                )
                if any(
                    field in existing_receipt
                    and str(existing_receipt.get(field) or "")
                    != str(readiness_receipt.get(field) or "")
                    for field in identity_fields
                ):
                    raise ContinuityError(
                        "COMPLETION_RECEIPT_CONFLICT",
                        "completion receipt is bound to a different initialization saga",
                    )
                readiness_receipt = existing_receipt
            else:
                _atomic_json(
                    receipt_path,
                    readiness_receipt,
                )
            verification = verify_initialization(
                root,
                commit["commit_id"],
            )
        if verification is not None and verification.get("status") == "verified":
            completion = initializer.complete(
                proposal_id,
                commit_id=str(commit["commit_id"]),
                verification={
                    key: value
                    for key, value in verification.items()
                    if key != "bootstrap_ready"
                },
                idempotency_key=f"{idempotency_key}:complete",
            )
            with service.store.transaction() as connection:
                for key, value in (
                    ("bootstrap_ready", "1"),
                    ("bootstrap_ready_commit_id", str(commit["commit_id"])),
                    (
                        "bootstrap_ready_power_spec_commit_id",
                        str(
                            (
                                proposal_payload.get(
                                    "power_spec_binding"
                                )
                                or {}
                            ).get("commit_id")
                            or ""
                        ),
                    ),
                    (
                        "bootstrap_ready_initialization_proposal_id",
                        canonical_id,
                    ),
                    (
                        "bootstrap_ready_receipt_sha256",
                        _sha256(_canonical_json(readiness_receipt)),
                    ),
                ):
                    connection.execute(
                        """
                        INSERT INTO state_meta(key, value, updated_at)
                        VALUES(?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            value=excluded.value,
                            updated_at=excluded.updated_at
                        """,
                        (key, value, _utc_now()),
                    )
    return {
        "status": (
            "completed"
            if (
                materialization is not None
                and materialization.get("status") == "completed"
                and projections.get("status") == "completed"
                and (
                    initializer is None
                    or (
                        verification is not None
                        and verification.get("status") == "verified"
                        and completion is not None
                    )
                )
            )
            else "degraded"
        ),
        "requested_proposal_id": proposal_id,
        "canon_proposal_id": canonical_id,
        "commit": commit,
        "materialization": materialization,
        "projections": projections,
        "replay": replay_result,
        "verification": verification,
        "initialization_session": completion,
        "bootstrap_ready": bool(completion and completion.get("bootstrap_ready")),
        "completion_receipt_path": (
            str(receipt_path.resolve()) if receipt_path is not None else None
        ),
    }


def verify_initialization(
    project_root: Path | str,
    commit_id: str,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    service = ContinuityService(root)
    commit = service.inspect_commit(commit_id)
    proposal = service.inspect_proposal(str(commit["proposal_id"]))
    proposal_payload = dict(proposal.get("payload") or {})
    lifecycle_package = dict(
        proposal_payload.get("lifecycle_package") or {}
    )
    bundle = dict(proposal_payload.get("bundle") or {})
    bundle_schema_version = str(
        lifecycle_package.get("initialization_bundle_schema_version")
        or (bundle.get("meta") or {}).get("bundle_schema_version")
        or (bundle.get("meta") or {}).get("protocol")
        or "plot-rag-init/v1"
    )
    power_spec_binding = dict(
        proposal_payload.get("power_spec_binding") or {}
    )
    is_v2 = bool(
        bundle_schema_version == "plot-rag-init/v2"
        or power_spec_binding
    )
    materialization = service.materialization_status(commit_id)
    manifest = [
        item
        for item in service.get_accepted_source_manifest(
            include_pending=True
        )
        if str(item.get("commit_id") or "") == str(commit_id)
    ]
    current_manifest = service.get_current_source_manifest_snapshot()
    current_manifest_by_path = {
        normalize_source_path(item.get("path") or item.get("source_path")).casefold(): item
        for item in current_manifest.get("entries") or []
    }
    file_results: list[dict[str, Any]] = []
    for item in materialization.get("files") or []:
        relative = normalize_source_path(item["relative_path"])
        path = root / relative
        actual = _sha256(path.read_bytes()) if path.is_file() else None
        expected = str(item["proposed_new_hash"])
        current_entry = current_manifest_by_path.get(relative.casefold())
        receipt_matches_current = actual == expected
        accepted_successor_matches_current = bool(
            actual
            and current_entry
            and str(current_entry.get("content_hash") or "") == actual
        )
        file_results.append(
            {
                "path": relative,
                "exists": path.is_file(),
                "expected": expected,
                "actual": actual,
                "receipt_matches_current": receipt_matches_current,
                "accepted_successor_matches_current": (
                    accepted_successor_matches_current
                ),
                "accepted_manifest_entry_id": (
                    current_entry.get("manifest_entry_id")
                    if current_entry
                    else None
                ),
                "matches": (
                    receipt_matches_current
                    or accepted_successor_matches_current
                ),
            }
        )
    validations: dict[str, bool] = {
        "initialization_proposal_kind": (
            proposal.get("proposal_kind") == "initialization_bundle"
        ),
        "initialization_commit_matches_proposal": (
            str(commit.get("proposal_id") or "")
            == str(proposal.get("proposal_id") or "")
        ),
        "materialization_completed": (
            materialization.get("status") == "completed"
        ),
        "materialized_files_match": all(
            item["matches"] for item in file_results
        ),
        "accepted_source_manifest_active": all(
            item["status"] in {"active", "superseded", "deactivated"}
            for item in manifest
        ),
        "initialization_package_hash_present": bool(
            proposal_payload.get("package_hash")
        ),
    }
    active_commit_ids = {
        str(item["commit_id"])
        for item in service.list_active_accepted_commits(
            authority_only=False
        )
    }
    validations["initialization_commit_active"] = (
        str(commit_id) in active_commit_ids
    )
    with service.store.read_connection() as connection:
        projection_row = connection.execute(
            """
            SELECT source_head_revision, source_active_revision,
                   run_status, projection_hash, completed_at
            FROM projection_runs
            WHERE projection_name='continuity'
              AND source_head_revision=?
              AND source_active_revision=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (
                int(commit["head_canon_revision"]),
                int(commit["active_canon_revision"]),
            ),
        ).fetchone()
    continuity_projection = (
        dict(projection_row) if projection_row is not None else None
    )
    final_projection_hash = str(
        (continuity_projection or {}).get("projection_hash")
        or commit.get("projection_hash")
        or ""
    )
    validations.update(
        {
            "continuity_projection_completed": (
                continuity_projection is not None
                and str(continuity_projection.get("run_status") or "")
                == "completed"
            ),
            "continuity_projection_revision_matches": (
                continuity_projection is not None
                and int(
                    continuity_projection.get(
                        "source_head_revision",
                        -1,
                    )
                )
                == int(commit["head_canon_revision"])
                and int(
                    continuity_projection.get(
                        "source_active_revision",
                        -1,
                    )
                )
                == int(commit["active_canon_revision"])
            ),
            "continuity_projection_hash_present": bool(
                final_projection_hash
            ),
        }
    )

    saga: dict[str, Any] | None = None
    spec_commit: dict[str, Any] | None = None
    spec_proposal: dict[str, Any] | None = None
    if is_v2:
        required_binding_fields = (
            "proposal_id",
            "commit_id",
            "package_hash",
            "power_package_hash",
            "projection_hash",
            "active_canon_revision",
        )
        validations["power_spec_binding_complete"] = all(
            field in power_spec_binding
            and (
                field == "power_package_hash"
                or str(power_spec_binding.get(field) or "").strip()
            )
            for field in required_binding_fields
        )
        try:
            spec_commit = service.inspect_commit(
                str(power_spec_binding.get("commit_id") or "")
            )
            spec_proposal = service.inspect_proposal(
                str(power_spec_binding.get("proposal_id") or "")
            )
        except ContinuityError:
            spec_commit = None
            spec_proposal = None
        expected_spec_package = dict(
            lifecycle_package.get("power_spec_package") or {}
        )
        spec_payload = dict(
            (spec_proposal or {}).get("payload") or {}
        )
        accepted_spec_package = dict(
            spec_payload.get("lifecycle_package") or {}
        )
        expected_spec_package_hash = str(
            expected_spec_package.get("package_hash") or ""
        )
        accepted_spec_package_hash = str(
            accepted_spec_package.get("package_hash")
            or spec_payload.get("package_hash")
            or ""
        )
        requested_spec_package_hash = str(
            power_spec_binding.get("requested_package_hash")
            or expected_spec_package_hash
        )
        expected_power_package_hash = str(
            expected_spec_package.get("power_package_hash") or ""
        )
        accepted_power_package_hash = str(
            accepted_spec_package.get("power_package_hash")
            or spec_payload.get("power_package_hash")
            or ""
        )
        bound_power_package_hash = str(
            power_spec_binding.get("power_package_hash") or ""
        )
        initialization_prepared_revision = int(
            proposal["prepared_canon_revision"]
        )
        validations.update(
            {
                "power_spec_commit_exists": spec_commit is not None,
                "power_spec_proposal_exists": spec_proposal is not None,
                "power_spec_proposal_kind": (
                    (spec_proposal or {}).get("proposal_kind")
                    == "power_spec_change"
                ),
                "power_spec_commit_matches_proposal": (
                    str((spec_commit or {}).get("proposal_id") or "")
                    == str(
                        power_spec_binding.get("proposal_id") or ""
                    )
                ),
                "power_spec_commit_active": (
                    str(power_spec_binding.get("commit_id") or "")
                    in active_commit_ids
                ),
                "power_spec_package_hash_matches": (
                    str(power_spec_binding.get("package_hash") or "")
                    == accepted_spec_package_hash
                    and requested_spec_package_hash
                    == expected_spec_package_hash
                    and (
                        accepted_spec_package_hash
                        == expected_spec_package_hash
                        or (
                            bool(expected_power_package_hash)
                            and accepted_power_package_hash
                            == expected_power_package_hash
                        )
                    )
                ),
                "power_spec_requested_package_hash_matches": (
                    requested_spec_package_hash
                    == expected_spec_package_hash
                ),
                "power_spec_accepted_package_hash_matches": (
                    str(power_spec_binding.get("package_hash") or "")
                    == accepted_spec_package_hash
                ),
                "power_package_hash_matches": (
                    bound_power_package_hash
                    == expected_power_package_hash
                    == accepted_power_package_hash
                ),
                "power_spec_projection_hash_matches": (
                    bool(
                        (spec_commit or {}).get("projection_hash")
                    )
                    and str(
                        power_spec_binding.get("projection_hash") or ""
                    )
                    == str(
                        (spec_commit or {}).get("projection_hash") or ""
                    )
                ),
                "initialization_prepared_after_power_spec": (
                    spec_commit is not None
                    and initialization_prepared_revision
                    >= int(spec_commit["active_canon_revision"])
                    and int(spec_commit["active_canon_revision"])
                    == int(
                        power_spec_binding.get(
                            "active_canon_revision",
                            -1,
                        )
                    )
                ),
                "initialization_commit_follows_prepared_revision": (
                    int(commit["active_canon_revision"])
                    == initialization_prepared_revision + 1
                ),
            }
        )
        saga = {
            "base_canon_revision": initialization_prepared_revision,
            "initialization_base_canon_revision": (
                initialization_prepared_revision
            ),
            "initialization_prepared_canon_revision": (
                initialization_prepared_revision
            ),
            "power_spec_canon_revision": (
                int(spec_commit["active_canon_revision"])
                if spec_commit is not None
                else None
            ),
            "initialization_canon_revision": int(
                commit["active_canon_revision"]
            ),
            "power_spec_reused": bool(
                power_spec_binding.get("power_spec_reused")
            ),
            "power_spec": {
                "proposal": spec_proposal,
                "commit": spec_commit,
                "binding": power_spec_binding,
            },
            "initialization": {
                "proposal": proposal,
                "commit": commit,
                "package_hash": str(
                    proposal_payload.get("package_hash") or ""
                ),
            },
        }

    receipt_path = root / ".plot-rag" / "completion-receipt.json"
    receipt: dict[str, Any] = {}
    receipt_error: str | None = None
    if receipt_path.is_file():
        try:
            loaded = json.loads(
                receipt_path.read_text(encoding="utf-8-sig")
            )
            if not isinstance(loaded, dict):
                raise ValueError(
                    "completion receipt root must be an object"
                )
            receipt = loaded
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            receipt_error = str(exc)
    if receipt_path.is_file():
        expected_receipt_schema = (
            "plot-rag-init/completion-v2"
            if is_v2
            else "plot-rag-init/completion-v1"
        )
        receipt_revisions = dict(
            receipt.get("canon_revisions") or {}
        )
        receipt_schema = str(receipt.get("schema_version") or "")
        legacy_v1_commit_projection_only = bool(
            receipt_schema == "plot-rag-init/completion-v1"
            and not receipt.get("commit_projection_hash")
            and not receipt.get("replay_projection_hash")
        )
        receipt_replay_projection_hash = str(
            final_projection_hash
            if legacy_v1_commit_projection_only
            else (
                receipt.get("replay_projection_hash")
                or receipt.get("projection_hash")
                or ""
            )
        )
        receipt_commit_projection_hash = str(
            receipt.get("commit_projection_hash")
            or (
                receipt.get("projection_hash")
                if receipt_schema == "plot-rag-init/completion-v1"
                and not receipt.get("replay_projection_hash")
                else ""
            )
            or ""
        )
        validations.update(
            {
                "completion_receipt_parseable": (
                    bool(receipt) and receipt_error is None
                ),
                "completion_receipt_schema": (
                    receipt.get("schema_version")
                    == expected_receipt_schema
                ),
                "completion_receipt_ready": (
                    receipt.get("bootstrap_ready") is True
                ),
                "completion_receipt_commit": (
                    str(receipt.get("commit_id") or "")
                    == str(commit_id)
                ),
                "completion_receipt_proposal": (
                    str(receipt.get("canon_proposal_id") or "")
                    == str(proposal["proposal_id"])
                ),
                "completion_receipt_package_hash": (
                    str(receipt.get("package_hash") or "")
                    == str(
                        proposal_payload.get("package_hash") or ""
                    )
                ),
                "completion_receipt_head_revision": (
                    int(receipt_revisions.get("head", -1))
                    == int(commit["head_canon_revision"])
                ),
                "completion_receipt_active_revision": (
                    int(receipt_revisions.get("active", -1))
                    == int(commit["active_canon_revision"])
                ),
                "completion_receipt_projection_hash": (
                    (
                        str(receipt.get("projection_hash") or "")
                        == str(commit["projection_hash"])
                    )
                    if legacy_v1_commit_projection_only
                    else (
                        str(receipt.get("projection_hash") or "")
                        == final_projection_hash
                    )
                ),
                "completion_receipt_replay_projection_hash": (
                    (
                        continuity_projection is not None
                        and str(
                            continuity_projection.get("run_status") or ""
                        )
                        == "completed"
                        and bool(final_projection_hash)
                    )
                    if legacy_v1_commit_projection_only
                    else (
                        receipt_replay_projection_hash
                        == final_projection_hash
                    )
                ),
                "completion_receipt_commit_projection_hash": (
                    receipt_commit_projection_hash
                    == str(commit["projection_hash"])
                ),
                "completion_receipt_event_hash": (
                    str(receipt.get("accepted_event_hash") or "")
                    == _sha256(
                        _canonical_json(commit.get("events") or [])
                    )
                ),
            }
        )
        if is_v2:
            receipt_spec = dict(receipt.get("power_spec") or {})
            receipt_initialization = dict(
                receipt.get("initialization") or {}
            )
            receipt_saga = dict(receipt.get("saga") or {})
            expected_power_spec_reused = bool(
                power_spec_binding.get("power_spec_reused")
            )
            expected_spec_grant_in_saga = bool(
                power_spec_binding.get(
                    "power_spec_grant_consumed_in_this_saga",
                    not expected_power_spec_reused,
                )
            )
            validations.update(
                {
                    "completion_receipt_power_spec_proposal": (
                        str(
                            receipt_spec.get("proposal_id") or ""
                        )
                        == str(
                            power_spec_binding.get("proposal_id")
                            or ""
                        )
                    ),
                    "completion_receipt_power_spec_commit": (
                        str(receipt_spec.get("commit_id") or "")
                        == str(
                            power_spec_binding.get("commit_id") or ""
                        )
                    ),
                    "completion_receipt_power_spec_hash": (
                        str(
                            receipt_spec.get("package_hash") or ""
                        )
                        == str(
                            power_spec_binding.get("package_hash")
                            or ""
                        )
                    ),
                    "completion_receipt_requested_power_spec_hash": (
                        str(
                            receipt_spec.get(
                                "requested_package_hash"
                            )
                            or ""
                        )
                        == str(
                            power_spec_binding.get(
                                "requested_package_hash"
                            )
                            or power_spec_binding.get("package_hash")
                            or ""
                        )
                    ),
                    "completion_receipt_power_package_hash": (
                        str(
                            receipt_spec.get(
                                "power_package_hash"
                            )
                            or ""
                        )
                        == str(
                            power_spec_binding.get(
                                "power_package_hash"
                            )
                            or ""
                        )
                    ),
                    "completion_receipt_initialization_commit": (
                        str(
                            receipt_initialization.get("commit_id")
                            or ""
                        )
                        == str(commit_id)
                    ),
                    "completion_receipt_initialization_hash": (
                        str(
                            receipt_initialization.get(
                                "package_hash"
                            )
                            or ""
                        )
                        == str(
                            proposal_payload.get("package_hash") or ""
                        )
                    ),
                    "completion_receipt_initialization_projection": (
                        str(
                            receipt_initialization.get(
                                "projection_hash"
                            )
                            or ""
                        )
                        == str(commit["projection_hash"])
                    ),
                    "completion_receipt_initialization_prepared_revision": (
                        int(
                            receipt_initialization.get(
                                "prepared_canon_revision",
                                -1,
                            )
                        )
                        == int(proposal["prepared_canon_revision"])
                    ),
                    "completion_receipt_initialization_base_revision": (
                        int(
                            receipt.get(
                                "initialization_base_canon_revision",
                                receipt.get("base_canon_revision", -1),
                            )
                        )
                        == int(proposal["prepared_canon_revision"])
                    ),
                    "completion_receipt_two_stage_revision": (
                        spec_commit is not None
                        and int(
                            receipt_spec.get(
                                "active_canon_revision",
                                -1,
                            )
                        )
                        == int(spec_commit["active_canon_revision"])
                        and int(
                            receipt_initialization.get(
                                "active_canon_revision",
                                -1,
                            )
                        )
                        == int(commit["active_canon_revision"])
                    ),
                    "completion_receipt_power_spec_reuse": (
                        receipt_saga.get("power_spec_reused")
                        is expected_power_spec_reused
                    ),
                    "completion_receipt_power_spec_grant_scope": (
                        receipt_saga.get(
                            "power_spec_grant_consumed_in_this_saga"
                        )
                        is expected_spec_grant_in_saga
                    ),
                    "completion_receipt_initialization_grant": (
                        receipt_saga.get(
                            "initialization_grant_consumed"
                        )
                        is True
                    ),
                }
            )

    complete = all(validations.values())
    failed_validations = sorted(
        key for key, passed in validations.items() if not passed
    )
    with service.store.read_connection() as connection:
        ready_row = connection.execute(
            "SELECT value FROM state_meta WHERE key='bootstrap_ready'"
        ).fetchone()
        ready_commit_row = connection.execute(
            "SELECT value FROM state_meta "
            "WHERE key='bootstrap_ready_commit_id'"
        ).fetchone()
    bootstrap_ready = bool(
        ready_row is not None
        and str(ready_row["value"]) == "1"
        and ready_commit_row is not None
        and str(ready_commit_row["value"]) == str(commit_id)
    )
    return {
        "status": "verified" if complete else "degraded",
        "commit": commit,
        "proposal": proposal,
        "bundle_schema_version": bundle_schema_version,
        "saga": saga,
        "materialization": materialization,
        "accepted_source_manifest": manifest,
        "files": file_results,
        "projection_hash": final_projection_hash,
        "commit_projection_hash": str(commit["projection_hash"]),
        "continuity_projection": continuity_projection,
        "current_projection_hash": service.projection_hash(),
        "receipt_path": str(receipt_path.resolve(strict=False)),
        "receipt": receipt or None,
        "receipt_error": receipt_error,
        "validations": validations,
        "failed_validations": failed_validations,
        "bootstrap_ready": bootstrap_ready,
    }


def _diagnostic_sqlite_component(
    path: Path,
    *,
    expected_tables: Sequence[str] = (),
    allowed_tables: Sequence[str] | None = None,
    meta_queries: Mapping[str, tuple[str, str]] | None = None,
    scalar_queries: Mapping[str, str] | None = None,
    record_queries: Mapping[str, str] | None = None,
    absent_if_all_tables_missing: bool = False,
) -> dict[str, Any]:
    resolved = path.expanduser().resolve(strict=False)
    if not resolved.is_file():
        return {
            "status": "not_created",
            "path": str(resolved),
            "integrity": None,
            "missing_tables": [],
            "unexpected_tables": [],
            "meta": {},
            "counts": {},
            "records": {},
            "read_only_snapshot": True,
        }
    try:
        target = SimpleNamespace(db_path=resolved)
        with state_rag._open_diagnostic_database(target) as connection:
            integrity_row = connection.execute("PRAGMA quick_check").fetchone()
            integrity = str(
                integrity_row[0] if integrity_row is not None else "unknown"
            )
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            missing = sorted(set(expected_tables) - tables)
            unexpected = (
                sorted(tables - set(allowed_tables))
                if allowed_tables is not None
                else []
            )
            component_absent = bool(
                absent_if_all_tables_missing
                and expected_tables
                and not (set(expected_tables) & tables)
            )
            meta: dict[str, Any] = {}
            counts: dict[str, Any] = {}
            records: dict[str, Any] = {}
            if not unexpected:
                for name, (table, key) in (meta_queries or {}).items():
                    if table not in tables:
                        meta[name] = None
                        continue
                    row = connection.execute(
                        f"SELECT value FROM {table} WHERE key=?",
                        (key,),
                    ).fetchone()
                    meta[name] = None if row is None else str(row[0])
                for name, sql in (scalar_queries or {}).items():
                    try:
                        row = connection.execute(sql).fetchone()
                        counts[name] = None if row is None else row[0]
                    except sqlite3.Error:
                        counts[name] = None
                for name, sql in (record_queries or {}).items():
                    try:
                        row = connection.execute(sql).fetchone()
                        records[name] = None if row is None else dict(row)
                    except sqlite3.Error:
                        records[name] = None
        if unexpected:
            status = "failed"
        elif component_absent:
            status = "not_created"
        else:
            status = "ok" if integrity == "ok" and not missing else "failed"
        result = {
            "status": status,
            "path": str(resolved),
            "integrity": integrity,
            "missing_tables": missing,
            "unexpected_tables": unexpected,
            "meta": meta,
            "counts": counts,
            "records": records,
            "database_exists": True,
            "read_only_snapshot": True,
        }
        if unexpected:
            result["reason"] = (
                "SQLite database contains tables outside the component "
                f"ownership contract: {unexpected}"
            )
        return result
    except Exception as exc:
        return {
            "status": "failed",
            "path": str(resolved),
            "integrity": None,
            "missing_tables": list(expected_tables),
            "unexpected_tables": [],
            "meta": {},
            "counts": {},
            "records": {},
            "read_only_snapshot": True,
            "reason": str(exc),
        }


def _diagnostic_source_manifest(
    root: Path,
    state_path: Path,
) -> dict[str, Any]:
    """Validate the current accepted source projection from a read-only snapshot."""

    resolved = state_path.expanduser().resolve(strict=False)
    if not resolved.is_file():
        return {
            "status": "not_created",
            "path": str(resolved),
            "read_only_snapshot": True,
        }
    try:
        target = SimpleNamespace(db_path=resolved)
        with state_rag._open_diagnostic_database(target) as connection:
            ledger = source_manifest_projection_status(connection)
            snapshot = current_manifest_snapshot(connection)
    except Exception as exc:
        return {
            "status": "failed",
            "path": str(resolved),
            "reason": str(exc),
            "read_only_snapshot": True,
        }

    entries = [dict(item) for item in snapshot.get("entries") or []]
    path_keys: list[str] = []
    files: list[dict[str, Any]] = []
    for entry in entries:
        relative = normalize_source_path(
            entry.get("path") or entry.get("source_path")
        )
        path_keys.append(relative.casefold())
        candidate = (root / relative).resolve(strict=False)
        inside = _inside(candidate, root)
        exists = inside and candidate.is_file()
        expected = str(entry.get("content_hash") or "")
        actual = _sha256_file(candidate) if exists else ""
        files.append(
            {
                "path": relative,
                "manifest_entry_id": entry.get("manifest_entry_id"),
                "commit_id": entry.get("commit_id"),
                "inside_project": inside,
                "exists": exists,
                "expected_sha256": expected or None,
                "actual_sha256": actual or None,
                "matches": bool(expected) and actual == expected,
            }
        )

    active_rows = int(ledger.get("active_rows") or 0)
    duplicate_active_rows = int(ledger.get("duplicate_active_rows") or 0)
    validations = {
        "history_present": int(ledger.get("history_rows") or 0) > 0,
        "active_rows_present": active_rows > 0,
        "physical_active_paths_unique": duplicate_active_rows == 0,
        "current_projection_covers_active_rows": len(entries) == active_rows,
        "current_paths_unique": len(path_keys) == len(set(path_keys)),
        "current_files_match": bool(files)
        and all(item["matches"] for item in files),
    }
    ready = all(validations.values())
    return {
        "status": "ok" if ready else "failed",
        "path": str(resolved),
        "managed": bool(snapshot.get("managed")),
        "history_rows": int(ledger.get("history_rows") or 0),
        "active_rows": active_rows,
        "unique_active_paths": int(ledger.get("unique_active_paths") or 0),
        "duplicate_active_rows": duplicate_active_rows,
        "current_entries": len(entries),
        "active_manifest_proposal_id": ledger.get(
            "active_manifest_proposal_id"
        ),
        "active_manifest_commit_id": ledger.get("active_manifest_commit_id"),
        "target_manifest_hash": ledger.get("target_manifest_hash"),
        "files": files,
        "validations": validations,
        "failed_validations": sorted(
            name for name, passed in validations.items() if not passed
        ),
        "read_only_snapshot": True,
    }


def _validate_component_schema(
    component: dict[str, Any],
    *,
    meta_key: str,
    supported: int,
    label: str,
) -> dict[str, Any]:
    if component.get("status") != "ok":
        return component
    stored = (component.get("meta") or {}).get(meta_key)
    if stored != str(supported):
        component["status"] = "failed"
        component["reason"] = (
            f"{label} schema version mismatch: "
            f"stored={stored!r}, supported={supported}"
        )
    return component


def _legacy_doctor_check(
    legacy: Mapping[str, Any],
    name: str,
    *,
    fallback_status: str = "not_created",
) -> dict[str, Any]:
    for raw in legacy.get("checks") or []:
        if str(raw.get("name") or "") == name:
            return {
                key: value
                for key, value in dict(raw).items()
                if key != "name"
            }
    return {
        "status": fallback_status,
        "reason": f"legacy doctor did not return the {name} check",
        "read_only_snapshot": True,
    }


def _diagnostic_method_pack() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[1]
        / "knowledge"
        / "webnovel_methods.json"
    )
    if not path.is_file():
        return {
            "status": "failed",
            "path": str(path.resolve(strict=False)),
            "reason": "bundled webnovel method pack is missing",
            "read_only_snapshot": True,
        }
    try:
        pack = WebnovelMethodPack(path)
        payload = dict(pack.payload)
        return {
            "status": "ok",
            "path": str(path.resolve()),
            "schema_version": int(payload.get("schema_version") or -1),
            "supported_schema_version": METHOD_PACK_SCHEMA_VERSION,
            "pack_id": str(payload.get("pack_id") or ""),
            "pack_version": str(payload.get("pack_version") or ""),
            "cards_count": len(pack.cards),
            "source_traced_cards": sum(
                1
                for card in pack.cards
                if isinstance(card.get("source"), Mapping)
                and bool((card.get("source") or {}).get("sha256"))
            ),
            "sha256": _sha256_file(path),
            "read_only_snapshot": True,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "path": str(path.resolve(strict=False)),
            "reason": str(exc),
            "read_only_snapshot": True,
        }


def _diagnostic_bootstrap_readiness(
    root: Path,
    continuity: Mapping[str, Any],
    source_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    receipt_path = root / ".plot-rag" / "completion-receipt.json"
    meta = dict(continuity.get("meta") or {})
    records = dict(continuity.get("records") or {})
    state_ready = meta.get("bootstrap_ready") == "1"
    commit_id = str(meta.get("bootstrap_ready_commit_id") or "")
    power_spec_commit_id = str(
        meta.get("bootstrap_ready_power_spec_commit_id") or ""
    )
    stored_receipt_hash = str(
        meta.get("bootstrap_ready_receipt_sha256") or ""
    )
    base = {
        "ready": False,
        "state_flag": state_ready,
        "commit_id": commit_id or None,
        "power_spec_commit_id": power_spec_commit_id or None,
        "receipt_path": str(receipt_path.resolve(strict=False)),
        "receipt_exists": receipt_path.is_file(),
        "read_only_snapshot": True,
    }
    continuity_status = str(continuity.get("status") or "")
    if continuity_status != "ok":
        return {
            **base,
            "status": (
                "not_created"
                if continuity_status == "not_created"
                else "failed"
            ),
            "reason": (
                "continuity storage is not initialized"
                if continuity_status == "not_created"
                else "continuity storage is not healthy"
            ),
        }

    if not state_ready:
        stale = bool(
            commit_id
            or stored_receipt_hash
            or receipt_path.is_file()
        )
        return {
            **base,
            "status": "failed" if stale else "not_ready",
            "reason": (
                "bootstrap readiness metadata and receipt disagree"
                if stale
                else "accepted initialization has not completed"
            ),
        }

    validations: dict[str, bool] = {
        "commit_id_present": bool(commit_id),
        "stored_receipt_hash_present": bool(stored_receipt_hash),
        "receipt_exists": receipt_path.is_file(),
    }
    receipt: dict[str, Any] = {}
    receipt_hash = ""
    receipt_error = ""
    if receipt_path.is_file():
        try:
            loaded = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
            if not isinstance(loaded, dict):
                raise ValueError("completion receipt root must be an object")
            receipt = loaded
            receipt_hash = _sha256(_canonical_json(receipt))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            receipt_error = str(exc)

    commit_record = records.get("bootstrap_commit") or {}
    materialization_record = (
        records.get("bootstrap_materialization") or {}
    )
    bootstrap_projection = records.get("bootstrap_projection") or {}
    active_projection = records.get("active_projection") or {}
    power_spec_commit = records.get("bootstrap_power_spec_commit") or {}
    receipt_revisions = dict(receipt.get("canon_revisions") or {})
    receipt_schema = str(receipt.get("schema_version") or "")
    legacy_v1_commit_projection_only = bool(
        receipt_schema == "plot-rag-init/completion-v1"
        and not receipt.get("commit_projection_hash")
        and not receipt.get("replay_projection_hash")
    )
    receipt_spec = dict(receipt.get("power_spec") or {})
    receipt_initialization = dict(receipt.get("initialization") or {})
    receipt_saga = dict(receipt.get("saga") or {})
    validations.update(
        {
            "receipt_parseable": bool(receipt) and not receipt_error,
            "receipt_schema_supported": (
                receipt_schema
                in {
                    "plot-rag-init/completion-v1",
                    "plot-rag-init/completion-v2",
                }
            ),
            "receipt_ready": receipt.get("bootstrap_ready") is True,
            "receipt_commit_matches": (
                bool(commit_id)
                and str(receipt.get("commit_id") or "") == commit_id
            ),
            "receipt_hash_matches": (
                bool(stored_receipt_hash)
                and receipt_hash == stored_receipt_hash
            ),
            "commit_exists": (
                str(commit_record.get("commit_id") or "") == commit_id
            ),
            "commit_is_bootstrap": (
                str(commit_record.get("artifact_stage") or "")
                == "bootstrap"
            ),
            "commit_is_authoritative": bool(
                commit_record.get("changes_authority")
            ),
            "materialization_completed": (
                str(materialization_record.get("run_status") or "")
                == "completed"
            ),
            "receipt_head_revision_matches_commit": (
                str(receipt_revisions.get("head"))
                == str(commit_record.get("head_revision_after"))
            ),
            "receipt_active_revision_matches_commit": (
                str(receipt_revisions.get("active"))
                == str(commit_record.get("active_revision_after"))
            ),
            "bootstrap_projection_completed": (
                str(bootstrap_projection.get("run_status") or "")
                == "completed"
            ),
            "bootstrap_projection_hash_matches": (
                (
                    bool(commit_record.get("projection_hash"))
                    and str(receipt.get("projection_hash") or "")
                    == str(commit_record.get("projection_hash") or "")
                )
                if legacy_v1_commit_projection_only
                else (
                    bool(bootstrap_projection.get("projection_hash"))
                    and str(receipt.get("projection_hash") or "")
                    == str(
                        bootstrap_projection.get("projection_hash") or ""
                    )
                )
            ),
            "receipt_replay_projection_hash_matches": (
                bool(bootstrap_projection.get("projection_hash"))
                if legacy_v1_commit_projection_only
                else (
                    str(
                        receipt.get("replay_projection_hash")
                        or receipt.get("projection_hash")
                        or ""
                    )
                    == str(
                        bootstrap_projection.get("projection_hash") or ""
                    )
                )
            ),
            "receipt_commit_projection_hash_matches": (
                str(
                    receipt.get("commit_projection_hash")
                    or (
                        receipt.get("projection_hash")
                        if receipt_schema
                        == "plot-rag-init/completion-v1"
                        and not receipt.get("replay_projection_hash")
                        else ""
                    )
                    or ""
                )
                == str(commit_record.get("projection_hash") or "")
            ),
            "active_projection_completed": (
                str(active_projection.get("run_status") or "")
                == "completed"
            ),
        }
    )
    if receipt_schema == "plot-rag-init/completion-v2":
        spec_revision = int(
            receipt_spec.get("active_canon_revision", -1)
        )
        initialization_prepared_revision = int(
            receipt_initialization.get(
                "prepared_canon_revision",
                receipt_saga.get(
                    "initialization_prepared_canon_revision",
                    receipt.get("base_canon_revision", -1),
                ),
            )
        )
        initialization_revision = int(
            receipt_initialization.get("active_canon_revision", -1)
        )
        power_spec_reused = bool(
            receipt_saga.get(
                "power_spec_reused",
                receipt.get("power_spec_reused", False),
            )
        )
        power_spec_grant_in_saga = receipt_saga.get(
            "power_spec_grant_consumed_in_this_saga",
            not power_spec_reused,
        )
        initialization_grant_consumed = receipt_saga.get(
            "initialization_grant_consumed",
            True,
        )
        validations.update(
            {
                "power_spec_commit_id_present": bool(
                    power_spec_commit_id
                ),
                "power_spec_commit_exists": (
                    bool(power_spec_commit_id)
                    and str(power_spec_commit.get("commit_id") or "")
                    == power_spec_commit_id
                ),
                "power_spec_commit_is_bootstrap": (
                    str(power_spec_commit.get("artifact_stage") or "")
                    == "bootstrap"
                ),
                "power_spec_commit_is_authoritative": bool(
                    power_spec_commit.get("changes_authority")
                ),
                "receipt_power_spec_commit_matches": (
                    str(receipt_spec.get("commit_id") or "")
                    == power_spec_commit_id
                ),
                "receipt_power_spec_proposal_present": bool(
                    receipt_spec.get("proposal_id")
                ),
                "receipt_power_spec_package_hash_present": bool(
                    receipt_spec.get("package_hash")
                ),
                "receipt_initialization_commit_matches": (
                    str(receipt_initialization.get("commit_id") or "")
                    == commit_id
                ),
                "receipt_initialization_proposal_matches": (
                    str(
                        receipt_initialization.get("proposal_id") or ""
                    )
                    == str(receipt.get("canon_proposal_id") or "")
                ),
                "receipt_initialization_package_hash_matches": (
                    str(
                        receipt_initialization.get("package_hash") or ""
                    )
                    == str(receipt.get("package_hash") or "")
                ),
                "receipt_initialization_projection_matches_commit": (
                    str(
                        receipt_initialization.get("projection_hash")
                        or ""
                    )
                    == str(commit_record.get("projection_hash") or "")
                ),
                "two_stage_revision_is_consecutive": (
                    spec_revision >= 0
                    and initialization_prepared_revision >= spec_revision
                    and initialization_revision
                    == initialization_prepared_revision + 1
                    and initialization_revision
                    == int(
                        commit_record.get("active_revision_after")
                        or -1
                    )
                ),
                "receipt_initialization_base_revision_matches": (
                    int(
                        receipt.get(
                            "initialization_base_canon_revision",
                            receipt.get(
                                "base_canon_revision",
                                -1,
                            ),
                        )
                    )
                    == initialization_prepared_revision
                ),
                "saga_power_spec_grant_scope_matches": (
                    power_spec_grant_in_saga
                    is (not power_spec_reused)
                ),
                "saga_initialization_grant_consumed": (
                    initialization_grant_consumed is True
                ),
                "saga_two_grants_consumed": (
                    receipt_saga.get("two_grants_consumed") is True
                ),
                "saga_two_cas_commits_completed": (
                    receipt_saga.get(
                        "two_cas_commits_completed"
                    )
                    is True
                ),
            }
        )

    verified_files: list[dict[str, Any]] = []
    current_manifest_files = {
        normalize_source_path(item.get("path")).casefold(): dict(item)
        for item in (source_manifest or {}).get("files") or []
        if item.get("path")
    }
    accepted_successors_ready = bool(
        (source_manifest or {}).get("status") == "ok"
        and (source_manifest or {}).get("active_manifest_commit_id")
    )
    raw_files = receipt.get("verified_files") or []
    files_valid = bool(raw_files)
    if not isinstance(raw_files, list):
        raw_files = []
        files_valid = False
    for raw in raw_files:
        item = dict(raw) if isinstance(raw, Mapping) else {}
        relative = str(item.get("path") or "")
        expected = str(item.get("sha256") or "")
        candidate = (root / relative).resolve(strict=False)
        inside = bool(relative) and _inside(candidate, root)
        exists = inside and candidate.is_file()
        actual = _sha256_file(candidate) if exists else ""
        receipt_matches_current = bool(expected) and actual == expected
        try:
            manifest_key = (
                normalize_source_path(relative).casefold()
                if relative
                else ""
            )
        except ContinuityError:
            manifest_key = ""
        current_manifest = current_manifest_files.get(manifest_key)
        accepted_successor_matches_current = bool(
            accepted_successors_ready
            and current_manifest
            and current_manifest.get("matches") is True
            and str(current_manifest.get("actual_sha256") or "") == actual
        )
        matches = (
            receipt_matches_current
            or accepted_successor_matches_current
        )
        files_valid = files_valid and inside and exists and matches
        verified_files.append(
            {
                "path": relative,
                "inside_project": inside,
                "exists": exists,
                "expected_sha256": expected or None,
                "actual_sha256": actual or None,
                "receipt_matches_current": receipt_matches_current,
                "accepted_successor_matches_current": (
                    accepted_successor_matches_current
                ),
                "accepted_manifest_entry_id": (
                    current_manifest.get("manifest_entry_id")
                    if current_manifest
                    else None
                ),
                "accepted_manifest_commit_id": (
                    current_manifest.get("commit_id")
                    if current_manifest
                    else None
                ),
                "validation_mode": (
                    "bootstrap_bytes"
                    if receipt_matches_current
                    else "accepted_manifest_successor"
                    if accepted_successor_matches_current
                    else "unaccepted_drift"
                ),
                "matches": matches,
            }
        )
    validations["verified_files_match"] = files_valid

    projection_runs = receipt.get("projection_runs") or {}
    projection_runs_valid = bool(projection_runs) and isinstance(
        projection_runs, Mapping
    )
    if isinstance(projection_runs, Mapping):
        projection_runs_valid = projection_runs_valid and all(
            str((value or {}).get("status") or "")
            in {"succeeded", "cached"}
            for value in projection_runs.values()
            if isinstance(value, Mapping)
        ) and all(
            isinstance(value, Mapping)
            for value in projection_runs.values()
        )
    validations["projection_runs_complete"] = projection_runs_valid

    ready = all(validations.values())
    failed_validations = sorted(
        name for name, passed in validations.items() if not passed
    )
    return {
        **base,
        "status": "ready" if ready else "failed",
        "ready": ready,
        "receipt_schema_version": receipt.get("schema_version"),
        "receipt_sha256": receipt_hash or None,
        "stored_receipt_sha256": stored_receipt_hash or None,
        "receipt_error": receipt_error or None,
        "commit": commit_record or None,
        "power_spec_commit": power_spec_commit or None,
        "materialization": materialization_record or None,
        "bootstrap_projection": bootstrap_projection or None,
        "active_projection": active_projection or None,
        "receipt": receipt or None,
        "verified_files": verified_files,
        "validations": validations,
        "failed_validations": failed_validations,
    }


def doctor_v1(project_root: Path | str) -> dict[str, Any]:
    """Inspect every v1 runtime surface without mutating project state."""

    root = Path(project_root).expanduser().resolve(strict=False)
    legacy = state_rag.doctor(root)
    try:
        config = load_config(root)
    except Exception as exc:
        return {
            "status": "failed",
            "project_root": str(root),
            "legacy": legacy,
            "checks": [],
            "zero_write": True,
            "reason": str(exc),
        }
    if int(config.get("config_version") or 1) < STRICT_CONFIG_VERSION:
        return legacy

    state_path = Path(config["state"]["db_path"])
    state = _diagnostic_sqlite_component(
        state_path,
        expected_tables=(
            "state_meta",
            "turns",
            "turn_commits",
            "state_events",
            "current_facts",
            "fact_vectors",
        ),
        allowed_tables=tuple(sorted(STATE_DATABASE_TABLES)),
        meta_queries={
            "schema_version": ("state_meta", "schema_version"),
        },
        scalar_queries={
            "turns": "SELECT COUNT(*) FROM turns",
            "facts": "SELECT COUNT(*) FROM current_facts",
            "events": "SELECT COUNT(*) FROM state_events",
            "commits": "SELECT COUNT(*) FROM turn_commits",
            "vectors": "SELECT COUNT(*) FROM fact_vectors",
        },
    )
    _validate_component_schema(
        state,
        meta_key="schema_version",
        supported=state_rag.SCHEMA_VERSION,
        label="state",
    )

    continuity = _diagnostic_sqlite_component(
        state_path,
        expected_tables=(
            "entities",
            "entity_aliases",
            "mention_resolutions",
            "artifacts",
            "proposals",
            "proposal_issues",
            "approval_grants",
            "canon_commits",
            "continuity_events",
            "event_links",
            "canon_facts",
            "timeless_facts",
            "planned_facts",
            "branch_facts",
            "fact_versions",
            "location_state",
            "inventory_state",
            "relation_state",
            *sorted(state_rag.CONTINUITY_POWER_TABLES),
            "belief_state",
            "open_loops",
            "accepted_source_manifest",
            "materialization_runs",
            "materialization_files",
            "materialization_journal",
            "projection_runs",
            "idempotency_records",
        ),
        allowed_tables=tuple(sorted(STATE_DATABASE_TABLES)),
        meta_queries={
            "legacy_schema_version": ("state_meta", "schema_version"),
            "continuity_schema_version": (
                "state_meta",
                "continuity_schema_version",
            ),
            "head_canon_revision": ("state_meta", "head_canon_revision"),
            "active_canon_revision": ("state_meta", "active_canon_revision"),
            "bootstrap_ready": ("state_meta", "bootstrap_ready"),
            "bootstrap_ready_commit_id": (
                "state_meta",
                "bootstrap_ready_commit_id",
            ),
            "bootstrap_ready_power_spec_commit_id": (
                "state_meta",
                "bootstrap_ready_power_spec_commit_id",
            ),
            "bootstrap_ready_receipt_sha256": (
                "state_meta",
                "bootstrap_ready_receipt_sha256",
            ),
        },
        scalar_queries={
            "entities": "SELECT COUNT(*) FROM entities",
            "proposals": "SELECT COUNT(*) FROM proposals",
            "accepted_commits": "SELECT COUNT(*) FROM canon_commits",
            "active_facts": (
                "SELECT "
                "(SELECT COUNT(*) FROM canon_facts) + "
                "(SELECT COUNT(*) FROM timeless_facts)"
            ),
            "open_loops": "SELECT COUNT(*) FROM open_loops",
            "manifest_active": (
                "SELECT COUNT(*) FROM accepted_source_manifest "
                "WHERE manifest_status='active'"
            ),
            "manifest_pending": (
                "SELECT COUNT(*) FROM accepted_source_manifest "
                "WHERE manifest_status<>'active'"
            ),
            "materialization_incomplete": (
                "SELECT COUNT(*) FROM materialization_runs "
                "WHERE run_status<>'completed'"
            ),
            "projection_incomplete": (
                "SELECT COUNT(*) FROM projection_runs "
                "WHERE run_status<>'completed'"
            ),
        },
        record_queries={
            "bootstrap_power_spec_commit": (
                "SELECT commit_id, proposal_id, operation, artifact_stage, "
                "head_revision_after, active_revision_after, "
                "changes_authority, projection_hash "
                "FROM canon_commits "
                "WHERE commit_id=("
                "SELECT value FROM state_meta "
                "WHERE key='bootstrap_ready_power_spec_commit_id'"
                ")"
            ),
            "bootstrap_commit": (
                "SELECT commit_id, operation, artifact_stage, "
                "head_revision_after, active_revision_after, "
                "changes_authority, projection_hash "
                "FROM canon_commits "
                "WHERE commit_id=("
                "SELECT value FROM state_meta "
                "WHERE key='bootstrap_ready_commit_id'"
                ")"
            ),
            "bootstrap_materialization": (
                "SELECT run_id, commit_id, run_status, completed_at "
                "FROM materialization_runs "
                "WHERE commit_id=("
                "SELECT value FROM state_meta "
                "WHERE key='bootstrap_ready_commit_id'"
                ")"
            ),
            "bootstrap_projection": (
                "SELECT source_head_revision, source_active_revision, "
                "run_status, projection_hash, completed_at "
                "FROM projection_runs "
                "WHERE projection_name='continuity' "
                "AND source_head_revision=("
                "SELECT head_revision_after FROM canon_commits "
                "WHERE commit_id=("
                "SELECT value FROM state_meta "
                "WHERE key='bootstrap_ready_commit_id'"
                ")) "
                "AND source_active_revision=("
                "SELECT active_revision_after FROM canon_commits "
                "WHERE commit_id=("
                "SELECT value FROM state_meta "
                "WHERE key='bootstrap_ready_commit_id'"
                ")) "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            "active_projection": (
                "SELECT source_head_revision, source_active_revision, "
                "run_status, projection_hash, completed_at "
                "FROM projection_runs "
                "WHERE projection_name='continuity' "
                "ORDER BY source_head_revision DESC, "
                "source_active_revision DESC, created_at DESC "
                "LIMIT 1"
            ),
        },
    )
    _validate_component_schema(
        continuity,
        meta_key="continuity_schema_version",
        supported=CONTINUITY_SCHEMA_VERSION,
        label="continuity",
    )
    source_manifest = _diagnostic_source_manifest(root, state_path)

    authority = _diagnostic_sqlite_component(
        _authority_index_path(root),
        expected_tables=(
            "authority_index_meta",
            "authority_files",
            "authority_chunks",
            "authority_vectors",
            "rerank_candidate_cache",
        ),
        allowed_tables=tuple(sorted(AUTHORITY_INDEX_TABLES)),
        meta_queries={
            "schema_version": (
                "authority_index_meta",
                "authority_index_schema_version",
            )
        },
        scalar_queries={
            "files": "SELECT COUNT(*) FROM authority_files",
            "chunks": "SELECT COUNT(*) FROM authority_chunks",
            "vectors": "SELECT COUNT(*) FROM authority_vectors",
            "candidate_cache": "SELECT COUNT(*) FROM rerank_candidate_cache",
        },
    )
    _validate_component_schema(
        authority,
        meta_key="schema_version",
        supported=AUTHORITY_INDEX_SCHEMA_VERSION,
        label="authority index",
    )

    initialization = _diagnostic_sqlite_component(
        Path(config["initialization"]["database_path"]),
        expected_tables=(
            "initialization_meta",
            "initialization_payload_blobs",
            "initialization_sessions",
            "initialization_revisions",
            "initialization_journal",
            "initialization_checkpoints",
            "initialization_idempotency",
            "initialization_proposals",
            "initialization_session_proposals",
            "initialization_source_versions",
            "initialization_session_sources",
            "initialization_remote_response_cache",
        ),
        allowed_tables=tuple(sorted(REMOTE_CACHE_SHARED_TABLES)),
        meta_queries={
            "schema_version": ("initialization_meta", "schema_version")
        },
        scalar_queries={
            "sessions": "SELECT COUNT(*) FROM initialization_sessions",
            "active_sessions": (
                "SELECT COUNT(*) FROM initialization_sessions "
                "WHERE status NOT IN ('COMPLETED','CANCELLED')"
            ),
            "frozen_proposals": (
                "SELECT COUNT(*) FROM initialization_sessions "
                "WHERE status='PROPOSAL_FROZEN'"
            ),
            "proposals": "SELECT COUNT(*) FROM initialization_proposals",
            "journal_entries": "SELECT COUNT(*) FROM initialization_journal",
            "source_versions": (
                "SELECT COUNT(*) FROM initialization_source_versions"
            ),
            "remote_cache_entries": (
                "SELECT COUNT(*) "
                "FROM initialization_remote_response_cache"
            ),
        },
    )
    _validate_component_schema(
        initialization,
        meta_key="schema_version",
        supported=INIT_DB_SCHEMA_VERSION,
        label="initialization",
    )

    longform_memory = _diagnostic_sqlite_component(
        _longform_database_path(root),
        expected_tables=(
            "longform_memory_meta",
            "memory_entries",
        ),
        allowed_tables=tuple(sorted(LONGFORM_SHARED_TABLES)),
        meta_queries={
            "schema_version": ("longform_memory_meta", "schema_version"),
        },
        scalar_queries={
            "entries": "SELECT COUNT(*) FROM memory_entries",
            "working": (
                "SELECT COUNT(*) FROM memory_entries WHERE layer='working'"
            ),
            "episodic": (
                "SELECT COUNT(*) FROM memory_entries WHERE layer='episodic'"
            ),
            "semantic": (
                "SELECT COUNT(*) FROM memory_entries WHERE layer='semantic'"
            ),
        },
        absent_if_all_tables_missing=True,
    )
    _validate_component_schema(
        longform_memory,
        meta_key="schema_version",
        supported=MEMORY_SCHEMA_VERSION,
        label="long-form memory",
    )

    longform_summary = _diagnostic_sqlite_component(
        _longform_database_path(root),
        expected_tables=(
            "longform_summary_meta",
            "accepted_summaries",
        ),
        allowed_tables=tuple(sorted(LONGFORM_SHARED_TABLES)),
        meta_queries={
            "schema_version": ("longform_summary_meta", "schema_version"),
        },
        scalar_queries={
            "chapter": (
                "SELECT COUNT(*) FROM accepted_summaries "
                "WHERE level='chapter'"
            ),
            "arc": (
                "SELECT COUNT(*) FROM accepted_summaries "
                "WHERE level='arc'"
            ),
            "volume": (
                "SELECT COUNT(*) FROM accepted_summaries "
                "WHERE level='volume'"
            ),
        },
        absent_if_all_tables_missing=True,
    )
    _validate_component_schema(
        longform_summary,
        meta_key="schema_version",
        supported=SUMMARY_SCHEMA_VERSION,
        label="long-form summary",
    )

    method_memory = _diagnostic_sqlite_component(
        _longform_database_path(root),
        expected_tables=(
            "craft_memory_meta",
            "craft_patterns",
        ),
        allowed_tables=tuple(sorted(LONGFORM_SHARED_TABLES)),
        meta_queries={
            "schema_version": ("craft_memory_meta", "schema_version"),
        },
        scalar_queries={
            "patterns": "SELECT COUNT(*) FROM craft_patterns",
        },
        absent_if_all_tables_missing=True,
    )
    _validate_component_schema(
        method_memory,
        meta_key="schema_version",
        supported=CRAFT_MEMORY_SCHEMA_VERSION,
        label="project method memory",
    )
    method_pack = _diagnostic_method_pack()
    method_failed = any(
        item.get("status") in {"failed", "error"}
        for item in (method_pack, method_memory)
    )
    longform_method = {
        "status": "failed" if method_failed else "ok",
        "mode": (
            "bundled_plus_project_memory"
            if method_memory.get("status") == "ok"
            else "bundled_pack_only"
        ),
        "method_pack": method_pack,
        "project_memory": method_memory,
        "read_only_snapshot": True,
    }

    longform_projection = _diagnostic_sqlite_component(
        _projection_database_path(root),
        expected_tables=(
            "longform_projection_meta",
            "projection_runs",
            "projection_outputs",
        ),
        allowed_tables=tuple(sorted(PROJECTION_TABLES)),
        meta_queries={
            "schema_version": ("longform_projection_meta", "schema_version")
        },
        scalar_queries={
            "runs": "SELECT COUNT(*) FROM projection_runs",
            "running_runs": (
                "SELECT COUNT(*) FROM projection_runs WHERE status='running'"
            ),
            "degraded_runs": (
                "SELECT COUNT(*) FROM projection_runs WHERE status='degraded'"
            ),
            "failed_runs": (
                "SELECT COUNT(*) FROM projection_runs WHERE status='failed'"
            ),
            "unresolved_degraded_runs": (
                "SELECT COUNT(*) FROM projection_runs AS run "
                "WHERE run.status='degraded' "
                "AND run.output_sha256 IS NULL AND NOT EXISTS ("
                "SELECT 1 FROM projection_outputs AS output "
                "WHERE output.projection_name=run.projection_name "
                "AND output.commit_id=run.commit_id "
                "AND output.input_sha256=run.input_sha256)"
            ),
            "unresolved_failed_runs": (
                "SELECT COUNT(*) FROM projection_runs AS run "
                "WHERE run.status='failed' AND NOT EXISTS ("
                "SELECT 1 FROM projection_outputs AS output "
                "WHERE output.projection_name=run.projection_name "
                "AND output.commit_id=run.commit_id "
                "AND output.input_sha256=run.input_sha256)"
            ),
            "outputs": "SELECT COUNT(*) FROM projection_outputs",
        },
    )
    _validate_component_schema(
        longform_projection,
        meta_key="schema_version",
        supported=PROJECTION_SCHEMA_VERSION,
        label="long-form projection",
    )
    if longform_projection.get("status") == "ok":
        projection_counts = dict(longform_projection.get("counts") or {})
        running_runs = int(projection_counts.get("running_runs") or 0)
        degraded_runs = int(projection_counts.get("degraded_runs") or 0)
        failed_runs = int(projection_counts.get("failed_runs") or 0)
        unresolved_degraded_runs = int(
            projection_counts.get("unresolved_degraded_runs") or 0
        )
        unresolved_failed_runs = int(
            projection_counts.get("unresolved_failed_runs") or 0
        )
        if (
            running_runs
            or unresolved_degraded_runs
            or unresolved_failed_runs
        ):
            longform_projection["status"] = "degraded"
            longform_projection["reason"] = (
                "long-form projection journal has unfinished runs: "
                f"running={running_runs}, "
                f"degraded={degraded_runs} "
                f"(unresolved={unresolved_degraded_runs}), "
                f"failed={failed_runs} "
                f"(unresolved={unresolved_failed_runs})"
            )

    config_check = {
        "status": "ok",
        "path": str((root / ".plot-rag" / "config.json").resolve()),
        "config_schema_version": config["config_schema_version"],
        "state_schema_version": config["state_schema_version"],
        "authority_index_schema_version": config[
            "authority_index_schema_version"
        ],
        "strict_lifecycle": bool(config["lifecycle"]["strict"]),
        "initialization_proposal_only": bool(
            config["initialization"]["proposal_only"]
        ),
        "read_only_snapshot": True,
    }
    bootstrap = _diagnostic_bootstrap_readiness(
        root,
        continuity,
        source_manifest,
    )
    components = {
        "config": config_check,
        "state": state,
        "continuity": continuity,
        "source_manifest": source_manifest,
        "authority_index": authority,
        "initialization_store": initialization,
        "longform_memory": longform_memory,
        "longform_summary": longform_summary,
        "longform_method": longform_method,
        "longform_projection": longform_projection,
        "bootstrap_readiness": bootstrap,
        "craft_catalog": _legacy_doctor_check(
            legacy,
            "craft_catalog",
            fallback_status="failed",
        ),
        "snapshot": _legacy_doctor_check(legacy, "snapshot"),
        "remote": _legacy_doctor_check(
            legacy,
            "remote",
            fallback_status="degraded",
        ),
    }
    checks = [
        {"name": name, **component}
        for name, component in components.items()
    ]
    failed = [
        item["name"] for item in checks if item.get("status") in {"failed", "error"}
    ]
    degraded = [
        item["name"] for item in checks if item.get("status") == "degraded"
    ]
    missing = [
        item["name"]
        for item in checks
        if item.get("status") in {"not_created", "not_ready"}
    ]
    status = (
        "failed"
        if failed
        else "degraded"
        if degraded or missing
        else "ready"
    )
    if legacy.get("status") == "degraded" and status == "ready":
        status = "degraded"
    return {
        "status": status,
        "runtime_version": RUNTIME_VERSION,
        "project_root": str(root),
        "config_version": config["config_version"],
        "strict_lifecycle": bool(config["lifecycle"]["strict"]),
        "bootstrap_ready": bool(bootstrap.get("ready")),
        "failed_checks": failed,
        "degraded_checks": degraded,
        "not_created_checks": missing,
        "components": components,
        "checks": checks,
        "legacy": legacy,
        "remote": legacy.get("remote") or {},
        "craft": legacy.get("craft") or {},
        "zero_write": True,
        "read_only_snapshot": True,
    }


def _config_migration_payload(
    root: Path,
    raw: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = load_config(root)
    payload = json.loads(json.dumps(dict(raw), ensure_ascii=False))
    payload.pop("version", None)
    payload["config_version"] = 3
    raw_authority_sources = payload.get("authority_sources")
    if raw_authority_sources is None:
        payload["authority_sources"] = normalized["authority_sources"]
    else:
        upgraded_authority_sources: list[dict[str, Any]] = []
        for source, normalized_source in zip(
            raw_authority_sources,
            normalized["authority_sources"],
            strict=True,
        ):
            upgraded = dict(source)
            for key in (
                "glob",
                "role",
                "scope_policy",
                "ingest_policy",
                "priority",
            ):
                upgraded.setdefault(key, normalized_source[key])
            upgraded_authority_sources.append(upgraded)
        payload["authority_sources"] = upgraded_authority_sources
    payload.pop("authority_globs", None)

    state = dict(payload.get("state") or {})
    state_defaults = {
        "enabled": True,
        "db_path": ".plot-rag/state.sqlite3",
        "snapshot_path": ".plot-rag/state_snapshot.json",
        "commit_dir": ".plot-rag/commits",
        "auto_retrieve": True,
        "auto_record": True,
        "fail_closed": False,
        "top_k": 12,
        "max_context_chars": 12000,
        "min_confidence": 0.72,
    }
    for key, value in state_defaults.items():
        state.setdefault(key, value)
    state["categories"] = list(
        dict.fromkeys(
            [
                *normalized["state"]["categories"],
                *STATE_CATEGORIES,
            ]
        )
    )
    payload["state"] = state

    power_system = dict(payload.get("power_system") or {})
    power_system_defaults = {
        "mode": "auto",
        "schema_version": "plot-rag-power/v1",
        "strict_progression": True,
        "comparison_mode": "conditional",
        "unknown_policy": "quarantine",
        "profiles": [],
    }
    for key, value in power_system_defaults.items():
        power_system.setdefault(key, value)
    payload["power_system"] = power_system

    grill = dict(payload.get("grill") or {})
    grill_defaults = {
        "enabled": True,
        "schema_version": "plot-rag-intent/v1",
        "database_path": ".plot-rag/grill.sqlite3",
        "one_question_per_turn": True,
        "recommend_answer": True,
        "explore_project_first": True,
        "max_questions": 6,
        "session_ttl_seconds": 21600,
        "required_fields": [
            "problem_to_solve",
            "expected_deliverable",
            "reader_experience",
            "protagonist_drive_conflict",
            "scope_endpoint",
            "success_criteria",
            "hard_constraints",
            "model_autonomy",
        ],
        "skip_phrases": [
            "跳过 Grill",
            "跳过盘问",
            "跳过目的确认",
            "按现有要求直接执行",
            "直接执行，不要追问",
        ],
        "cancel_phrases": [
            "取消本轮 Grill",
            "结束本轮盘问",
            "停止本轮盘问",
            "放弃本轮任务",
        ],
    }
    for key, value in grill_defaults.items():
        grill.setdefault(key, value)
    payload["grill"] = grill

    lifecycle = dict(payload.get("lifecycle") or {})
    lifecycle_defaults = {
        "strict": True,
        "longform_context_chars": 7000,
        "index_embeddings_on_prepare": False,
        "approval_ttl_seconds": 300,
        "candidate_cache_limit": 5000,
        "projection_history_limit": 20,
    }
    for key, value in lifecycle_defaults.items():
        lifecycle.setdefault(key, value)
    lifecycle["strict"] = True
    payload["lifecycle"] = lifecycle

    initialization = dict(payload.get("initialization") or {})
    initialization_defaults = {
        "schema_version": "auto",
        "database_path": ".plot-rag/init.sqlite3",
        "proposal_only": True,
        "default_mode": "auto",
        "default_target_profile": "plot_ready",
        "default_interaction_profile": "balanced",
        "source_max_bytes": 16777216,
        "exclude_globs": [
            ".git/**",
            ".plot-rag/**",
            ".plot-rag-init/**",
            "__pycache__/**",
            "node_modules/**",
        ],
    }
    for key, value in initialization_defaults.items():
        initialization.setdefault(key, value)
    initialization["proposal_only"] = True
    payload["initialization"] = initialization

    advantage = dict(payload.get("advantage") or {})
    advantage_defaults = {
        "enabled": False,
        "shadow": True,
        "schema_version": "plot-rag-advantage/v1",
        "strict_runtime_validation": False,
        "readable_projection": True,
        "mandatory_context": True,
    }
    for key, value in advantage_defaults.items():
        advantage.setdefault(key, value)
    payload["advantage"] = advantage
    return payload


def migrate_project_config(
    project_root: Path | str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    config_path = root / ".plot-rag" / "config.json"
    source_bytes = config_path.read_bytes()
    raw = json.loads(source_bytes.decode("utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("project config root must be an object")
    from_version = raw.get("config_version", raw.get("version", 1))
    if type(from_version) is not int:
        raise ValueError(
            "config migration source version must be an exact JSON integer"
        )
    if from_version == 3:
        load_config(root)
        return {
            "status": "current",
            "component": "config",
            "from_version": 3,
            "to_version": 3,
            "changed": False,
            "dry_run": bool(dry_run),
            "path": str(config_path),
            "sha256": _sha256(source_bytes),
        }
    if from_version not in {1, 2}:
        raise ValueError(f"unsupported config migration source: {from_version}")
    payload = _config_migration_payload(root, raw)
    rendered = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")

    with tempfile.TemporaryDirectory(prefix="plot-rag-config-migrate-") as temp:
        validation_root = Path(temp)
        validation_config = validation_root / ".plot-rag" / "config.json"
        validation_config.parent.mkdir(parents=True, exist_ok=True)
        validation_config.write_bytes(rendered)
        load_config(validation_root)

    old_hash = _sha256(source_bytes)
    new_hash = _sha256(rendered)
    result: dict[str, Any] = {
        "status": "dry_run" if dry_run else "migrated",
        "component": "config",
        "from_version": from_version,
        "to_version": 3,
        "changed": old_hash != new_hash,
        "dry_run": bool(dry_run),
        "path": str(config_path),
        "old_sha256": old_hash,
        "new_sha256": new_hash,
        "authority_sources": payload["authority_sources"],
        "backup_path": None,
        "migration_record": None,
    }
    if dry_run:
        return result

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = (
        root
        / ".plot-rag"
        / "backups"
        / f"config.json.v{from_version}.{stamp}.{old_hash[:12]}.bak"
    )
    _atomic_bytes(backup, source_bytes)
    _atomic_bytes(config_path, rendered)
    record_path = (
        root
        / ".plot-rag"
        / "migrations"
        / f"{stamp}.config-v{from_version}-to-v3.json"
    )
    record = {
        **result,
        "backup_path": str(backup),
        "migration_record": str(record_path),
        "completed_at": _utc_now(),
        "rollback": {
            "component": "config",
            "target_path": str(config_path),
            "backup_path": str(backup),
            "expected_current_sha256": new_hash,
        },
    }
    _atomic_json(record_path, record)
    record["backup_path"] = str(backup)
    record["migration_record"] = str(record_path)
    return record


def migrate_state_schema(
    project_root: Path | str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    config = load_config(root)
    state_path = Path(config["state"]["db_path"])
    if not state_path.is_file():
        return {
            "status": "not_created",
            "component": "state",
            "changed": False,
            "dry_run": bool(dry_run),
            "path": str(state_path),
            "from_version": None,
            "to_version": CONTINUITY_SCHEMA_VERSION,
            "backup_path": None,
            "migration_record": None,
        }
    diagnostic = _diagnostic_sqlite_component(
        state_path,
        allowed_tables=tuple(sorted(STATE_DATABASE_TABLES)),
        meta_queries={
            "legacy_schema_version": ("state_meta", "schema_version"),
            "continuity_schema_version": (
                "state_meta",
                "continuity_schema_version",
            ),
        },
        scalar_queries={
            "user_tables": (
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ),
        },
    )
    unexpected_tables = list(diagnostic.get("unexpected_tables") or [])
    if unexpected_tables:
        raise ValueError(
            "STATE_DATABASE_UNOWNED: state database contains foreign "
            f"user tables: {unexpected_tables}"
        )
    if diagnostic["status"] == "failed":
        raise ValueError(
            "state database is not readable for migration: "
            + str(diagnostic.get("reason") or diagnostic.get("integrity"))
        )
    try:
        legacy_version = int(
            diagnostic["meta"].get("legacy_schema_version") or 0
        )
        continuity_version = int(
            diagnostic["meta"].get("continuity_schema_version") or 0
        )
    except (TypeError, ValueError) as exc:
        raise SchemaVersionError(
            "STATE_SCHEMA_UNREADABLE",
            "schema versions must be integers",
        ) from exc
    validate_schema_versions(
        user_tables_present=bool(
            int(diagnostic["counts"].get("user_tables") or 0)
        ),
        legacy_version=legacy_version,
        continuity_version=continuity_version,
    )
    if continuity_version == CONTINUITY_SCHEMA_VERSION:
        return {
            "status": "current",
            "component": "state",
            "changed": False,
            "dry_run": bool(dry_run),
            "path": str(state_path),
            "from_version": continuity_version,
            "legacy_schema_version": legacy_version,
            "to_version": CONTINUITY_SCHEMA_VERSION,
            "sha256": _sha256_file(state_path),
            "backup_path": None,
            "migration_record": None,
        }
    before_hash = _sha256_file(state_path)
    result: dict[str, Any] = {
        "status": "dry_run" if dry_run else "migrated",
        "component": "state",
        "changed": True,
        "dry_run": bool(dry_run),
        "path": str(state_path),
        "from_version": continuity_version,
        "legacy_schema_version": legacy_version,
        "to_version": CONTINUITY_SCHEMA_VERSION,
        "old_sha256": before_hash,
        "backup_path": None,
        "migration_record": None,
    }
    if dry_run:
        return result
    service = ContinuityService(root)
    status = service.schema_status()
    with service.store.read_connection() as connection:
        item_projection_metadata = read_item_projection_metadata(connection)
        advantage_projection_metadata = read_advantage_projection_metadata(
            connection
        )
    item_projection_hash = str(
        item_projection_metadata.get(ITEM_PROJECTION_META_HASH) or ""
    )
    advantage_projection_hash = str(
        advantage_projection_metadata.get(ADVANTAGE_META_HASH) or ""
    )
    after_hash = _sha256_file(state_path)
    backup = status.get("migration_backup")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    record_path = (
        root
        / ".plot-rag"
        / "migrations"
        / f"{stamp}.state-v{continuity_version}-to-v"
        f"{CONTINUITY_SCHEMA_VERSION}.json"
    )
    record = {
        **result,
        "new_sha256": after_hash,
        "backup_path": backup,
        "migration_record": str(record_path),
        "schema_status": status,
        "item_projection_hash": item_projection_hash,
        "advantage_projection_hash": advantage_projection_hash,
        "completed_at": _utc_now(),
        "rollback": {
            "component": "state",
            "target_path": str(state_path),
            "backup_path": backup,
            "expected_current_sha256": after_hash,
            "expected_item_projection_hash": item_projection_hash,
            "expected_advantage_projection_hash": advantage_projection_hash,
            "readable_projection_cleanup": [
                {
                    "projection": "item",
                    "action": "remove_derived_projection",
                    "paths": [
                        str(root / ".plot-rag" / "物品"),
                        str(root / ".plot-rag" / ".item-readable-backup"),
                        str(root / ".plot-rag" / ".item-readable-stage-*"),
                        str(root / ".plot-rag" / ".item-readable.lock"),
                    ],
                },
                {
                    "projection": "advantage",
                    "action": "remove_derived_projection",
                    "paths": [
                        str(root / ".plot-rag" / "金手指"),
                        str(root / ".plot-rag" / ".advantage-readable-backup"),
                        str(root / ".plot-rag" / ".advantage-readable-stage-*"),
                        str(root / ".plot-rag" / ".advantage-readable.lock"),
                    ],
                },
            ],
        },
    }
    _atomic_json(record_path, record)
    return record


def migrate_project(
    project_root: Path | str,
    *,
    component: str = "all",
    dry_run: bool = False,
) -> dict[str, Any]:
    selected = str(component or "all").strip().lower()
    if selected not in {"all", "config", "state"}:
        raise ValueError("migration component must be all, config, or state")
    root = Path(project_root).expanduser().resolve()
    results: list[dict[str, Any]] = []
    if selected in {"all", "config"}:
        results.append(migrate_project_config(root, dry_run=dry_run))
    if selected in {"all", "state"}:
        results.append(migrate_state_schema(root, dry_run=dry_run))
    failed = [
        item for item in results if item.get("status") in {"failed", "error"}
    ]
    return {
        "status": "failed" if failed else "dry_run" if dry_run else "completed",
        "component": selected,
        "dry_run": bool(dry_run),
        "project_root": str(root),
        "results": results,
    }


def recover_longform_projection(
    project_root: Path | str,
    run_id: str,
) -> dict[str, Any]:
    """Recover one abandoned long-form run and retry its vector projection.

    The journal owns the fail-closed owner liveness decision. This runtime
    surface only proceeds after the exact running row has become retryable.
    Repeating a completed recovery returns a cached result instead of creating
    another projection run.
    """

    root = Path(project_root).expanduser().resolve()
    requested_run_id = str(run_id or "").strip()
    if not requested_run_id:
        raise ValueError("long-form projection recovery requires run_id")

    journal = ProjectionJournal(
        _projection_database_path(root),
        auto_recover=False,
    )

    def find_run() -> dict[str, Any]:
        return journal.inspect_run(
            requested_run_id,
            include_payload=False,
        )

    source = find_run()
    recovered = journal.recover_interrupted_runs(
        run_ids=(requested_run_id,),
    )
    recovered_receipt = next(
        (
            item
            for item in recovered
            if str(item.get("run_id") or "") == requested_run_id
        ),
        None,
    )
    source = find_run()

    source_status = str(source.get("status") or "")
    if source_status == "running":
        raise ValueError(
            "projection run is still owned by a live or unverifiable "
            f"process: {requested_run_id}"
        )

    projection_name = str(source.get("projection_name") or "")
    completed_retry = journal.latest_succeeded_run(
        projection_name=projection_name,
        commit_id=str(source.get("commit_id") or ""),
        input_sha256=str(source.get("input_sha256") or ""),
        retry_of=requested_run_id,
    )
    if source_status == "succeeded" or completed_retry is not None:
        completed = completed_retry or source
        return {
            "status": "cached",
            "project_root": str(root),
            "requested_run_id": requested_run_id,
            "run_id": str(completed.get("run_id") or ""),
            "projection_name": str(
                completed.get("projection_name") or ""
            ),
            "commit_id": str(completed.get("commit_id") or ""),
            "attempt": int(completed.get("attempt") or 0),
            "retry_of": completed.get("retry_of"),
            "recovered_interrupted_run": recovered_receipt,
        }

    if projection_name != "vector":
        raise ValueError(
            "production recovery currently supports vector projection "
            f"runs only, not {projection_name or 'unknown'}"
        )
    if source_status not in {"failed", "degraded"}:
        raise ValueError(
            "only an abandoned, failed, or degraded projection run can "
            f"be recovered: {requested_run_id} is {source_status or 'unknown'}"
        )

    retry = journal.retry(
        requested_run_id,
        lambda payload: _project_longform_vectors(root, payload),
        wait_for_running=True,
    )
    retry_run_id = str(retry.get("run_id") or "")
    retry_row = (
        journal.inspect_run(
            retry_run_id,
            include_payload=False,
        )
        if retry_run_id
        else journal.latest_succeeded_run(
            projection_name=projection_name,
            commit_id=str(source.get("commit_id") or ""),
            input_sha256=str(source.get("input_sha256") or ""),
            retry_of=requested_run_id,
        )
        or {}
    )
    return {
        **retry,
        "project_root": str(root),
        "requested_run_id": requested_run_id,
        "run_id": retry_run_id or retry_row.get("run_id"),
        "attempt": int(
            retry_row.get("attempt")
            or int(source.get("attempt") or 0) + 1
        ),
        "retry_of": requested_run_id,
        "recovered_interrupted_run": recovered_receipt,
    }


def longform_status(project_root: Path | str) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    config = load_config(root)
    lifecycle = dict(config.get("lifecycle") or {})
    craft = dict(config.get("craft") or {})
    index_embeddings = bool(
        lifecycle.get("index_embeddings_on_prepare", False)
    )
    query_embeddings = bool(craft.get("use_embedding", True))
    use_rerank = bool(craft.get("use_rerank", True))
    prepare_index = _authority_index(
        root,
        with_embeddings=index_embeddings,
    )
    query_index = _authority_index(
        root,
        with_embeddings=query_embeddings,
        with_rerank=use_rerank,
    )
    prepare_schema = prepare_index.schema_info()
    query_schema = query_index.schema_info()
    memory = LayeredMemoryStore(_longform_database_path(root))
    summaries = AcceptedSummaryStore(_longform_database_path(root))
    patterns = ProjectPatternStore(_longform_database_path(root))
    journal = ProjectionJournal(_projection_database_path(root))
    projection_limit = min(
        500,
        max(
            5,
            int(lifecycle.get("projection_history_limit", 20)) * 5,
        ),
    )
    projection_runs, total_projection_runs = journal.run_snapshot(
        include_payload=False,
        limit=projection_limit,
        newest_first=True,
    )
    return {
        "status": "ready",
        "authority_index": prepare_schema,
        "index": {
            "prepare_refresh": {
                "embedding_generation_requested": index_embeddings,
                "schema": prepare_schema,
            },
            "query_policy": {
                "embedding_requested": query_embeddings,
                "embedding_enabled": bool(
                    query_schema.get("embedding_enabled")
                ),
                "embedding_model": query_schema.get("embedding_model"),
                "rerank_requested": use_rerank,
                "rerank_enabled": bool(query_schema.get("rerank_enabled")),
                "rerank_model": query_schema.get("rerank_model"),
            },
            "query_schema": query_schema,
        },
        "memory_counts": memory.counts(),
        "summary_counts": {
            "chapter": len(summaries.list("chapter")),
            "arc": len(summaries.list("arc")),
            "volume": len(summaries.list("volume")),
        },
        "project_pattern_count": patterns.count(),
        "projection_runs": projection_runs,
        "projection_run_summary": {
            "total_count": total_projection_runs,
            "returned_count": len(projection_runs),
            "limit": projection_limit,
            "truncated": total_projection_runs > len(projection_runs),
            "payloads_included": False,
            "order": "newest_first",
        },
    }


def run_longform_benchmark(
    manifest_path: Path | str | None = None,
) -> dict[str, Any]:
    path = (
        Path(manifest_path).expanduser().resolve()
        if manifest_path is not None
        else Path(__file__).resolve().parents[1]
        / "benchmarks"
        / "fixtures"
        / "longform_annotations.v1.jsonl"
    )
    records = load_annotation_manifest(path)
    first = records[0] if records else {}
    suite = str(first.get("suite") or "").strip()
    is_power_suite = suite == "plot-rag-power" or {
        "profile",
        "case_kind",
        "stop_envelope",
    }.issubset(first)
    if is_power_suite:
        validation = validate_power_annotation_manifest(path)
        result = run_power_annotation_benchmark(path)
        quality_gate = dict(result.get("quality_gate") or {})
        passed = bool(quality_gate.get("passed"))
        return {
            "status": "passed" if passed else "failed",
            "suite": "plot-rag-power",
            "manifest": str(path),
            "validation": validation,
            "quality_gate": quality_gate,
            "result": result,
        }
    validation = validate_annotation_manifest(path)
    result = run_annotation_benchmark(path)
    critical_recall = min(
        float((result["category_metrics"].get(category) or {}).get("recall", 0.0))
        for category in ("location", "inventory", "story_time", "relation")
    )
    passed = (
        result["accepted_delta_precision"] >= 0.99
        and result["accepted_delta_recall"] >= 0.95
        and critical_recall >= 0.98
        and result["zero_delta_accuracy"] >= 0.98
        and result["quarantine_recall"] == 1.0
    )
    return {
        "status": "passed" if passed else "failed",
        "suite": "plot-rag-longform",
        "manifest": str(path),
        "validation": validation,
        "quality_gate": {
            "accepted_delta_precision_min": 0.99,
            "accepted_delta_recall_min": 0.95,
            "critical_category_recall_min": 0.98,
            "zero_delta_accuracy_min": 0.98,
            "quarantine_recall_required": 1.0,
            "observed_critical_category_recall": critical_recall,
            "passed": passed,
        },
        "result": result,
    }


__all__ = [
    "RUNTIME_VERSION",
    "accept_plot_proposal",
    "apply_initialization_proposal",
    "build_longform_context",
    "compare_power_conditions",
    "doctor_v1",
    "explain_power_action",
    "infer_artifact_context",
    "init_service",
    "is_strict_lifecycle",
    "issue_host_approval",
    "legacy_deltas_to_events",
    "list_power_systems",
    "longform_status",
    "migrate_project",
    "migrate_project_config",
    "migrate_state_schema",
    "prepare_plot_turn",
    "prepare_initialization_apply",
    "preview_power_spec_change",
    "propose_plot_turn",
    "propose_power_spec_change",
    "query_power_state",
    "query_progression_path",
    "query_continuity",
    "query_continuity_text",
    "recover_longform_projection",
    "refresh_longform_index",
    "register_initialization_proposal",
    "reject_plot_proposal",
    "replay_continuity",
    "resolve_canon_proposal_id",
    "retract_plot_proposal",
    "run_longform_benchmark",
    "preview_source_manifest_change",
    "propose_source_manifest_change",
    "source_manifest_status",
    "validate_power_spec_change",
    "verify_initialization",
]
