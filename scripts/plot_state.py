#!/usr/bin/env python3
"""Unified CLI for plot RAG, strict canon lifecycle, and story initialization."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import importlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import v1_runtime as v1
from continuity import ContinuityService
from event_experience import EventExperienceService
from extraction_jobs import ExtractionJobQueue
import performance_runtime
from plot_rag import load_config, locate_project_root
from state_rag import dump_state, query_craft, query_state


ARTIFACT_STAGES = (
    "bootstrap",
    "brainstorm",
    "outline",
    "draft",
    "final",
    "published",
)
INIT_MODES = ("auto", "new", "ingest", "hybrid")
INIT_TARGETS = (
    "plot_ready",
    "world_bible",
    "normalize_only",
    "continuity_ready",
)
INIT_INTERACTIONS = ("minimal", "balanced", "deep")
INIT_VIEWS = (
    "summary",
    "sources",
    "conflicts",
    "gaps",
    "questions",
    "normalized",
    "diff",
    "proposal",
)
FAILED_STATUSES = {"failed", "error"}
PLUGIN_VERSION = "1.6.5"
KNOWLEDGE_PLANES = (
    "objective",
    "actor_belief",
    "public_narrative",
    "reader_disclosed",
    "author_plan",
)
ADVANTAGE_VISIBILITIES = ("generation", "inspection", "raw")
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


def _path(value: str | Path | None, *, default: Path | None = None) -> Path:
    selected = Path(value) if value not in {None, ""} else default
    if selected is None:
        raise ValueError("a path is required")
    return selected.expanduser().resolve(strict=False)


def _root(value: str | None) -> Path:
    start = _path(value, default=Path.cwd())
    root = locate_project_root(start)
    if root is None or not (root / ".plot-rag" / "config.json").is_file():
        if value is not None and str(value).strip():
            raise ValueError(
                "no .plot-rag/config.json found at or above explicitly "
                f"provided project root {start}"
            )
        raise ValueError(
            f"no .plot-rag/config.json found from {start}; "
            "pass --project-root explicitly"
        )
    return root.resolve()


def _plain_project(value: str | None) -> Path:
    return _path(value, default=Path.cwd())


def _workspace(value: str | None, project: Path | None) -> Path:
    return _path(
        value,
        default=(project.parent if project is not None else Path.cwd()),
    )


def _resolve_paths(
    values: Sequence[str] | None,
    *,
    base: Path,
) -> list[Path]:
    resolved: list[Path] = []
    for value in values or ():
        candidate = Path(value).expanduser()
        path = (
            candidate.resolve(strict=False)
            if candidate.is_absolute()
            else (base / candidate).resolve(strict=False)
        )
        if path not in resolved:
            resolved.append(path)
    return resolved


def _resolve_optional_path(
    value: str | None,
    *,
    base: Path,
) -> Path | None:
    if not value:
        return None
    return _resolve_paths((value,), base=base)[0]


def _text_input(
    *,
    file_value: str | None,
    text_value: str | None,
    label: str,
) -> str:
    if file_value:
        return Path(file_value).expanduser().read_text(encoding="utf-8-sig")
    if text_value is not None:
        return text_value
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise ValueError(f"{label} requires a file, inline text, or stdin")


def _json_input(value: str) -> Any:
    if value == "-":
        text = sys.stdin.read()
    else:
        candidate = Path(value).expanduser()
        try:
            is_file = candidate.is_file()
        except OSError:
            is_file = False
        text = (
            candidate.read_text(encoding="utf-8-sig")
            if is_file
            else value
        )
    return json.loads(text)


def _json_mapping(value: str, *, label: str) -> dict[str, Any]:
    payload = _json_input(value)
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return payload


def _resolved_actor_id(
    service: ContinuityService,
    *,
    actor_entity_id: str | None,
    mention: str | None,
) -> str:
    explicit = str(actor_entity_id or "").strip()
    if explicit:
        return explicit
    named = str(mention or "").strip()
    if not named:
        raise ValueError("inventory requires --actor-id or --mention")
    resolution = service.resolve_mention(named, persist=False)
    entity_id = str(resolution.get("entity_id") or "").strip()
    if not entity_id:
        raise ValueError(f"actor mention is unresolved: {named}")
    return entity_id


def _load_advantage_queries():
    """Load the optional Advantage query layer only when a query is used."""

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
def _advantage_read_connection(service: ContinuityService):
    """Open an existing continuity database without creating or migrating it."""

    store = service.store
    db_path = getattr(store, "db_path", None)
    if db_path is None:
        # Compatibility path for small host/test stores that already expose a
        # genuinely read-only context manager. Production stores always expose
        # db_path and therefore use SQLite mode=ro below.
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
    """Strip author/control-plane fields from generation-facing point queries."""

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
    service: ContinuityService,
    *,
    helper_name: str,
    advantage_id: str,
    result_key: str | None,
    kwargs: Mapping[str, Any] | None = None,
    allow_none: bool = False,
) -> dict[str, Any]:
    """Call one Advantage helper without mutating the continuity database."""

    normalized_id = str(advantage_id or "").strip()
    if not normalized_id:
        raise ValueError("advantage query requires --advantage-id")
    queries = _load_advantage_queries()
    helper = getattr(queries, helper_name, None)
    if not callable(helper):
        raise RuntimeError(f"Advantage query helper is missing: {helper_name}")
    call_kwargs = dict(kwargs or {})
    visibility = str(
        call_kwargs.pop("visibility", "generation") or "generation"
    ).strip().casefold()
    if visibility not in ADVANTAGE_VISIBILITIES:
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


def _add_advantage_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--advantage-id", required=True)


def _add_advantage_branch(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--branch-id", default="main")


def _add_advantage_knowledge_scope(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--knowledge-plane", choices=KNOWLEDGE_PLANES)
    parser.add_argument("--observer-entity-id", "--observer-id")
    parser.add_argument(
        "--visibility",
        choices=ADVANTAGE_VISIBILITIES,
        default="generation",
    )


def _add_advantage_visibility(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--visibility",
        choices=ADVANTAGE_VISIBILITIES,
        default="generation",
    )


def _interactive_approval_id(
    project_root: Path,
    proposal_id: str,
    *,
    expected_canon_revision: int,
    operations: Sequence[str] | None,
    workspace_root: Path | None = None,
) -> str:
    """Issue a grant only after two exact confirmations on a real TTY."""

    if not sys.stdin.isatty():
        raise ValueError(
            "approval grant issuance requires an interactive TTY; "
            "non-interactive callers must pass --approval-id"
        )
    first = input("输入完整 proposal ID 以确认：").strip()
    second = input("再次输入同一 proposal ID：").strip()
    if first != proposal_id or second != proposal_id or first != second:
        raise ValueError("proposal ID confirmation mismatch")
    issued = v1.issue_host_approval(
        project_root,
        proposal_id,
        expected_canon_revision=int(expected_canon_revision),
        issuer=f"local-cli:{getpass.getuser()}",
        channel="interactive_cli",
        operations=(tuple(operations) if operations is not None else None),
        workspace_root=workspace_root,
    )
    return str(issued["grant"]["approval_id"])


def _approval_id(
    args: argparse.Namespace,
    project_root: Path,
    *,
    operations: Sequence[str] | None,
    workspace_root: Path | None = None,
) -> str:
    existing = str(getattr(args, "approval_id", "") or "").strip()
    if existing:
        return existing
    return _interactive_approval_id(
        project_root,
        str(args.proposal_id),
        expected_canon_revision=int(args.expected_canon_revision),
        operations=operations,
        workspace_root=workspace_root,
    )


def _add_project_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-root", default=argparse.SUPPRESS)


def _add_workspace_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace-root", default=argparse.SUPPRESS)


def _add_runtime_roots(
    parser: argparse.ArgumentParser,
    *,
    workspace: bool = False,
) -> None:
    _add_project_root(parser)
    if workspace:
        _add_workspace_root(parser)


def _add_turn_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--request-id", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--turn-id", default="")


def _add_artifact_context(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--artifact-stage", choices=ARTIFACT_STAGES)
    parser.add_argument("--branch-id")
    parser.add_argument("--chapter-no", type=int)
    parser.add_argument("--scene-index", type=int)
    parser.add_argument("--artifact-id")
    parser.add_argument(
        "--task",
        choices=("book", "volume", "arc", "outline", "scene", "prose", "revision"),
    )


def _add_power_temporal_context(
    parser: argparse.ArgumentParser,
    *,
    actor: bool = False,
    include_system_id: bool = True,
) -> None:
    _add_project_root(parser)
    if actor:
        parser.add_argument("--mention")
        parser.add_argument("--entity-id")
    if include_system_id:
        parser.add_argument("--system-id")
    parser.add_argument("--chapter-no", type=int)
    parser.add_argument("--scene-index", type=int)
    parser.add_argument("--branch-id")
    parser.add_argument(
        "--knowledge-plane",
        action="append",
        dest="knowledge_planes",
        choices=(
            "objective",
            "actor_belief",
            "public_narrative",
            "reader_disclosed",
            "author_plan",
        ),
    )
    parser.add_argument("--include-provisional", action="store_true")


def _add_init_start_options(
    parser: argparse.ArgumentParser,
    *,
    mutating: bool,
) -> None:
    parser.add_argument("--mode", choices=INIT_MODES, default="auto")
    parser.add_argument("--target-profile", choices=INIT_TARGETS, default="plot_ready")
    parser.add_argument(
        "--interaction-profile",
        choices=INIT_INTERACTIONS,
        default="balanced",
    )
    parser.add_argument("--seed")
    parser.add_argument("--seed-file")
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--expected-canon-revision", type=int)
    if mutating:
        parser.add_argument("--idempotency-key", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Retrieve plot context, save proposal-only deltas, manage accepted "
            "canon, and initialize webnovel projects."
        )
    )
    parser.add_argument("--project-root")
    parser.add_argument("--workspace-root")
    parser.add_argument(
        "--version",
        action="version",
        version=(
            f"plot-rag-gate {PLUGIN_VERSION} "
            f"(runtime schema {v1.RUNTIME_VERSION})"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser(
        "prepare",
        help="retrieve generation context and bind a canon revision receipt",
    )
    _add_project_root(prepare)
    prepare.add_argument("--prompt", required=True)
    _add_turn_identity(prepare)
    _add_artifact_context(prepare)

    for name in ("propose", "commit"):
        proposal = sub.add_parser(
            name,
            help=(
                "extract a proposal from finalized assistant text; the strict "
                "lifecycle never accepts it automatically"
            ),
        )
        _add_project_root(proposal)
        source = proposal.add_mutually_exclusive_group()
        source.add_argument("--assistant-file")
        source.add_argument("--assistant-text")
        proposal.add_argument("--prompt", default="")
        _add_turn_identity(proposal)

    query = sub.add_parser("query", help="query accepted continuity state")
    _add_project_root(query)
    query.add_argument("--query")
    query.add_argument("--category", action="append", dest="categories")
    query.add_argument("--top-k", type=int)
    query.add_argument("--mention")
    query.add_argument("--entity-id")
    query.add_argument("--fact-type")
    query.add_argument(
        "--scope",
        choices=("current", "planned", "historical", "timeless"),
    )
    query.add_argument("--chapter-no", type=int)
    query.add_argument("--scene-index", type=int)
    query.add_argument("--branch-id")
    query.add_argument("--include-historical", action="store_true")
    query.add_argument("--include-provisional", action="store_true")
    query.add_argument("--exclude-relations", action="store_true")

    query_at = sub.add_parser(
        "query-at",
        help="query accepted continuity at a chapter, scene, or branch",
    )
    _add_project_root(query_at)
    query_at.add_argument("--mention")
    query_at.add_argument("--entity-id")
    query_at.add_argument("--fact-type")
    query_at.add_argument(
        "--scope",
        choices=("current", "planned", "historical", "timeless"),
    )
    query_at.add_argument("--chapter-no", type=int)
    query_at.add_argument("--scene-index", type=int)
    query_at.add_argument("--branch-id")
    query_at.add_argument("--include-historical", action="store_true")
    query_at.add_argument("--include-provisional", action="store_true")
    query_at.add_argument("--exclude-relations", action="store_true")

    craft = sub.add_parser("craft", help="retrieve task-relevant craft methods")
    _add_project_root(craft)
    craft.add_argument("--query", required=True)
    craft.add_argument("--top-k", type=int)

    dump = sub.add_parser("dump", help="read compatible current facts and history")
    _add_project_root(dump)
    dump.add_argument("--subject")
    dump.add_argument("--category")

    doctor_parser = sub.add_parser(
        "doctor",
        help="run unified zero-write runtime diagnostics",
    )
    _add_project_root(doctor_parser)

    proposals = sub.add_parser("list-proposals", help="list lifecycle proposals")
    _add_project_root(proposals)
    proposals.add_argument(
        "--canon-status",
        choices=("proposed", "accepted", "rejected", "retracted"),
    )
    proposals.add_argument("--branch-id")

    inspect = sub.add_parser("inspect-proposal", help="inspect one proposal")
    _add_project_root(inspect)
    inspect.add_argument("--proposal-id", required=True)

    accept = sub.add_parser(
        "accept-proposal",
        help=(
            "accept one proposal; without --approval-id, a real TTY must "
            "confirm the full proposal ID twice"
        ),
    )
    _add_project_root(accept)
    _add_workspace_root(accept)
    accept.add_argument("--proposal-id", required=True)
    accept.add_argument(
        "--approval-id",
        help="consume an existing host grant instead of interactive confirmation",
    )
    accept.add_argument("--expected-canon-revision", type=int, required=True)

    reject = sub.add_parser("reject-proposal", help="reject one proposal")
    _add_project_root(reject)
    reject.add_argument("--proposal-id", required=True)
    reject.add_argument("--reason", required=True)
    reject.add_argument("--idempotency-key", required=True)

    retract = sub.add_parser(
        "retract-proposal",
        help=(
            "retract accepted canon; without --approval-id, a real TTY must "
            "confirm the full proposal ID twice"
        ),
    )
    _add_project_root(retract)
    retract.add_argument("--proposal-id", required=True)
    retract.add_argument(
        "--approval-id",
        help="consume an existing host grant instead of interactive confirmation",
    )
    retract.add_argument("--expected-canon-revision", type=int, required=True)
    retract.add_argument("--reason", required=True)

    proposal_group = sub.add_parser(
        "proposal",
        help="inspect and change proposal lifecycle",
    )
    _add_project_root(proposal_group)
    proposal_sub = proposal_group.add_subparsers(
        dest="proposal_command",
        required=True,
    )
    proposal_list = proposal_sub.add_parser("list")
    _add_project_root(proposal_list)
    proposal_list.add_argument(
        "--canon-status",
        choices=("proposed", "accepted", "rejected", "retracted"),
    )
    proposal_list.add_argument("--branch-id")
    proposal_inspect = proposal_sub.add_parser("inspect")
    _add_project_root(proposal_inspect)
    proposal_inspect.add_argument("--proposal-id", required=True)
    proposal_accept = proposal_sub.add_parser("accept")
    _add_project_root(proposal_accept)
    _add_workspace_root(proposal_accept)
    proposal_accept.add_argument("--proposal-id", required=True)
    proposal_accept.add_argument(
        "--approval-id",
        help="otherwise a real TTY must confirm the full proposal ID twice",
    )
    proposal_accept.add_argument(
        "--expected-canon-revision",
        type=int,
        required=True,
    )
    proposal_reject = proposal_sub.add_parser("reject")
    _add_project_root(proposal_reject)
    proposal_reject.add_argument("--proposal-id", required=True)
    proposal_reject.add_argument("--reason", required=True)
    proposal_reject.add_argument("--idempotency-key", required=True)
    proposal_retract = proposal_sub.add_parser("retract")
    _add_project_root(proposal_retract)
    proposal_retract.add_argument("--proposal-id", required=True)
    proposal_retract.add_argument(
        "--approval-id",
        help="otherwise a real TTY must confirm the full proposal ID twice",
    )
    proposal_retract.add_argument(
        "--expected-canon-revision",
        type=int,
        required=True,
    )
    proposal_retract.add_argument("--reason", required=True)

    replay = sub.add_parser(
        "replay",
        help="rebuild deterministic accepted projections",
    )
    _add_project_root(replay)

    source_manifest = sub.add_parser(
        "source-manifest",
        help="inspect or propose accepted source-manifest changes",
    )
    _add_project_root(source_manifest)
    source_manifest_sub = source_manifest.add_subparsers(
        dest="source_manifest_command",
        required=True,
    )
    source_manifest_status = source_manifest_sub.add_parser(
        "status",
        help="inspect the source-manifest ledger and current projection",
    )
    _add_project_root(source_manifest_status)
    source_manifest_preview = source_manifest_sub.add_parser(
        "preview",
        help="validate a migration plan without saving a proposal",
    )
    _add_project_root(source_manifest_preview)
    source_manifest_preview.add_argument(
        "--plan",
        "--plan-json",
        dest="plan_json",
        required=True,
    )
    source_manifest_preview.add_argument(
        "--expected-canon-revision",
        type=int,
        required=True,
    )
    source_manifest_propose = source_manifest_sub.add_parser(
        "propose",
        help="freeze a migration plan as a proposal-only artifact",
    )
    _add_project_root(source_manifest_propose)
    source_manifest_propose.add_argument(
        "--plan",
        "--plan-json",
        dest="plan_json",
        required=True,
    )
    source_manifest_propose.add_argument(
        "--expected-canon-revision",
        type=int,
        required=True,
    )
    source_manifest_propose.add_argument(
        "--idempotency-key",
        required=True,
    )

    power_spec = sub.add_parser(
        "power-spec",
        help="validate, preview, or propose a standalone PowerSpec import",
    )
    power_spec_sub = power_spec.add_subparsers(
        dest="power_spec_command",
        required=True,
    )
    power_spec_validate = power_spec_sub.add_parser(
        "validate",
        help="strictly validate and compile a PowerSpec without project access",
    )
    power_spec_validate.add_argument(
        "--spec",
        "--spec-json",
        dest="spec_json",
        required=True,
    )
    power_spec_preview = power_spec_sub.add_parser(
        "preview",
        help="preview a PowerSpec proposal against one canon revision",
    )
    _add_project_root(power_spec_preview)
    power_spec_preview.add_argument(
        "--spec",
        "--spec-json",
        dest="spec_json",
        required=True,
    )
    power_spec_preview.add_argument(
        "--expected-canon-revision",
        type=int,
        required=True,
    )
    power_spec_propose = power_spec_sub.add_parser(
        "propose",
        help="register entities and freeze a proposal-only PowerSpec change",
    )
    _add_project_root(power_spec_propose)
    power_spec_propose.add_argument(
        "--spec",
        "--spec-json",
        dest="spec_json",
        required=True,
    )
    power_spec_propose.add_argument(
        "--expected-canon-revision",
        type=int,
        required=True,
    )
    power_spec_propose.add_argument(
        "--idempotency-key",
        required=True,
    )

    longform = sub.add_parser("longform", help="manage long-form retrieval layers")
    _add_project_root(longform)
    longform_sub = longform.add_subparsers(dest="longform_command", required=True)
    refresh = longform_sub.add_parser("refresh", help="refresh accepted authority index")
    _add_project_root(refresh)
    refresh.add_argument("--with-embeddings", action="store_true")
    index = longform_sub.add_parser(
        "index",
        help="alias of refresh for persistent authority indexing",
    )
    _add_project_root(index)
    index.add_argument("--with-embeddings", action="store_true")
    context = longform_sub.add_parser("context", help="build continuity context")
    _add_project_root(context)
    context.add_argument("--prompt", required=True)
    context.add_argument("--max-context-chars", type=int)
    _add_artifact_context(context)
    longform_status = longform_sub.add_parser(
        "status",
        help="read long-form projection status",
    )
    _add_project_root(longform_status)
    longform_recover = longform_sub.add_parser(
        "recover",
        help="recover and retry one abandoned long-form projection run",
    )
    _add_project_root(longform_recover)
    longform_recover.add_argument("--run-id", required=True)
    benchmark = longform_sub.add_parser(
        "benchmark",
        help="run legacy or power quality fixture selected by manifest",
    )
    _add_project_root(benchmark)
    benchmark.add_argument("--manifest")

    performance = sub.add_parser(
        "performance",
        help="inspect and benchmark the v1.5 prepare/extraction runtime",
    )
    performance_sub = performance.add_subparsers(
        dest="performance_command",
        required=True,
    )
    performance_status = performance_sub.add_parser(
        "status",
        help="read redacted project-local performance telemetry",
    )
    _add_project_root(performance_status)
    performance_benchmark = performance_sub.add_parser(
        "benchmark",
        help="run the deterministic offline v1.5 performance harness",
    )
    _add_project_root(performance_benchmark)
    performance_benchmark.add_argument(
        "--manifest",
        help="inline fixture JSON or a JSON fixture path",
    )
    performance_benchmark.add_argument(
        "--options-json",
        default="{}",
        help="inline JSON object, JSON file path, or '-' for stdin",
    )
    performance_compare = performance_sub.add_parser(
        "compare",
        help="compare telemetry metrics from two performance reports",
    )
    performance_compare.add_argument(
        "--left",
        "--left-report",
        required=True,
        dest="left",
        help="left/baseline report JSON or report path",
    )
    performance_compare.add_argument(
        "--right",
        "--right-report",
        required=True,
        dest="right",
        help="right/candidate report JSON or report path",
    )

    extraction = sub.add_parser(
        "extraction",
        help="inspect and retry durable asynchronous extraction jobs",
    )
    _add_project_root(extraction)
    extraction_sub = extraction.add_subparsers(
        dest="extraction_command",
        required=True,
    )
    extraction_list = extraction_sub.add_parser(
        "list",
        help="list durable extraction jobs",
    )
    _add_project_root(extraction_list)
    extraction_list.add_argument(
        "--status",
        action="append",
        dest="statuses",
    )
    extraction_list.add_argument("--branch-id")
    extraction_list.add_argument("--sequence-no", type=int)
    extraction_list.add_argument("--receipt-id")
    extraction_list.add_argument("--limit", type=int, default=100)
    extraction_list.add_argument("--offset", type=int, default=0)
    extraction_inspect = extraction_sub.add_parser(
        "inspect",
        help="inspect one immutable extraction job binding",
    )
    _add_project_root(extraction_inspect)
    extraction_inspect.add_argument("--job-id", required=True)
    extraction_retry = extraction_sub.add_parser(
        "retry",
        help="CAS a failed extraction job back to queued",
    )
    _add_project_root(extraction_retry)
    extraction_retry.add_argument("--job-id", required=True)
    extraction_retry.add_argument(
        "--expected-attempt-count",
        type=int,
        required=True,
    )
    extraction_retry.add_argument("--next-attempt-at")

    experience = sub.add_parser(
        "experience",
        help="manage non-canon event reader-experience contracts",
    )
    _add_project_root(experience)
    experience_sub = experience.add_subparsers(
        dest="experience_command",
        required=True,
    )
    experience_propose = experience_sub.add_parser(
        "propose",
        help="propose one EventExperienceContract for an existing EventSeed",
    )
    _add_project_root(experience_propose)
    experience_propose.add_argument(
        "--contract",
        "--contract-json",
        dest="contract_json",
        required=True,
        help="inline contract JSON, JSON file path, or '-' for stdin",
    )
    experience_propose.add_argument(
        "--expected-control-revision",
        type=int,
        required=True,
    )
    experience_propose.add_argument("--idempotency-key", required=True)
    experience_inspect = experience_sub.add_parser(
        "inspect",
        help="inspect one event-experience contract",
    )
    _add_project_root(experience_inspect)
    experience_inspect.add_argument("--contract-id", required=True)
    experience_lock = experience_sub.add_parser(
        "lock",
        help="lock a proposed event-experience contract by revision CAS",
    )
    _add_project_root(experience_lock)
    experience_lock.add_argument("--contract-id", required=True)
    experience_lock.add_argument("--expected-contract-hash")
    experience_lock.add_argument(
        "--expected-control-revision",
        type=int,
        required=True,
    )
    experience_lock.add_argument("--idempotency-key", required=True)
    experience_review = experience_sub.add_parser(
        "review",
        help="record a post-generation experience review with exact evidence",
    )
    _add_project_root(experience_review)
    experience_review.add_argument(
        "--review",
        "--review-json",
        dest="review_json",
        required=True,
        help="inline review JSON, JSON file path, or '-' for stdin",
    )
    review_source = experience_review.add_mutually_exclusive_group(
        required=True
    )
    review_source.add_argument("--assistant-file")
    review_source.add_argument("--assistant-text")
    experience_review.add_argument(
        "--expected-control-revision",
        type=int,
        required=True,
    )
    experience_review.add_argument("--idempotency-key", required=True)

    item = sub.add_parser(
        "item",
        help="query accepted strong-typed item definitions and runtime state",
    )
    _add_project_root(item)
    item_sub = item.add_subparsers(dest="item_command", required=True)
    item_definition = item_sub.add_parser(
        "definition",
        help="query one item definition",
    )
    _add_project_root(item_definition)
    item_definition.add_argument(
        "--definition-id",
        "--item-definition-id",
        dest="definition_id",
        required=True,
    )
    item_instance = item_sub.add_parser(
        "instance",
        help="query one unique item instance",
    )
    _add_project_root(item_instance)
    item_instance.add_argument(
        "--instance-id",
        "--item-instance-id",
        dest="instance_id",
        required=True,
    )
    item_inventory = item_sub.add_parser(
        "inventory",
        help="query legal ownership, custody, carried, stored, and legacy inventory",
    )
    _add_project_root(item_inventory)
    inventory_actor = item_inventory.add_mutually_exclusive_group(
        required=True
    )
    inventory_actor.add_argument(
        "--actor-id",
        "--actor-entity-id",
        dest="actor_id",
    )
    inventory_actor.add_argument("--mention")
    item_custody = item_sub.add_parser(
        "custody",
        help="query legal and physical custody separately",
    )
    _add_project_root(item_custody)
    item_custody.add_argument(
        "--subject-type",
        required=True,
        choices=("item_instance", "item_stack", "instance", "stack"),
        help="typed custody subject kind",
    )
    item_custody.add_argument("--subject-id", required=True)
    item_function = item_sub.add_parser(
        "function",
        help="query one declared item function and its bindings",
    )
    _add_project_root(item_function)
    item_function.add_argument("--function-id", required=True)
    item_function.add_argument(
        "--instance-id",
        "--item-instance-id",
        dest="instance_id",
    )
    item_function.add_argument("--stack-id")
    item_runtime = item_sub.add_parser(
        "runtime",
        help="query item and function durability, charges, cooldowns, and flags",
    )
    _add_project_root(item_runtime)
    item_runtime.add_argument(
        "--instance-id",
        "--item-instance-id",
        dest="instance_id",
        required=True,
    )
    item_history = item_sub.add_parser(
        "history",
        help="query accepted item use history",
    )
    _add_project_root(item_history)
    item_history.add_argument(
        "--instance-id",
        "--item-instance-id",
        dest="instance_id",
    )
    item_history.add_argument("--stack-id")
    item_history.add_argument(
        "--actor-id",
        "--actor-entity-id",
        dest="actor_id",
    )
    item_history.add_argument("--limit", type=int, default=100)
    item_observations = item_sub.add_parser(
        "observations",
        help="query knowledge-plane-scoped item observations",
    )
    _add_project_root(item_observations)
    item_observations.add_argument(
        "--instance-id",
        "--item-instance-id",
        dest="instance_id",
    )
    item_observations.add_argument("--stack-id")
    item_observations.add_argument(
        "--observer-id",
        "--observer-entity-id",
        dest="observer_id",
    )
    item_observations.add_argument("--knowledge-plane")
    item_observations.add_argument("--limit", type=int, default=100)
    _add_advantage_visibility(item_observations)

    advantage = sub.add_parser(
        "advantage",
        help="query accepted special-item and golden-finger projections",
    )
    _add_project_root(advantage)
    advantage_sub = advantage.add_subparsers(
        dest="advantage_command",
        required=True,
    )
    advantage_definition = advantage_sub.add_parser(
        "definition",
        help="query one stable Advantage definition",
    )
    _add_project_root(advantage_definition)
    _add_advantage_id(advantage_definition)
    _add_advantage_visibility(advantage_definition)
    advantage_anchors = advantage_sub.add_parser(
        "anchors",
        aliases=("anchor",),
        help="query the active bindings and carriers of one Advantage",
    )
    _add_project_root(advantage_anchors)
    _add_advantage_id(advantage_anchors)
    _add_advantage_visibility(advantage_anchors)
    advantage_anchors.add_argument(
        "--include-inactive",
        action="store_true",
    )
    advantage_anchors.add_argument(
        "--include-noncanon",
        action="store_true",
    )
    advantage_runtime = advantage_sub.add_parser(
        "runtime",
        help="query current Advantage resources, cooldowns, risks, and stage",
    )
    _add_project_root(advantage_runtime)
    _add_advantage_id(advantage_runtime)
    _add_advantage_branch(advantage_runtime)
    _add_advantage_visibility(advantage_runtime)
    advantage_modules = advantage_sub.add_parser(
        "modules",
        aliases=("module",),
        help="query declared Advantage modules and their prerequisites",
    )
    _add_project_root(advantage_modules)
    _add_advantage_id(advantage_modules)
    _add_advantage_visibility(advantage_modules)
    advantage_modules.add_argument("--enabled-only", action="store_true")
    advantage_ledger = advantage_sub.add_parser(
        "ledger",
        help="query accepted Advantage reward, cost, and conversion entries",
    )
    _add_project_root(advantage_ledger)
    _add_advantage_id(advantage_ledger)
    _add_advantage_visibility(advantage_ledger)
    advantage_ledger.add_argument("--limit", type=int, default=50)
    advantage_ledger.add_argument("--entry-kind")
    advantage_ledger.add_argument("--branch-id")
    advantage_knowledge = advantage_sub.add_parser(
        "knowledge",
        help="query knowledge-plane-scoped Advantage claims and evidence",
    )
    _add_project_root(advantage_knowledge)
    _add_advantage_id(advantage_knowledge)
    _add_advantage_knowledge_scope(advantage_knowledge)
    advantage_knowledge.add_argument(
        "--include-noncanon",
        action="store_true",
    )
    advantage_progression = advantage_sub.add_parser(
        "progression",
        help="query the current Advantage stage, slots, and legal unlock path",
    )
    _add_project_root(advantage_progression)
    _add_advantage_id(advantage_progression)
    _add_advantage_branch(advantage_progression)
    _add_advantage_visibility(advantage_progression)
    advantage_exposure = advantage_sub.add_parser(
        "exposure",
        help="query current exposure, pollution, trace, and counterplay risk",
    )
    _add_project_root(advantage_exposure)
    _add_advantage_id(advantage_exposure)
    _add_advantage_branch(advantage_exposure)
    _add_advantage_visibility(advantage_exposure)

    special_item = sub.add_parser(
        "special-item",
        help="build the mandatory combined context for one special item",
    )
    _add_project_root(special_item)
    special_item_sub = special_item.add_subparsers(
        dest="special_item_command",
        required=True,
    )
    for command_name in ("context", "inventory"):
        special_item_context = special_item_sub.add_parser(
            command_name,
            help="query combined Advantage, item, power, knowledge, and risk context",
        )
        _add_project_root(special_item_context)
        _add_advantage_id(special_item_context)
        _add_advantage_branch(special_item_context)
        _add_advantage_knowledge_scope(special_item_context)
        special_item_context.add_argument(
            "--ledger-limit",
            type=int,
            default=10,
        )

    special_item_context_direct = sub.add_parser(
        "special-item-context",
        help="direct alias for special-item context",
    )
    _add_project_root(special_item_context_direct)
    _add_advantage_id(special_item_context_direct)
    _add_advantage_branch(special_item_context_direct)
    _add_advantage_knowledge_scope(special_item_context_direct)
    special_item_context_direct.add_argument(
        "--ledger-limit",
        type=int,
        default=10,
    )

    power = sub.add_parser(
        "power",
        help="query accepted power-system definitions and runtime state",
    )
    _add_project_root(power)
    power_sub = power.add_subparsers(dest="power_command", required=True)
    power_systems = power_sub.add_parser(
        "systems",
        help="list accepted power systems and modeling profiles",
    )
    _add_power_temporal_context(
        power_systems,
        include_system_id=False,
    )
    power_state = power_sub.add_parser(
        "state",
        help="query progression, abilities, resources, status, and bindings",
    )
    _add_power_temporal_context(power_state, actor=True)
    power_state.add_argument("--track-id")
    power_state.add_argument("--ability-id")
    power_state.add_argument("--resource-id")
    power_state.add_argument("--include-historical", action="store_true")
    power_path = power_sub.add_parser(
        "path",
        help="query legal accepted progression edges and blockers",
    )
    _add_power_temporal_context(power_path, actor=True)
    power_path.add_argument("--track-id")
    power_path.add_argument("--target-rank-id")
    power_explain = power_sub.add_parser(
        "explain",
        help="explain whether an actor can perform a power action now",
    )
    _add_power_temporal_context(power_explain, actor=True)
    power_explain.add_argument("--action-id", required=True)
    power_explain.add_argument("--track-id")
    power_explain.add_argument("--ability-id")
    power_explain.add_argument("--resource-id")
    power_explain.add_argument("--target-rank-id")
    power_compare = power_sub.add_parser(
        "compare",
        help="compare two actors as a conditional matrix without declaring a winner",
    )
    _add_power_temporal_context(power_compare)
    power_compare.add_argument("--left-mention")
    power_compare.add_argument("--left-entity-id")
    power_compare.add_argument("--right-mention")
    power_compare.add_argument("--right-entity-id")
    power_compare.add_argument(
        "--conditions-json",
        default="{}",
        help=(
            "inline JSON object, JSON file path, or '-' for stdin; "
            "describes environment, preparation, or target conditions"
        ),
    )

    init = sub.add_parser("init", help="initialize or normalize a story project")
    _add_runtime_roots(init, workspace=True)
    init_sub = init.add_subparsers(dest="init_command", required=True)
    init_start = init_sub.add_parser("start", help="start a resumable init session")
    _add_runtime_roots(init_start, workspace=True)
    _add_init_start_options(init_start, mutating=True)
    init_start.add_argument("--session-id")
    init_start.add_argument("--host-session-id")
    init_start.add_argument("--host-turn-id")

    init_dry = init_sub.add_parser(
        "dry-run",
        help="build an in-memory proposal report with zero persistent writes",
    )
    _add_runtime_roots(init_dry, workspace=True)
    _add_init_start_options(init_dry, mutating=False)
    init_dry.add_argument("--output")

    advance = init_sub.add_parser("advance", help="resume from the last checkpoint")
    _add_runtime_roots(advance, workspace=True)
    advance.add_argument("--session-id", required=True)
    advance.add_argument("--expected-session-revision", type=int, required=True)
    advance.add_argument("--idempotency-key", required=True)

    answer = init_sub.add_parser("answer", help="apply structured answers")
    _add_runtime_roots(answer, workspace=True)
    answer.add_argument("--session-id", required=True)
    answer.add_argument("--answers-file", required=True)
    answer.add_argument("--expected-session-revision", type=int, required=True)
    answer.add_argument("--idempotency-key", required=True)

    init_inspect = init_sub.add_parser("inspect", help="read one init session")
    _add_runtime_roots(init_inspect, workspace=True)
    init_inspect.add_argument("--session-id", required=True)
    init_inspect.add_argument("--view", choices=INIT_VIEWS, default="summary")

    init_propose = init_sub.add_parser(
        "propose",
        help="freeze a reviewed InitializationBundle proposal",
    )
    _add_runtime_roots(init_propose, workspace=True)
    init_propose.add_argument("--session-id", required=True)
    init_propose.add_argument("--expected-session-revision", type=int, required=True)
    init_propose.add_argument("--idempotency-key", required=True)

    init_apply = init_sub.add_parser(
        "apply",
        help=(
            "materialize a frozen proposal; without --approval-id, a real TTY "
            "must confirm the full proposal ID twice"
        ),
    )
    _add_runtime_roots(init_apply, workspace=True)
    init_apply.add_argument("--proposal-id", required=True)
    init_apply.add_argument(
        "--approval-id",
        help="consume an existing host grant instead of interactive confirmation",
    )
    init_apply.add_argument("--expected-canon-revision", type=int, required=True)
    init_apply.add_argument("--idempotency-key", required=True)

    init_verify = init_sub.add_parser("verify", help="verify completion receipt")
    _add_runtime_roots(init_verify, workspace=True)
    init_verify.add_argument("--commit-id", required=True)

    init_list = init_sub.add_parser("list", help="list initialization sessions")
    _add_runtime_roots(init_list, workspace=True)
    init_list.add_argument("--active-only", action="store_true")

    init_cancel = init_sub.add_parser("cancel", help="cancel one init session")
    _add_runtime_roots(init_cancel, workspace=True)
    init_cancel.add_argument("--session-id", required=True)
    init_cancel.add_argument("--expected-session-revision", type=int, required=True)
    init_cancel.add_argument("--idempotency-key", required=True)
    init_cancel.add_argument("--reason", default="")

    migrate = sub.add_parser(
        "migrate",
        help="backup and migrate config/state schemas with rollback receipts",
    )
    _add_project_root(migrate)
    migrate.add_argument(
        "--component",
        choices=("all", "config", "state"),
        default="all",
    )
    migrate.add_argument("--dry-run", action="store_true")
    return parser


def _init_service(args: argparse.Namespace, project: Path | None):
    workspace = _workspace(
        getattr(args, "workspace_root", None),
        project,
    )
    configured_project = (
        project
        if project is not None
        and (project / ".plot-rag" / "config.json").is_file()
        else None
    )
    return (
        v1.init_service(
            workspace,
            project_root=configured_project,
        ),
        workspace,
    )


def _dispatch_init(args: argparse.Namespace) -> dict[str, Any]:
    project = (
        _plain_project(getattr(args, "project_root", None))
        if getattr(args, "project_root", None)
        else None
    )
    if args.init_command in {"start", "dry-run"} and project is None:
        raise ValueError(
            f"init {args.init_command} requires --project-root; "
            "the target may be an empty directory"
        )
    initializer, workspace = _init_service(args, project)

    if args.init_command == "start":
        return initializer.start(
            project_root=project,
            mode=args.mode,
            target_profile=args.target_profile,
            interaction_profile=args.interaction_profile,
            seed=args.seed,
            seed_file=_resolve_optional_path(
                args.seed_file,
                base=workspace,
            ),
            sources=_resolve_paths(args.source, base=workspace),
            expected_canon_revision=args.expected_canon_revision,
            idempotency_key=args.idempotency_key,
            session_id=args.session_id,
            host_session_id=args.host_session_id,
            host_turn_id=args.host_turn_id,
        )
    if args.init_command == "dry-run":
        result = initializer.dry_run(
            project_root=project,
            mode=args.mode,
            target_profile=args.target_profile,
            interaction_profile=args.interaction_profile,
            seed=args.seed,
            seed_file=_resolve_optional_path(
                args.seed_file,
                base=workspace,
            ),
            sources=_resolve_paths(args.source, base=workspace),
            expected_canon_revision=args.expected_canon_revision,
        )
        if args.output:
            output = Path(args.output).expanduser().resolve(strict=False)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            result["output"] = str(output)
        return result
    if args.init_command == "advance":
        return initializer.advance(
            args.session_id,
            expected_session_revision=args.expected_session_revision,
            idempotency_key=args.idempotency_key,
        )
    if args.init_command == "answer":
        answers = _json_input(args.answers_file)
        if not isinstance(answers, dict):
            raise ValueError("answers file must contain one JSON object")
        return initializer.answer(
            args.session_id,
            answers,
            expected_session_revision=args.expected_session_revision,
            idempotency_key=args.idempotency_key,
        )
    if args.init_command == "inspect":
        return initializer.inspect(args.session_id, view=args.view)
    if args.init_command == "propose":
        return initializer.propose(
            args.session_id,
            expected_session_revision=args.expected_session_revision,
            idempotency_key=args.idempotency_key,
        )
    if args.init_command == "apply":
        if project is None:
            frozen = initializer.storage.load_proposal(args.proposal_id)
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
            project = _path(target)
        requirement = v1.prepare_initialization_apply(
            project,
            args.proposal_id,
            workspace_root=workspace,
        )
        if requirement.get("status") == "POWER_SPEC_APPROVAL_REQUIRED":
            return requirement
        approval_id = _approval_id(
            args,
            project,
            operations=("accept_initialization", "materialize"),
            workspace_root=workspace,
        )
        return v1.apply_initialization_proposal(
            project,
            args.proposal_id,
            approval_id=approval_id,
            expected_canon_revision=args.expected_canon_revision,
            idempotency_key=args.idempotency_key,
            workspace_root=workspace,
            materialize=True,
        )
    if args.init_command == "verify":
        if project is None:
            project = locate_project_root(Path.cwd())
        if project is None:
            raise ValueError(
                "init verify requires --project-root or a configured project cwd"
            )
        return v1.verify_initialization(project, args.commit_id)
    if args.init_command == "list":
        return initializer.list(
            project_root=project,
            active_only=args.active_only,
        )
    return initializer.cancel(
        args.session_id,
        expected_session_revision=args.expected_session_revision,
        idempotency_key=args.idempotency_key,
        reason=args.reason,
    )


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "init":
        return _dispatch_init(args)
    if (
        args.command == "performance"
        and args.performance_command == "compare"
    ):
        return performance_runtime.compare_reports(
            _json_mapping(args.left, label="--left"),
            _json_mapping(args.right, label="--right"),
        )
    if (
        args.command == "longform"
        and args.longform_command == "benchmark"
    ):
        return v1.run_longform_benchmark(
            _resolve_optional_path(
                args.manifest,
                base=Path.cwd(),
            )
        )
    if (
        args.command == "power-spec"
        and args.power_spec_command == "validate"
    ):
        return v1.validate_power_spec_change(
            _json_mapping(args.spec_json, label="--spec")
        )

    root = _root(getattr(args, "project_root", None))
    if args.command == "performance":
        if args.performance_command == "status":
            return performance_runtime.get_status(root)
        if args.performance_command == "benchmark":
            manifest = (
                _json_mapping(args.manifest, label="--manifest")
                if args.manifest
                else None
            )
            options = _json_mapping(
                args.options_json,
                label="--options-json",
            )
            return performance_runtime.run_benchmark(
                root,
                manifest=manifest,
                options=options,
            )
    if args.command == "extraction":
        queue = ExtractionJobQueue(root)
        if args.extraction_command == "list":
            jobs = queue.list_jobs(
                status=args.statuses,
                branch_id=args.branch_id,
                sequence_no=args.sequence_no,
                receipt_id=args.receipt_id,
                limit=args.limit,
                offset=args.offset,
            )
            return {
                "status": "ready",
                "count": len(jobs),
                "jobs": jobs,
            }
        if args.extraction_command == "inspect":
            return {
                "status": "ready",
                "job": queue.inspect(args.job_id),
            }
        if args.extraction_command == "retry":
            return queue.retry(
                args.job_id,
                expected_attempt_count=args.expected_attempt_count,
                next_attempt_at=args.next_attempt_at,
            )
    if args.command == "experience":
        service = EventExperienceService.for_project(root)
        if args.experience_command == "propose":
            return service.propose_contract(
                _json_mapping(
                    args.contract_json,
                    label="--contract-json",
                ),
                expected_control_revision=args.expected_control_revision,
                idempotency_key=args.idempotency_key,
            )
        if args.experience_command == "inspect":
            return {
                "status": "ready",
                "control_revision": service.get_control_revision(),
                "contract": service.get_contract(args.contract_id),
            }
        if args.experience_command == "lock":
            return service.lock_contract(
                args.contract_id,
                expected_control_revision=args.expected_control_revision,
                idempotency_key=args.idempotency_key,
                expected_contract_hash=args.expected_contract_hash,
            )
        if args.experience_command == "review":
            assistant_text = _text_input(
                file_value=args.assistant_file,
                text_value=args.assistant_text,
                label="experience review",
            )
            return service.record_review(
                _json_mapping(
                    args.review_json,
                    label="--review-json",
                ),
                expected_control_revision=args.expected_control_revision,
                idempotency_key=args.idempotency_key,
                assistant_text=assistant_text,
            )
    if args.command == "advantage":
        service = ContinuityService(root)
        if args.advantage_command == "definition":
            return _advantage_query_payload(
                service,
                helper_name="query_advantage_definition",
                advantage_id=args.advantage_id,
                result_key="definition",
                kwargs={"visibility": args.visibility},
            )
        if args.advantage_command in {"anchors", "anchor"}:
            return _advantage_query_payload(
                service,
                helper_name="query_advantage_anchors",
                advantage_id=args.advantage_id,
                result_key="anchors",
                kwargs={
                    "active_only": not args.include_inactive,
                    "include_noncanon": args.include_noncanon,
                    "visibility": args.visibility,
                },
            )
        if args.advantage_command == "runtime":
            return _advantage_query_payload(
                service,
                helper_name="query_advantage_runtime",
                advantage_id=args.advantage_id,
                result_key="runtime",
                kwargs={
                    "branch_id": args.branch_id,
                    "visibility": args.visibility,
                },
                allow_none=True,
            )
        if args.advantage_command in {"modules", "module"}:
            return _advantage_query_payload(
                service,
                helper_name="query_advantage_modules",
                advantage_id=args.advantage_id,
                result_key="modules",
                kwargs={
                    "enabled_only": args.enabled_only,
                    "visibility": args.visibility,
                },
            )
        if args.advantage_command == "ledger":
            return _advantage_query_payload(
                service,
                helper_name="query_advantage_ledger",
                advantage_id=args.advantage_id,
                result_key="ledger",
                kwargs={
                    "limit": args.limit,
                    "entry_kind": args.entry_kind,
                    "branch_id": args.branch_id,
                    "visibility": args.visibility,
                },
            )
        if args.advantage_command == "knowledge":
            return _advantage_query_payload(
                service,
                helper_name="query_advantage_knowledge",
                advantage_id=args.advantage_id,
                result_key="knowledge",
                kwargs={
                    "knowledge_plane": args.knowledge_plane,
                    "observer_entity_id": args.observer_entity_id,
                    "include_noncanon": args.include_noncanon,
                    "visibility": args.visibility,
                },
            )
        if args.advantage_command == "progression":
            return _advantage_query_payload(
                service,
                helper_name="query_advantage_progression",
                advantage_id=args.advantage_id,
                result_key="progression",
                kwargs={
                    "branch_id": args.branch_id,
                    "visibility": args.visibility,
                },
            )
        if args.advantage_command == "exposure":
            return _advantage_query_payload(
                service,
                helper_name="query_advantage_exposure",
                advantage_id=args.advantage_id,
                result_key="exposure",
                kwargs={
                    "branch_id": args.branch_id,
                    "visibility": args.visibility,
                },
            )
    if args.command in {"special-item", "special-item-context"}:
        return _advantage_query_payload(
            ContinuityService(root),
            helper_name="query_advantage_context",
            advantage_id=args.advantage_id,
            result_key=None,
            kwargs={
                "branch_id": args.branch_id,
                "knowledge_plane": args.knowledge_plane,
                "observer_entity_id": args.observer_entity_id,
                "ledger_limit": args.ledger_limit,
                "visibility": args.visibility,
            },
        )
    if args.command == "item":
        service = ContinuityService(root)
        if args.item_command == "definition":
            return service.query_item_definition(args.definition_id)
        if args.item_command == "instance":
            return service.query_item_instance(args.instance_id)
        if args.item_command == "inventory":
            actor_id = _resolved_actor_id(
                service,
                actor_entity_id=args.actor_id,
                mention=args.mention,
            )
            return service.query_actor_inventory(actor_id)
        if args.item_command == "custody":
            subject_type = {
                "instance": "item_instance",
                "stack": "item_stack",
            }.get(args.subject_type, args.subject_type)
            return service.query_item_custody(
                subject_type=subject_type,
                subject_id=args.subject_id,
            )
        if args.item_command == "function":
            return service.query_item_function(
                args.function_id,
                item_instance_id=args.instance_id,
                stack_id=args.stack_id,
            )
        if args.item_command == "runtime":
            return service.query_item_runtime(args.instance_id)
        if args.item_command == "history":
            return service.query_item_history(
                item_instance_id=args.instance_id,
                stack_id=args.stack_id,
                actor_entity_id=args.actor_id,
                limit=args.limit,
            )
        if args.item_command == "observations":
            return service.query_item_observations(
                item_instance_id=args.instance_id,
                stack_id=args.stack_id,
                observer_entity_id=args.observer_id,
                knowledge_plane=args.knowledge_plane,
                limit=args.limit,
                visibility=args.visibility,
            )
    if args.command == "prepare":
        return v1.prepare_plot_turn(
            root,
            args.prompt,
            request_id=args.request_id,
            session_id=args.session_id,
            turn_id=args.turn_id,
            artifact_stage=args.artifact_stage,
            branch_id=args.branch_id,
            chapter_no=args.chapter_no,
            scene_index=args.scene_index,
            artifact_id=args.artifact_id,
            task=args.task,
        )
    if args.command in {"propose", "commit"}:
        assistant_text = _text_input(
            file_value=args.assistant_file,
            text_value=args.assistant_text,
            label=args.command,
        )
        return v1.propose_plot_turn(
            root,
            assistant_text,
            request_id=args.request_id,
            session_id=args.session_id,
            turn_id=args.turn_id,
            prompt=args.prompt,
        )
    if args.command in {"query", "query-at"}:
        if args.command == "query-at":
            if not v1.is_strict_lifecycle(root):
                raise ValueError("query-at requires config v3 strict lifecycle")
            return v1.query_continuity(
                root,
                mention=args.mention,
                entity_id=args.entity_id,
                fact_type=args.fact_type,
                scope=args.scope,
                chapter_no=args.chapter_no,
                scene_index=args.scene_index,
                branch_id=args.branch_id,
                include_historical=args.include_historical,
                include_provisional=args.include_provisional,
                include_relations=not args.exclude_relations,
            )
        advanced = any(
            (
                args.mention,
                args.entity_id,
                args.fact_type,
                args.scope,
                args.chapter_no is not None,
                args.scene_index is not None,
                args.branch_id,
                args.include_provisional,
                args.exclude_relations,
            )
        )
        if not v1.is_strict_lifecycle(root):
            if advanced and not (args.query or args.mention):
                raise ValueError(
                    "legacy query requires --query or --mention"
                )
            return query_state(
                root,
                args.query or args.mention or "",
                categories=args.categories,
                top_k=args.top_k,
            )
        if advanced:
            return v1.query_continuity(
                root,
                mention=args.mention or args.query,
                entity_id=args.entity_id,
                fact_type=args.fact_type,
                scope=args.scope,
                chapter_no=args.chapter_no,
                scene_index=args.scene_index,
                branch_id=args.branch_id,
                include_historical=args.include_historical,
                include_provisional=args.include_provisional,
                include_relations=not args.exclude_relations,
            )
        if not args.query:
            raise ValueError("query requires --query or an advanced entity filter")
        return v1.query_continuity_text(
            root,
            args.query,
            categories=args.categories,
            top_k=args.top_k,
            include_historical=args.include_historical,
        )
    if args.command == "craft":
        return query_craft(root, args.query, top_k=args.top_k)
    if args.command == "dump":
        if v1.is_strict_lifecycle(root):
            config = load_config(root)
            state_path = Path(config["state"]["db_path"])
            if state_path.is_file():
                return v1.query_continuity_text(
                    root,
                    args.subject or "",
                    subject=args.subject,
                    category=args.category,
                    top_k=200,
                    include_historical=True,
                )
        return dump_state(root, subject=args.subject, category=args.category)
    if args.command == "doctor":
        return v1.doctor_v1(root)
    if args.command == "power":
        common = {
            "chapter_no": args.chapter_no,
            "scene_index": args.scene_index,
            "branch_id": args.branch_id,
            "knowledge_planes": args.knowledge_planes,
            "include_provisional": args.include_provisional,
        }
        if args.power_command == "systems":
            return v1.list_power_systems(root, **common)
        if args.power_command == "state":
            return v1.query_power_state(
                root,
                mention=args.mention,
                entity_id=args.entity_id,
                system_id=args.system_id,
                track_id=args.track_id,
                ability_id=args.ability_id,
                resource_id=args.resource_id,
                include_historical=args.include_historical,
                **common,
            )
        if args.power_command == "path":
            return v1.query_progression_path(
                root,
                mention=args.mention,
                entity_id=args.entity_id,
                system_id=args.system_id,
                track_id=args.track_id,
                target_rank_id=args.target_rank_id,
                **common,
            )
        if args.power_command == "explain":
            return v1.explain_power_action(
                root,
                action_id=args.action_id,
                mention=args.mention,
                entity_id=args.entity_id,
                system_id=args.system_id,
                track_id=args.track_id,
                ability_id=args.ability_id,
                resource_id=args.resource_id,
                target_rank_id=args.target_rank_id,
                **common,
            )
        if args.power_command == "compare":
            conditions = _json_input(args.conditions_json)
            if not isinstance(conditions, dict):
                raise ValueError("--conditions-json must contain a JSON object")
            return v1.compare_power_conditions(
                root,
                left_mention=args.left_mention,
                left_entity_id=args.left_entity_id,
                right_mention=args.right_mention,
                right_entity_id=args.right_entity_id,
                system_id=args.system_id,
                conditions=conditions,
                **common,
            )
    proposal_command = (
        args.proposal_command
        if args.command == "proposal"
        else {
            "list-proposals": "list",
            "inspect-proposal": "inspect",
            "accept-proposal": "accept",
            "reject-proposal": "reject",
            "retract-proposal": "retract",
        }.get(args.command)
    )
    if proposal_command == "list":
        service = ContinuityService(root)
        proposals = service.list_proposals(
            canon_status=args.canon_status,
            branch_id=args.branch_id,
        )
        return {
            "status": "ready",
            "canon_revisions": service.get_canon_revisions(),
            "count": len(proposals),
            "proposals": proposals,
        }
    if proposal_command == "inspect":
        service = ContinuityService(root)
        return {
            "status": "ready",
            "canon_revisions": service.get_canon_revisions(),
            "proposal": service.inspect_proposal(args.proposal_id),
        }
    if proposal_command == "accept":
        approval_id = _approval_id(
            args,
            root,
            operations=None,
            workspace_root=(
                _path(args.workspace_root)
                if getattr(args, "workspace_root", None)
                else None
            ),
        )
        return v1.accept_plot_proposal(
            root,
            args.proposal_id,
            approval_id=approval_id,
            expected_canon_revision=args.expected_canon_revision,
            workspace_root=getattr(args, "workspace_root", None),
        )
    if proposal_command == "reject":
        return v1.reject_plot_proposal(
            root,
            args.proposal_id,
            reason=args.reason,
            idempotency_key=args.idempotency_key,
        )
    if proposal_command == "retract":
        approval_id = _approval_id(
            args,
            root,
            operations=("retract",),
        )
        return v1.retract_plot_proposal(
            root,
            args.proposal_id,
            approval_id=approval_id,
            expected_canon_revision=args.expected_canon_revision,
            reason=args.reason,
        )
    if args.command == "source-manifest":
        if args.source_manifest_command == "status":
            return v1.source_manifest_status(root)
        plan = _json_mapping(args.plan_json, label="--plan")
        if args.source_manifest_command == "preview":
            return v1.preview_source_manifest_change(
                root,
                plan,
                expected_canon_revision=args.expected_canon_revision,
            )
        if args.source_manifest_command == "propose":
            return v1.propose_source_manifest_change(
                root,
                plan,
                expected_canon_revision=args.expected_canon_revision,
                idempotency_key=args.idempotency_key,
            )
    if args.command == "power-spec":
        power_spec = _json_mapping(args.spec_json, label="--spec")
        if args.power_spec_command == "preview":
            return v1.preview_power_spec_change(
                root,
                power_spec,
                expected_canon_revision=args.expected_canon_revision,
            )
        if args.power_spec_command == "propose":
            return v1.propose_power_spec_change(
                root,
                power_spec,
                expected_canon_revision=args.expected_canon_revision,
                idempotency_key=args.idempotency_key,
            )
    if args.command == "replay":
        return v1.replay_continuity(root)
    if args.command == "migrate":
        return v1.migrate_project(
            root,
            component=args.component,
            dry_run=args.dry_run,
        )
    if args.command == "longform":
        if args.longform_command in {"refresh", "index"}:
            return v1.refresh_longform_index(
                root,
                with_embeddings=args.with_embeddings,
            )
        if args.longform_command == "context":
            artifact_context = v1.infer_artifact_context(
                args.prompt,
                artifact_stage=args.artifact_stage,
                branch_id=args.branch_id,
                chapter_no=args.chapter_no,
                scene_index=args.scene_index,
                artifact_id=args.artifact_id,
                task=args.task,
            )
            return v1.build_longform_context(
                root,
                args.prompt,
                artifact_context=artifact_context,
                max_context_chars=args.max_context_chars,
            )
        if args.longform_command == "status":
            return v1.longform_status(root)
        if args.longform_command == "recover":
            return v1.recover_longform_projection(root, args.run_id)
    raise ValueError(f"unsupported command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _dispatch(args)
    except Exception as exc:
        result = {
            "status": "failed",
            "reason": str(exc),
        }
        code = getattr(exc, "code", None)
        details = getattr(exc, "details", None)
        if code:
            result["code"] = str(code)
        if isinstance(details, dict):
            result["details"] = details
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return (
        1
        if str(result.get("status") or "").casefold() in FAILED_STATUSES
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
