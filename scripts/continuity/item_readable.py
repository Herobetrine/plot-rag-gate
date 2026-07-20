"""Deterministic human-readable projection for accepted item state.

The Markdown tree is deliberately disposable.  It is rebuilt only from the
schema-v6 item projection and never participates in continuity or item
projection hashing.  Publication happens after the authoritative SQLite
transaction has committed, so an I/O failure can only degrade this readable
surface; it cannot roll back accepted canon.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .items import (
    ITEM_PROJECTION_META_ACTIVE_REVISION,
    ITEM_PROJECTION_META_HASH,
    ITEM_PROJECTION_META_HEAD_REVISION,
    ITEM_PROJECTION_META_VERSION,
    read_item_projection_metadata,
)


READABLE_PROJECTION_SCHEMA_VERSION = "plot-rag-item-readable/v1"
READABLE_PROJECTION_DIRECTORY = "物品"
READABLE_PROJECTION_INDEX = "物品索引.md"
READABLE_PROJECTION_INSTANCES = "实例"

_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_JSON_COLUMNS = frozenset(
    {
        "definition_json",
        "instance_json",
        "batch_json",
        "binding_json",
        "state_json",
        "story_coordinate_json",
        "cooldown_until_json",
        "delta_json",
        "before_json",
        "after_json",
        "observation_json",
        "evidence_json",
    }
)
_PROCESS_LOCK_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}


class ItemReadableProjectionError(RuntimeError):
    """Raised when the disposable readable projection cannot be published."""


def _canonical_json(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":") if indent is None else None,
        indent=indent,
        allow_nan=False,
    )


def _decode_json(value: Any, field: str) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ItemReadableProjectionError(
            f"{field} in accepted item projection is not valid JSON"
        ) from exc


def _decoded_row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for column in _JSON_COLUMNS.intersection(result):
        result[column.removesuffix("_json")] = _decode_json(
            result.pop(column),
            column,
        )
    return result


def _rows(
    connection: sqlite3.Connection,
    query: str,
    parameters: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    return [
        _decoded_row(row)
        for row in connection.execute(query, tuple(parameters)).fetchall()
    ]


def _process_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path))
    with _PROCESS_LOCK_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


@contextlib.contextmanager
def _publication_lock(state_dir: Path):
    """Serialize tree swaps across threads and cooperating processes."""

    process_lock = _process_lock(state_dir)
    with process_lock:
        lock_path = state_dir / ".item-readable.lock"
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def readable_projection_enabled(project_root: Path | str) -> bool:
    """Read only the documented item projection flag.

    A missing config or missing ``items`` section uses the v1.5 default
    (enabled).  An explicit ``false`` performs no publication or cleanup.
    """

    root = Path(project_root).expanduser().resolve()
    config_path = root / ".plot-rag" / "config.json"
    if not config_path.is_file():
        return True
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ItemReadableProjectionError(
            "project config could not be read for items.readable_projection"
        ) from exc
    if not isinstance(raw, Mapping):
        raise ItemReadableProjectionError("project config root must be an object")
    items = raw.get("items", {})
    if not isinstance(items, Mapping):
        raise ItemReadableProjectionError("config.items must be an object")
    enabled = items.get("readable_projection", True)
    if not isinstance(enabled, bool):
        raise ItemReadableProjectionError(
            "config.items.readable_projection must be a boolean"
        )
    return enabled


def _portable_basename(identifier: str, *, prefix: str) -> str:
    candidate = str(identifier)
    stem = candidate.split(".", 1)[0].upper()
    if (
        _PORTABLE_ID.fullmatch(candidate)
        and stem not in _WINDOWS_RESERVED_NAMES
        and not candidate.endswith((".", " "))
    ):
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest}"


def _filename_map(
    identifiers: Iterable[str],
    *,
    prefix: str,
) -> dict[str, str]:
    """Return stable, Windows-safe, case-insensitive collision-free names."""

    ordered = sorted({str(identifier) for identifier in identifiers})
    candidates = {
        identifier: _portable_basename(identifier, prefix=prefix)
        for identifier in ordered
    }
    groups: dict[str, list[str]] = {}
    for identifier, candidate in candidates.items():
        groups.setdefault(candidate.casefold(), []).append(identifier)
    for collision in groups.values():
        if len(collision) <= 1:
            continue
        for identifier in collision:
            digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
            candidates[identifier] = f"{prefix}-{digest}"
    return {
        identifier: f"{candidates[identifier]}.md"
        for identifier in ordered
    }


def _markdown_inline(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float)):
        text = _canonical_json(value)
    elif isinstance(value, str):
        text = value
    else:
        text = _canonical_json(value)
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("`", "\\`")
    )


def _code(value: Any) -> str:
    return (
        f"`{_markdown_inline(value)}`"
        if value is not None and value != ""
        else "—"
    )


def _json_block(value: Any) -> list[str]:
    return ["```json", _canonical_json(value, indent=2), "```"]


def _entity_label(
    entity_id: Any,
    entities: Mapping[str, Mapping[str, Any]],
) -> str:
    if entity_id is None or str(entity_id) == "":
        return "—"
    normalized = str(entity_id)
    entity = entities.get(normalized)
    if entity is None:
        return _code(normalized)
    return (
        f"{_markdown_inline(entity.get('canonical_name'))} "
        f"({_code(normalized)})"
    )


def _definition_display_name(
    definition: Mapping[str, Any],
    entities: Mapping[str, Mapping[str, Any]],
) -> str:
    payload = dict(definition.get("definition") or {})
    entity_id = definition.get("item_entity_id")
    entity = entities.get(str(entity_id)) if entity_id else None
    for value in (
        payload.get("canonical_name"),
        payload.get("name"),
        entity.get("canonical_name") if entity else None,
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(definition["item_definition_id"])


def _instance_display_name(
    instance: Mapping[str, Any],
    entities: Mapping[str, Mapping[str, Any]],
) -> str:
    payload = dict(instance.get("instance") or {})
    entity_id = instance.get("item_entity_id")
    entity = entities.get(str(entity_id)) if entity_id else None
    for value in (
        payload.get("instance_name"),
        payload.get("name"),
        entity.get("canonical_name") if entity else None,
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(instance["item_instance_id"])


def _source_lines(
    source_ids: Iterable[Any],
    events: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    normalized = sorted(
        {
            str(source_id)
            for source_id in source_ids
            if source_id is not None and str(source_id)
        }
    )
    if not normalized:
        return [
            "- 没有 accepted item event 来源；该记录来自明确标注的 legacy "
            "兼容投影，未据名称或旧属性补写功能。"
        ]
    lines: list[str] = []
    for source_id in normalized:
        event = events.get(source_id)
        lines.extend([f"### {_code(source_id)}"])
        if event is None:
            lines.append("- 来源事件元数据未进入当前可读快照。")
            continue
        lines.extend(
            [
                "| 字段 | accepted 值 |",
                "| --- | --- |",
                f"| event_type | {_markdown_inline(event.get('event_type'))} |",
                f"| scope | {_markdown_inline(event.get('scope'))} |",
                f"| branch_id | {_markdown_inline(event.get('branch_id'))} |",
                f"| artifact_id | {_markdown_inline(event.get('artifact_id'))} |",
                f"| chapter_no | {_markdown_inline(event.get('chapter_no'))} |",
                f"| scene_index | {_markdown_inline(event.get('scene_index'))} |",
                f"| story_time | {_markdown_inline(event.get('story_time'))} |",
                "",
                "证据（逐字保存，不作扩写）：",
                "",
                *_json_block(event.get("evidence") or {}),
            ]
        )
    return lines


def _projection_header(
    metadata: Mapping[str, Any],
    *,
    document_kind: str,
) -> list[str]:
    return [
        "> 可重建派生物：内容只来自 accepted schema-v6 item projection；"
        "SQLite accepted immutable events 仍是权威层。",
        "",
        "| 投影字段 | 值 |",
        "| --- | --- |",
        f"| readable schema | {_code(READABLE_PROJECTION_SCHEMA_VERSION)} |",
        f"| document kind | {_code(document_kind)} |",
        f"| item schema | {_markdown_inline(metadata.get(ITEM_PROJECTION_META_VERSION))} |",
        f"| item projection hash | {_code(metadata.get(ITEM_PROJECTION_META_HASH))} |",
        f"| source head revision | {_markdown_inline(metadata.get(ITEM_PROJECTION_META_HEAD_REVISION))} |",
        f"| source active revision | {_markdown_inline(metadata.get(ITEM_PROJECTION_META_ACTIVE_REVISION))} |",
        "",
    ]


def _snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    metadata = read_item_projection_metadata(connection)
    definitions = _rows(
        connection,
        "SELECT * FROM item_definitions ORDER BY item_definition_id",
    )
    instances = _rows(
        connection,
        "SELECT * FROM item_instances ORDER BY item_instance_id",
    )
    stacks = _rows(
        connection,
        "SELECT * FROM item_stacks ORDER BY stack_id",
    )
    functions = _rows(
        connection,
        "SELECT * FROM item_function_definitions ORDER BY function_id",
    )
    bindings = _rows(
        connection,
        "SELECT * FROM item_function_bindings ORDER BY binding_id",
    )
    custody = _rows(
        connection,
        """
        SELECT * FROM item_custody_state
        ORDER BY subject_type, subject_id
        """,
    )
    runtime = _rows(
        connection,
        "SELECT * FROM item_runtime_state ORDER BY item_instance_id",
    )
    function_runtime = _rows(
        connection,
        """
        SELECT * FROM item_function_runtime_state
        ORDER BY item_instance_id, function_id
        """,
    )
    history = _rows(
        connection,
        """
        SELECT * FROM item_use_history
        ORDER BY updated_order, source_event_id
        """,
    )
    observations = _rows(
        connection,
        """
        SELECT * FROM item_observations
        ORDER BY updated_order, observation_key
        """,
    )
    entities = {
        str(row["entity_id"]): row
        for row in _rows(
            connection,
            """
            SELECT entity_id, entity_type, canonical_name, attributes_json
            FROM entities
            ORDER BY entity_id
            """,
        )
    }
    events = {
        str(row["event_id"]): row
        for row in _rows(
            connection,
            """
            SELECT e.event_id, e.event_type, e.scope, e.branch_id,
                   e.artifact_id, e.chapter_no, e.scene_index, e.story_time,
                   e.evidence_json
            FROM continuity_events AS e
            JOIN canon_commits AS c ON c.commit_id=e.commit_id
            WHERE c.operation='accept'
            ORDER BY c.head_revision_after, e.event_ordinal, e.event_id
            """,
        )
    }
    return {
        "metadata": metadata,
        "definitions": definitions,
        "instances": instances,
        "stacks": stacks,
        "functions": functions,
        "bindings": bindings,
        "custody": custody,
        "runtime": runtime,
        "function_runtime": function_runtime,
        "history": history,
        "observations": observations,
        "entities": entities,
        "events": events,
    }


def _render_index(
    snapshot: Mapping[str, Any],
    definition_files: Mapping[str, str],
    instance_files: Mapping[str, str],
) -> str:
    metadata = snapshot["metadata"]
    definitions = snapshot["definitions"]
    instances = snapshot["instances"]
    stacks = snapshot["stacks"]
    functions = snapshot["functions"]
    custody_by_subject = {
        (str(row["subject_type"]), str(row["subject_id"])): row
        for row in snapshot["custody"]
    }
    entities = snapshot["entities"]
    instance_count: dict[str, int] = {}
    stack_count: dict[str, int] = {}
    function_count: dict[str, int] = {}
    for row in instances:
        key = str(row["item_definition_id"])
        instance_count[key] = instance_count.get(key, 0) + 1
    for row in stacks:
        key = str(row["item_definition_id"])
        stack_count[key] = stack_count.get(key, 0) + 1
    for row in functions:
        key = str(row["item_definition_id"])
        function_count[key] = function_count.get(key, 0) + 1

    lines = [
        "# 物品索引",
        "",
        *_projection_header(metadata, document_kind="item_index"),
        "## 物品定义",
        "",
    ]
    if definitions:
        lines.extend(
            [
                "| 定义 ID | 名称 | 状态 | 类型 | 实例 | 堆叠 | 已记录功能定义 |",
                "| --- | --- | --- | --- | ---: | ---: | ---: |",
            ]
        )
        for row in definitions:
            definition_id = str(row["item_definition_id"])
            link = definition_files[definition_id]
            lines.append(
                "| [{identifier}]({link}) | {name} | {status} | {kind} | "
                "{instances} | {stacks} | {functions} |".format(
                    identifier=_markdown_inline(definition_id),
                    link=link,
                    name=_markdown_inline(
                        _definition_display_name(row, entities)
                    ),
                    status=_markdown_inline(row.get("item_status")),
                    kind=_markdown_inline(row.get("item_kind")),
                    instances=instance_count.get(definition_id, 0),
                    stacks=stack_count.get(definition_id, 0),
                    functions=function_count.get(definition_id, 0),
                )
            )
    else:
        lines.append("- 当前 accepted item projection 没有物品定义。")

    lines.extend(["", "## 物品实例", ""])
    if instances:
        lines.extend(
            [
                "| 实例 ID | 名称 | 定义 ID | 状态 | 保管状态 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for row in instances:
            instance_id = str(row["item_instance_id"])
            definition_id = str(row["item_definition_id"])
            custody = custody_by_subject.get(("item_instance", instance_id))
            lines.append(
                "| [{identifier}](实例/{link}) | {name} | "
                "[{definition}]({definition_link}) | {status} | {custody} |".format(
                    identifier=_markdown_inline(instance_id),
                    link=instance_files[instance_id],
                    name=_markdown_inline(_instance_display_name(row, entities)),
                    definition=_markdown_inline(definition_id),
                    definition_link=definition_files.get(definition_id, ""),
                    status=_markdown_inline(row.get("instance_status")),
                    custody=_markdown_inline(
                        custody.get("custody_status") if custody else None
                    ),
                )
            )
    else:
        lines.append("- 当前 accepted item projection 没有物品实例。")

    lines.extend(["", "## 物品堆叠", ""])
    if stacks:
        lines.extend(
            [
                "| Stack ID | 定义 ID | 数量 | 状态 | 保管状态 |",
                "| --- | --- | ---: | --- | --- |",
            ]
        )
        for row in stacks:
            stack_id = str(row["stack_id"])
            definition_id = str(row["item_definition_id"])
            custody = custody_by_subject.get(("item_stack", stack_id))
            lines.append(
                "| {stack_id} | [{definition}]({definition_link}) | "
                "{quantity} | {status} | {custody} |".format(
                    stack_id=_code(stack_id),
                    definition=_markdown_inline(definition_id),
                    definition_link=definition_files.get(definition_id, ""),
                    quantity=_markdown_inline(row.get("quantity")),
                    status=_markdown_inline(row.get("stack_status")),
                    custody=_markdown_inline(
                        custody.get("custody_status") if custody else None
                    ),
                )
            )
    else:
        lines.append("- 当前 accepted item projection 没有物品堆叠。")

    lines.extend(
        [
            "",
            "## 语义边界",
            "",
            "- 功能计数只统计 accepted `item_function_definitions` 行。",
            "- 名称、旧 attributes、空字段或物品类型不会被解释成未记录功能。",
            "- 本目录可随时删除并从 SQLite accepted projection 重建。",
            "",
        ]
    )
    return "\n".join(lines)


def _render_definition(
    definition: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    definition_files: Mapping[str, str],
    instance_files: Mapping[str, str],
) -> str:
    definition_id = str(definition["item_definition_id"])
    metadata = snapshot["metadata"]
    entities = snapshot["entities"]
    events = snapshot["events"]
    instances = [
        row
        for row in snapshot["instances"]
        if str(row["item_definition_id"]) == definition_id
    ]
    stacks = [
        row
        for row in snapshot["stacks"]
        if str(row["item_definition_id"]) == definition_id
    ]
    functions = [
        row
        for row in snapshot["functions"]
        if str(row["item_definition_id"]) == definition_id
    ]
    function_ids = {str(row["function_id"]) for row in functions}
    bindings = [
        row
        for row in snapshot["bindings"]
        if str(row.get("item_definition_id") or "") == definition_id
        or str(row.get("function_id") or "") in function_ids
    ]
    custody_by_subject = {
        (str(row["subject_type"]), str(row["subject_id"])): row
        for row in snapshot["custody"]
    }
    source_ids: set[Any] = {definition.get("source_event_id")}
    for collection in (instances, stacks, functions, bindings):
        source_ids.update(row.get("source_event_id") for row in collection)

    lines = [
        f"# {_markdown_inline(_definition_display_name(definition, entities))}",
        "",
        f"稳定定义 ID：{_code(definition_id)}",
        "",
        *_projection_header(metadata, document_kind="item_definition"),
        "## 基本信息",
        "",
        "| 字段 | accepted 值 |",
        "| --- | --- |",
        f"| item_definition_id | {_code(definition_id)} |",
        f"| item_entity_id | {_entity_label(definition.get('item_entity_id'), entities)} |",
        f"| item_status | {_markdown_inline(definition.get('item_status'))} |",
        f"| item_kind | {_markdown_inline(definition.get('item_kind'))} |",
        f"| stack_policy | {_markdown_inline(definition.get('stack_policy'))} |",
        f"| uniqueness_policy | {_markdown_inline(definition.get('uniqueness_policy'))} |",
        "",
        "## accepted 定义载荷",
        "",
        *_json_block(definition.get("definition") or {}),
        "",
        "## 已记录功能定义",
        "",
    ]
    if not functions:
        lines.extend(
            [
                "- 没有 accepted `ItemFunctionDefinition` 记录。",
                "- 不从物品名称、类型、legacy attributes 或空字段推断功能。",
            ]
        )
    else:
        bindings_by_function: dict[str, list[Mapping[str, Any]]] = {}
        for binding in bindings:
            bindings_by_function.setdefault(
                str(binding["function_id"]), []
            ).append(binding)
        for function in functions:
            function_id = str(function["function_id"])
            lines.extend(
                [
                    f"### {_code(function_id)}",
                    "",
                    "| 字段 | accepted 值 |",
                    "| --- | --- |",
                    f"| function_status | {_markdown_inline(function.get('function_status'))} |",
                    f"| effect_owner | {_markdown_inline(function.get('effect_owner'))} |",
                    "",
                    *_json_block(function.get("definition") or {}),
                    "",
                    "绑定：",
                ]
            )
            relevant = bindings_by_function.get(function_id, [])
            if relevant:
                for binding in relevant:
                    targets = {
                        key: binding.get(key)
                        for key in (
                            "item_definition_id",
                            "item_instance_id",
                            "stack_id",
                        )
                        if binding.get(key) is not None
                    }
                    lines.append(
                        "- {binding_id} / {status} / target={target}".format(
                            binding_id=_code(binding.get("binding_id")),
                            status=_markdown_inline(
                                binding.get("binding_status")
                            ),
                            target=_code(_canonical_json(targets)),
                        )
                    )
            else:
                lines.append("- 没有 accepted function binding。")

    lines.extend(["", "## 实例", ""])
    if instances:
        lines.extend(
            [
                "| 实例 ID | 名称 | 状态 | 保管状态 |",
                "| --- | --- | --- | --- |",
            ]
        )
        for instance in instances:
            instance_id = str(instance["item_instance_id"])
            custody = custody_by_subject.get(("item_instance", instance_id))
            lines.append(
                "| [{identifier}](实例/{link}) | {name} | {status} | "
                "{custody} |".format(
                    identifier=_markdown_inline(instance_id),
                    link=instance_files[instance_id],
                    name=_markdown_inline(
                        _instance_display_name(instance, entities)
                    ),
                    status=_markdown_inline(instance.get("instance_status")),
                    custody=_markdown_inline(
                        custody.get("custody_status") if custody else None
                    ),
                )
            )
    else:
        lines.append("- 没有 accepted item instance。")

    lines.extend(["", "## 堆叠", ""])
    if stacks:
        for stack in stacks:
            stack_id = str(stack["stack_id"])
            custody = custody_by_subject.get(("item_stack", stack_id))
            lines.extend(
                [
                    f"### {_code(stack_id)}",
                    "",
                    "| 字段 | accepted 值 |",
                    "| --- | --- |",
                    f"| quantity | {_markdown_inline(stack.get('quantity'))} |",
                    f"| stack_status | {_markdown_inline(stack.get('stack_status'))} |",
                    f"| custody_status | {_markdown_inline(custody.get('custody_status') if custody else None)} |",
                    "",
                    "批次载荷：",
                    "",
                    *_json_block(stack.get("batch") or {}),
                ]
            )
            if custody:
                lines.extend(
                    [
                        "",
                        "保管载荷：",
                        "",
                        *_json_block(custody),
                    ]
                )
    else:
        lines.append("- 没有 accepted item stack。")

    if str(definition.get("item_status") or "").startswith("legacy_"):
        lines.extend(
            [
                "",
                "## Legacy 边界",
                "",
                "- legacy attributes 仅按 accepted 兼容投影原样展示。",
                "- `legacy_unmodeled` / `legacy_self_instance` 不代表已确认功能、"
                "stack 语义或运行态。",
            ]
        )

    lines.extend(["", "## accepted 来源", "", *_source_lines(source_ids, events), ""])
    return "\n".join(lines)


def _render_instance(
    instance: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    definition_files: Mapping[str, str],
    instance_files: Mapping[str, str],
) -> str:
    instance_id = str(instance["item_instance_id"])
    definition_id = str(instance["item_definition_id"])
    metadata = snapshot["metadata"]
    entities = snapshot["entities"]
    events = snapshot["events"]
    custody = next(
        (
            row
            for row in snapshot["custody"]
            if str(row["subject_type"]) == "item_instance"
            and str(row["subject_id"]) == instance_id
        ),
        None,
    )
    runtime = next(
        (
            row
            for row in snapshot["runtime"]
            if str(row["item_instance_id"]) == instance_id
        ),
        None,
    )
    bindings = [
        row
        for row in snapshot["bindings"]
        if str(row.get("item_instance_id") or "") == instance_id
        or str(row.get("item_definition_id") or "") == definition_id
    ]
    function_runtime = [
        row
        for row in snapshot["function_runtime"]
        if str(row["item_instance_id"]) == instance_id
    ]
    history = [
        row
        for row in snapshot["history"]
        if str(row.get("item_instance_id") or "") == instance_id
    ]
    observations = [
        row
        for row in snapshot["observations"]
        if str(row.get("item_instance_id") or "") == instance_id
    ]
    source_ids: set[Any] = {instance.get("source_event_id")}
    for value in (
        custody,
        runtime,
        *bindings,
        *function_runtime,
        *history,
        *observations,
    ):
        if value:
            source_ids.add(value.get("source_event_id"))

    lines = [
        f"# {_markdown_inline(_instance_display_name(instance, entities))}",
        "",
        f"稳定实例 ID：{_code(instance_id)}",
        "",
        *_projection_header(metadata, document_kind="item_instance"),
        "## 基本信息",
        "",
        "| 字段 | accepted 值 |",
        "| --- | --- |",
        f"| item_instance_id | {_code(instance_id)} |",
        f"| item_definition_id | [{_markdown_inline(definition_id)}](../{definition_files.get(definition_id, '')}) |",
        f"| item_entity_id | {_entity_label(instance.get('item_entity_id'), entities)} |",
        f"| instance_status | {_markdown_inline(instance.get('instance_status'))} |",
        f"| story_coordinate | {_code(_canonical_json(instance.get('story_coordinate') or {}))} |",
        "",
        "## accepted 实例载荷",
        "",
        *_json_block(instance.get("instance") or {}),
        "",
        "## 保管与位置",
        "",
    ]
    if custody is None:
        lines.append("- 没有 accepted custody 记录。")
    else:
        lines.extend(
            [
                "| 字段 | accepted 值 |",
                "| --- | --- |",
                f"| custody_status | {_markdown_inline(custody.get('custody_status'))} |",
                f"| quantity | {_markdown_inline(custody.get('quantity'))} |",
                f"| legal_owner | {_entity_label(custody.get('legal_owner_entity_id'), entities)} |",
                f"| custodian | {_entity_label(custody.get('custodian_entity_id'), entities)} |",
                f"| carrier | {_entity_label(custody.get('carrier_entity_id'), entities)} |",
                f"| location | {_entity_label(custody.get('location_entity_id'), entities)} |",
                f"| access_controller | {_entity_label(custody.get('access_controller_entity_id'), entities)} |",
                f"| container_instance_id | {_code(custody.get('container_instance_id'))} |",
                f"| story_coordinate | {_code(_canonical_json(custody.get('story_coordinate') or {}))} |",
                "",
                "custody state：",
                "",
                *_json_block(custody.get("state") or {}),
            ]
        )
        container_id = custody.get("container_instance_id")
        if container_id is not None and str(container_id) in instance_files:
            lines.extend(
                [
                    "",
                    "容器档案："
                    f"[{_markdown_inline(container_id)}]"
                    f"({instance_files[str(container_id)]})",
                ]
            )

    lines.extend(["", "## 运行态", ""])
    if runtime is None:
        lines.append("- 没有 accepted item runtime 记录。")
    else:
        lines.extend(
            [
                "| 字段 | accepted 值 |",
                "| --- | --- |",
                f"| durability | {_markdown_inline(runtime.get('durability'))} |",
                f"| max_durability | {_markdown_inline(runtime.get('max_durability'))} |",
                f"| energy | {_markdown_inline(runtime.get('energy'))} |",
                f"| max_energy | {_markdown_inline(runtime.get('max_energy'))} |",
                f"| sealed | {_markdown_inline(bool(runtime.get('sealed')))} |",
                f"| damaged | {_markdown_inline(bool(runtime.get('damaged')))} |",
                f"| destroyed | {_markdown_inline(bool(runtime.get('destroyed')))} |",
                f"| active | {_markdown_inline(bool(runtime.get('active')))} |",
                f"| equipped_by | {_entity_label(runtime.get('equipped_by_entity_id'), entities)} |",
                f"| slot_key | {_markdown_inline(runtime.get('slot_key'))} |",
                f"| bound_actor | {_entity_label(runtime.get('bound_actor_entity_id'), entities)} |",
                f"| story_coordinate | {_code(_canonical_json(runtime.get('story_coordinate') or {}))} |",
                "",
                "runtime state：",
                "",
                *_json_block(runtime.get("state") or {}),
            ]
        )

    lines.extend(["", "## 功能绑定与功能运行态", ""])
    if not bindings and not function_runtime:
        lines.extend(
            [
                "- 没有 accepted function binding 或 function runtime。",
                "- 不从名称、物品类型或 attributes 推断功能。",
            ]
        )
    else:
        if bindings:
            lines.extend(
                [
                    "### 绑定",
                    "",
                    "| binding_id | function_id | 绑定层级 | 状态 |",
                    "| --- | --- | --- | --- |",
                ]
            )
            for binding in bindings:
                layer = (
                    "instance"
                    if binding.get("item_instance_id") is not None
                    else "definition"
                )
                lines.append(
                    "| {binding} | {function} | {layer} | {status} |".format(
                        binding=_code(binding.get("binding_id")),
                        function=_code(binding.get("function_id")),
                        layer=_markdown_inline(layer),
                        status=_markdown_inline(
                            binding.get("binding_status")
                        ),
                    )
                )
        if function_runtime:
            lines.extend(["", "### 功能运行态", ""])
            for row in function_runtime:
                lines.extend(
                    [
                        f"#### {_code(row.get('function_id'))}",
                        "",
                        "| 字段 | accepted 值 |",
                        "| --- | --- |",
                        f"| enabled | {_markdown_inline(bool(row.get('enabled')))} |",
                        f"| unlock_state | {_markdown_inline(row.get('unlock_state'))} |",
                        f"| remaining_charges | {_markdown_inline(row.get('remaining_charges'))} |",
                        f"| cooldown_until | {_code(_canonical_json(row.get('cooldown_until')))} |",
                        "",
                        *_json_block(row.get("state") or {}),
                    ]
                )

    lines.extend(["", "## 使用历史", ""])
    if not history:
        lines.append("- 没有 accepted use history。")
    else:
        for row in history:
            lines.extend(
                [
                    f"### {_code(row.get('source_event_id'))}",
                    "",
                    "| 字段 | accepted 值 |",
                    "| --- | --- |",
                    f"| action | {_markdown_inline(row.get('action'))} |",
                    f"| function_id | {_code(row.get('function_id'))} |",
                    f"| actor | {_entity_label(row.get('actor_entity_id'), entities)} |",
                    f"| target | {_entity_label(row.get('target_entity_id'), entities)} |",
                    f"| chapter_no | {_markdown_inline(row.get('chapter_no'))} |",
                    f"| scene_index | {_markdown_inline(row.get('scene_index'))} |",
                    "",
                    "delta / before / after：",
                    "",
                    *_json_block(
                        {
                            "delta": row.get("delta") or {},
                            "before": row.get("before") or {},
                            "after": row.get("after") or {},
                            "story_coordinate": row.get("story_coordinate")
                            or {},
                        }
                    ),
                ]
            )

    lines.extend(["", "## 观察记录", ""])
    if not observations:
        lines.append("- 没有 accepted item observation。")
    else:
        for row in observations:
            lines.extend(
                [
                    f"### {_code(row.get('observation_key'))}",
                    "",
                    "| 字段 | accepted 值 |",
                    "| --- | --- |",
                    f"| observer | {_entity_label(row.get('observer_entity_id'), entities)} |",
                    f"| action | {_markdown_inline(row.get('observation_action'))} |",
                    f"| knowledge_plane | {_markdown_inline(row.get('knowledge_plane'))} |",
                    f"| function_id | {_code(row.get('function_id'))} |",
                    "",
                    *_json_block(row.get("observation") or {}),
                ]
            )

    lines.extend(["", "## accepted 来源", "", *_source_lines(source_ids, events), ""])
    return "\n".join(lines)


def build_item_readable_files(
    connection: sqlite3.Connection,
) -> tuple[dict[PurePosixPath, bytes], dict[str, Any]]:
    """Render one deterministic Markdown tree from one SQLite read snapshot."""

    snapshot = _snapshot(connection)
    definitions = snapshot["definitions"]
    instances = snapshot["instances"]
    definition_files = _filename_map(
        (str(row["item_definition_id"]) for row in definitions),
        prefix="definition",
    )
    instance_files = _filename_map(
        (str(row["item_instance_id"]) for row in instances),
        prefix="instance",
    )
    rendered: dict[PurePosixPath, bytes] = {
        PurePosixPath(READABLE_PROJECTION_INDEX): _render_index(
            snapshot,
            definition_files,
            instance_files,
        ).encode("utf-8")
    }
    for definition in definitions:
        definition_id = str(definition["item_definition_id"])
        rendered[PurePosixPath(definition_files[definition_id])] = (
            _render_definition(
                definition,
                snapshot,
                definition_files,
                instance_files,
            ).encode("utf-8")
        )
    for instance in instances:
        instance_id = str(instance["item_instance_id"])
        rendered[
            PurePosixPath(
                READABLE_PROJECTION_INSTANCES,
                instance_files[instance_id],
            )
        ] = _render_instance(
            instance,
            snapshot,
            definition_files,
            instance_files,
        ).encode("utf-8")
    return rendered, {
        "definition_count": len(definitions),
        "instance_count": len(instances),
        "stack_count": len(snapshot["stacks"]),
        "function_count": len(snapshot["functions"]),
        "item_projection_hash": str(
            snapshot["metadata"].get(ITEM_PROJECTION_META_HASH) or ""
        ),
    }


def _assert_inside(root: Path, target: Path) -> None:
    try:
        # Windows can expose the same existing directory through an 8.3
        # short-name alias (for example ``RUNNER~1``) while resolving a child
        # through its long name (for example ``runneradmin``).  Resolve both
        # operands so the existing prefix is compared by its final path while
        # still allowing a not-yet-created target suffix.  ``relative_to``
        # remains the fail-closed boundary check for ``..``, other drives, and
        # resolved symlink/reparse escapes.
        resolved_root = root.resolve(strict=False)
        resolved_target = target.resolve(strict=False)
        resolved_target.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ItemReadableProjectionError(
            f"readable projection path escapes project root: {target}"
        ) from exc


def _is_reparse_path(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return False
    return bool(attributes & 0x400)


def _assert_safe_directory(path: Path, *, root: Path) -> None:
    _assert_inside(root, path.resolve(strict=False))
    if _is_reparse_path(path):
        raise ItemReadableProjectionError(
            f"readable projection refuses a symlink/reparse directory: {path}"
        )
    if path.exists() and not path.is_dir():
        raise ItemReadableProjectionError(
            f"readable projection path is not a directory: {path}"
        )


def _relative_target(stage: Path, relative: PurePosixPath) -> Path:
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ItemReadableProjectionError(
            f"unsafe readable projection path: {relative}"
        )
    target = stage.joinpath(*relative.parts)
    _assert_inside(stage.resolve(), target.resolve(strict=False))
    return target


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_staging_tree(
    stage: Path,
    files: Mapping[PurePosixPath, bytes],
) -> None:
    instance_dir = stage / READABLE_PROJECTION_INSTANCES
    instance_dir.mkdir(parents=True, exist_ok=False)
    for relative, content in sorted(
        files.items(),
        key=lambda pair: pair[0].as_posix(),
    ):
        target = _relative_target(stage, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    _fsync_directory(instance_dir)
    _fsync_directory(stage)


def _publish_tree(
    project_root: Path,
    files: Mapping[PurePosixPath, bytes],
) -> Path:
    state_dir = project_root / ".plot-rag"
    if state_dir.exists():
        _assert_safe_directory(state_dir, root=project_root)
    else:
        state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / READABLE_PROJECTION_DIRECTORY
    backup = state_dir / ".item-readable-backup"
    _assert_safe_directory(target, root=project_root)
    _assert_safe_directory(backup, root=project_root)

    with _publication_lock(state_dir):
        if backup.exists():
            if target.exists():
                shutil.rmtree(backup)
            else:
                os.replace(backup, target)

        stage = Path(
            tempfile.mkdtemp(
                prefix=".item-readable-stage-",
                dir=state_dir,
            )
        )
        published = False
        try:
            _write_staging_tree(stage, files)
            if target.exists():
                os.replace(target, backup)
            try:
                os.replace(stage, target)
                published = True
            except BaseException:
                if backup.exists() and not target.exists():
                    os.replace(backup, target)
                raise
            finally:
                if stage.exists():
                    shutil.rmtree(stage, ignore_errors=True)
            _fsync_directory(state_dir)
            if backup.exists():
                shutil.rmtree(backup)
            return target.resolve()
        except BaseException:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            if not published and backup.exists() and not target.exists():
                with contextlib.suppress(OSError):
                    os.replace(backup, target)
            raise


def refresh_item_readable_projection(store: Any) -> dict[str, Any]:
    """Rebuild and atomically publish the readable item tree."""

    project_root = Path(store.project_root).expanduser().resolve()
    if not readable_projection_enabled(project_root):
        return {
            "status": "disabled",
            "enabled": False,
            "file_count": 0,
            "path": str(
                (
                    project_root
                    / ".plot-rag"
                    / READABLE_PROJECTION_DIRECTORY
                ).resolve(strict=False)
            ),
        }

    with store.read_connection() as connection:
        connection.execute("BEGIN")
        try:
            files, counts = build_item_readable_files(connection)
        finally:
            with contextlib.suppress(sqlite3.Error):
                connection.rollback()
    target = _publish_tree(project_root, files)
    return {
        "status": "completed",
        "enabled": True,
        "file_count": len(files),
        "path": str(target),
        **counts,
    }


def refresh_item_readable_projection_safe(store: Any) -> dict[str, Any]:
    """Return a degraded receipt instead of affecting accepted canon."""

    try:
        return refresh_item_readable_projection(store)
    except Exception as exc:
        return {
            "status": "degraded",
            "enabled": True,
            "error_type": type(exc).__name__,
            "message": str(exc),
        }


__all__ = [
    "ItemReadableProjectionError",
    "READABLE_PROJECTION_DIRECTORY",
    "READABLE_PROJECTION_INDEX",
    "READABLE_PROJECTION_INSTANCES",
    "READABLE_PROJECTION_SCHEMA_VERSION",
    "build_item_readable_files",
    "readable_projection_enabled",
    "refresh_item_readable_projection",
    "refresh_item_readable_projection_safe",
]
