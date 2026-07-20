"""Disposable human-readable cards for the accepted Advantage projection.

The Markdown tree is a presentation surface, not a source of canon.  It is
rebuilt from one read snapshot of ``scripts.continuity.advantages`` and is
published only after the authoritative transaction has committed.  The
Advantage core is imported lazily so this module can be installed before, or
disabled independently from, the optional projection runtime.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


ADVANTAGE_READABLE_SCHEMA_VERSION = "plot-rag-advantage-readable/v1"
ADVANTAGE_READABLE_DIRECTORY = "金手指"
ADVANTAGE_READABLE_INDEX = "金手指索引.md"
ADVANTAGE_READABLE_DEFINITIONS = "定义"
ADVANTAGE_READABLE_MODULES = "模块"
ADVANTAGE_READABLE_RUNTIME = "运行态"

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
_PROCESS_LOCK_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}

_TABLE_ALIASES = {
    "definitions": frozenset(
        {
            "definitions",
            "advantage_definitions",
            "advantage_definition_state",
        }
    ),
    "anchors": frozenset(
        {
            "anchors",
            "advantage_anchors",
            "advantage_anchor_state",
        }
    ),
    "modules": frozenset(
        {
            "module_definitions",
            "advantage_modules",
            "advantage_module_definitions",
            "advantage_module_state",
        }
    ),
    "slots": frozenset(
        {
            "runtime_slots",
            "advantage_runtime_slots",
            "advantage_runtime_slot_definitions",
            "advantage_slot_definitions",
        }
    ),
    "runtime": frozenset(
        {
            "runtime_state",
            "advantage_runtime",
            "advantage_runtime_state",
            "advantage_module_runtime",
            "advantage_module_runtime_state",
        }
    ),
    "ledger": frozenset(
        {
            "ledger",
            "advantage_ledger",
            "advantage_ledger_entries",
        }
    ),
    "knowledge": frozenset(
        {
            "knowledge",
            "advantage_knowledge",
            "advantage_knowledge_state",
        }
    ),
    "contracts": frozenset(
        {
            "contracts",
            "advantage_contracts",
            "advantage_contract_state",
        }
    ),
    "narrative": frozenset(
        {
            "narrative_contracts",
            "advantage_narrative_contracts",
            "advantage_narrative_contract_state",
        }
    ),
}


class AdvantageReadableProjectionError(RuntimeError):
    """A readable Advantage tree could not be built or published."""


def _canonical_json(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":") if indent is None else None,
        indent=indent,
        allow_nan=False,
    )


def _load_advantage_core() -> Any:
    """Dynamically load the optional Advantage projection implementation."""

    failures: list[ModuleNotFoundError] = []
    for module_name in (
        "continuity.advantages",
        "scripts.continuity.advantages",
    ):
        try:
            return importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name not in {module_name, "scripts", "continuity"}:
                raise
            failures.append(exc)
    raise AdvantageReadableProjectionError(
        "Advantage projection runtime is not installed"
    ) from failures[-1]


def advantage_readable_projection_enabled(project_root: Path | str) -> bool:
    """Return the documented ``advantage.readable_projection`` switch.

    Missing configuration keeps the additive projection enabled.  Explicit
    ``false`` is a strict no-op: an existing tree is neither rewritten nor
    cleaned up.
    """

    root = Path(project_root).expanduser().resolve()
    config_path = root / ".plot-rag" / "config.json"
    if not config_path.is_file():
        return True
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise AdvantageReadableProjectionError(
            "project config could not be read for "
            "advantage.readable_projection"
        ) from exc
    if not isinstance(raw, Mapping):
        raise AdvantageReadableProjectionError(
            "project config root must be an object"
        )
    advantage = (
        raw["advantage"]
        if "advantage" in raw
        else raw.get("advantages", {})
    )
    if not isinstance(advantage, Mapping):
        raise AdvantageReadableProjectionError(
            "config.advantage must be an object"
        )
    enabled = advantage.get("readable_projection", True)
    if not isinstance(enabled, bool):
        raise AdvantageReadableProjectionError(
            "config.advantage.readable_projection must be a boolean"
        )
    return enabled


# Keep the noun-first spelling used by early service integration drafts.
readable_advantage_projection_enabled = (
    advantage_readable_projection_enabled
)


def _decode_row(
    row: Mapping[str, Any],
    *,
    table_name: str,
) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for raw_key, value in row.items():
        key = str(raw_key)
        if key.endswith("_json"):
            target_key = key.removesuffix("_json")
            decoded_value: Any = value
            # The Advantage core currently returns already-decoded values
            # while retaining the physical ``*_json`` column name.  Also
            # tolerate a raw JSON row supplied by a compatible core without
            # trying to decode ordinary JSON string scalars a second time.
            if isinstance(value, str) and value.lstrip().startswith(
                ("{", "[", '"')
            ):
                try:
                    decoded_value = json.loads(value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    decoded_value = value
            if target_key in decoded:
                raise AdvantageReadableProjectionError(
                    f"{table_name} exposes both {target_key} and {key}"
                )
            decoded[target_key] = decoded_value
        else:
            decoded[key] = value
    decoded["_projection_table"] = table_name
    return decoded


def _table_category(table_name: str) -> str | None:
    normalized = table_name.casefold()
    for category, aliases in _TABLE_ALIASES.items():
        if normalized in aliases:
            return category
    if "narrative" in normalized and "contract" in normalized:
        return "narrative"
    if "knowledge" in normalized:
        return "knowledge"
    if "ledger" in normalized:
        return "ledger"
    if "anchor" in normalized:
        return "anchors"
    if "slot" in normalized:
        return "slots"
    if "runtime" in normalized:
        return "runtime"
    if "module" in normalized and "definition" in normalized:
        return "modules"
    if "contract" in normalized:
        return "contracts"
    if (
        "advantage" in normalized
        and "definition" in normalized
        and "module" not in normalized
    ):
        return "definitions"
    return None


def _normalized_tables(payload: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    raw_tables = payload.get("tables", payload)
    if not isinstance(raw_tables, Mapping):
        raise AdvantageReadableProjectionError(
            "advantage_projection_payload().tables must be an object"
        )
    categories: dict[str, list[dict[str, Any]]] = {
        category: [] for category in _TABLE_ALIASES
    }
    for raw_name, raw_rows in raw_tables.items():
        table_name = str(raw_name)
        category = _table_category(table_name)
        if category is None:
            continue
        if raw_rows is None:
            continue
        if not isinstance(raw_rows, (list, tuple)):
            raise AdvantageReadableProjectionError(
                f"{table_name} projection rows must be an array"
            )
        for raw_row in raw_rows:
            if not isinstance(raw_row, Mapping):
                raise AdvantageReadableProjectionError(
                    f"{table_name} projection row must be an object"
                )
            categories[category].append(
                _decode_row(raw_row, table_name=table_name)
            )
    for rows in categories.values():
        rows.sort(key=_canonical_json)
    return categories


def _metadata_value(
    metadata: Mapping[str, Any],
    *keys: str,
    default: Any = None,
) -> Any:
    for key in keys:
        if key in metadata:
            return metadata[key]
    return default


def _snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    core = _load_advantage_core()
    payload_reader = getattr(core, "advantage_projection_payload", None)
    metadata_reader = getattr(
        core,
        "read_advantage_projection_metadata",
        None,
    )
    if not callable(payload_reader):
        raise AdvantageReadableProjectionError(
            "Advantage core is missing advantage_projection_payload"
        )
    if not callable(metadata_reader):
        raise AdvantageReadableProjectionError(
            "Advantage core is missing read_advantage_projection_metadata"
        )
    payload = payload_reader(connection)
    metadata = metadata_reader(connection)
    if not isinstance(payload, Mapping):
        raise AdvantageReadableProjectionError(
            "advantage_projection_payload() must return an object"
        )
    if not isinstance(metadata, Mapping):
        raise AdvantageReadableProjectionError(
            "read_advantage_projection_metadata() must return an object"
        )
    merged_metadata = dict(metadata)
    for key, value in payload.items():
        if key != "tables" and not isinstance(value, (Mapping, list, tuple)):
            merged_metadata.setdefault(str(key), value)
    return {
        "metadata": merged_metadata,
        **_normalized_tables(payload),
    }


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
    ordered = sorted({str(identifier) for identifier in identifiers})
    candidates = {
        identifier: _portable_basename(identifier, prefix=prefix)
        for identifier in ordered
    }
    collisions: dict[str, list[str]] = {}
    for identifier, candidate in candidates.items():
        collisions.setdefault(candidate.casefold(), []).append(identifier)
    for group in collisions.values():
        if len(group) <= 1:
            continue
        for identifier in group:
            digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
            candidates[identifier] = f"{prefix}-{digest}"
    return {
        identifier: f"{candidates[identifier]}.md"
        for identifier in ordered
    }


def _runtime_filename_map(
    keys: Iterable[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    ordered = sorted(set(keys))
    candidates = {
        key: _portable_basename(
            f"{key[0]}--{key[1]}",
            prefix="runtime",
        )
        for key in ordered
    }
    collisions: dict[str, list[tuple[str, str]]] = {}
    for key, candidate in candidates.items():
        collisions.setdefault(candidate.casefold(), []).append(key)
    for group in collisions.values():
        if len(group) <= 1:
            continue
        for key in group:
            digest = hashlib.sha256(
                _canonical_json(list(key)).encode("utf-8")
            ).hexdigest()
            candidates[key] = f"runtime-{digest}"
    return {key: f"{candidates[key]}.md" for key in ordered}


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
    if value is None or value == "":
        return "—"
    return f"`{_markdown_inline(value)}`"


def _json_block(value: Any) -> list[str]:
    return ["```json", _canonical_json(value, indent=2), "```"]


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in row.items()
        if str(key) != "_projection_table"
    }


def _nested_mappings(row: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    yield row
    for key in (
        "definition",
        "payload",
        "spec",
        "anchor",
        "module",
        "runtime",
        "state",
        "knowledge",
        "contract",
        "narrative_contract",
    ):
        nested = row.get(key)
        if isinstance(nested, Mapping):
            yield nested


def _value(row: Mapping[str, Any], *keys: str) -> Any:
    for mapping in _nested_mappings(row):
        for key in keys:
            if key in mapping and mapping[key] is not None:
                return mapping[key]
    return None


def _required_id(
    row: Mapping[str, Any],
    *,
    kind: str,
    keys: tuple[str, ...],
) -> str:
    value = _value(row, *keys)
    normalized = str(value or "").strip()
    if not normalized:
        table = row.get("_projection_table")
        raise AdvantageReadableProjectionError(
            f"{kind} row in {table} has no stable identifier"
        )
    return normalized


def _advantage_id(row: Mapping[str, Any]) -> str:
    return _required_id(
        row,
        kind="Advantage",
        keys=("advantage_id",),
    )


def _module_id(row: Mapping[str, Any]) -> str:
    return _required_id(
        row,
        kind="Advantage module",
        keys=("module_id", "advantage_module_id"),
    )


def _branch_id(row: Mapping[str, Any]) -> str:
    value = _value(row, "branch_id")
    return str(value).strip() if value not in (None, "") else "main"


def _definition_title(row: Mapping[str, Any]) -> str:
    for key in ("title", "canonical_name", "name", "display_name"):
        value = _value(row, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _advantage_id(row)


def _enabled_value(row: Mapping[str, Any]) -> Any:
    value = _value(row, "enabled")
    if value in (0, 1) and not isinstance(value, bool):
        return bool(value)
    return value


def _projection_header(
    metadata: Mapping[str, Any],
    *,
    document_kind: str,
) -> list[str]:
    return [
        "> 可重建派生物：内容只来自 accepted Advantage projection；"
        "SQLite accepted immutable events 仍是权威层。",
        "",
        "| 投影字段 | 值 |",
        "| --- | --- |",
        f"| readable schema | {_code(ADVANTAGE_READABLE_SCHEMA_VERSION)} |",
        f"| document kind | {_code(document_kind)} |",
        "| advantage schema | {value} |".format(
            value=_markdown_inline(
                _metadata_value(
                    metadata,
                    "schema_version",
                    "advantage_schema_version",
                    "projection_schema_version",
                )
            )
        ),
        "| advantage projection hash | {value} |".format(
            value=_code(
                _metadata_value(
                    metadata,
                    "projection_hash",
                    "advantage_projection_hash",
                    "hash",
                )
            )
        ),
        "| source head revision | {value} |".format(
            value=_markdown_inline(
                _metadata_value(
                    metadata,
                    "head_revision",
                    "source_head_revision",
                    "canon_head_revision",
                )
            )
        ),
        "| source active revision | {value} |".format(
            value=_markdown_inline(
                _metadata_value(
                    metadata,
                    "active_revision",
                    "source_active_revision",
                    "canon_active_revision",
                )
            )
        ),
        "",
    ]


def _rows_for_advantage(
    rows: Iterable[Mapping[str, Any]],
    advantage_id: str,
) -> list[Mapping[str, Any]]:
    return [
        row
        for row in rows
        if str(_value(row, "advantage_id") or "") == advantage_id
    ]


def _module_advantage_map(
    modules: Iterable[Mapping[str, Any]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in modules:
        module_id = _module_id(row)
        advantage_id = _advantage_id(row)
        previous = result.get(module_id)
        if previous is not None and previous != advantage_id:
            raise AdvantageReadableProjectionError(
                f"module {module_id} belongs to multiple advantages"
            )
        result[module_id] = advantage_id
    return result


def _runtime_groups(
    snapshot: Mapping[str, Any],
) -> dict[tuple[str, str], list[Mapping[str, Any]]]:
    module_owners = _module_advantage_map(snapshot["modules"])
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in snapshot["runtime"]:
        raw_advantage = _value(row, "advantage_id")
        if raw_advantage in (None, ""):
            raw_module = _value(row, "module_id", "advantage_module_id")
            raw_advantage = module_owners.get(str(raw_module or ""))
        advantage_id = str(raw_advantage or "").strip()
        if not advantage_id:
            raise AdvantageReadableProjectionError(
                "Advantage runtime row has neither advantage_id nor a "
                "module_id bound to an accepted module definition"
            )
        key = (advantage_id, _branch_id(row))
        groups.setdefault(key, []).append(row)
    for rows in groups.values():
        rows.sort(key=_canonical_json)
    return dict(sorted(groups.items()))


def _render_index(
    snapshot: Mapping[str, Any],
    definition_files: Mapping[str, str],
    module_files: Mapping[str, str],
    runtime_files: Mapping[tuple[str, str], str],
    runtime_groups: Mapping[
        tuple[str, str],
        list[Mapping[str, Any]],
    ],
) -> str:
    definitions = snapshot["definitions"]
    modules = snapshot["modules"]
    knowledge = snapshot["knowledge"]
    metadata = snapshot["metadata"]
    module_counts: dict[str, int] = {}
    knowledge_counts: dict[str, int] = {}
    runtime_counts: dict[str, int] = {}
    for row in modules:
        advantage_id = _advantage_id(row)
        module_counts[advantage_id] = module_counts.get(advantage_id, 0) + 1
    for row in knowledge:
        advantage_id = str(_value(row, "advantage_id") or "")
        if advantage_id:
            knowledge_counts[advantage_id] = (
                knowledge_counts.get(advantage_id, 0) + 1
            )
    for advantage_id, _branch_id_value in runtime_groups:
        runtime_counts[advantage_id] = runtime_counts.get(advantage_id, 0) + 1

    lines = [
        "# 金手指索引",
        "",
        *_projection_header(metadata, document_kind="advantage_index"),
        "## Advantage 定义",
        "",
    ]
    if definitions:
        lines.extend(
            [
                "| Advantage ID | 标题 | Profiles | 状态 | 模块 | 运行态卡 | 知识记录 |",
                "| --- | --- | --- | --- | ---: | ---: | ---: |",
            ]
        )
        for row in definitions:
            advantage_id = _advantage_id(row)
            profiles = _value(row, "profiles", "profile")
            lines.append(
                "| [{identifier}](定义/{link}) | {title} | {profiles} | "
                "{status} | {modules} | {runtime} | {knowledge} |".format(
                    identifier=_markdown_inline(advantage_id),
                    link=definition_files[advantage_id],
                    title=_markdown_inline(_definition_title(row)),
                    profiles=_markdown_inline(profiles),
                    status=_markdown_inline(
                        _value(
                            row,
                            "status",
                            "advantage_status",
                            "definition_status",
                            "canon_status",
                        )
                    ),
                    modules=module_counts.get(advantage_id, 0),
                    runtime=runtime_counts.get(advantage_id, 0),
                    knowledge=knowledge_counts.get(advantage_id, 0),
                )
            )
    else:
        lines.append(
            "- 当前 accepted Advantage projection 没有金手指定义。"
        )

    lines.extend(["", "## 模块", ""])
    if modules:
        lines.extend(
            [
                "| Module ID | Advantage ID | 类型 | 权威状态 | 运行状态 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for row in modules:
            module_id = _module_id(row)
            advantage_id = _advantage_id(row)
            definition_link = definition_files.get(advantage_id)
            advantage_cell = (
                f"[{_markdown_inline(advantage_id)}]"
                f"(定义/{definition_link})"
                if definition_link
                else _code(advantage_id)
            )
            lines.append(
                "| [{module_id}](模块/{link}) | {advantage} | {kind} | "
                "{authority} | {status} |".format(
                    module_id=_markdown_inline(module_id),
                    link=module_files[module_id],
                    advantage=advantage_cell,
                    kind=_markdown_inline(
                        _value(row, "kind", "module_kind")
                    ),
                    authority=_markdown_inline(
                        _value(row, "authority_status")
                    ),
                    status=_markdown_inline(
                        _value(row, "status", "module_status")
                    ),
                )
            )
    else:
        lines.append("- 当前 accepted Advantage projection 没有模块定义。")

    lines.extend(["", "## 运行态", ""])
    if runtime_groups:
        lines.extend(
            [
                "| Advantage ID | Branch | Stage | Enabled | 卡片 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for key, rows in runtime_groups.items():
            advantage_id, branch_id = key
            primary = next(
                (
                    row
                    for row in rows
                    if _value(row, "module_id", "advantage_module_id")
                    in (None, "")
                ),
                rows[0],
            )
            lines.append(
                "| {advantage} | {branch} | {stage} | {enabled} | "
                "[打开](运行态/{link}) |".format(
                    advantage=_code(advantage_id),
                    branch=_code(branch_id),
                    stage=_markdown_inline(_value(primary, "stage")),
                    enabled=_markdown_inline(_enabled_value(primary)),
                    link=runtime_files[key],
                )
            )
    else:
        lines.append("- 当前 accepted Advantage projection 没有运行态。")

    lines.extend(
        [
            "",
            "## 语义边界",
            "",
            "- 定义、模块、运行态、知识与契约只展示 accepted "
            "Advantage projection 的明确字段。",
            "- 名称、profile、空字段或物品属性不会被解释成尚未记录的能力。",
            "- `canon`、`planned`、`rumor`、`misread` 等状态原样保留，"
            "不会互相提升。",
            "- 本目录可随时删除并从 SQLite accepted projection 重建。",
            "",
        ]
    )
    return "\n".join(lines)


def _render_definition(
    definition: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    definition_files: Mapping[str, str],
    module_files: Mapping[str, str],
    runtime_files: Mapping[tuple[str, str], str],
    runtime_groups: Mapping[
        tuple[str, str],
        list[Mapping[str, Any]],
    ],
) -> str:
    advantage_id = _advantage_id(definition)
    modules = _rows_for_advantage(snapshot["modules"], advantage_id)
    anchors = _rows_for_advantage(snapshot["anchors"], advantage_id)
    slots = _rows_for_advantage(snapshot["slots"], advantage_id)
    knowledge = _rows_for_advantage(snapshot["knowledge"], advantage_id)
    contracts = _rows_for_advantage(snapshot["contracts"], advantage_id)
    narrative = _rows_for_advantage(snapshot["narrative"], advantage_id)
    relevant_runtime = [
        key for key in runtime_groups if key[0] == advantage_id
    ]

    lines = [
        f"# {_markdown_inline(_definition_title(definition))}",
        "",
        f"稳定 Advantage ID：{_code(advantage_id)}",
        "",
        *_projection_header(
            snapshot["metadata"],
            document_kind="advantage_definition",
        ),
        "## 基本信息",
        "",
        "| 字段 | accepted 值 |",
        "| --- | --- |",
        f"| advantage_id | {_code(advantage_id)} |",
        f"| profiles | {_markdown_inline(_value(definition, 'profiles', 'profile'))} |",
        f"| status | {_markdown_inline(_value(definition, 'status', 'advantage_status', 'definition_status', 'canon_status'))} |",
        f"| lifecycle_status | {_markdown_inline(_value(definition, 'lifecycle_status'))} |",
        f"| anchor_type | {_markdown_inline(_value(definition, 'anchor_type'))} |",
        f"| acquisition_mode | {_markdown_inline(_value(definition, 'acquisition_mode'))} |",
        f"| uniqueness | {_markdown_inline(_value(definition, 'uniqueness'))} |",
        f"| reveal_stage | {_markdown_inline(_value(definition, 'reveal_stage'))} |",
        "",
        "## 叙事承诺与反制",
        "",
        "| 字段 | accepted 值 |",
        "| --- | --- |",
        f"| promise | {_markdown_inline(_value(definition, 'promise', 'reading_promise'))} |",
        f"| counterplay | {_markdown_inline(_value(definition, 'counterplay'))} |",
        "",
        "## accepted 定义载荷",
        "",
        *_json_block(_public_row(definition)),
        "",
        "## 锚点",
        "",
    ]
    if anchors:
        for row in anchors:
            anchor_id = _value(row, "anchor_id")
            lines.extend(
                [
                    f"### {_code(anchor_id)}",
                    "",
                    "| 字段 | accepted 值 |",
                    "| --- | --- |",
                    f"| anchor_type | {_markdown_inline(_value(row, 'anchor_type'))} |",
                    f"| owner_entity_id | {_code(_value(row, 'owner_entity_id'))} |",
                    f"| binding_state | {_markdown_inline(_value(row, 'binding_state', 'status'))} |",
                    f"| transfer_rule | {_markdown_inline(_value(row, 'transfer_rule'))} |",
                    "",
                    *_json_block(_public_row(row)),
                ]
            )
    else:
        lines.append("- 没有 accepted AdvantageAnchor 记录。")

    lines.extend(["", "## 模块", ""])
    if modules:
        lines.extend(
            [
                "| Module ID | 类型 | 权威状态 | 运行状态 |",
                "| --- | --- | --- | --- |",
            ]
        )
        for row in modules:
            module_id = _module_id(row)
            lines.append(
                "| [{module_id}](../模块/{link}) | {kind} | "
                "{authority} | {status} |".format(
                    module_id=_markdown_inline(module_id),
                    link=module_files[module_id],
                    kind=_markdown_inline(
                        _value(row, "kind", "module_kind")
                    ),
                    authority=_markdown_inline(
                        _value(row, "authority_status")
                    ),
                    status=_markdown_inline(
                        _value(row, "status", "module_status")
                    ),
                )
            )
    else:
        lines.extend(
            [
                "- 没有 accepted AdvantageModuleDefinition 记录。",
                "- 不从标题、profile 或锚点类型补写模块能力。",
            ]
        )

    lines.extend(["", "## 运行态卡", ""])
    if relevant_runtime:
        for key in relevant_runtime:
            lines.append(
                "- branch {branch}: [打开](../运行态/{link})".format(
                    branch=_code(key[1]),
                    link=runtime_files[key],
                )
            )
    else:
        lines.append("- 没有 accepted AdvantageRuntime 记录。")

    lines.extend(["", "## 槽位与成长定义", ""])
    if slots:
        for row in slots:
            lines.extend(
                [
                    f"### {_code(_value(row, 'slot_id'))}",
                    "",
                    *_json_block(_public_row(row)),
                ]
            )
    else:
        lines.append("- 没有 accepted RuntimeSlotDefinition 记录。")

    lines.extend(["", "## 知识与揭示", ""])
    if knowledge:
        lines.extend(
            [
                "| Knowledge ID | Plane | 状态 | Reveal stage | Confidence | Claim |",
                "| --- | --- | --- | --- | ---: | --- |",
            ]
        )
        for row in knowledge:
            lines.append(
                "| {identifier} | {plane} | {status} | {reveal} | "
                "{confidence} | {claim} |".format(
                    identifier=_code(
                        _value(
                            row,
                            "knowledge_id",
                            "claim_id",
                            "knowledge_key",
                        )
                    ),
                    plane=_markdown_inline(
                        _value(row, "knowledge_plane", "plane")
                    ),
                    status=_markdown_inline(
                        _value(
                            row,
                            "status",
                            "knowledge_status",
                            "claim_status",
                        )
                    ),
                    reveal=_markdown_inline(
                        _value(row, "reveal_stage")
                    ),
                    confidence=_markdown_inline(
                        _value(row, "confidence")
                    ),
                    claim=_markdown_inline(_value(row, "claim")),
                )
            )
        lines.extend(["", "完整 accepted 知识载荷：", ""])
        for row in knowledge:
            lines.extend(_json_block(_public_row(row)))
            lines.append("")
    else:
        lines.append("- 没有 accepted AdvantageKnowledge 记录。")

    lines.extend(["", "## 契约", ""])
    if contracts:
        for row in contracts:
            lines.extend(
                [
                    f"### {_code(_value(row, 'contract_id'))}",
                    "",
                    *_json_block(_public_row(row)),
                ]
            )
    else:
        lines.append("- 没有 accepted AdvantageContract 记录。")

    lines.extend(["", "## 叙事循环", ""])
    if narrative:
        for row in narrative:
            lines.extend(_json_block(_public_row(row)))
            lines.append("")
    else:
        lines.append("- 没有独立的 accepted AdvantageNarrativeContract 记录。")

    lines.extend(["", "## accepted 来源字段", ""])
    source_rows = [definition, *anchors, *modules, *knowledge, *contracts]
    source_ids = sorted(
        {
            str(source_id)
            for row in source_rows
            for source_id in (
                _value(row, "source_event_id"),
                _value(row, "event_id"),
            )
            if source_id not in (None, "")
        }
    )
    if source_ids:
        lines.extend(f"- {_code(source_id)}" for source_id in source_ids)
    else:
        lines.append("- 当前投影行没有独立 source/event ID 字段。")
    lines.append("")
    return "\n".join(lines)


def _render_module(
    module: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    definition_files: Mapping[str, str],
    runtime_files: Mapping[tuple[str, str], str],
    runtime_groups: Mapping[
        tuple[str, str],
        list[Mapping[str, Any]],
    ],
) -> str:
    module_id = _module_id(module)
    advantage_id = _advantage_id(module)
    runtime_links = [
        key
        for key, rows in runtime_groups.items()
        if any(
            str(_value(row, "module_id", "advantage_module_id") or "")
            == module_id
            for row in rows
        )
    ]
    definition_link = definition_files.get(advantage_id)
    lines = [
        f"# 模块 {_markdown_inline(module_id)}",
        "",
        *_projection_header(
            snapshot["metadata"],
            document_kind="advantage_module",
        ),
        "## 基本信息",
        "",
        "| 字段 | accepted 值 |",
        "| --- | --- |",
        f"| module_id | {_code(module_id)} |",
        f"| advantage_id | {_code(advantage_id)} |",
        f"| kind | {_markdown_inline(_value(module, 'kind', 'module_kind'))} |",
        f"| authority_status | {_markdown_inline(_value(module, 'authority_status'))} |",
        f"| module_status | {_markdown_inline(_value(module, 'status', 'module_status'))} |",
        f"| reveal_stage | {_markdown_inline(_value(module, 'reveal_stage'))} |",
    ]
    if definition_link:
        lines.extend(
            [
                "",
                f"所属定义：[打开](../定义/{definition_link})",
            ]
        )
    lines.extend(["", "## 执行合同", ""])
    for field in (
        "trigger",
        "preconditions",
        "targets",
        "range",
        "effects",
        "costs",
        "side_effects",
        "failure_modes",
        "counters",
    ):
        lines.extend(
            [
                f"### `{field}`",
                "",
                *_json_block(_value(module, field)),
                "",
            ]
        )
    lines.extend(
        [
            "## accepted 模块载荷",
            "",
            *_json_block(_public_row(module)),
            "",
            "## 运行态卡",
            "",
        ]
    )
    if runtime_links:
        for key in runtime_links:
            lines.append(
                "- branch {branch}: [打开](../运行态/{link})".format(
                    branch=_code(key[1]),
                    link=runtime_files[key],
                )
            )
    else:
        lines.append("- 没有 accepted module runtime 记录。")
    lines.extend(
        [
            "",
            "## 语义边界",
            "",
            "- 空执行字段保持为空；不根据 module kind 或名称生成默认效果。",
            "- 前置、目标、效果、成本、副作用与失败模式按 accepted "
            "载荷逐字段展示。",
            "",
        ]
    )
    return "\n".join(lines)


def _branch_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    advantage_id: str,
    branch_id: str,
) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for row in rows:
        if str(_value(row, "advantage_id") or "") != advantage_id:
            continue
        row_branch = _value(row, "branch_id")
        if row_branch in (None, "") or str(row_branch) == branch_id:
            result.append(row)
    return result


def _render_runtime(
    key: tuple[str, str],
    rows: list[Mapping[str, Any]],
    snapshot: Mapping[str, Any],
    definition_files: Mapping[str, str],
    module_files: Mapping[str, str],
) -> str:
    advantage_id, branch_id = key
    primary = next(
        (
            row
            for row in rows
            if _value(row, "module_id", "advantage_module_id")
            in (None, "")
        ),
        rows[0],
    )
    ledger = _branch_rows(
        snapshot["ledger"],
        advantage_id=advantage_id,
        branch_id=branch_id,
    )
    knowledge = _branch_rows(
        snapshot["knowledge"],
        advantage_id=advantage_id,
        branch_id=branch_id,
    )
    contracts = _branch_rows(
        snapshot["contracts"],
        advantage_id=advantage_id,
        branch_id=branch_id,
    )
    definition_link = definition_files.get(advantage_id)
    lines = [
        f"# {_markdown_inline(advantage_id)} / {_markdown_inline(branch_id)}",
        "",
        *_projection_header(
            snapshot["metadata"],
            document_kind="advantage_runtime",
        ),
        "## 当前运行态摘要",
        "",
        "| 字段 | accepted 值 |",
        "| --- | --- |",
        f"| advantage_id | {_code(advantage_id)} |",
        f"| branch_id | {_code(branch_id)} |",
        f"| stage | {_markdown_inline(_value(primary, 'stage'))} |",
        f"| enabled | {_markdown_inline(_enabled_value(primary))} |",
        f"| status | {_markdown_inline(_value(primary, 'status', 'runtime_status'))} |",
        f"| charges | {_markdown_inline(_value(primary, 'charges'))} |",
        f"| max_charges | {_markdown_inline(_value(primary, 'max_charges'))} |",
        f"| cooldown | {_markdown_inline(_value(primary, 'cooldown', 'cooldown_until'))} |",
        f"| resources | {_markdown_inline(_value(primary, 'resources'))} |",
        f"| pollution | {_markdown_inline(_value(primary, 'pollution'))} |",
        f"| exposure | {_markdown_inline(_value(primary, 'exposure'))} |",
        f"| debt | {_markdown_inline(_value(primary, 'debt'))} |",
        f"| unlocked_modules | {_markdown_inline(_value(primary, 'unlocked_modules'))} |",
    ]
    if definition_link:
        lines.extend(
            [
                "",
                f"所属定义：[打开](../定义/{definition_link})",
            ]
        )
    lines.extend(["", "## accepted 运行态行", ""])
    for row in rows:
        module_id = _value(row, "module_id", "advantage_module_id")
        if module_id not in (None, ""):
            module_name = str(module_id)
            module_link = module_files.get(module_name)
            if module_link:
                lines.append(
                    f"模块：[{_markdown_inline(module_name)}]"
                    f"(../模块/{module_link})"
                )
            else:
                lines.append(f"模块：{_code(module_name)}")
            lines.append("")
        lines.extend(_json_block(_public_row(row)))
        lines.append("")

    lines.extend(["## 账本", ""])
    if ledger:
        for row in ledger:
            entry_id = _value(row, "entry_id", "ledger_entry_id")
            lines.extend(
                [
                    f"### {_code(entry_id)}",
                    "",
                    *_json_block(_public_row(row)),
                    "",
                ]
            )
    else:
        lines.append("- 当前 branch 没有 accepted AdvantageLedger 记录。")

    lines.extend(["", "## 当前知识与揭示", ""])
    if knowledge:
        lines.extend(
            [
                "| Plane | 状态 | Reveal stage | Claim |",
                "| --- | --- | --- | --- |",
            ]
        )
        for row in knowledge:
            lines.append(
                "| {plane} | {status} | {reveal} | {claim} |".format(
                    plane=_markdown_inline(
                        _value(row, "knowledge_plane", "plane")
                    ),
                    status=_markdown_inline(
                        _value(
                            row,
                            "status",
                            "knowledge_status",
                            "claim_status",
                        )
                    ),
                    reveal=_markdown_inline(
                        _value(row, "reveal_stage")
                    ),
                    claim=_markdown_inline(_value(row, "claim")),
                )
            )
    else:
        lines.append("- 当前 branch 没有 accepted AdvantageKnowledge 记录。")

    lines.extend(["", "## 契约与债务", ""])
    if contracts:
        for row in contracts:
            lines.extend(_json_block(_public_row(row)))
            lines.append("")
    else:
        lines.append("- 当前 branch 没有 accepted AdvantageContract 记录。")
    lines.append("")
    return "\n".join(lines)


def build_advantage_readable_files(
    connection: sqlite3.Connection,
) -> tuple[dict[PurePosixPath, bytes], dict[str, Any]]:
    """Render a deterministic Markdown tree from one SQLite read snapshot."""

    snapshot = _snapshot(connection)
    definitions = snapshot["definitions"]
    modules = snapshot["modules"]
    definition_ids = [_advantage_id(row) for row in definitions]
    module_ids = [_module_id(row) for row in modules]
    if len(set(definition_ids)) != len(definition_ids):
        raise AdvantageReadableProjectionError(
            "accepted Advantage projection contains duplicate advantage_id rows"
        )
    if len(set(module_ids)) != len(module_ids):
        raise AdvantageReadableProjectionError(
            "accepted Advantage projection contains duplicate module_id rows"
        )
    definition_files = _filename_map(
        definition_ids,
        prefix="advantage",
    )
    module_files = _filename_map(module_ids, prefix="module")
    runtime_groups = _runtime_groups(snapshot)
    runtime_files = _runtime_filename_map(runtime_groups)

    rendered: dict[PurePosixPath, bytes] = {
        PurePosixPath(ADVANTAGE_READABLE_INDEX): _render_index(
            snapshot,
            definition_files,
            module_files,
            runtime_files,
            runtime_groups,
        ).encode("utf-8")
    }
    for definition in definitions:
        advantage_id = _advantage_id(definition)
        rendered[
            PurePosixPath(
                ADVANTAGE_READABLE_DEFINITIONS,
                definition_files[advantage_id],
            )
        ] = _render_definition(
            definition,
            snapshot,
            definition_files,
            module_files,
            runtime_files,
            runtime_groups,
        ).encode("utf-8")
    for module in modules:
        module_id = _module_id(module)
        rendered[
            PurePosixPath(
                ADVANTAGE_READABLE_MODULES,
                module_files[module_id],
            )
        ] = _render_module(
            module,
            snapshot,
            definition_files,
            runtime_files,
            runtime_groups,
        ).encode("utf-8")
    for key, rows in runtime_groups.items():
        rendered[
            PurePosixPath(
                ADVANTAGE_READABLE_RUNTIME,
                runtime_files[key],
            )
        ] = _render_runtime(
            key,
            rows,
            snapshot,
            definition_files,
            module_files,
        ).encode("utf-8")

    readable_digest = hashlib.sha256()
    for relative, content in sorted(
        rendered.items(),
        key=lambda pair: pair[0].as_posix(),
    ):
        readable_digest.update(relative.as_posix().encode("utf-8"))
        readable_digest.update(b"\0")
        readable_digest.update(content)
        readable_digest.update(b"\0")
    metadata = snapshot["metadata"]
    return rendered, {
        "definition_count": len(definitions),
        "anchor_count": len(snapshot["anchors"]),
        "module_count": len(modules),
        "slot_count": len(snapshot["slots"]),
        "runtime_count": len(snapshot["runtime"]),
        "runtime_card_count": len(runtime_groups),
        "ledger_count": len(snapshot["ledger"]),
        "knowledge_count": len(snapshot["knowledge"]),
        "contract_count": len(snapshot["contracts"]),
        "narrative_contract_count": len(snapshot["narrative"]),
        "advantage_projection_hash": str(
            _metadata_value(
                metadata,
                "projection_hash",
                "advantage_projection_hash",
                "hash",
                default="",
            )
            or ""
        ),
        "advantage_schema_version": str(
            _metadata_value(
                metadata,
                "schema_version",
                "advantage_schema_version",
                "projection_schema_version",
                default="",
            )
            or ""
        ),
        "readable_tree_hash": (
            "advantage_readable_" + readable_digest.hexdigest()
        ),
    }


def _assert_inside(root: Path, target: Path) -> None:
    try:
        resolved_root = root.resolve(strict=False)
        resolved_target = target.resolve(strict=False)
        resolved_target.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise AdvantageReadableProjectionError(
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
        raise AdvantageReadableProjectionError(
            f"readable projection refuses a symlink/reparse directory: {path}"
        )
    if path.exists() and not path.is_dir():
        raise AdvantageReadableProjectionError(
            f"readable projection path is not a directory: {path}"
        )


def _relative_target(stage: Path, relative: PurePosixPath) -> Path:
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise AdvantageReadableProjectionError(
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


def _process_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path))
    with _PROCESS_LOCK_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


def _assert_safe_lock_path(state_dir: Path, lock_path: Path) -> None:
    _assert_inside(state_dir, lock_path)
    if _is_reparse_path(lock_path):
        raise AdvantageReadableProjectionError(
            f"readable projection refuses a symlink/reparse lock: {lock_path}"
        )
    if lock_path.exists() and not lock_path.is_file():
        raise AdvantageReadableProjectionError(
            f"readable projection lock is not a file: {lock_path}"
        )


@contextlib.contextmanager
def _publication_lock(state_dir: Path):
    process_lock = _process_lock(state_dir)
    with process_lock:
        lock_path = state_dir / ".advantage-readable.lock"
        _assert_safe_lock_path(state_dir, lock_path)
        flags = os.O_RDWR | os.O_CREAT
        flags |= int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(str(lock_path), flags, 0o600)
        except OSError as exc:
            raise AdvantageReadableProjectionError(
                f"readable projection lock could not be opened safely: {lock_path}"
            ) from exc
        try:
            # No bytes are written until both the directory entry and the
            # opened handle have been verified.  This closes the common
            # pre-check/open race without ever touching a symlink target.
            _assert_safe_lock_path(state_dir, lock_path)
            handle_stat = os.fstat(descriptor)
            path_stat = lock_path.lstat()
            if not stat.S_ISREG(handle_stat.st_mode) or not stat.S_ISREG(
                path_stat.st_mode
            ):
                raise AdvantageReadableProjectionError(
                    f"readable projection lock is not a regular file: {lock_path}"
                )
            if (
                getattr(handle_stat, "st_ino", 0)
                and getattr(path_stat, "st_ino", 0)
                and (
                    handle_stat.st_dev,
                    handle_stat.st_ino,
                )
                != (
                    path_stat.st_dev,
                    path_stat.st_ino,
                )
            ):
                raise AdvantageReadableProjectionError(
                    f"readable projection lock changed while opening: {lock_path}"
                )
            handle = os.fdopen(descriptor, "r+b")
            descriptor = -1
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        with handle:
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


def _tree_matches_files(
    target: Path,
    files: Mapping[PurePosixPath, bytes],
) -> bool:
    if not target.is_dir() or _is_reparse_path(target):
        return False
    for directory in (
        ADVANTAGE_READABLE_DEFINITIONS,
        ADVANTAGE_READABLE_MODULES,
        ADVANTAGE_READABLE_RUNTIME,
    ):
        child = target / directory
        if not child.is_dir() or _is_reparse_path(child):
            return False
    actual: dict[PurePosixPath, bytes] = {}
    for child in sorted(target.rglob("*")):
        if _is_reparse_path(child):
            raise AdvantageReadableProjectionError(
                "readable projection refuses a symlink/reparse entry: "
                f"{child}"
            )
        if child.is_dir():
            continue
        if not child.is_file():
            return False
        relative = PurePosixPath(child.relative_to(target).as_posix())
        if relative not in files:
            return False
        actual[relative] = child.read_bytes()
    return actual == dict(files)


def _write_staging_tree(
    stage: Path,
    files: Mapping[PurePosixPath, bytes],
) -> None:
    generated_directories = (
        ADVANTAGE_READABLE_DEFINITIONS,
        ADVANTAGE_READABLE_MODULES,
        ADVANTAGE_READABLE_RUNTIME,
    )
    for directory in generated_directories:
        (stage / directory).mkdir(parents=True, exist_ok=False)
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
    for directory in generated_directories:
        _fsync_directory(stage / directory)
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
    target = state_dir / ADVANTAGE_READABLE_DIRECTORY
    backup = state_dir / ".advantage-readable-backup"
    _assert_safe_directory(target, root=project_root)
    _assert_safe_directory(backup, root=project_root)

    with _publication_lock(state_dir):
        _assert_safe_directory(target, root=project_root)
        _assert_safe_directory(backup, root=project_root)
        if backup.exists():
            if target.exists():
                shutil.rmtree(backup)
            else:
                os.replace(backup, target)
        if _tree_matches_files(target, files):
            return target.resolve()

        stage = Path(
            tempfile.mkdtemp(
                prefix=".advantage-readable-stage-",
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


def refresh_advantage_readable_projection(store: Any) -> dict[str, Any]:
    """Rebuild and atomically publish the readable Advantage tree."""

    project_root = Path(store.project_root).expanduser().resolve()
    target = (
        project_root
        / ".plot-rag"
        / ADVANTAGE_READABLE_DIRECTORY
    ).resolve(strict=False)
    if not advantage_readable_projection_enabled(project_root):
        return {
            "status": "disabled",
            "enabled": False,
            "file_count": 0,
            "path": str(target),
        }

    with store.read_connection() as connection:
        connection.execute("BEGIN")
        try:
            files, counts = build_advantage_readable_files(connection)
        finally:
            with contextlib.suppress(sqlite3.Error):
                connection.rollback()
    published = _publish_tree(project_root, files)
    return {
        "status": "completed",
        "enabled": True,
        "file_count": len(files),
        "path": str(published),
        **counts,
    }


def refresh_advantage_readable_projection_safe(
    store: Any,
) -> dict[str, Any]:
    """Return a degraded receipt without affecting accepted canon."""

    try:
        return refresh_advantage_readable_projection(store)
    except Exception as exc:
        project_root = Path(store.project_root).expanduser().resolve()
        return {
            "status": "degraded",
            "enabled": True,
            "file_count": 0,
            "path": str(
                (
                    project_root
                    / ".plot-rag"
                    / ADVANTAGE_READABLE_DIRECTORY
                ).resolve(strict=False)
            ),
            "error_type": type(exc).__name__,
            "message": str(exc),
        }


def remove_advantage_readable_projection(
    project_root: Path | str,
) -> dict[str, Any]:
    """Remove generated Advantage cards and interrupted publication debris."""

    root = Path(project_root).expanduser().resolve()
    state_dir = root / ".plot-rag"
    target = state_dir / ADVANTAGE_READABLE_DIRECTORY
    if not state_dir.exists():
        return {
            "status": "completed",
            "removed": False,
            "path": str(target.resolve(strict=False)),
        }
    _assert_safe_directory(state_dir, root=root)
    _assert_safe_directory(target, root=root)
    backup = state_dir / ".advantage-readable-backup"
    _assert_safe_directory(backup, root=root)
    lock_path = state_dir / ".advantage-readable.lock"
    _assert_safe_lock_path(state_dir, lock_path)
    removed = False
    with _publication_lock(state_dir):
        cleanup_paths = [
            target,
            backup,
            *sorted(state_dir.glob(".advantage-readable-stage-*")),
        ]
        for path in cleanup_paths:
            _assert_safe_directory(path, root=root)
            if path.exists():
                shutil.rmtree(path)
                removed = True
        _fsync_directory(state_dir)
    # Rollback callers stop target-project processes before cleanup.  Remove
    # the now-closed generated lock as part of the disposable surface.
    if lock_path.exists():
        lock_path.unlink()
        removed = True
        _fsync_directory(state_dir)
    return {
        "status": "completed",
        "removed": removed,
        "path": str(target.resolve(strict=False)),
    }


def remove_advantage_readable_projection_safe(
    project_root: Path | str,
) -> dict[str, Any]:
    """Return a degraded cleanup receipt instead of interrupting rollback."""

    root = Path(project_root).expanduser().resolve()
    target = root / ".plot-rag" / ADVANTAGE_READABLE_DIRECTORY
    try:
        return remove_advantage_readable_projection(root)
    except Exception as exc:
        return {
            "status": "degraded",
            "removed": False,
            "path": str(target.resolve(strict=False)),
            "error_type": type(exc).__name__,
            "message": str(exc),
        }


__all__ = [
    "ADVANTAGE_READABLE_DEFINITIONS",
    "ADVANTAGE_READABLE_DIRECTORY",
    "ADVANTAGE_READABLE_INDEX",
    "ADVANTAGE_READABLE_MODULES",
    "ADVANTAGE_READABLE_RUNTIME",
    "ADVANTAGE_READABLE_SCHEMA_VERSION",
    "AdvantageReadableProjectionError",
    "advantage_readable_projection_enabled",
    "build_advantage_readable_files",
    "readable_advantage_projection_enabled",
    "remove_advantage_readable_projection",
    "remove_advantage_readable_projection_safe",
    "refresh_advantage_readable_projection",
    "refresh_advantage_readable_projection_safe",
]
