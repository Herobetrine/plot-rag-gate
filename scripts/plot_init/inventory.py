"""Read-only source inventory, classification, and deterministic claim extraction."""

from __future__ import annotations

import copy
import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator

from .canonical import (
    canonical_hash,
    canonical_json,
    normalize_real_path,
    path_is_within,
    sha256_bytes,
    stable_id,
)
from .constants import (
    DEFAULT_EXCLUDED_PARTS,
    GENERATED_FILE_NAMES,
    MAX_SOURCE_BYTES,
    TEXT_EXTENSIONS,
)
from .errors import PlotInitError
from .normalized import NORMALIZED_EXPORT_FORMAT, parse_normalized_export
from .remote_model import (
    LOW_CONFIDENCE_THRESHOLD,
    resolve_claim_review,
    resolve_classification_review,
)

if TYPE_CHECKING:
    from .remote_cache import RemoteResponseCache


_CONFIRMED_MARKERS = (
    "状态：已发布",
    "状态: 已发布",
    "状态：已定稿",
    "状态: 已定稿",
    "作者确认",
    "[已发布]",
    "[已定稿]",
    "[正典]",
)
_ACCEPTED_PLAN_MARKERS = (
    "状态：已确认",
    "状态: 已确认",
    "状态：已接受",
    "状态: 已接受",
    "[已确认]",
    "[已接受]",
)
_TODO_MARKERS = ("todo", "待办", "待定", "灵感", "脑洞", "随手记")
_REFERENCE_MARKERS = ("参考", "资料", "研究", "外部", "摘录", "竞品", "拆书")
_OUTLINE_MARKERS = ("大纲", "卷纲", "章纲", "剧情规划", "计划")
_DRAFT_MARKERS = ("草稿", "draft", "working", "未定稿")
_SETTING_MARKERS = ("设定", "世界观", "角色表", "人物表", "力量体系", "势力")
_CANON_MARKERS = ("正文", "章节", "chapter", "published", "final")
_REFERENCE_STATUS_MARKERS = (
    "状态：参考",
    "状态: 参考",
    "状态：资料",
    "状态: 资料",
    "[参考]",
    "[资料]",
)
_TODO_STATUS_MARKERS = (
    "状态：待定",
    "状态: 待定",
    "状态：灵感",
    "状态: 灵感",
    "状态：脑洞",
    "状态: 脑洞",
    "[待定]",
    "[灵感]",
    "[脑洞]",
)
_DRAFT_STATUS_MARKERS = (
    "状态：草稿",
    "状态: 草稿",
    "状态：未定稿",
    "状态: 未定稿",
    "[草稿]",
    "[未定稿]",
)

_KEY_PREDICATES = {
    "题材": "genre.primary_engine",
    "主类型": "genre.primary_engine",
    "类型": "genre.primary_engine",
    "读者承诺": "genre.reading_promise",
    "阅读承诺": "genre.reading_promise",
    "调性": "genre.tone",
    "差异化": "genre.differentiator",
    "主角": "actor.protagonist",
    "对手": "actor.opponent",
    "反派": "actor.opponent",
    "第三方": "actor.third_party",
    "当前位置": "actor.location",
    "位置": "actor.location",
    "目标": "actor.goal",
    "外在目标": "actor.goal",
    "长期欲望": "actor.desire",
    "核心规则": "world.rule",
    "世界规则": "world.rule",
    "稀缺资源": "world.scarce_resource",
    "生存资源": "world.survival_resource",
    "当前压力": "world.pressure",
    "力量体系": "power.profile",
    "力量类型": "power.profile",
    "体系名称": "power.system",
    "力量体系名称": "power.system",
    "成长轨": "progression.track",
    "成长轨定义": "progression.track",
    "境界节点": "rank.node",
    "阶段节点": "rank.node",
    "晋升边": "rank.edge",
    "境界": "progression.state",
    "当前境界": "progression.state",
    "等级": "progression.state",
    "当前等级": "progression.state",
    "能力": "ability.owns",
    "技能": "ability.owns",
    "法术": "ability.owns",
    "功法": "ability.owns",
    "能力来源": "ability.source",
    "能力代价": "ability.cost",
    "能力成本": "ability.cost",
    "能力限制": "ability.limit",
    "使用条件": "ability.condition",
    "冷却": "ability.cooldown",
    "反制": "ability.counter",
    "资源池": "resource.definition",
    "资源定义": "resource.definition",
    "当前资源": "resource.state",
    "恢复规则": "resource.recovery",
    "状态定义": "status.definition",
    "当前状态": "status.state",
    "资格定义": "qualification.definition",
    "当前资格": "qualification.state",
    "力量绑定": "binding.state",
    "来源绑定": "binding.state",
    "克制规则": "counter.rule",
    "桥接规则": "bridge.rule",
    "转换规则": "conversion.rule",
    "已观察能力": "observation.capability",
    "物品定义": "item.definition",
    "道具定义": "item.definition",
    "物品实例": "item.instance",
    "道具实例": "item.instance",
    "物品堆": "item.stack",
    "物品批次": "item.stack",
    "物品功能": "item.function",
    "道具功能": "item.function",
    "物品用途": "item.function",
    "物品保管": "item.custody",
    "物品归属": "item.custody",
    "物品运行态": "item.runtime",
    "物品功能运行态": "item.function_runtime",
    "物品观察": "item.observation",
    "物品效果观察": "item.observation",
    "持有物品": "inventory.holds",
    "持有道具": "inventory.holds",
    "晋升条件": "progression.prerequisite",
    "突破失败": "progression.failure_outcome",
    "故事时间": "timeline.anchor",
    "历法": "timeline.calendar",
    "触发事件": "story.inciting_event",
    "失败代价": "story.failure_cost",
    "第一条事件链": "story.first_event_chain",
    "第一卷变化": "story.volume_one_change",
    "终局问题": "story.endgame_question",
    "章级反馈": "serialization.chapter_feedback_loop",
    "兑现窗口": "serialization.promise_window",
    "卷级循环": "serialization.volume_loop",
}

_ENTITY_PREFIXES = {
    "actor": "character",
    "location": "location",
    "item": "item",
    "faction": "faction",
    "ability": "ability",
    "power": "power_system",
    "progression": "progression_track",
    "rank": "rank_node",
    "resource": "resource_pool",
    "status": "status_effect",
    "binding": "concept",
    "qualification": "qualification",
    "conversion": "conversion_rule",
    "observation": "concept",
    "counter": "counter_rule",
    "bridge": "bridge_rule",
    "world": "concept",
    "story": "plot",
    "timeline": "time",
    "genre": "contract",
    "serialization": "contract",
}


def _is_junction(path: Path) -> bool:
    checker = getattr(os.path, "isjunction", None)
    if callable(checker):
        try:
            return bool(checker(path))
        except OSError:
            return False
    try:
        stat = path.lstat()
    except OSError:
        return False
    file_attributes = int(getattr(stat, "st_file_attributes", 0))
    reparse_point = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(file_attributes & reparse_point) and not path.is_symlink()


def _excluded_reason(path: Path, root: Path) -> str | None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return "outside_source_root"
    lowered = [part.casefold() for part in relative.parts]
    if ".plot-rag" in lowered:
        return "generated_plot_rag_storage"
    if any(part in DEFAULT_EXCLUDED_PARTS for part in lowered):
        return "excluded_directory"
    if path.name.casefold() in GENERATED_FILE_NAMES:
        return "generated_runtime_file"
    suffix = path.suffix.casefold()
    if suffix in {".sqlite3", ".sqlite", ".db", ".wal", ".shm", ".pyc"}:
        return "generated_or_binary_storage"
    return None


def _walk_source_root(root: Path) -> tuple[list[Path], list[dict[str, Any]]]:
    files: list[Path] = []
    issues: list[dict[str, Any]] = []
    if not root.exists():
        raise PlotInitError("SOURCE_NOT_FOUND", f"source path does not exist: {root}")
    if root.is_symlink() or _is_junction(root):
        raise PlotInitError(
            "UNSAFE_SOURCE_ROOT",
            f"source root cannot be a symlink or junction: {root}",
        )
    real_root = root.resolve(strict=True)
    if any(part.casefold() == ".plot-rag" for part in real_root.parts):
        return [], [
            {
                "path": str(real_root),
                "kind": "file" if real_root.is_file() else "directory",
                "reason": "generated_plot_rag_storage",
            }
        ]
    if real_root.is_file():
        return [real_root], issues

    for current, directory_names, file_names in os.walk(real_root, followlinks=False):
        current_path = Path(current)
        safe_directories: list[str] = []
        for name in sorted(directory_names, key=str.casefold):
            candidate = current_path / name
            reason = _excluded_reason(candidate, real_root)
            if reason:
                issues.append(
                    {
                        "path": str(candidate),
                        "kind": "directory",
                        "reason": reason,
                    }
                )
                continue
            if candidate.is_symlink() or _is_junction(candidate):
                issues.append(
                    {
                        "path": str(candidate),
                        "kind": "directory",
                        "reason": "symlink_or_junction_excluded",
                    }
                )
                continue
            resolved = candidate.resolve(strict=False)
            if not path_is_within(resolved, real_root):
                issues.append(
                    {
                        "path": str(candidate),
                        "kind": "directory",
                        "reason": "outside_source_root",
                    }
                )
                continue
            safe_directories.append(name)
        directory_names[:] = safe_directories

        for name in sorted(file_names, key=str.casefold):
            candidate = current_path / name
            reason = _excluded_reason(candidate, real_root)
            if reason:
                issues.append(
                    {"path": str(candidate), "kind": "file", "reason": reason}
                )
                continue
            if candidate.is_symlink():
                issues.append(
                    {
                        "path": str(candidate),
                        "kind": "file",
                        "reason": "symlink_excluded",
                    }
                )
                continue
            resolved = candidate.resolve(strict=False)
            if not path_is_within(resolved, real_root):
                issues.append(
                    {
                        "path": str(candidate),
                        "kind": "file",
                        "reason": "outside_source_root",
                    }
                )
                continue
            files.append(resolved)
    return files, issues


def _decode_text(raw: bytes) -> tuple[str | None, str, str | None]:
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig"), "utf-8-bom", None
        except UnicodeDecodeError as exc:
            return None, "unknown", str(exc)
    if b"\x00" in raw[:8192]:
        return None, "binary", "NUL byte detected"
    for encoding, label in (
        ("utf-8", "utf-8"),
        ("gbk", "gbk"),
        ("gb18030", "gb18030"),
    ):
        try:
            return raw.decode(encoding), label, None
        except UnicodeDecodeError:
            continue
    return None, "unknown", "unsupported text encoding"


def _classify_source(
    path: Path,
    text: str,
    *,
    source_hash: str | None = None,
    remote_cache: "RemoteResponseCache | None" = None,
) -> dict[str, Any]:
    path_text = path.as_posix().casefold()
    header_text = text[:4096].casefold()
    combined = f"{path_text}\n{header_text}"
    confirmed = any(
        marker.casefold() in header_text for marker in _CONFIRMED_MARKERS
    )
    accepted_plan = any(
        marker.casefold() in header_text for marker in _ACCEPTED_PLAN_MARKERS
    )
    path_reference = any(
        marker.casefold() in path_text for marker in _REFERENCE_MARKERS
    )
    path_todo = any(marker.casefold() in path_text for marker in _TODO_MARKERS)
    path_outline = any(
        marker.casefold() in path_text for marker in _OUTLINE_MARKERS
    )
    path_draft = any(
        marker.casefold() in path_text for marker in _DRAFT_MARKERS
    )
    path_setting = any(
        marker.casefold() in path_text for marker in _SETTING_MARKERS
    )
    path_canon = any(marker.casefold() in path_text for marker in _CANON_MARKERS)
    explicit_reference = any(
        marker.casefold() in header_text for marker in _REFERENCE_STATUS_MARKERS
    )
    explicit_todo = any(
        marker.casefold() in header_text for marker in _TODO_STATUS_MARKERS
    )
    explicit_draft = any(
        marker.casefold() in header_text for marker in _DRAFT_STATUS_MARKERS
    )

    if NORMALIZED_EXPORT_FORMAT.casefold() in combined:
        role = "setting"
        tier = "T3"
        stage = "normalized"
        scope_policy = "preserve_unknown"
        ingest_policy = "include"
        priority = 110
        confidence = 1.0
    elif confirmed:
        if path_outline:
            role = "outline"
            tier = "T2"
            stage = "outline"
            scope_policy = "planned_only"
            ingest_policy = "include"
            priority = 80
        elif path_setting:
            role = "setting"
            tier = "T1"
            stage = "final"
            scope_policy = "timeless_candidate"
            ingest_policy = "include"
            priority = 95
        else:
            # An explicit final/published marker outranks ordinary prose words
            # such as “参考”“计划”“待定”.  Those words often occur inside
            # published chapters and must not silently demote the whole file.
            role = "canon"
            tier = "T1"
            stage = "published"
            scope_policy = "infer_and_review"
            ingest_policy = "include"
            priority = 100 if path_canon else 92
        confidence = 0.98
    elif accepted_plan:
        role = "outline"
        tier = "T2"
        stage = "outline"
        scope_policy = "planned_only"
        ingest_policy = "include"
        priority = 75
        confidence = 0.95
    elif explicit_reference or path_reference:
        role = "reference"
        tier = "T5"
        stage = "reference"
        scope_policy = "preserve_unknown"
        ingest_policy = "exclude"
        priority = 10
        confidence = 0.92
    elif explicit_todo or path_todo:
        role = "note"
        tier = "T4"
        stage = "brainstorm"
        scope_policy = "planned_only"
        ingest_policy = "review"
        priority = 20
        confidence = 0.9
    elif path_outline:
        role = "outline"
        tier = "T3"
        stage = "outline"
        scope_policy = "planned_only"
        ingest_policy = "review"
        priority = 55
        confidence = 0.75
    elif explicit_draft or path_draft:
        role = "draft"
        tier = "T3"
        stage = "draft"
        scope_policy = "infer_and_review"
        ingest_policy = "review"
        priority = 45
        confidence = 0.85
    elif path_setting:
        role = "setting"
        tier = "T3"
        stage = "draft"
        scope_policy = "timeless_candidate"
        ingest_policy = "review"
        priority = 60
        confidence = 0.78
    elif path_canon:
        role = "canon"
        tier = "T3"
        stage = "draft"
        scope_policy = "infer_and_review"
        ingest_policy = "review"
        priority = 65
        confidence = 0.72
    else:
        role = "note"
        tier = "T4"
        stage = "brainstorm"
        scope_policy = "preserve_unknown"
        ingest_policy = "review"
        priority = 25
        confidence = 0.45

    result = {
        "source_role": role,
        "authority_tier": tier,
        "artifact_stage": stage,
        "scope_policy": scope_policy,
        "ingest_policy": ingest_policy,
        "branch_id": "main",
        "chapter_hint": _chapter_hint(path),
        "priority": priority,
        "classification_confidence": confidence,
        "classification_basis": (
            "validated_normalized_export"
            if NORMALIZED_EXPORT_FORMAT.casefold() in combined
            else
            "explicit_status_and_path"
            if confirmed or accepted_plan or explicit_reference or explicit_todo or explicit_draft
            else "path_candidate_only"
        ),
    }
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        review = resolve_classification_review(
            path=path.as_posix(),
            source_text=text,
            source_hash=source_hash or sha256_bytes(text.encode("utf-8")),
            local_classification=result,
            remote_cache=remote_cache,
        )
        result["remote_classification_review"] = review["diagnostics"]
        proposal = review.get("proposal")
        if isinstance(proposal, dict):
            # A remote classification can only adjust the candidate role.
            # Every authority-bearing dimension remains locally forced to the
            # proposal-only T4/review boundary.
            result.update(
                {
                    "source_role": str(proposal["source_role"]),
                    "authority_tier": "T4",
                    "artifact_stage": "brainstorm",
                    "scope_policy": "preserve_unknown",
                    "ingest_policy": "review",
                    "priority": min(int(result["priority"]), 40),
                    "classification_basis": "remote_ambiguity_proposal",
                }
            )
    return result


def _chapter_hint(path: Path) -> int | None:
    match = re.search(r"(?:第\s*)?(\d{1,7})\s*(?:章|chapter)?", path.stem, re.I)
    return int(match.group(1)) if match else None


def _source_display_path(path: Path, root: Path, root_index: int) -> str:
    relative = path.relative_to(root).as_posix()
    if relative == ".":
        relative = path.name
    return f"source-{root_index + 1}/{relative}"


def inventory_sources(
    source_paths: Iterable[Path | str],
    *,
    previous_manifest: list[dict[str, Any]] | None = None,
    max_file_bytes: int = MAX_SOURCE_BYTES,
    remote_cache: "RemoteResponseCache | None" = None,
) -> dict[str, Any]:
    """Scan explicitly selected roots without following external links or writing files."""

    roots: list[Path] = []
    for raw in source_paths:
        path = Path(raw).expanduser().resolve(strict=False)
        if path not in roots:
            roots.append(path)
    if not roots:
        return {
            "source_roots": [],
            "documents": [],
            "source_manifest": [],
            "issues": [],
            "duplicates": [],
            "snapshot_hash": canonical_hash([]),
            "source_diff": {
                "added": [],
                "changed": [],
                "removed": [],
                "unchanged": [],
            },
        }

    previous_by_id = {
        str(item.get("source_id")): item for item in (previous_manifest or [])
    }
    documents: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen_real_paths: set[str] = set()

    for root_index, root in enumerate(roots):
        candidates, root_issues = _walk_source_root(root)
        issues.extend(root_issues)
        boundary = root if root.is_dir() else root.parent
        for path in candidates:
            normalized_real = normalize_real_path(path)
            if normalized_real in seen_real_paths:
                continue
            seen_real_paths.add(normalized_real)
            stat = path.stat()
            raw = path.read_bytes()
            content_hash = sha256_bytes(raw)
            source_id = stable_id("src", normalized_real)
            source_version_id = stable_id("srcv", source_id, content_hash)
            previous = previous_by_id.get(source_id)
            previous_version = (
                str(previous.get("source_version_id")) if previous else ""
            )
            previous_head = int(previous.get("head_revision") or 0) if previous else 0
            head_revision = (
                previous_head
                if previous_version == source_version_id and previous_head > 0
                else previous_head + 1
            )

            descriptor: dict[str, Any] = {
                "source_id": source_id,
                "source_version_id": source_version_id,
                "head_revision": head_revision,
                "active_revision": (
                    previous.get("active_revision") if previous else None
                ),
                "path": _source_display_path(path, boundary, root_index),
                "real_path": str(path),
                "normalized_real_path": normalized_real,
                "content_hash": content_hash,
                "size": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
                "format": path.suffix.casefold().lstrip(".") or "text",
                "encoding": "binary",
                "parse_status": "excluded",
                "parse_error": None,
            }
            if len(raw) > max_file_bytes:
                descriptor["parse_error"] = "source exceeds configured size limit"
                descriptor["exclude_reason"] = "too_large"
                documents.append(descriptor)
                continue
            if path.suffix.casefold() not in TEXT_EXTENSIONS:
                descriptor["parse_error"] = "unsupported source extension"
                descriptor["exclude_reason"] = "unsupported_extension"
                documents.append(descriptor)
                continue
            text, encoding, decode_error = _decode_text(raw)
            descriptor["encoding"] = encoding
            if text is None:
                descriptor["parse_error"] = decode_error
                descriptor["exclude_reason"] = "binary_or_unsupported_encoding"
                documents.append(descriptor)
                continue
            descriptor.update(
                _classify_source(
                    path,
                    text,
                    source_hash=content_hash,
                    remote_cache=remote_cache,
                )
            )
            descriptor["parse_status"] = "parsed"
            descriptor["_text"] = text
            descriptor["_line_count"] = len(text.splitlines())
            if path.suffix.casefold() == ".json":
                try:
                    descriptor["_json"] = json.loads(text)
                except json.JSONDecodeError as exc:
                    descriptor["parse_status"] = "parse_error"
                    descriptor["parse_error"] = (
                        f"JSON line {exc.lineno}, column {exc.colno}: {exc.msg}"
                    )
            documents.append(descriptor)

    by_hash: dict[str, list[str]] = defaultdict(list)
    for item in documents:
        if item.get("parse_status") == "parsed":
            by_hash[str(item["content_hash"])].append(str(item["source_id"]))
    duplicates = [
        {"content_hash": digest, "source_ids": sorted(source_ids)}
        for digest, source_ids in sorted(by_hash.items())
        if len(source_ids) > 1
    ]
    duplicate_lookup = {
        source_id: group["content_hash"]
        for group in duplicates
        for source_id in group["source_ids"]
    }
    for item in documents:
        item["duplicate_group_hash"] = duplicate_lookup.get(str(item["source_id"]))

    source_manifest = [_public_descriptor(item) for item in documents]
    current_by_id = {str(item["source_id"]): item for item in source_manifest}
    added = sorted(set(current_by_id) - set(previous_by_id))
    removed = sorted(set(previous_by_id) - set(current_by_id))
    changed: list[dict[str, Any]] = []
    unchanged: list[str] = []
    for source_id in sorted(set(current_by_id) & set(previous_by_id)):
        old = previous_by_id[source_id]
        new = current_by_id[source_id]
        if old.get("source_version_id") == new.get("source_version_id"):
            unchanged.append(source_id)
        else:
            changed.append(
                {
                    "source_id": source_id,
                    "old_source_version_id": old.get("source_version_id"),
                    "new_source_version_id": new.get("source_version_id"),
                    "old_content_hash": old.get("content_hash"),
                    "new_content_hash": new.get("content_hash"),
                }
            )

    snapshot_material = [
        {
            "source_id": item["source_id"],
            "source_version_id": item["source_version_id"],
            "parse_status": item["parse_status"],
            "ingest_policy": item.get("ingest_policy"),
        }
        for item in sorted(source_manifest, key=lambda value: str(value["source_id"]))
    ]
    return {
        "source_roots": [str(root) for root in roots],
        "documents": documents,
        "source_manifest": source_manifest,
        "issues": issues,
        "duplicates": duplicates,
        "snapshot_hash": canonical_hash(snapshot_material),
        "source_diff": {
            "added": added,
            "changed": changed,
            "removed": removed,
            "unchanged": unchanged,
        },
    }


def _public_descriptor(document: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in document.items()
        if not str(key).startswith("_")
    }


def _line_scope(line: str, descriptor: dict[str, Any], predicate: str) -> str | None:
    if predicate == "world.rule" or predicate.startswith(
        (
            "power.",
            "ability.source",
            "ability.cost",
            "ability.limit",
            "ability.counter",
            "progression.track",
            "rank.",
            "resource.definition",
            "status.definition",
            "qualification.definition",
            "counter.",
            "bridge.",
            "conversion.",
        )
    ):
        return "timeless"
    if any(token in line for token in ("曾经", "过去", "当年", "年前", "历史上")):
        return "historical"
    if any(token in line for token in ("将会", "计划", "下一章", "未来", "准备")):
        return "planned"
    if descriptor.get("scope_policy") == "planned_only":
        return "planned"
    if any(token in line for token in ("当前", "现在", "如今", "此刻", "已经")):
        return "current"
    if descriptor.get("scope_policy") == "timeless_candidate":
        return "timeless" if predicate.startswith("world.") else None
    return None


def _knowledge_plane(line: str, descriptor: dict[str, Any]) -> str:
    if re.search(r"[\w\u4e00-\u9fff]{1,16}(?:认为|相信|误以为|怀疑)", line):
        return "actor_belief"
    if any(token in line for token in ("传说", "众所周知", "公开说法", "民间认为")):
        return "public_narrative"
    if any(token in line for token in ("读者得知", "揭示给读者", "读者已知")):
        return "reader_disclosed"
    if descriptor.get("source_role") in {"outline", "note"}:
        return "author_plan"
    return "objective"


def _modality(line: str) -> str:
    if any(token in line for token in ("如果", "假如", "倘若", "可能", "也许")):
        return "conditional"
    if any(token in line for token in ("方案", "备选", "假设")):
        return "hypothetical"
    return "asserted"


def _story_time(line: str) -> dict[str, Any] | None:
    patterns = (
        r"(?P<calendar>[\u4e00-\u9fffA-Za-z]{1,12})历(?P<year>[零〇一二三四五六七八九十百千万\d]+)年"
        r"(?P<month>[零〇一二三四五六七八九十\d]+)月"
        r"(?P<day>初?[零〇一二三四五六七八九十廿卅\d]+)",
        r"第(?P<day>\d+)天",
        r"(?P<year>\d{4})[-/年](?P<month>\d{1,2})[-/月](?P<day>\d{1,2})",
    )
    for pattern in patterns:
        match = re.search(pattern, line)
        if match:
            return {
                "raw": match.group(0),
                "calendar": match.groupdict().get("calendar"),
                "year": match.groupdict().get("year"),
                "month": match.groupdict().get("month"),
                "day": match.groupdict().get("day"),
                "normalized_order": None,
                "confidence": 0.8,
            }
    return None


def _claim(
    descriptor: dict[str, Any],
    *,
    subject: str,
    predicate: str,
    value: Any,
    evidence: str,
    line_start: int,
    line_end: int | None = None,
    support_type: str = "exact",
) -> dict[str, Any]:
    source_id = str(descriptor["source_id"])
    source_version_id = str(descriptor["source_version_id"])
    normalized_claim = {
        "subject": subject.strip(),
        "predicate": predicate,
        "value": value,
    }
    claim_id = stable_id(
        "claim",
        source_version_id,
        [line_start, line_end or line_start],
        normalized_claim,
    )
    line = evidence.strip()
    scope = _line_scope(line, descriptor, predicate)
    plane = _knowledge_plane(line, descriptor)
    modality = _modality(line)
    status = (
        "source_supported"
        if descriptor.get("authority_tier") in {"T0", "T1", "T2", "T3"}
        else "model_proposed"
    )
    return {
        "claim_id": claim_id,
        "source_id": source_id,
        "source_version_id": source_version_id,
        "subject": subject.strip() or "作品",
        "predicate": predicate,
        "object_or_value": value,
        "exact_evidence": line,
        "path": descriptor["path"],
        "line_start": line_start,
        "line_end": line_end or line_start,
        "source_hash": descriptor["content_hash"],
        "support_type": support_type,
        "source_role": descriptor.get("source_role", "note"),
        "authority_tier": descriptor.get("authority_tier", "T4"),
        "field_status": status,
        "canon_status": "proposed",
        "origin": "source_extract",
        "scope": scope,
        "knowledge_plane": plane,
        "modality": modality,
        "branch_id": descriptor.get("branch_id") or "main",
        "story_time": _story_time(line),
        "extraction_model": "local-deterministic-v1",
        "prompt_version": "none",
        "response_hash": canonical_hash(normalized_claim),
        "confidence": min(
            0.98,
            max(0.35, float(descriptor.get("classification_confidence") or 0.5)),
        ),
    }


def _json_claims(
    descriptor: dict[str, Any],
    payload: Any,
    *,
    path: tuple[str, ...] = (),
) -> Iterator[dict[str, Any]]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield from _json_claims(descriptor, value, path=(*path, str(key)))
        return
    if isinstance(payload, list):
        for index, value in enumerate(payload):
            yield from _json_claims(descriptor, value, path=(*path, str(index)))
        return
    if not path:
        return
    key = path[-1]
    predicate = _KEY_PREDICATES.get(key)
    if predicate is None:
        dotted = ".".join(path).casefold()
        if any(token in dotted for token in ("rule", "规则")):
            predicate = "world.rule"
        elif any(token in dotted for token in ("location", "位置", "地点")):
            predicate = "actor.location"
        elif any(token in dotted for token in ("time", "时间", "历法")):
            predicate = "timeline.anchor"
        elif any(token in dotted for token in ("goal", "目标")):
            predicate = "actor.goal"
        else:
            predicate = f"source.{'.'.join(path)}"
    subject = path[-2] if len(path) > 1 else "作品"
    text = str(descriptor.get("_text") or "")
    line_no = 1
    evidence = canonical_json({key: payload})
    for index, line in enumerate(text.splitlines(), start=1):
        if f'"{key}"' in line or key in line:
            line_no = index
            evidence = line.strip()
            break
    yield _claim(
        descriptor,
        subject=subject,
        predicate=predicate,
        value=payload,
        evidence=evidence,
        line_start=line_no,
    )


def _with_remote_claim_review(
    document: dict[str, Any],
    local_claims: list[dict[str, Any]],
    *,
    remote_cache: "RemoteResponseCache | None",
) -> list[dict[str, Any]]:
    confidence = float(document.get("classification_confidence") or 0.0)
    if local_claims and confidence >= LOW_CONFIDENCE_THRESHOLD:
        return _deduplicate_claims(local_claims)

    review = resolve_claim_review(
        path=str(document.get("path") or ""),
        source_text=str(document.get("_text") or ""),
        source_hash=str(document.get("content_hash") or ""),
        local_claim_count=len(local_claims),
        classification_confidence=confidence,
        remote_cache=remote_cache,
    )
    diagnostics = copy.deepcopy(review["diagnostics"])
    document["remote_claim_review"] = diagnostics
    proposal = review.get("proposal")
    if not isinstance(proposal, dict):
        return _deduplicate_claims(local_claims)

    model = str(diagnostics.get("model") or "remote-model")
    response_hash = str(review.get("response_hash") or "")
    remote_claims: list[dict[str, Any]] = []
    for candidate in proposal.get("claims") or []:
        if not isinstance(candidate, dict):
            continue
        claim = _claim(
            document,
            subject=str(candidate["subject"]),
            predicate=str(candidate["predicate"]),
            value=copy.deepcopy(candidate["object_or_value"]),
            evidence=str(candidate["exact_evidence"]),
            line_start=int(candidate["line_start"]),
            line_end=int(candidate["line_end"]),
            support_type="exact",
        )
        claim.update(
            {
                "authority_tier": "T4",
                "field_status": "model_proposed",
                "canon_status": "proposed",
                "origin": "remote_ambiguity_proposal",
                # The remote response never selects a current/timeless scope.
                # A later local review decision must supply that classification.
                "scope": None,
                "extraction_model": model,
                "prompt_version": "plot-rag-init-remote-review/v1",
                "response_hash": response_hash or claim["response_hash"],
                "confidence": min(
                    0.69,
                    max(0.35, float(candidate.get("confidence") or 0.0)),
                ),
                "remote_review": copy.deepcopy(diagnostics),
            }
        )
        remote_claims.append(claim)
    return _deduplicate_claims([*local_claims, *remote_claims])


def _structured_power_value(predicate: str, value: str) -> Any:
    """Parse the documented compact power notation without adding facts."""

    text = value.strip()
    track_kinds = {
        "线性": "ordered_rank",
        "有序": "ordered_rank",
        "ordered_rank": "ordered_rank",
        "数值": "numeric_level",
        "numeric_level": "numeric_level",
        "分支": "branch_tree",
        "branch_tree": "branch_tree",
        "dag": "dag",
        "状态机": "state_machine",
        "state_machine": "state_machine",
        "开放": "open_ended",
        "open_ended": "open_ended",
        "无等级": "none",
        "none": "none",
    }
    if predicate == "progression.track":
        match = re.fullmatch(
            r"(?P<name>[^|｜]+)[|｜](?P<kind>[^|｜]+)",
            text,
        )
        if match:
            kind = track_kinds.get(match.group("kind").strip().casefold())
            if kind:
                return {
                    "name": match.group("name").strip(),
                    "track_kind": kind,
                }
    if predicate == "progression.state":
        match = re.fullmatch(
            r"(?P<track>[^|｜]+)[|｜](?P<rank>[^|｜]+)",
            text,
        )
        if match:
            return {
                "track_name": match.group("track").strip(),
                "rank_name": match.group("rank").strip(),
            }
    if predicate == "rank.node":
        match = re.fullmatch(
            r"(?P<track>[^|｜]+)[|｜](?P<rank>[^|｜]+)",
            text,
        )
        if match:
            return {
                "track_name": match.group("track").strip(),
                "name": match.group("rank").strip(),
            }
    if predicate == "rank.edge":
        match = re.fullmatch(
            r"(?P<track>[^|｜]+)[|｜](?P<left>.+?)(?:->|→)(?P<right>.+)",
            text,
        )
        if match:
            return {
                "track_name": match.group("track").strip(),
                "from_node_ids": [match.group("left").strip()],
                "to_node_id": match.group("right").strip(),
            }
    if predicate in {
        "resource.state",
        "status.state",
        "qualification.state",
    }:
        match = re.fullmatch(
            r"(?P<name>[^=＝]+)[=＝](?P<quantity>-?\d+(?:\.\d+)?)",
            text,
        )
        if match:
            field = {
                "resource.state": "resource_name",
                "status.state": "status_name",
                "qualification.state": "qualification_name",
            }[predicate]
            amount = float(match.group("quantity"))
            return {
                field: match.group("name").strip(),
                (
                    "amount"
                    if predicate == "resource.state"
                    else "stacks"
                    if predicate == "status.state"
                    else "quantity"
                ): int(amount) if amount.is_integer() else amount,
            }
    if predicate == "binding.state":
        parts = [part.strip() for part in re.split(r"[|｜]", text)]
        if len(parts) >= 2 and all(parts[:2]):
            return {
                "source_type": parts[0],
                "source_name": parts[1],
                "ability_ids": [
                    part
                    for part in re.split(r"[、,，/]", parts[2])
                    if part.strip()
                ]
                if len(parts) >= 3
                else [],
            }
    if predicate == "conversion.rule":
        match = re.fullmatch(
            r"(?P<source>.+?)(?:->|→)(?P<target>[^=＝]+)"
            r"[=＝](?P<ratio>\d+(?:\.\d+)?)",
            text,
        )
        if match:
            return {
                "name": text,
                "source_resource": match.group("source").strip(),
                "target_resource": match.group("target").strip(),
                "ratio": float(match.group("ratio")),
            }
    if predicate == "observation.capability":
        parts = [part.strip() for part in re.split(r"[|｜]", text)]
        if len(parts) >= 2 and all(parts[:2]):
            result: dict[str, Any] = {
                "subject_entity_id": parts[0],
                "ability_name": parts[1],
                "observed_fields": (
                    [
                        item.strip()
                        for item in re.split(r"[、,，/]", parts[2])
                        if item.strip()
                    ]
                    if len(parts) >= 3
                    else []
                ),
            }
            if len(parts) >= 4:
                try:
                    result["confidence"] = float(parts[3])
                except ValueError:
                    pass
            return result
    return text


def _structured_item_value(predicate: str, value: str) -> Any:
    """Parse explicit compact item records without upgrading prose into rules."""

    text = value.strip()
    if text.startswith(("{", "[")):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        if decoded is not None:
            return decoded
    parts = [part.strip() for part in re.split(r"[|｜]", text)]
    if predicate == "item.definition" and parts and parts[0]:
        result: dict[str, Any] = {"name": parts[0]}
        if len(parts) >= 2 and parts[1]:
            result["item_kind"] = parts[1]
        if len(parts) >= 3 and parts[2]:
            result["stack_policy"] = parts[2]
        if len(parts) >= 4 and parts[3]:
            result["uniqueness_policy"] = parts[3]
        return result
    if predicate == "item.instance" and len(parts) >= 2 and all(parts[:2]):
        result = {
            "definition_name": parts[0],
            "instance_name": parts[1],
        }
        if len(parts) >= 3 and parts[2]:
            result["serial_or_mark"] = parts[2]
        return result
    if predicate == "item.stack" and len(parts) >= 2 and all(parts[:2]):
        result = {
            "definition_name": parts[0],
            "stack_name": parts[1],
        }
        if len(parts) >= 3 and parts[2]:
            try:
                quantity = float(parts[2])
            except ValueError:
                pass
            else:
                result["quantity"] = (
                    int(quantity) if quantity.is_integer() else quantity
                )
        return result
    if predicate == "item.function" and len(parts) >= 2 and all(parts[:2]):
        result = {
            "item_name": parts[0],
            "name": parts[1],
        }
        if len(parts) >= 3 and parts[2]:
            result["activation_kind"] = parts[2]
        if len(parts) >= 4 and parts[3]:
            result["effect"] = parts[3]
        return result
    if predicate == "item.custody" and len(parts) >= 2 and all(parts[:2]):
        result = {
            "subject_type": parts[0],
            "subject_id": parts[1],
        }
        if len(parts) >= 3 and parts[2]:
            result["carrier"] = parts[2]
        if len(parts) >= 4 and parts[3]:
            result["location"] = parts[3]
        if len(parts) >= 5 and parts[4]:
            result["custody_status"] = parts[4]
        return result
    if predicate in {"item.runtime", "item.function_runtime"} and parts:
        result = {}
        if predicate == "item.runtime":
            result["item_instance_id"] = parts[0]
            offset = 1
        elif len(parts) >= 2:
            result["item_instance_id"] = parts[0]
            result["function_id"] = parts[1]
            offset = 2
        else:
            return text
        for part in parts[offset:]:
            match = re.fullmatch(r"(?P<key>[^=＝]+)[=＝](?P<value>.+)", part)
            if not match:
                continue
            key = match.group("key").strip()
            raw = match.group("value").strip()
            if raw.casefold() in {"true", "false"}:
                parsed: Any = raw.casefold() == "true"
            else:
                try:
                    number = float(raw)
                except ValueError:
                    parsed = raw
                else:
                    parsed = int(number) if number.is_integer() else number
            result[key] = parsed
        return result
    if predicate == "item.observation" and len(parts) >= 2 and all(parts[:2]):
        result = {
            "item_name": parts[0],
            "description": parts[1],
        }
        if len(parts) >= 3 and parts[2]:
            result["knowledge_plane"] = parts[2]
        if len(parts) >= 4 and parts[3]:
            result["observer"] = parts[3]
        return result
    return text


def _structured_claim_value(predicate: str, value: str) -> Any:
    if predicate.startswith("item."):
        return _structured_item_value(predicate, value)
    return _structured_power_value(predicate, value)


_ABILITY_LABEL_PATTERN = (
    r"(?:能力|技能|法术|功法|神通|异能|术式|招式|天赋|秘术|战技)"
)
_BARE_ABILITY_LABELS = frozenset(
    {
        "能力",
        "技能",
        "法术",
        "功法",
        "神通",
        "异能",
        "术式",
        "招式",
        "天赋",
        "秘术",
        "战技",
    }
)
_ABILITY_DIRECT_VERBS = {
    "掌握",
    "学会",
    "觉醒",
    "解锁",
    "领悟",
    "习得",
    "修成",
    "练成",
}
_ABILITY_STRONG_LEARNING_VERBS = {
    "掌握",
    "学会",
    "领悟",
    "习得",
    "修成",
    "练成",
}
_ABILITY_NAME_SUFFIXES = (
    "能力",
    "技能",
    "法术",
    "魔法",
    "神通",
    "异能",
    "天赋",
    "血脉",
    "领域",
    "剑意",
    "刀意",
    "身法",
    "心法",
    "秘法",
    "术",
    "法",
    "诀",
    "功",
    "经",
    "咒",
    "印",
    "阵",
    "式",
    "技",
    "拳",
    "掌",
    "剑",
    "刀",
    "枪",
    "步",
    "瞳",
    "眼",
)
_NON_ABILITY_NAME_SUFFIXES = (
    "权力",
    "权限",
    "职权",
    "话语权",
    "主动权",
    "所有权",
    "控制权",
    "自主权",
    "审批权",
    "分配权",
    "调配权",
    "容量",
    "通道",
    "席位",
    "资格",
    "预算",
    "情况",
    "信息",
    "证据",
    "材料",
    "线路",
    "制度",
    "规则",
    "条例",
    "法律",
    "计划",
    "方案",
    "政策",
    "流程",
    "职责",
    "岗位",
    "资源",
    "技术",
    "书籍",
    "书",
    "手册",
    "汇编",
    "法规",
    "档案",
)


def _clean_ability_name(value: str) -> str:
    return value.strip().strip("《》「」“”\"'。！？!?；;，,：:")


def _is_clean_direct_ability_name(value: str) -> bool:
    name = _clean_ability_name(value)
    if not name or name in _BARE_ABILITY_LABELS or len(name) > 16:
        return False
    if re.search(r"(?:的|以及|并且|或者|从而|因此)", name):
        return False
    if any(name.endswith(suffix) for suffix in _NON_ABILITY_NAME_SUFFIXES):
        return False
    return bool(
        re.fullmatch(
            r"[\u4e00-\u9fffA-Za-z0-9·_-]{1,16}",
            name,
        )
    )


def _looks_like_ability_name(value: str) -> bool:
    name = _clean_ability_name(value)
    return _is_clean_direct_ability_name(name) and any(
        name.endswith(suffix) for suffix in _ABILITY_NAME_SUFFIXES
    )


def _parse_ability_expression(expression: str, verb: str) -> str | None:
    text = expression.strip()
    text = re.sub(r"^(?:一门|一项|一种|一个)\s*", "", text)
    normalized_verb = verb.removesuffix("了")
    quoted = re.match(
        rf"(?:(?P<label>{_ABILITY_LABEL_PATTERN})\s*)?"
        r"(?:名为|叫作|叫做)?\s*[《「“](?P<name>[^》」”]{1,30})[》」”]"
        r"(?=$|[，,。！？!?；;：:])",
        text,
    )
    if quoted:
        name = _clean_ability_name(quoted.group("name"))
        if not name or name in _BARE_ABILITY_LABELS:
            return None
        if quoted.group("label"):
            return name
        if normalized_verb in _ABILITY_STRONG_LEARNING_VERBS | {
            "觉醒",
            "解锁",
        }:
            return name if _is_clean_direct_ability_name(name) else None
        if normalized_verb in {"拥有", "获得"}:
            return name if _looks_like_ability_name(name) else None
        return None

    labeled = re.fullmatch(
        rf"{_ABILITY_LABEL_PATTERN}\s*(?:名为|叫作|叫做)?\s*"
        r"[：:]?\s*(?P<name>[\u4e00-\u9fffA-Za-z0-9·_-]{1,30})",
        text,
    )
    if labeled:
        name = _clean_ability_name(labeled.group("name"))
        return name if _is_clean_direct_ability_name(name) else None

    name = _clean_ability_name(text)
    if normalized_verb not in _ABILITY_DIRECT_VERBS:
        return None
    if normalized_verb in {"觉醒", "解锁"}:
        return name if _is_clean_direct_ability_name(name) else None
    return name if _looks_like_ability_name(name) else None


def _extract_ability_ownership(line: str) -> tuple[str, str] | None:
    text = re.sub(r"^(?:[-*]\s*)", "", line.strip())
    match = re.fullmatch(
        r"(?P<actor>[\u4e00-\u9fffA-Za-z]"
        r"[\w\u4e00-\u9fff·]{0,15}?)"
        r"(?P<verb>掌握了?|学会了?|觉醒了?|解锁了?|领悟了?|"
        r"习得了?|修成了?|练成了?|获得了?|拥有)"
        r"(?P<expression>.+?)[。！？!?；;]?",
        text,
    )
    if not match:
        return None
    ability_name = _parse_ability_expression(
        match.group("expression"),
        match.group("verb"),
    )
    if not ability_name:
        return None
    return match.group("actor").strip(), ability_name


def _parse_ability_rule_head(
    head: str,
    known_ability_names: set[str],
) -> str | None:
    text = head.strip().removesuffix("的").strip()
    quoted = re.fullmatch(
        rf"(?:{_ABILITY_LABEL_PATTERN}\s*)?"
        r"[《「“](?P<name>[^》」”]{1,30})[》」”]",
        text,
    )
    if quoted:
        name = _clean_ability_name(quoted.group("name"))
        return name if name not in _BARE_ABILITY_LABELS else None

    labeled = re.fullmatch(
        rf"{_ABILITY_LABEL_PATTERN}\s*"
        r"(?P<name>[\u4e00-\u9fffA-Za-z0-9·_-]{1,30})",
        text,
    )
    if labeled:
        name = _clean_ability_name(labeled.group("name"))
        return name if _is_clean_direct_ability_name(name) else None

    name = _clean_ability_name(text)
    if name in _BARE_ABILITY_LABELS:
        return None
    if name in known_ability_names or _looks_like_ability_name(name):
        return name
    return None


def _extract_ability_rule(
    line: str,
    known_ability_names: set[str],
) -> tuple[str, str, str] | None:
    text = re.sub(r"^(?:[-*]\s*)", "", line.strip())
    match = re.fullmatch(
        r"(?P<head>.+?)(?:的)?"
        r"(?P<dimension>代价|成本|限制|边界|反制|冷却|来源|触发条件)"
        r"\s*[：:为]\s*(?P<value>.+)",
        text,
    )
    if not match:
        return None
    ability_name = _parse_ability_rule_head(
        match.group("head"),
        known_ability_names,
    )
    if not ability_name:
        return None
    suffix = {
        "代价": "cost",
        "成本": "cost",
        "限制": "limit",
        "边界": "limit",
        "反制": "counter",
        "冷却": "cooldown",
        "来源": "source",
        "触发条件": "condition",
    }[match.group("dimension")]
    return ability_name, suffix, match.group("value").strip()


def extract_claims(
    document: dict[str, Any],
    *,
    remote_cache: "RemoteResponseCache | None" = None,
) -> list[dict[str, Any]]:
    if document.get("parse_status") != "parsed":
        return []
    if document.get("ingest_policy") == "exclude":
        return []
    if "_json" in document:
        normalized = parse_normalized_export(document["_json"])
        if normalized is not None:
            provenance = normalized.get("provenance")
            claims = (
                provenance.get("claims")
                if isinstance(provenance, dict)
                else []
            )
            local_claims = (
                copy.deepcopy(claims) if isinstance(claims, list) else []
            )
            return _with_remote_claim_review(
                document,
                local_claims,
                remote_cache=remote_cache,
            )
        claims = list(_json_claims(document, document["_json"]))
        return _with_remote_claim_review(
            document,
            _deduplicate_claims(claims),
            remote_cache=remote_cache,
        )

    text = str(document.get("_text") or "")
    claims: list[dict[str, Any]] = []
    known_ability_names: set[str] = set()
    current_subject = "作品"
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        heading = re.match(r"^#{1,6}\s+(.+)$", line)
        if heading:
            current_subject = heading.group(1).strip()
            continue

        structured_predicate: str | None = None
        key_value = re.match(
            r"^(?:[-*]\s*)?(?P<key>[\w\u4e00-\u9fff·\-]{1,24})\s*[：:]\s*(?P<value>.+)$",
            line,
        )
        if key_value:
            key = key_value.group("key").strip()
            value = key_value.group("value").strip()
            predicate = _KEY_PREDICATES.get(key)
            if predicate:
                structured_predicate = predicate
                normalized_value = _structured_claim_value(
                    predicate,
                    value,
                )
                subject = (
                    value
                    if predicate in {"actor.protagonist", "actor.opponent", "actor.third_party"}
                    else current_subject
                )
                bare_ability_placeholder = (
                    predicate == "ability.owns"
                    and isinstance(normalized_value, str)
                    and _clean_ability_name(normalized_value)
                    in _BARE_ABILITY_LABELS
                ) or (
                    predicate.startswith("ability.")
                    and predicate != "ability.owns"
                    and _clean_ability_name(subject)
                    in _BARE_ABILITY_LABELS
                )
                if not bare_ability_placeholder:
                    claims.append(
                        _claim(
                            document,
                            subject=subject,
                            predicate=predicate,
                            value=normalized_value,
                            evidence=line,
                            line_start=line_no,
                        )
                    )
                if (
                    not bare_ability_placeholder
                    and predicate == "ability.owns"
                    and isinstance(normalized_value, str)
                ):
                    known_ability_names.add(
                        _clean_ability_name(normalized_value)
                    )

        ability_ownership = _extract_ability_ownership(line)
        if ability_ownership:
            actor_name, ability_name = ability_ownership
            claims.append(
                _claim(
                    document,
                    subject=actor_name,
                    predicate="ability.owns",
                    value=ability_name,
                    evidence=line,
                    line_start=line_no,
                )
            )
            known_ability_names.add(ability_name)

        ability_rule = (
            None
            if structured_predicate is not None
            and structured_predicate.startswith("ability.")
            else _extract_ability_rule(line, known_ability_names)
        )
        if ability_rule:
            ability_name, suffix, rule_value = ability_rule
            claims.append(
                _claim(
                    document,
                    subject=ability_name,
                    predicate=f"ability.{suffix}",
                    value=rule_value,
                    evidence=line,
                    line_start=line_no,
                )
            )

        alias_match = re.search(
            r"(?P<name>[\u4e00-\u9fffA-Za-z][\w\u4e00-\u9fff·]{1,20})"
            r"(?:（|\()?(?:别名|又名|昵称|称号)[：:为]?\s*"
            r"(?P<aliases>[\w\u4e00-\u9fff·、，,/\s]{1,80})(?:）|\))?",
            line,
        )
        if alias_match:
            aliases = [
                part.strip()
                for part in re.split(r"[、，,/\s]+", alias_match.group("aliases"))
                if part.strip()
            ]
            claims.append(
                _claim(
                    document,
                    subject=alias_match.group("name"),
                    predicate="entity.alias",
                    value=aliases,
                    evidence=line,
                    line_start=line_no,
                )
            )

        held_match = re.search(
            r"(?P<actor>[\u4e00-\u9fffA-Za-z][\w\u4e00-\u9fff·]{0,20})"
            r"(?:持有|拥有|携带|获得了?|拿到)"
            r"(?P<item>[\u4e00-\u9fffA-Za-z0-9·《》「」]{1,30})",
            line,
        )
        if held_match and not ability_ownership:
            claims.append(
                _claim(
                    document,
                    subject=held_match.group("actor"),
                    predicate="inventory.holds",
                    value=held_match.group("item").strip("。；;，,"),
                    evidence=line,
                    line_start=line_no,
                )
            )

        location_match = re.search(
            r"(?P<actor>[\u4e00-\u9fffA-Za-z][\w\u4e00-\u9fff·]{0,20})"
            r"(?:位于|身处|抵达|进入|来到|停留在|住在)"
            r"(?P<location>[\u4e00-\u9fffA-Za-z0-9·]{1,30})",
            line,
        )
        if location_match:
            claims.append(
                _claim(
                    document,
                    subject=location_match.group("actor"),
                    predicate="actor.location",
                    value=location_match.group("location").strip("。；;，,"),
                    evidence=line,
                    line_start=line_no,
                )
            )

        relation_match = re.search(
            r"(?P<a>[\u4e00-\u9fffA-Za-z][\w\u4e00-\u9fff·]{0,20})"
            r"与(?P<b>[\u4e00-\u9fffA-Za-z][\w\u4e00-\u9fff·]{0,20})"
            r"(?:是|成为|结成|形成)(?P<relation>[\u4e00-\u9fffA-Za-z]{1,20})",
            line,
        )
        if relation_match:
            claims.append(
                _claim(
                    document,
                    subject=relation_match.group("a"),
                    predicate="relation",
                    value={
                        "target": relation_match.group("b"),
                        "type": relation_match.group("relation").strip("。；;，,"),
                    },
                    evidence=line,
                    line_start=line_no,
                )
            )

        time_value = _story_time(line)
        if time_value:
            claims.append(
                _claim(
                    document,
                    subject="故事时间",
                    predicate="timeline.anchor",
                    value=time_value,
                    evidence=line,
                    line_start=line_no,
                )
            )

        if any(token in line for token in ("规则", "必须", "不得", "无法绕过")):
            claims.append(
                _claim(
                    document,
                    subject=current_subject,
                    predicate="world.rule",
                    value=line,
                    evidence=line,
                    line_start=line_no,
                )
            )
        if any(token in line for token in ("伏笔", "承诺", "谜团", "期限", "倒计时")):
            claims.append(
                _claim(
                    document,
                    subject=current_subject,
                    predicate="open_loop",
                    value=line,
                    evidence=line,
                    line_start=line_no,
                )
            )
    return _with_remote_claim_review(
        document,
        _deduplicate_claims(claims),
        remote_cache=remote_cache,
    )


def _deduplicate_claims(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for claim in claims:
        key = canonical_json(
            {
                "source_version_id": claim["source_version_id"],
                "line_start": claim["line_start"],
                "subject": claim["subject"],
                "predicate": claim["predicate"],
                "value": claim["object_or_value"],
            }
        )
        unique.setdefault(key, claim)
    return sorted(
        unique.values(),
        key=lambda item: (
            str(item["path"]),
            int(item["line_start"]),
            str(item["claim_id"]),
        ),
    )


def entity_type_for_claim(claim: dict[str, Any]) -> str:
    predicate = str(claim.get("predicate") or "")
    prefix = predicate.split(".", 1)[0]
    return _ENTITY_PREFIXES.get(prefix, "concept")
