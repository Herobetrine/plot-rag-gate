#!/usr/bin/env python3
"""Stdio MCP server for the plot RAG v1 lifecycle and initialization runtime."""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote


SERVER_NAME = "plot-rag-state"
SERVER_VERSION = "1.6.4"
PROTOCOL_VERSION = "2025-06-18"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
_ADVANTAGE_VISIBILITIES = frozenset({"generation", "inspection", "raw"})
_ADVANTAGE_GENERATION_CONTROL_KEYS = frozenset(
    {
        "advantage_status",
        "authority_status",
        "control_metadata",
        "control_json",
        "definition_json",
        "experience_contract_json",
        "knowledge_status",
        "lifecycle_status",
        "origin",
        "package_hash",
        "runtime_json",
        "source_claim_ids",
        "source_claim_ids_json",
        "source_event_id",
        "updated_order",
    }
)
_ADVANTAGE_HIDDEN = object()


def _load_runtime():
    scripts = Path(__file__).resolve().parent
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from continuity import ContinuityService
    from plot_rag import locate_project_root
    from state_rag import doctor, dump_state, query_craft, query_state
    import v1_runtime

    return (
        locate_project_root,
        query_state,
        query_craft,
        dump_state,
        doctor,
        ContinuityService,
        v1_runtime,
    )


def _load_advantage_queries():
    """Load the optional Advantage query layer only for Advantage tools."""

    failures: list[ModuleNotFoundError] = []
    for module_name in (
        "continuity.advantages",
        "scripts.continuity.advantages",
    ):
        try:
            return importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name not in {module_name, "scripts"}:
                raise
            failures.append(exc)
    raise RuntimeError(
        "Advantage query runtime is not installed"
    ) from failures[-1]


@contextlib.contextmanager
def _advantage_read_connection(service: Any):
    """Open an existing continuity database without creating or migrating it."""

    store = service.store
    db_path = getattr(store, "db_path", None)
    if db_path is None:
        with store.read_connection() as connection:
            yield connection
        return

    path = Path(db_path).expanduser().resolve(strict=False)
    if not path.is_file():
        raise FileNotFoundError(path)
    raw = os.fspath(path)
    if os.name == "nt":
        if raw.startswith("\\\\?\\UNC\\"):
            raw = "\\\\" + raw[len("\\\\?\\UNC\\") :]
        elif raw.startswith("\\\\?\\"):
            raw = raw[len("\\\\?\\") :]
        uri = "file:" + quote(raw, safe="") + "?mode=ro"
    else:
        uri = path.as_uri() + "?mode=ro"
    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=30.0,
        isolation_level=None,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        yield connection
    finally:
        connection.close()


def _generation_safe_advantage_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        if str(value.get("knowledge_plane") or "").casefold() == "author_plan":
            return _ADVANTAGE_HIDDEN
        safe: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            folded = key.casefold()
            if (
                folded in _ADVANTAGE_GENERATION_CONTROL_KEYS
                or folded.startswith(("author_", "control_"))
                or "author_plan" in folded
                or "canon_revision" in folded
                or folded.startswith(("approval_", "grant_", "proposal_"))
            ):
                continue
            normalized = _generation_safe_advantage_value(child)
            if normalized is not _ADVANTAGE_HIDDEN:
                safe[key] = normalized
        return safe
    if isinstance(value, list):
        safe_list: list[Any] = []
        for child in value:
            normalized = _generation_safe_advantage_value(child)
            if normalized is not _ADVANTAGE_HIDDEN:
                safe_list.append(normalized)
        return safe_list
    return value


def _advantage_query_payload(
    service: Any,
    *,
    helper_name: str,
    advantage_id: str,
    result_key: str | None,
    kwargs: Mapping[str, Any] | None = None,
    allow_none: bool = False,
) -> dict[str, Any]:
    normalized_id = str(advantage_id or "").strip()
    if not normalized_id:
        raise ValueError("advantage_id is required")
    queries = _load_advantage_queries()
    helper = getattr(queries, helper_name, None)
    if not callable(helper):
        raise RuntimeError(f"Advantage query helper is missing: {helper_name}")
    call_kwargs = dict(kwargs or {})
    visibility = str(
        call_kwargs.pop("visibility", "generation") or "generation"
    ).strip().casefold()
    if visibility not in _ADVANTAGE_VISIBILITIES:
        raise ValueError(f"unsupported Advantage visibility mode: {visibility}")
    try:
        with _advantage_read_connection(service) as connection:
            definition_helper = getattr(
                queries,
                "query_advantage_definition",
                None,
            )
            if not callable(definition_helper):
                raise RuntimeError(
                    "Advantage query helper is missing: "
                    "query_advantage_definition"
                )
            definition = definition_helper(connection, normalized_id)
            if definition is None:
                raise ValueError(f"unknown advantage: {normalized_id}")
            if visibility == "generation" and (
                not isinstance(definition, Mapping)
                or str(definition.get("advantage_status") or "") != "canon"
                or str(definition.get("lifecycle_status") or "") != "active"
            ):
                raise ValueError(f"unknown advantage: {normalized_id}")

            if helper_name == "query_advantage_definition":
                result = definition
            else:
                target_kwargs = dict(call_kwargs)
                if visibility == "generation":
                    if helper_name == "query_advantage_anchors":
                        target_kwargs["active_only"] = True
                        target_kwargs["include_noncanon"] = False
                    elif helper_name == "query_advantage_modules":
                        target_kwargs["enabled_only"] = True
                    elif helper_name in {
                        "query_advantage_ledger",
                        "query_advantage_progression",
                    }:
                        modules_helper = getattr(
                            queries,
                            "query_advantage_modules",
                            None,
                        )
                        if not callable(modules_helper):
                            raise RuntimeError(
                                "Advantage query helper is missing: "
                                "query_advantage_modules"
                            )
                        visible_modules = modules_helper(
                            connection,
                            normalized_id,
                            enabled_only=True,
                            include_noncanon=False,
                        )
                        target_kwargs["visible_module_ids"] = [
                            str(row.get("module_id") or "")
                            for row in visible_modules
                            if isinstance(row, Mapping)
                            and str(row.get("module_id") or "")
                        ]
                        if helper_name == "query_advantage_ledger":
                            target_kwargs["visibility"] = "generation"
                        else:
                            target_kwargs["generation_visible_only"] = True
                    elif helper_name == "query_advantage_knowledge":
                        target_kwargs["visibility"] = "generation"
                        target_kwargs["include_noncanon"] = False
                    elif helper_name == "query_advantage_exposure":
                        target_kwargs["generation_visible_only"] = True
                    elif helper_name == "query_advantage_context":
                        target_kwargs["visibility"] = "generation"
                elif helper_name in {
                    "query_advantage_context",
                    "query_advantage_knowledge",
                    "query_advantage_ledger",
                }:
                    target_kwargs["visibility"] = visibility
                result = helper(
                    connection,
                    normalized_id,
                    **target_kwargs,
                )
            if visibility == "generation":
                result = _generation_safe_advantage_value(result)
    except FileNotFoundError as exc:
        raise ValueError(f"unknown advantage: {normalized_id}") from exc
    if result is None and not allow_none:
        raise ValueError(f"unknown advantage: {normalized_id}")
    payload: dict[str, Any] = {
        "status": "ready",
        "advantage_id": normalized_id,
        "visibility": visibility,
    }
    if result_key is None:
        if not isinstance(result, dict):
            raise TypeError(
                f"{helper_name} must return an object for context queries"
            )
        if "definition" in result and result["definition"] is None:
            raise ValueError(f"unknown advantage: {normalized_id}")
        payload.update(result)
    else:
        payload[result_key] = result
        if isinstance(result, list):
            payload["count"] = len(result)
    return payload


def _project_root(value: Any) -> Path:
    locate_project_root, *_ = _load_runtime()
    start = Path(str(value or os.getcwd())).expanduser().resolve()
    root = locate_project_root(start)
    if root is None or not (root / ".plot-rag" / "config.json").is_file():
        if value is not None and str(value).strip():
            raise ValueError(
                "no .plot-rag/config.json found at or above explicitly "
                f"provided project_root {start}"
            )
        raise ValueError(
            f"no .plot-rag/config.json found from {start}; pass project_root explicitly"
        )
    return root


def _plain_path(value: Any, *, default: Any = None) -> Path:
    selected = value if value is not None and str(value).strip() else default
    if selected is None or not str(selected).strip():
        selected = os.getcwd()
    return Path(str(selected)).expanduser().resolve(strict=False)


def _init_paths(arguments: dict[str, Any]) -> tuple[Path, Path | None]:
    project_value = arguments.get("project_root")
    workspace = _plain_path(
        arguments.get("workspace_root"),
        default=project_value or os.getcwd(),
    )
    if project_value is None or not str(project_value).strip():
        return workspace, None
    project = Path(str(project_value)).expanduser()
    if not project.is_absolute():
        project = workspace / project
    return workspace, project.resolve(strict=False)


def _relative_to_workspace(value: Any, workspace: Path) -> Path | None:
    if value is None or not str(value).strip():
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = workspace / path
    return path.resolve(strict=False)


def _source_paths(values: Any, workspace: Path) -> list[Path] | None:
    if values is None:
        return None
    return [
        path
        for value in values
        if (path := _relative_to_workspace(value, workspace)) is not None
    ]


def _one_report_input(
    arguments: dict[str, Any],
    *,
    report_key: str,
    path_key: str,
) -> dict[str, Any] | Path:
    report = arguments.get(report_key)
    path = arguments.get(path_key)
    if report is not None and path is not None:
        raise ValueError(
            f"pass either {report_key} or {path_key}, not both"
        )
    if report is not None:
        if not isinstance(report, dict):
            raise ValueError(f"{report_key} must be an object")
        return dict(report)
    if path is not None and str(path).strip():
        return _plain_path(path)
    raise ValueError(f"{report_key} or {path_key} is required")


def _schema(
    properties: dict[str, Any],
    required: Iterable[str] = (),
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    required_items = list(required)
    if required_items:
        schema["required"] = required_items
    return schema


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: Iterable[str] = (),
    *,
    read_only: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "description": description,
        "inputSchema": _schema(properties, required),
    }
    if read_only:
        result["annotations"] = {"readOnlyHint": True}
    return result


PROJECT_ROOT = {
    "project_root": {
        "type": "string",
        "minLength": 1,
        "description": "Project directory containing .plot-rag/config.json.",
    }
}
SOURCE_MANIFEST_PLAN = {
    "type": "object",
    "minProperties": 1,
    "description": (
        "Complete plot-rag-source-manifest-migration-plan/v1 object. "
        "The continuity service performs the authoritative plan validation."
    ),
}
POWER_SPEC_DOCUMENT = {
    "type": "object",
    "minProperties": 1,
    "description": (
        "Complete plot-rag-power/v1 aggregate. Stable entities, lifecycle "
        "events, and proposal hashes are compiled locally."
    ),
}
INIT_ROOTS = {
    "project_root": {
        "type": "string",
        "description": (
            "Initialization target. It may be new and does not need "
            ".plot-rag/config.json yet."
        ),
    },
    "workspace_root": {
        "type": "string",
        "description": (
            "Workspace that owns initialization session storage. Defaults to "
            "project_root, then the server working directory."
        ),
    },
}
INIT_PROFILE_PROPERTIES = {
    "mode": {
        "type": "string",
        "enum": ["auto", "new", "ingest", "hybrid"],
        "default": "auto",
    },
    "target_profile": {
        "type": "string",
        "enum": [
            "plot_ready",
            "world_bible",
            "normalize_only",
            "continuity_ready",
        ],
        "default": "plot_ready",
    },
    "interaction_profile": {
        "type": "string",
        "enum": ["minimal", "balanced", "deep"],
        "default": "balanced",
    },
    "seed": {
        "description": "Free-form text or a structured initialization seed."
    },
    "seed_file": {"type": "string"},
    "sources": {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "uniqueItems": True,
    },
    "expected_canon_revision": {"type": "integer", "minimum": 0},
}
POWER_TEMPORAL_PROPERTIES = {
    "system_id": {"type": "string"},
    "chapter_no": {"type": "integer", "minimum": 1},
    "scene_index": {"type": "integer", "minimum": 0},
    "branch_id": {"type": "string"},
    "knowledge_planes": {
        "type": "array",
        "items": {
            "type": "string",
            "enum": [
                "objective",
                "actor_belief",
                "public_narrative",
                "reader_disclosed",
                "author_plan",
            ],
        },
        "uniqueItems": True,
    },
    "include_provisional": {"type": "boolean", "default": False},
}
POWER_LIST_TEMPORAL_PROPERTIES = {
    name: schema
    for name, schema in POWER_TEMPORAL_PROPERTIES.items()
    if name != "system_id"
}
POWER_ACTOR_PROPERTIES = {
    "mention": {"type": "string"},
    "entity_id": {"type": "string"},
}
KNOWLEDGE_PLANE = {
    "type": "string",
    "enum": [
        "objective",
        "actor_belief",
        "public_narrative",
        "reader_disclosed",
        "author_plan",
    ],
}
ADVANTAGE_VISIBILITY = {
    "type": "string",
    "enum": ["generation", "inspection", "raw"],
    "default": "generation",
}
ADVANTAGE_ID = {
    "advantage_id": {
        "type": "string",
        "minLength": 1,
        "description": "Stable ID of one accepted Advantage projection.",
    }
}
ADVANTAGE_BRANCH = {
    "branch_id": {
        "type": "string",
        "minLength": 1,
        "default": "main",
    }
}
ADVANTAGE_KNOWLEDGE_SCOPE = {
    "knowledge_plane": KNOWLEDGE_PLANE,
    "observer_entity_id": {"type": "string"},
    "visibility": ADVANTAGE_VISIBILITY,
}


TOOLS: list[dict[str, Any]] = [
    _tool(
        "prepare_plot_turn",
        (
            "Prepare a plot-generation turn. Config v3 binds the receipt to the "
            "active canon revision and injects accepted long-form continuity; "
            "older configs retain their compatible behavior."
        ),
        {
            **PROJECT_ROOT,
            "prompt": {"type": "string", "minLength": 1},
            "request_id": {"type": "string"},
            "session_id": {"type": "string"},
            "turn_id": {"type": "string"},
            "artifact_stage": {
                "type": "string",
                "enum": [
                    "brainstorm",
                    "outline",
                    "draft",
                    "final",
                    "published",
                    "bootstrap",
                ],
            },
            "branch_id": {"type": "string"},
            "chapter_no": {"type": "integer", "minimum": 1},
            "scene_index": {"type": "integer", "minimum": 0},
            "artifact_id": {"type": "string"},
            "task": {"type": "string"},
        },
        ("prompt", "project_root"),
    ),
    _tool(
        "commit_plot_turn",
        (
            "Process a finalized assistant draft. Config v3 creates a validated "
            "proposal only; v1/v2 projects retain the legacy automatic commit. "
            "A v3 proposal must later consume a host-issued approval_id."
        ),
        {
            **PROJECT_ROOT,
            "assistant_text": {"type": "string", "minLength": 1},
            "receipt_id": {"type": "string"},
            "request_id": {"type": "string"},
            "prompt": {"type": "string"},
            "session_id": {"type": "string"},
            "turn_id": {"type": "string"},
        },
        ("assistant_text", "project_root"),
    ),
    _tool(
        "propose_plot_turn",
        (
            "Create the validated Stop-stage proposal for a finalized assistant "
            "draft. This is the canonical config-v3 name; commit_plot_turn "
            "remains as the compatibility alias."
        ),
        {
            **PROJECT_ROOT,
            "assistant_text": {"type": "string", "minLength": 1},
            "receipt_id": {"type": "string"},
            "request_id": {"type": "string"},
            "prompt": {"type": "string"},
            "session_id": {"type": "string"},
            "turn_id": {"type": "string"},
        },
        ("assistant_text", "project_root"),
    ),
    _tool(
        "query_plot_state",
        (
            "Run the compatible hybrid query over recorded plot state for a "
            "focused character, relationship, location, inventory, or time need."
        ),
        {
            **PROJECT_ROOT,
            "query": {"type": "string", "minLength": 1},
            "categories": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            },
            "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        ("query", "project_root"),
    ),
    _tool(
        "get_plot_performance_status",
        (
            "Read redacted prepare, extraction, cache, and remote telemetry "
            "without changing project state."
        ),
        PROJECT_ROOT,
        ("project_root",),
        read_only=True,
    ),
    _tool(
        "run_plot_performance_benchmark",
        (
            "Run the deterministic offline v1.5 performance harness in "
            "temporary storage and verify accepted canon remains unchanged."
        ),
        {
            **PROJECT_ROOT,
            "manifest": {"type": "object"},
            "manifest_path": {"type": "string", "minLength": 1},
            "options": {"type": "object"},
        },
        ("project_root",),
        read_only=True,
    ),
    _tool(
        "compare_plot_prepare_paths",
        (
            "Compare metric-level deltas between two redacted performance "
            "reports. Each side may be an embedded object or local JSON path."
        ),
        {
            "left_report": {"type": "object"},
            "left_path": {"type": "string", "minLength": 1},
            "right_report": {"type": "object"},
            "right_path": {"type": "string", "minLength": 1},
        },
        read_only=True,
    ),
    _tool(
        "list_plot_extraction_jobs",
        "List durable extraction jobs and their immutable hash bindings.",
        {
            **PROJECT_ROOT,
            "statuses": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "queued",
                        "running",
                        "succeeded",
                        "failed",
                        "cancelled",
                    ],
                },
                "uniqueItems": True,
            },
            "branch_id": {"type": "string"},
            "sequence_no": {"type": "integer", "minimum": 0},
            "receipt_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "offset": {"type": "integer", "minimum": 0},
        },
        ("project_root",),
        read_only=True,
    ),
    _tool(
        "inspect_plot_extraction_job",
        "Inspect one durable extraction job without changing its lease or state.",
        {
            **PROJECT_ROOT,
            "job_id": {"type": "string", "minLength": 1},
        },
        ("project_root", "job_id"),
        read_only=True,
    ),
    _tool(
        "retry_plot_extraction_job",
        "CAS a failed extraction job back to queued at its current epoch.",
        {
            **PROJECT_ROOT,
            "job_id": {"type": "string", "minLength": 1},
            "expected_attempt_count": {"type": "integer", "minimum": 0},
            "next_attempt_at": {"type": "string", "minLength": 1},
        },
        ("project_root", "job_id", "expected_attempt_count"),
    ),
    _tool(
        "propose_event_experience",
        (
            "Propose one non-canon EventExperienceContract for an existing "
            "EventSeed using control-plane revision CAS."
        ),
        {
            **PROJECT_ROOT,
            "contract": {"type": "object"},
            "expected_control_revision": {"type": "integer", "minimum": 0},
            "idempotency_key": {"type": "string", "minLength": 1},
        },
        (
            "project_root",
            "contract",
            "expected_control_revision",
            "idempotency_key",
        ),
    ),
    _tool(
        "inspect_event_experience",
        "Read one event-experience contract and the current control revision.",
        {
            **PROJECT_ROOT,
            "contract_id": {"type": "string", "minLength": 1},
        },
        ("project_root", "contract_id"),
        read_only=True,
    ),
    _tool(
        "lock_event_experience",
        "Lock one proposed EventExperienceContract using revision and hash CAS.",
        {
            **PROJECT_ROOT,
            "contract_id": {"type": "string", "minLength": 1},
            "expected_contract_hash": {
                "type": "string",
                "minLength": 64,
            },
            "expected_control_revision": {"type": "integer", "minimum": 0},
            "idempotency_key": {"type": "string", "minLength": 1},
        },
        (
            "project_root",
            "contract_id",
            "expected_control_revision",
            "idempotency_key",
        ),
    ),
    _tool(
        "review_event_experience",
        (
            "Record a post-generation experience review whose quotes are "
            "verified against the supplied assistant text."
        ),
        {
            **PROJECT_ROOT,
            "review": {"type": "object"},
            "assistant_text": {"type": "string", "minLength": 1},
            "expected_control_revision": {"type": "integer", "minimum": 0},
            "idempotency_key": {"type": "string", "minLength": 1},
        },
        (
            "project_root",
            "review",
            "assistant_text",
            "expected_control_revision",
            "idempotency_key",
        ),
    ),
    _tool(
        "query_item_definition",
        "Read one accepted strong-typed item definition.",
        {
            **PROJECT_ROOT,
            "item_definition_id": {"type": "string", "minLength": 1},
        },
        ("project_root", "item_definition_id"),
        read_only=True,
    ),
    _tool(
        "query_item_instance",
        "Read one accepted unique item instance.",
        {
            **PROJECT_ROOT,
            "item_instance_id": {"type": "string", "minLength": 1},
        },
        ("project_root", "item_instance_id"),
        read_only=True,
    ),
    _tool(
        "query_item_function",
        "Read one accepted item function and its active bindings.",
        {
            **PROJECT_ROOT,
            "function_id": {"type": "string", "minLength": 1},
            "item_instance_id": {"type": "string"},
            "stack_id": {"type": "string"},
        },
        ("project_root", "function_id"),
        read_only=True,
    ),
    _tool(
        "query_item_runtime",
        "Read durability, charges, cooldowns, flags, and function runtime.",
        {
            **PROJECT_ROOT,
            "item_instance_id": {"type": "string", "minLength": 1},
        },
        ("project_root", "item_instance_id"),
        read_only=True,
    ),
    _tool(
        "query_item_custody",
        "Read legal ownership and physical custody for an item instance or stack.",
        {
            **PROJECT_ROOT,
            "subject_type": {
                "type": "string",
                "enum": ["item_instance", "item_stack"],
            },
            "subject_id": {"type": "string", "minLength": 1},
        },
        ("project_root", "subject_type", "subject_id"),
        read_only=True,
    ),
    _tool(
        "query_actor_inventory",
        (
            "Read owned, custodied, carried, stored, equipped, and legacy "
            "inventory for one actor."
        ),
        {
            **PROJECT_ROOT,
            "actor_entity_id": {"type": "string"},
            "mention": {"type": "string"},
        },
        ("project_root",),
        read_only=True,
    ),
    _tool(
        "query_item_history",
        "Read accepted item use history with optional item, stack, or actor filters.",
        {
            **PROJECT_ROOT,
            "item_instance_id": {"type": "string"},
            "stack_id": {"type": "string"},
            "actor_entity_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        },
        ("project_root",),
        read_only=True,
    ),
    _tool(
        "query_item_observations",
        "Read knowledge-plane-scoped item observations.",
        {
            **PROJECT_ROOT,
            "item_instance_id": {"type": "string"},
            "stack_id": {"type": "string"},
            "observer_entity_id": {"type": "string"},
            "knowledge_plane": {
                "type": "string",
                "enum": [
                    "objective",
                    "actor_belief",
                    "public_narrative",
                    "reader_disclosed",
                    "author_plan",
                ],
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "visibility": ADVANTAGE_VISIBILITY,
        },
        ("project_root",),
        read_only=True,
    ),
    _tool(
        "query_advantage_definition",
        (
            "Read one accepted special-item or golden-finger definition, "
            "including its stable profiles, promise, limits, and counterplay."
        ),
        {
            **PROJECT_ROOT,
            **ADVANTAGE_ID,
            "visibility": ADVANTAGE_VISIBILITY,
        },
        ("project_root", "advantage_id"),
        read_only=True,
    ),
    _tool(
        "query_advantage_anchors",
        (
            "Read the stable carriers, owners, binding states, transfer rules, "
            "and story coordinates of one Advantage."
        ),
        {
            **PROJECT_ROOT,
            **ADVANTAGE_ID,
            "include_inactive": {"type": "boolean", "default": False},
            "include_noncanon": {"type": "boolean", "default": False},
            "visibility": ADVANTAGE_VISIBILITY,
        },
        ("project_root", "advantage_id"),
        read_only=True,
    ),
    _tool(
        "query_advantage_runtime",
        (
            "Read the current stage, resources, charges, cooldowns, pollution, "
            "exposure, and branch-scoped runtime for one Advantage."
        ),
        {
            **PROJECT_ROOT,
            **ADVANTAGE_ID,
            **ADVANTAGE_BRANCH,
            "visibility": ADVANTAGE_VISIBILITY,
        },
        ("project_root", "advantage_id"),
        read_only=True,
    ),
    _tool(
        "query_advantage_modules",
        (
            "Read declared Advantage modules with triggers, prerequisites, "
            "costs, effects, side effects, and failure modes."
        ),
        {
            **PROJECT_ROOT,
            **ADVANTAGE_ID,
            "enabled_only": {"type": "boolean", "default": False},
            "visibility": ADVANTAGE_VISIBILITY,
        },
        ("project_root", "advantage_id"),
        read_only=True,
    ),
    _tool(
        "query_advantage_ledger",
        (
            "Read accepted Advantage reward, cost, conversion, loss, and "
            "provenance ledger entries."
        ),
        {
            **PROJECT_ROOT,
            **ADVANTAGE_ID,
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "default": 50,
            },
            "entry_kind": {"type": "string"},
            "branch_id": {"type": "string"},
            "visibility": ADVANTAGE_VISIBILITY,
        },
        ("project_root", "advantage_id"),
        read_only=True,
    ),
    _tool(
        "query_advantage_knowledge",
        (
            "Read objective, actor-belief, public, reader-disclosed, or "
            "author-plan Advantage claims without crossing knowledge planes."
        ),
        {
            **PROJECT_ROOT,
            **ADVANTAGE_ID,
            **ADVANTAGE_KNOWLEDGE_SCOPE,
            "include_noncanon": {"type": "boolean", "default": False},
        },
        ("project_root", "advantage_id"),
        read_only=True,
    ),
    _tool(
        "query_advantage_progression",
        (
            "Read the current Advantage stage, slots, capacity, unlock graph, "
            "satisfied prerequisites, and blockers."
        ),
        {
            **PROJECT_ROOT,
            **ADVANTAGE_ID,
            **ADVANTAGE_BRANCH,
            "visibility": ADVANTAGE_VISIBILITY,
        },
        ("project_root", "advantage_id"),
        read_only=True,
    ),
    _tool(
        "query_advantage_exposure",
        (
            "Read branch-scoped exposure, pollution, trace, contract debt, "
            "counterplay, and threshold risk for one Advantage."
        ),
        {
            **PROJECT_ROOT,
            **ADVANTAGE_ID,
            **ADVANTAGE_BRANCH,
            "visibility": ADVANTAGE_VISIBILITY,
        },
        ("project_root", "advantage_id"),
        read_only=True,
    ),
    _tool(
        "query_special_item_context",
        (
            "Build the mandatory combined definition, module, runtime, ledger, "
            "knowledge, progression, exposure, and narrative-contract context "
            "for one special item before plot design."
        ),
        {
            **PROJECT_ROOT,
            **ADVANTAGE_ID,
            **ADVANTAGE_BRANCH,
            **ADVANTAGE_KNOWLEDGE_SCOPE,
            "ledger_limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "default": 10,
            },
        },
        ("project_root", "advantage_id"),
        read_only=True,
    ),
    _tool(
        "validate_power_spec_change",
        (
            "Strictly normalize and compile a complete plot-rag-power/v1 "
            "aggregate without project access or persistent writes."
        ),
        {
            "power_spec": POWER_SPEC_DOCUMENT,
        },
        ("power_spec",),
        read_only=True,
    ),
    _tool(
        "preview_power_spec_change",
        (
            "Preview the deterministic entities, events, and hashes for a "
            "standalone PowerSpec proposal at one accepted canon revision."
        ),
        {
            **PROJECT_ROOT,
            "power_spec": POWER_SPEC_DOCUMENT,
            "expected_canon_revision": {"type": "integer", "minimum": 0},
        },
        ("project_root", "power_spec", "expected_canon_revision"),
        read_only=True,
    ),
    _tool(
        "propose_power_spec_change",
        (
            "Register deterministic PowerSpec entities and freeze an immutable "
            "power_spec_change proposal. This tool does not issue or consume "
            "an approval grant and does not change accepted canon."
        ),
        {
            **PROJECT_ROOT,
            "power_spec": POWER_SPEC_DOCUMENT,
            "expected_canon_revision": {"type": "integer", "minimum": 0},
            "idempotency_key": {"type": "string", "minLength": 1},
        },
        (
            "project_root",
            "power_spec",
            "expected_canon_revision",
            "idempotency_key",
        ),
    ),
    _tool(
        "list_power_systems",
        "List accepted power systems, profiles, namespaces, and modeling status.",
        {
            **PROJECT_ROOT,
            **POWER_LIST_TEMPORAL_PROPERTIES,
        },
        ("project_root",),
        read_only=True,
    ),
    _tool(
        "query_power_state",
        (
            "Query accepted actor progression, abilities, resources, cooldowns, "
            "status effects, bindings, qualifications, and observations."
        ),
        {
            **PROJECT_ROOT,
            **POWER_ACTOR_PROPERTIES,
            **POWER_TEMPORAL_PROPERTIES,
            "track_id": {"type": "string"},
            "ability_id": {"type": "string"},
            "resource_id": {"type": "string"},
            "include_historical": {"type": "boolean", "default": False},
        },
        ("project_root",),
        read_only=True,
    ),
    _tool(
        "query_progression_path",
        (
            "Return accepted legal progression edges, satisfied prerequisites, "
            "blocking requirements, and failure outcomes."
        ),
        {
            **PROJECT_ROOT,
            **POWER_ACTOR_PROPERTIES,
            **POWER_TEMPORAL_PROPERTIES,
            "track_id": {"type": "string"},
            "target_rank_id": {"type": "string"},
        },
        ("project_root",),
        read_only=True,
    ),
    _tool(
        "explain_power_action",
        (
            "Explain deterministically whether an actor can use an ability or "
            "take another power action at the requested story coordinate."
        ),
        {
            **PROJECT_ROOT,
            **POWER_ACTOR_PROPERTIES,
            **POWER_TEMPORAL_PROPERTIES,
            "action_id": {"type": "string", "minLength": 1},
            "track_id": {"type": "string"},
            "ability_id": {"type": "string"},
            "resource_id": {"type": "string"},
            "target_rank_id": {"type": "string"},
        },
        ("project_root", "action_id"),
        read_only=True,
    ),
    _tool(
        "compare_power_conditions",
        (
            "Compare two actors through a conditional advantage matrix; never "
            "returns an unconditional winner."
        ),
        {
            **PROJECT_ROOT,
            **POWER_TEMPORAL_PROPERTIES,
            "left_mention": {"type": "string"},
            "left_entity_id": {"type": "string"},
            "right_mention": {"type": "string"},
            "right_entity_id": {"type": "string"},
            "conditions": {"type": "object"},
        },
        ("project_root",),
        read_only=True,
    ),
    _tool(
        "query_plot_craft",
        "Retrieve task-relevant plot-design method cards without changing canon.",
        {
            **PROJECT_ROOT,
            "query": {"type": "string", "minLength": 1},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 8},
        },
        ("query", "project_root"),
        read_only=True,
    ),
    _tool(
        "get_plot_state",
        "Read the compatible current projection and event history.",
        {
            **PROJECT_ROOT,
            "subject": {"type": "string"},
            "category": {"type": "string"},
        },
        ("project_root",),
    ),
    _tool(
        "doctor_plot_rag",
        (
            "Inspect configuration, schemas, storage, and remote readiness "
            "without exposing secrets."
        ),
        PROJECT_ROOT,
        ("project_root",),
        read_only=True,
    ),
    _tool(
        "list_plot_proposals",
        "List lifecycle proposals, optionally filtered by canon status or branch.",
        {
            **PROJECT_ROOT,
            "canon_status": {
                "type": "string",
                "enum": ["proposed", "accepted", "rejected", "retracted"],
            },
            "branch_id": {"type": "string"},
        },
        ("project_root",),
    ),
    _tool(
        "inspect_plot_proposal",
        "Read one immutable lifecycle proposal, its events, payload, and issues.",
        {
            **PROJECT_ROOT,
            "proposal_id": {"type": "string", "minLength": 1},
        },
        ("project_root", "proposal_id"),
    ),
    _tool(
        "reject_plot_proposal",
        "Reject a proposed lifecycle artifact while preserving its audit history.",
        {
            **PROJECT_ROOT,
            "proposal_id": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "minLength": 1},
            "idempotency_key": {"type": "string", "minLength": 1},
        },
        ("project_root", "proposal_id", "reason"),
    ),
    _tool(
        "accept_plot_proposal",
        (
            "Accept a proposal by consuming a one-time approval_id issued by a "
            "trusted host outside MCP. Proposal kind selects accept, "
            "accept_power_spec, accept_source_manifest, or "
            "accept_initialization. This server never issues approval grants."
        ),
        {
            **INIT_ROOTS,
            "proposal_id": {"type": "string", "minLength": 1},
            "approval_id": {"type": "string", "minLength": 1},
            "expected_canon_revision": {"type": "integer", "minimum": 0},
        },
        (
            "project_root",
            "proposal_id",
            "approval_id",
            "expected_canon_revision",
        ),
    ),
    _tool(
        "retract_plot_proposal",
        (
            "Retract an accepted proposal by consuming a host-issued approval_id "
            "and replaying the resulting immutable retraction commit."
        ),
        {
            **PROJECT_ROOT,
            "proposal_id": {"type": "string", "minLength": 1},
            "approval_id": {"type": "string", "minLength": 1},
            "expected_canon_revision": {"type": "integer", "minimum": 0},
            "reason": {"type": "string", "minLength": 1},
        },
        (
            "project_root",
            "proposal_id",
            "approval_id",
            "expected_canon_revision",
            "reason",
        ),
    ),
    _tool(
        "query_plot_state_at",
        (
            "Query accepted continuity at a chapter/scene boundary, with optional "
            "historical, provisional branch, and relation projections."
        ),
        {
            **PROJECT_ROOT,
            "mention": {"type": "string"},
            "entity_id": {"type": "string"},
            "fact_type": {"type": "string"},
            "scope": {
                "type": "string",
                "enum": ["current", "planned", "historical", "timeless"],
            },
            "chapter_no": {"type": "integer", "minimum": 1},
            "scene_index": {"type": "integer", "minimum": 0},
            "branch_id": {"type": "string"},
            "include_historical": {"type": "boolean", "default": False},
            "include_provisional": {"type": "boolean", "default": False},
            "include_relations": {"type": "boolean", "default": True},
        },
        ("project_root",),
    ),
    _tool(
        "replay_plot_continuity",
        "Rebuild deterministic accepted projections and refresh the continuity snapshot.",
        PROJECT_ROOT,
        ("project_root",),
    ),
    _tool(
        "get_source_manifest_status",
        (
            "Read the accepted source-manifest ledger, active projection, "
            "history, and canon revisions without changing canon."
        ),
        PROJECT_ROOT,
        ("project_root",),
        read_only=True,
    ),
    _tool(
        "preview_source_manifest_change",
        (
            "Validate a complete source-manifest migration plan against the "
            "active canon revision without saving a proposal."
        ),
        {
            **PROJECT_ROOT,
            "plan": SOURCE_MANIFEST_PLAN,
            "expected_canon_revision": {"type": "integer", "minimum": 0},
        },
        ("project_root", "plan", "expected_canon_revision"),
        read_only=True,
    ),
    _tool(
        "propose_source_manifest_change",
        (
            "Freeze a validated source-manifest migration plan as an immutable "
            "proposal. This tool does not issue or consume an approval grant "
            "and does not change accepted canon."
        ),
        {
            **PROJECT_ROOT,
            "plan": SOURCE_MANIFEST_PLAN,
            "expected_canon_revision": {"type": "integer", "minimum": 0},
            "idempotency_key": {"type": "string", "minLength": 1},
        },
        (
            "project_root",
            "plan",
            "expected_canon_revision",
            "idempotency_key",
        ),
    ),
    _tool(
        "start_story_initialization",
        (
            "Start a persistent new/ingest/hybrid initialization session. The "
            "target may be an empty directory with no plot-rag config."
        ),
        {
            **INIT_ROOTS,
            **INIT_PROFILE_PROPERTIES,
            "idempotency_key": {"type": "string", "minLength": 1},
            "session_id": {"type": "string", "minLength": 1},
        },
        ("idempotency_key",),
    ),
    _tool(
        "dry_run_story_initialization",
        (
            "Build an in-memory initialization report with zero session, "
            "database, canon, config, or source-file writes."
        ),
        {**INIT_ROOTS, **INIT_PROFILE_PROPERTIES},
        read_only=True,
    ),
    _tool(
        "advance_story_initialization",
        "Advance a persistent initialization session using revision CAS.",
        {
            **INIT_ROOTS,
            "session_id": {"type": "string", "minLength": 1},
            "expected_session_revision": {"type": "integer", "minimum": 0},
            "idempotency_key": {"type": "string", "minLength": 1},
        },
        (
            "session_id",
            "expected_session_revision",
            "idempotency_key",
        ),
    ),
    _tool(
        "answer_story_initialization",
        "Submit one or more initialization decision answers using revision CAS.",
        {
            **INIT_ROOTS,
            "session_id": {"type": "string", "minLength": 1},
            "answers": {
                "type": "object",
                "minProperties": 1,
                "additionalProperties": True,
            },
            "expected_session_revision": {"type": "integer", "minimum": 0},
            "idempotency_key": {"type": "string", "minLength": 1},
        },
        (
            "session_id",
            "answers",
            "expected_session_revision",
            "idempotency_key",
        ),
    ),
    _tool(
        "inspect_story_initialization",
        "Read a session view without advancing or changing initialization state.",
        {
            **INIT_ROOTS,
            "session_id": {"type": "string", "minLength": 1},
            "view": {
                "type": "string",
                "enum": [
                    "summary",
                    "sources",
                    "conflicts",
                    "gaps",
                    "questions",
                    "normalized",
                    "diff",
                    "proposal",
                    "journal",
                    "checkpoints",
                    "all",
                ],
                "default": "summary",
            },
        },
        ("session_id",),
        read_only=True,
    ),
    _tool(
        "build_story_initialization_proposal",
        "Freeze a reviewed InitializationBundle proposal without changing canon.",
        {
            **INIT_ROOTS,
            "session_id": {"type": "string", "minLength": 1},
            "expected_session_revision": {"type": "integer", "minimum": 0},
            "idempotency_key": {"type": "string", "minLength": 1},
        },
        (
            "session_id",
            "expected_session_revision",
            "idempotency_key",
        ),
    ),
    _tool(
        "apply_story_initialization",
        (
            "Accept and materialize a frozen InitializationBundle as one "
            "recoverable saga by "
            "consuming a host-issued approval_id. If v2 power definitions still "
            "need their first grant, omission of approval_id returns "
            "POWER_SPEC_APPROVAL_REQUIRED without consuming a grant. MCP has "
            "no grant issuer."
        ),
        {
            **INIT_ROOTS,
            "proposal_id": {"type": "string", "minLength": 1},
            "approval_id": {"type": "string", "minLength": 1},
            "expected_canon_revision": {"type": "integer", "minimum": 0},
            "idempotency_key": {"type": "string", "minLength": 1},
        },
        (
            "proposal_id",
            "expected_canon_revision",
            "idempotency_key",
        ),
    ),
    _tool(
        "verify_story_initialization",
        "Verify accepted initialization materialization and projection hashes.",
        {
            **PROJECT_ROOT,
            "commit_id": {"type": "string", "minLength": 1},
        },
        ("commit_id",),
    ),
    _tool(
        "list_story_initializations",
        "List persisted initialization sessions without advancing them.",
        {
            **INIT_ROOTS,
            "active_only": {"type": "boolean", "default": False},
        },
        read_only=True,
    ),
    _tool(
        "cancel_story_initialization",
        "Cancel an initialization session while leaving canon unchanged.",
        {
            **INIT_ROOTS,
            "session_id": {"type": "string", "minLength": 1},
            "expected_session_revision": {"type": "integer", "minimum": 0},
            "idempotency_key": {"type": "string", "minLength": 1},
            "reason": {"type": "string"},
        },
        (
            "session_id",
            "expected_session_revision",
            "idempotency_key",
        ),
    ),
    _tool(
        "refresh_longform_index",
        "Incrementally refresh the accepted long-form authority index.",
        {
            **PROJECT_ROOT,
            "with_embeddings": {"type": "boolean", "default": False},
        },
        ("project_root",),
    ),
    _tool(
        "recover_longform_projection",
        (
            "Recover one abandoned long-form projection journal row and retry "
            "its vector projection without changing canon."
        ),
        {
            **PROJECT_ROOT,
            "run_id": {"type": "string", "minLength": 1},
        },
        ("project_root", "run_id"),
    ),
    _tool(
        "build_longform_context",
        (
            "Build the accepted continuity contract, layered memory, authority "
            "passages, open loops, and webnovel method context for a task."
        ),
        {
            **PROJECT_ROOT,
            "prompt": {"type": "string", "minLength": 1},
            "artifact_context": {"type": "object"},
            "max_context_chars": {
                "type": "integer",
                "minimum": 512,
                "maximum": 100000,
            },
        },
        ("project_root", "prompt"),
    ),
    _tool(
        "get_longform_status",
        "Read authority-index, memory, summary, pattern, and projection-run status.",
        PROJECT_ROOT,
        ("project_root",),
    ),
    _tool(
        "run_longform_benchmark",
        (
            "Run the deterministic long-form or typed power-system annotation "
            "benchmark, selected from the manifest suite."
        ),
        {"manifest_path": {"type": "string"}},
        read_only=True,
    ),
]


_TOOL_INPUT_SCHEMAS = {
    str(tool["name"]): dict(tool["inputSchema"])
    for tool in TOOLS
}


def _schema_value_marker(value: Any) -> tuple[str, str]:
    """Return a type-sensitive canonical marker for uniqueItems checks."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("tool arguments must contain valid JSON values") from exc
    return type(value).__name__, encoded


def _validate_schema_value(
    value: Any,
    schema: dict[str, Any],
    *,
    path: str,
) -> None:
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
    elif expected_type == "array":
        if not isinstance(value, list):
            raise ValueError(f"{path} must be an array")
    elif expected_type == "string":
        if type(value) is not str:
            raise ValueError(f"{path} must be a string")
    elif expected_type == "integer":
        if type(value) is not int:
            raise ValueError(f"{path} must be an integer")
    elif expected_type == "boolean":
        if type(value) is not bool:
            raise ValueError(f"{path} must be a boolean")
    elif expected_type is not None:
        raise ValueError(f"{path} uses unsupported schema type {expected_type!r}")

    if "enum" in schema and value not in schema["enum"]:
        choices = ", ".join(repr(item) for item in schema["enum"])
        raise ValueError(f"{path} must be one of: {choices}")

    if expected_type == "string":
        minimum_length = schema.get("minLength")
        if minimum_length is not None and len(value) < int(minimum_length):
            raise ValueError(
                f"{path} must contain at least {int(minimum_length)} characters"
            )

    if expected_type == "integer":
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            raise ValueError(f"{path} must be >= {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"{path} must be <= {maximum}")

    if expected_type == "array":
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema_value(
                    item,
                    item_schema,
                    path=f"{path}[{index}]",
                )
        if schema.get("uniqueItems"):
            seen: set[tuple[str, str]] = set()
            for index, item in enumerate(value):
                marker = _schema_value_marker(item)
                if marker in seen:
                    raise ValueError(
                        f"{path}[{index}] duplicates an earlier array item"
                    )
                seen.add(marker)

    if expected_type == "object":
        minimum_properties = schema.get("minProperties")
        if minimum_properties is not None and len(value) < int(
            minimum_properties
        ):
            raise ValueError(
                f"{path} must contain at least "
                f"{int(minimum_properties)} properties"
            )
        properties = schema.get("properties")
        property_schemas = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        required_names = required if isinstance(required, list) else []
        missing = [name for name in required_names if name not in value]
        if missing:
            raise ValueError(
                f"{path} is missing required properties: "
                + ", ".join(str(name) for name in missing)
            )
        additional = schema.get("additionalProperties", True)
        unknown = [name for name in value if name not in property_schemas]
        if additional is False and unknown:
            raise ValueError(
                f"{path} contains unsupported properties: "
                + ", ".join(sorted(str(name) for name in unknown))
            )
        for name, child_schema in property_schemas.items():
            if name in value and isinstance(child_schema, dict):
                _validate_schema_value(
                    value[name],
                    child_schema,
                    path=f"{path}.{name}",
                )
        if isinstance(additional, dict):
            for name in unknown:
                _validate_schema_value(
                    value[name],
                    additional,
                    path=f"{path}.{name}",
                )


def _validate_tool_arguments(name: str, arguments: dict[str, Any]) -> None:
    schema = _TOOL_INPUT_SCHEMAS.get(name)
    if schema is None:
        raise ValueError(f"unknown tool: {name}")
    _validate_schema_value(arguments, schema, path="tool arguments")


def _dispatch_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    (
        _,
        query_state,
        query_craft,
        dump_state,
        doctor,
        ContinuityService,
        v1,
    ) = _load_runtime()
    from event_experience import EventExperienceService
    from extraction_jobs import ExtractionJobQueue
    import performance_runtime

    if name == "compare_plot_prepare_paths":
        return performance_runtime.compare_reports(
            _one_report_input(
                arguments,
                report_key="left_report",
                path_key="left_path",
            ),
            _one_report_input(
                arguments,
                report_key="right_report",
                path_key="right_path",
            ),
        )

    if name in {
        "start_story_initialization",
        "dry_run_story_initialization",
        "advance_story_initialization",
        "answer_story_initialization",
        "inspect_story_initialization",
        "build_story_initialization_proposal",
        "list_story_initializations",
        "cancel_story_initialization",
    }:
        workspace, project = _init_paths(arguments)
        initializer = v1.init_service(workspace, project_root=project)
        common = {
            "project_root": project,
        }
        if name in {
            "start_story_initialization",
            "dry_run_story_initialization",
        }:
            common.update(
                {
                    "mode": str(arguments.get("mode") or "auto"),
                    "target_profile": str(
                        arguments.get("target_profile") or "plot_ready"
                    ),
                    "interaction_profile": str(
                        arguments.get("interaction_profile") or "balanced"
                    ),
                    "seed": arguments.get("seed"),
                    "seed_file": _relative_to_workspace(
                        arguments.get("seed_file"), workspace
                    ),
                    "sources": _source_paths(
                        arguments.get("sources"), workspace
                    ),
                    "expected_canon_revision": arguments.get(
                        "expected_canon_revision"
                    ),
                }
            )
        if name == "start_story_initialization":
            return initializer.start(
                **common,
                idempotency_key=str(arguments.get("idempotency_key") or ""),
                session_id=str(arguments.get("session_id") or "") or None,
            )
        if name == "dry_run_story_initialization":
            return initializer.dry_run(**common)
        if name == "advance_story_initialization":
            return initializer.advance(
                str(arguments.get("session_id") or ""),
                expected_session_revision=arguments.get(
                    "expected_session_revision"
                ),
                idempotency_key=str(arguments.get("idempotency_key") or ""),
            )
        if name == "answer_story_initialization":
            return initializer.answer(
                str(arguments.get("session_id") or ""),
                dict(arguments.get("answers") or {}),
                expected_session_revision=arguments.get(
                    "expected_session_revision"
                ),
                idempotency_key=str(arguments.get("idempotency_key") or ""),
            )
        if name == "inspect_story_initialization":
            return initializer.inspect(
                str(arguments.get("session_id") or ""),
                view=str(arguments.get("view") or "summary"),
            )
        if name == "build_story_initialization_proposal":
            return initializer.propose(
                str(arguments.get("session_id") or ""),
                expected_session_revision=arguments.get(
                    "expected_session_revision"
                ),
                idempotency_key=str(arguments.get("idempotency_key") or ""),
            )
        if name == "list_story_initializations":
            return initializer.list(
                project_root=project,
                active_only=bool(arguments.get("active_only", False)),
            )
        return initializer.cancel(
            str(arguments.get("session_id") or ""),
            expected_session_revision=arguments.get(
                "expected_session_revision"
            ),
            idempotency_key=str(arguments.get("idempotency_key") or ""),
            reason=str(arguments.get("reason") or ""),
        )

    if name == "run_longform_benchmark":
        manifest = arguments.get("manifest_path")
        return v1.run_longform_benchmark(
            _plain_path(manifest) if manifest else None
        )
    if name == "validate_power_spec_change":
        return v1.validate_power_spec_change(
            dict(arguments.get("power_spec") or {})
        )

    if name == "apply_story_initialization":
        workspace, project = _init_paths(arguments)
        if project is None:
            initializer = v1.init_service(workspace)
            frozen = initializer.storage.load_proposal(
                str(arguments.get("proposal_id") or "")
            )
            target = str(
                frozen.get("target_project_real_path")
                or (frozen.get("bundle") or {}).get(
                    "target_project_real_path"
                )
                or ""
            ).strip()
            if not target:
                raise ValueError(
                    "initialization proposal does not identify a target project"
                )
            project = Path(target).expanduser().resolve(strict=False)
        approval_id = str(arguments.get("approval_id") or "").strip()
        if not approval_id:
            requirement = v1.prepare_initialization_apply(
                project,
                str(arguments.get("proposal_id") or ""),
                workspace_root=workspace,
            )
            if (
                requirement.get("status")
                == "POWER_SPEC_APPROVAL_REQUIRED"
            ):
                return requirement
            raise ValueError(
                "accepted initialization stage requires approval_id"
            )
        return v1.apply_initialization_proposal(
            project,
            str(arguments.get("proposal_id") or ""),
            approval_id=approval_id,
            expected_canon_revision=arguments.get(
                "expected_canon_revision"
            ),
            idempotency_key=str(arguments.get("idempotency_key") or ""),
            workspace_root=workspace,
            materialize=True,
        )

    root = _project_root(arguments.get("project_root"))

    if name == "get_plot_performance_status":
        return performance_runtime.get_status(root)
    if name == "run_plot_performance_benchmark":
        manifest = arguments.get("manifest")
        manifest_path = arguments.get("manifest_path")
        if manifest is not None and manifest_path is not None:
            raise ValueError("pass either manifest or manifest_path, not both")
        return performance_runtime.run_benchmark(
            root,
            manifest=(dict(manifest) if isinstance(manifest, dict) else None),
            path=(
                _plain_path(manifest_path)
                if manifest_path is not None
                else None
            ),
            options=dict(arguments.get("options") or {}),
        )
    if name == "get_source_manifest_status":
        return v1.source_manifest_status(root)
    if name == "preview_source_manifest_change":
        return v1.preview_source_manifest_change(
            root,
            dict(arguments.get("plan") or {}),
            expected_canon_revision=arguments.get(
                "expected_canon_revision"
            ),
        )
    if name == "propose_source_manifest_change":
        return v1.propose_source_manifest_change(
            root,
            dict(arguments.get("plan") or {}),
            expected_canon_revision=arguments.get(
                "expected_canon_revision"
            ),
            idempotency_key=str(
                arguments.get("idempotency_key") or ""
            ),
        )
    if name == "preview_power_spec_change":
        return v1.preview_power_spec_change(
            root,
            dict(arguments.get("power_spec") or {}),
            expected_canon_revision=arguments.get(
                "expected_canon_revision"
            ),
        )
    if name == "propose_power_spec_change":
        return v1.propose_power_spec_change(
            root,
            dict(arguments.get("power_spec") or {}),
            expected_canon_revision=arguments.get(
                "expected_canon_revision"
            ),
            idempotency_key=str(
                arguments.get("idempotency_key") or ""
            ),
        )
    if name in {
        "list_plot_extraction_jobs",
        "inspect_plot_extraction_job",
        "retry_plot_extraction_job",
    }:
        queue = ExtractionJobQueue(root)
        if name == "list_plot_extraction_jobs":
            jobs = queue.list_jobs(
                status=arguments.get("statuses"),
                branch_id=str(arguments.get("branch_id") or "") or None,
                sequence_no=arguments.get("sequence_no"),
                receipt_id=str(arguments.get("receipt_id") or "") or None,
                limit=arguments.get("limit", 100),
                offset=arguments.get("offset", 0),
            )
            return {
                "status": "ready",
                "count": len(jobs),
                "jobs": jobs,
            }
        if name == "inspect_plot_extraction_job":
            return {
                "status": "ready",
                "job": queue.inspect(str(arguments.get("job_id") or "")),
            }
        return queue.retry(
            str(arguments.get("job_id") or ""),
            expected_attempt_count=arguments.get(
                "expected_attempt_count"
            ),
            next_attempt_at=arguments.get("next_attempt_at"),
        )
    if name in {
        "propose_event_experience",
        "inspect_event_experience",
        "lock_event_experience",
        "review_event_experience",
    }:
        service = EventExperienceService.for_project(root)
        if name == "propose_event_experience":
            return service.propose_contract(
                dict(arguments.get("contract") or {}),
                expected_control_revision=arguments.get(
                    "expected_control_revision"
                ),
                idempotency_key=str(
                    arguments.get("idempotency_key") or ""
                ),
            )
        if name == "inspect_event_experience":
            return {
                "status": "ready",
                "control_revision": service.get_control_revision(),
                "contract": service.get_contract(
                    str(arguments.get("contract_id") or "")
                ),
            }
        if name == "lock_event_experience":
            return service.lock_contract(
                str(arguments.get("contract_id") or ""),
                expected_control_revision=arguments.get(
                    "expected_control_revision"
                ),
                idempotency_key=str(
                    arguments.get("idempotency_key") or ""
                ),
                expected_contract_hash=(
                    str(arguments.get("expected_contract_hash") or "")
                    or None
                ),
            )
        return service.record_review(
            dict(arguments.get("review") or {}),
            expected_control_revision=arguments.get(
                "expected_control_revision"
            ),
            idempotency_key=str(
                arguments.get("idempotency_key") or ""
            ),
            assistant_text=str(arguments.get("assistant_text") or ""),
        )
    if name == "query_item_definition":
        return ContinuityService(root).query_item_definition(
            str(arguments.get("item_definition_id") or "")
        )
    if name == "query_item_instance":
        return ContinuityService(root).query_item_instance(
            str(arguments.get("item_instance_id") or "")
        )
    if name == "query_item_function":
        return ContinuityService(root).query_item_function(
            str(arguments.get("function_id") or ""),
            item_instance_id=(
                str(arguments.get("item_instance_id") or "") or None
            ),
            stack_id=str(arguments.get("stack_id") or "") or None,
        )
    if name == "query_item_runtime":
        return ContinuityService(root).query_item_runtime(
            str(arguments.get("item_instance_id") or "")
        )
    if name == "query_item_custody":
        return ContinuityService(root).query_item_custody(
            subject_type=str(arguments.get("subject_type") or ""),
            subject_id=str(arguments.get("subject_id") or ""),
        )
    if name == "query_actor_inventory":
        service = ContinuityService(root)
        actor_entity_id = str(
            arguments.get("actor_entity_id") or ""
        ).strip()
        if not actor_entity_id:
            mention = str(arguments.get("mention") or "").strip()
            if not mention:
                raise ValueError(
                    "query_actor_inventory requires actor_entity_id or mention"
                )
            resolution = service.resolve_mention(mention, persist=False)
            actor_entity_id = str(
                resolution.get("entity_id") or ""
            ).strip()
            if not actor_entity_id:
                raise ValueError(f"actor mention is unresolved: {mention}")
        return service.query_actor_inventory(actor_entity_id)
    if name == "query_item_history":
        return ContinuityService(root).query_item_history(
            item_instance_id=(
                str(arguments.get("item_instance_id") or "") or None
            ),
            stack_id=str(arguments.get("stack_id") or "") or None,
            actor_entity_id=(
                str(arguments.get("actor_entity_id") or "") or None
            ),
            limit=arguments.get("limit", 100),
        )
    if name == "query_item_observations":
        return ContinuityService(root).query_item_observations(
            item_instance_id=(
                str(arguments.get("item_instance_id") or "") or None
            ),
            stack_id=str(arguments.get("stack_id") or "") or None,
            observer_entity_id=(
                str(arguments.get("observer_entity_id") or "") or None
            ),
            knowledge_plane=(
                str(arguments.get("knowledge_plane") or "") or None
            ),
            limit=arguments.get("limit", 100),
            visibility=str(
                arguments.get("visibility") or "generation"
            ),
        )
    if name in {
        "query_advantage_definition",
        "query_advantage_anchors",
        "query_advantage_runtime",
        "query_advantage_modules",
        "query_advantage_ledger",
        "query_advantage_knowledge",
        "query_advantage_progression",
        "query_advantage_exposure",
        "query_special_item_context",
    }:
        service = ContinuityService(root)
        advantage_id = str(arguments.get("advantage_id") or "")
        if name == "query_advantage_definition":
            return _advantage_query_payload(
                service,
                helper_name="query_advantage_definition",
                advantage_id=advantage_id,
                result_key="definition",
                kwargs={
                    "visibility": str(
                        arguments.get("visibility") or "generation"
                    )
                },
            )
        if name == "query_advantage_anchors":
            return _advantage_query_payload(
                service,
                helper_name="query_advantage_anchors",
                advantage_id=advantage_id,
                result_key="anchors",
                kwargs={
                    "active_only": not bool(
                        arguments.get("include_inactive", False)
                    ),
                    "include_noncanon": bool(
                        arguments.get("include_noncanon", False)
                    ),
                    "visibility": str(
                        arguments.get("visibility") or "generation"
                    ),
                },
            )
        if name == "query_advantage_runtime":
            return _advantage_query_payload(
                service,
                helper_name="query_advantage_runtime",
                advantage_id=advantage_id,
                result_key="runtime",
                kwargs={
                    "branch_id": str(arguments.get("branch_id") or "main"),
                    "visibility": str(
                        arguments.get("visibility") or "generation"
                    ),
                },
                allow_none=True,
            )
        if name == "query_advantage_modules":
            return _advantage_query_payload(
                service,
                helper_name="query_advantage_modules",
                advantage_id=advantage_id,
                result_key="modules",
                kwargs={
                    "enabled_only": bool(
                        arguments.get("enabled_only", False)
                    ),
                    "visibility": str(
                        arguments.get("visibility") or "generation"
                    ),
                },
            )
        if name == "query_advantage_ledger":
            return _advantage_query_payload(
                service,
                helper_name="query_advantage_ledger",
                advantage_id=advantage_id,
                result_key="ledger",
                kwargs={
                    "limit": arguments.get("limit", 50),
                    "entry_kind": (
                        str(arguments.get("entry_kind") or "") or None
                    ),
                    "branch_id": (
                        str(arguments.get("branch_id") or "") or None
                    ),
                    "visibility": str(
                        arguments.get("visibility") or "generation"
                    ),
                },
            )
        if name == "query_advantage_knowledge":
            return _advantage_query_payload(
                service,
                helper_name="query_advantage_knowledge",
                advantage_id=advantage_id,
                result_key="knowledge",
                kwargs={
                    "knowledge_plane": (
                        str(arguments.get("knowledge_plane") or "") or None
                    ),
                    "observer_entity_id": (
                        str(arguments.get("observer_entity_id") or "") or None
                    ),
                    "include_noncanon": bool(
                        arguments.get("include_noncanon", False)
                    ),
                    "visibility": str(
                        arguments.get("visibility") or "generation"
                    ),
                },
            )
        if name == "query_advantage_progression":
            return _advantage_query_payload(
                service,
                helper_name="query_advantage_progression",
                advantage_id=advantage_id,
                result_key="progression",
                kwargs={
                    "branch_id": str(arguments.get("branch_id") or "main"),
                    "visibility": str(
                        arguments.get("visibility") or "generation"
                    ),
                },
            )
        if name == "query_advantage_exposure":
            return _advantage_query_payload(
                service,
                helper_name="query_advantage_exposure",
                advantage_id=advantage_id,
                result_key="exposure",
                kwargs={
                    "branch_id": str(arguments.get("branch_id") or "main"),
                    "visibility": str(
                        arguments.get("visibility") or "generation"
                    ),
                },
            )
        return _advantage_query_payload(
            service,
            helper_name="query_advantage_context",
            advantage_id=advantage_id,
            result_key=None,
            kwargs={
                "branch_id": str(arguments.get("branch_id") or "main"),
                "knowledge_plane": (
                    str(arguments.get("knowledge_plane") or "") or None
                ),
                "observer_entity_id": (
                    str(arguments.get("observer_entity_id") or "") or None
                ),
                "ledger_limit": arguments.get("ledger_limit", 10),
                "visibility": str(
                    arguments.get("visibility") or "generation"
                ),
            },
        )
    if name == "prepare_plot_turn":
        return v1.prepare_plot_turn(
            root,
            str(arguments.get("prompt") or ""),
            request_id=str(arguments.get("request_id") or ""),
            session_id=str(arguments.get("session_id") or ""),
            turn_id=str(arguments.get("turn_id") or ""),
            artifact_stage=arguments.get("artifact_stage"),
            branch_id=arguments.get("branch_id"),
            chapter_no=arguments.get("chapter_no"),
            scene_index=arguments.get("scene_index"),
            artifact_id=arguments.get("artifact_id"),
            task=arguments.get("task"),
        )
    if name in {"commit_plot_turn", "propose_plot_turn"}:
        request_id = str(
            arguments.get("receipt_id")
            or arguments.get("request_id")
            or ""
        )
        return v1.propose_plot_turn(
            root,
            str(arguments.get("assistant_text") or ""),
            request_id=request_id,
            session_id=str(arguments.get("session_id") or ""),
            prompt=str(arguments.get("prompt") or ""),
            turn_id=str(arguments.get("turn_id") or ""),
        )
    if name == "query_plot_state":
        if v1.is_strict_lifecycle(root):
            return v1.query_continuity_text(
                root,
                str(arguments.get("query") or ""),
                categories=arguments.get("categories"),
                top_k=arguments.get("top_k"),
            )
        return query_state(
            root,
            str(arguments.get("query") or ""),
            categories=arguments.get("categories"),
            top_k=arguments.get("top_k"),
        )
    power_temporal = {
        "chapter_no": arguments.get("chapter_no"),
        "scene_index": arguments.get("scene_index"),
        "branch_id": str(arguments.get("branch_id") or "") or None,
        "knowledge_planes": arguments.get("knowledge_planes"),
        "include_provisional": bool(
            arguments.get("include_provisional", False)
        ),
    }
    if name == "list_power_systems":
        return v1.list_power_systems(root, **power_temporal)
    power_common = {
        "system_id": str(arguments.get("system_id") or "") or None,
        **power_temporal,
    }
    if name == "query_power_state":
        return v1.query_power_state(
            root,
            mention=str(arguments.get("mention") or "") or None,
            entity_id=str(arguments.get("entity_id") or "") or None,
            track_id=str(arguments.get("track_id") or "") or None,
            ability_id=str(arguments.get("ability_id") or "") or None,
            resource_id=str(arguments.get("resource_id") or "") or None,
            include_historical=bool(
                arguments.get("include_historical", False)
            ),
            **power_common,
        )
    if name == "query_progression_path":
        return v1.query_progression_path(
            root,
            mention=str(arguments.get("mention") or "") or None,
            entity_id=str(arguments.get("entity_id") or "") or None,
            track_id=str(arguments.get("track_id") or "") or None,
            target_rank_id=str(
                arguments.get("target_rank_id") or ""
            ) or None,
            **power_common,
        )
    if name == "explain_power_action":
        return v1.explain_power_action(
            root,
            action_id=str(arguments.get("action_id") or ""),
            mention=str(arguments.get("mention") or "") or None,
            entity_id=str(arguments.get("entity_id") or "") or None,
            track_id=str(arguments.get("track_id") or "") or None,
            ability_id=str(arguments.get("ability_id") or "") or None,
            resource_id=str(arguments.get("resource_id") or "") or None,
            target_rank_id=str(
                arguments.get("target_rank_id") or ""
            ) or None,
            **power_common,
        )
    if name == "compare_power_conditions":
        return v1.compare_power_conditions(
            root,
            left_mention=str(arguments.get("left_mention") or "") or None,
            left_entity_id=str(
                arguments.get("left_entity_id") or ""
            ) or None,
            right_mention=str(
                arguments.get("right_mention") or ""
            ) or None,
            right_entity_id=str(
                arguments.get("right_entity_id") or ""
            ) or None,
            conditions=dict(arguments.get("conditions") or {}),
            **power_common,
        )
    if name == "query_plot_craft":
        return query_craft(
            root,
            str(arguments.get("query") or ""),
            top_k=arguments.get("top_k"),
        )
    if name == "get_plot_state":
        if v1.is_strict_lifecycle(root):
            return v1.query_continuity_text(
                root,
                str(arguments.get("subject") or ""),
                subject=str(arguments.get("subject") or "") or None,
                category=str(arguments.get("category") or "") or None,
                top_k=200,
            )
        return dump_state(
            root,
            subject=str(arguments.get("subject") or "") or None,
            category=str(arguments.get("category") or "") or None,
        )
    if name == "doctor_plot_rag":
        return v1.doctor_v1(root)
    if name == "list_plot_proposals":
        service = ContinuityService(root)
        proposals = service.list_proposals(
            canon_status=str(arguments.get("canon_status") or "") or None,
            branch_id=str(arguments.get("branch_id") or "") or None,
        )
        return {
            "status": "ready",
            "canon_revisions": service.get_canon_revisions(),
            "count": len(proposals),
            "proposals": proposals,
        }
    if name == "inspect_plot_proposal":
        service = ContinuityService(root)
        return {
            "status": "ready",
            "canon_revisions": service.get_canon_revisions(),
            "proposal": service.inspect_proposal(
                str(arguments.get("proposal_id") or "")
            ),
        }
    if name == "reject_plot_proposal":
        return v1.reject_plot_proposal(
            root,
            str(arguments.get("proposal_id") or ""),
            reason=str(arguments.get("reason") or ""),
            idempotency_key=(
                str(arguments.get("idempotency_key") or "") or None
            ),
        )
    if name == "accept_plot_proposal":
        return v1.accept_plot_proposal(
            root,
            str(arguments.get("proposal_id") or ""),
            approval_id=str(arguments.get("approval_id") or ""),
            expected_canon_revision=arguments.get(
                "expected_canon_revision"
            ),
            workspace_root=(
                _plain_path(arguments.get("workspace_root"))
                if arguments.get("workspace_root")
                else None
            ),
        )
    if name == "retract_plot_proposal":
        return v1.retract_plot_proposal(
            root,
            str(arguments.get("proposal_id") or ""),
            approval_id=str(arguments.get("approval_id") or ""),
            expected_canon_revision=arguments.get(
                "expected_canon_revision"
            ),
            reason=str(arguments.get("reason") or ""),
        )
    if name == "query_plot_state_at":
        return v1.query_continuity(
            root,
            mention=str(arguments.get("mention") or "") or None,
            entity_id=str(arguments.get("entity_id") or "") or None,
            fact_type=str(arguments.get("fact_type") or "") or None,
            scope=str(arguments.get("scope") or "") or None,
            chapter_no=arguments.get("chapter_no"),
            scene_index=arguments.get("scene_index"),
            branch_id=str(arguments.get("branch_id") or "") or None,
            include_historical=bool(
                arguments.get("include_historical", False)
            ),
            include_provisional=bool(
                arguments.get("include_provisional", False)
            ),
            include_relations=bool(
                arguments.get("include_relations", True)
            ),
        )
    if name == "replay_plot_continuity":
        return v1.replay_continuity(root)
    if name == "verify_story_initialization":
        return v1.verify_initialization(
            root,
            str(arguments.get("commit_id") or ""),
        )
    if name == "refresh_longform_index":
        return v1.refresh_longform_index(
            root,
            with_embeddings=bool(arguments.get("with_embeddings", False)),
        )
    if name == "recover_longform_projection":
        return v1.recover_longform_projection(
            root,
            str(arguments.get("run_id") or ""),
        )
    if name == "build_longform_context":
        return v1.build_longform_context(
            root,
            str(arguments.get("prompt") or ""),
            artifact_context=arguments.get("artifact_context"),
            max_context_chars=arguments.get("max_context_chars"),
        )
    if name == "get_longform_status":
        return v1.longform_status(root)
    raise ValueError(f"unknown tool: {name}")


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
    }
    if is_error:
        result["isError"] = True
    return result


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = str(message.get("method") or "")
    request_id = message.get("id")
    if request_id is None:
        return None

    if method == "initialize":
        requested = str(
            (message.get("params") or {}).get("protocolVersion") or ""
        )
        protocol = (
            requested
            if requested
            in {"2024-11-05", "2025-03-26", PROTOCOL_VERSION}
            else PROTOCOL_VERSION
        )
        result = {
            "protocolVersion": protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params")
        if params is None:
            params = {}
        try:
            if not isinstance(params, dict):
                raise ValueError("tools/call params must be an object")
            arguments = params.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be an object")
            name = str(params.get("name") or "")
            _validate_tool_arguments(name, arguments)
            payload = _dispatch_tool(
                name,
                arguments,
            )
            result = _tool_result(payload)
        except Exception as exc:
            result = _tool_result(
                {"status": "ERROR", "reason": str(exc)},
                is_error=True,
            )
    elif method in {"resources/list", "prompts/list"}:
        result = (
            {"resources": []}
            if method.startswith("resources")
            else {"prompts": []}
        )
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32601,
                "message": f"method not found: {method}",
            },
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    for raw in sys.stdin:
        if not raw.strip():
            continue
        message: dict[str, Any] | None = None
        try:
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError("JSON-RPC message must be an object")
            response = _handle(message)
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
        except Exception as exc:
            error_id = message.get("id") if isinstance(message, dict) else None
            error = {
                "jsonrpc": "2.0",
                "id": error_id,
                "error": {"code": -32603, "message": str(exc)},
            }
            print(json.dumps(error, ensure_ascii=False), flush=True)
            if os.environ.get("PLOT_RAG_MCP_DEBUG"):
                traceback.print_exc(file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
