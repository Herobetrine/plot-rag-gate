from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from benchmarks.v15_live_e2e import (  # noqa: E402
    PROMPT_FIXTURE_SCHEMA,
    STATE_MATRIX,
    collect_benchmark_provenance,
    compare_tree_snapshots,
    load_prompt_fixture,
    public_tree_snapshot,
    run_v15_live_e2e,
    scan_text_for_credentials,
    tree_snapshot,
    write_redacted_report,
)


VALIDATION_SCHEMA = "plot-rag-v15-live-e2e-validation/v1"
DEFAULT_PROMPTS = (
    PLUGIN_ROOT
    / "benchmarks"
    / "fixtures"
    / "v15_generic_live_prompts.v1.json"
)


def _default_output(mode: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (
        PLUGIN_ROOT
        / ".plot-rag-benchmark"
        / "v15-live-e2e"
        / f"{timestamp}-{mode}"
        / "result.redacted.json"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _validate_prompt_limit(value: int | None) -> None:
    if value is not None and (
        isinstance(value, bool) or int(value) < 1
    ):
        raise ValueError("--prompt-limit must be a positive integer")


def validate_inputs(
    *,
    project_root: Path | str,
    prompts_path: Path | str,
    prompt_limit: int | None = None,
    include_strict: bool = True,
) -> dict[str, Any]:
    """Read-only preflight for the source project and prompt fixture.

    This deliberately does not call ``run_v15_live_e2e``: validation creates
    no workspace, copies no project files, writes no report, and performs no
    remote request.
    """

    source = Path(project_root).expanduser().resolve()
    prompts_file = Path(prompts_path).expanduser().resolve()
    _validate_prompt_limit(prompt_limit)

    before = tree_snapshot(source)
    prompts = load_prompt_fixture(prompts_file)
    selected = (
        prompts[: int(prompt_limit)]
        if prompt_limit is not None
        else prompts
    )
    if not selected:
        raise ValueError("at least one prompt is required")

    config_path = source / ".plot-rag" / "config.json"
    if config_path.is_file():
        config_value = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config_value, Mapping):
            raise ValueError("project .plot-rag/config.json must be an object")
        config_mode = "project"
        config_version = config_value.get("config_version")
    else:
        template_path = PLUGIN_ROOT / "templates" / "config.v3.json"
        template_value = json.loads(template_path.read_text(encoding="utf-8"))
        if not isinstance(template_value, Mapping):
            raise ValueError("default config template must be an object")
        config_mode = "template_fallback"
        config_version = template_value.get("config_version")

    after = tree_snapshot(source)
    comparison = compare_tree_snapshots(before, after)
    result: dict[str, Any] = {
        "schema_version": VALIDATION_SCHEMA,
        "status": "valid" if comparison["unchanged"] else "invalid",
        "prompt_fixture_schema": PROMPT_FIXTURE_SCHEMA,
        "prompt_fixture_sha256": _sha256_bytes(prompts_file.read_bytes()),
        "fixture_prompt_count": len(prompts),
        "selected_prompt_count": len(selected),
        "state_count": len(STATE_MATRIX),
        "expected_measured_round_count": len(selected) * len(STATE_MATRIX),
        "task_counts": dict(
            sorted(Counter(str(item["task"]) for item in selected).items())
        ),
        "artifact_stage_counts": dict(
            sorted(
                Counter(
                    str(item["artifact_stage"]) for item in selected
                ).items()
            )
        ),
        "strict_chain_requested": bool(include_strict),
        "provenance": collect_benchmark_provenance(),
        "project_config": {
            "mode": config_mode,
            "config_version": config_version,
        },
        "source_snapshot": public_tree_snapshot(before),
        "formal_project_tree": comparison,
        "side_effect_contract": {
            "workspace_created": False,
            "project_copied": False,
            "report_written": False,
            "remote_calls": 0,
        },
    }
    credential_scan = scan_text_for_credentials(_canonical_json(result))
    result["credential_scan"] = {
        **credential_scan,
        "scope": "validation_stdout",
    }
    if credential_scan["finding_count"]:
        result["status"] = "invalid"
    return result


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Validate or run the isolated plot-rag-gate v1.5 four-state A/B "
            "and strict continuity benchmark without writing the source "
            "novel project."
        )
    )
    value.add_argument(
        "command",
        choices=("validate", "offline", "live"),
        nargs="?",
        default=None,
        help=(
            "validate is read-only; offline uses deterministic local "
            "Embedding/Rerank; live uses SiliconFlow."
        ),
    )
    value.add_argument("--project-root", required=True)
    value.add_argument(
        "--transport",
        choices=("offline", "live"),
        help=argparse.SUPPRESS,
    )
    value.add_argument("--prompts", default=str(DEFAULT_PROMPTS))
    value.add_argument("--workspace-parent")
    value.add_argument("--prompt-limit", type=int)
    value.add_argument("--warmup", action="store_true")
    value.add_argument("--skip-strict", action="store_true")
    value.add_argument(
        "--chat-extraction-smoke",
        action="store_true",
        help=(
            "with live transport, run one isolated real SiliconFlow Chat "
            "extraction and report its latency separately from Prepare"
        ),
    )
    value.add_argument("--keep-workspace", action="store_true")
    value.add_argument("--output")
    value.add_argument("--overwrite", action="store_true")
    value.add_argument("--pretty", action="store_true")
    return value


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    arguments = argument_parser.parse_args(argv)
    if arguments.command and arguments.transport:
        argument_parser.error(
            "use either the validate/offline/live command or --transport"
        )
    command = arguments.command or arguments.transport or "offline"
    project_root = Path(arguments.project_root).expanduser().resolve()

    if command == "validate":
        forbidden = [
            name
            for name, enabled in (
                ("--workspace-parent", arguments.workspace_parent is not None),
                ("--warmup", bool(arguments.warmup)),
                (
                    "--chat-extraction-smoke",
                    bool(arguments.chat_extraction_smoke),
                ),
                ("--keep-workspace", bool(arguments.keep_workspace)),
                ("--output", arguments.output is not None),
                ("--overwrite", bool(arguments.overwrite)),
            )
            if enabled
        ]
        if forbidden:
            argument_parser.error(
                "validate is read-only and does not accept "
                + ", ".join(forbidden)
            )
        result = validate_inputs(
            project_root=project_root,
            prompts_path=arguments.prompts,
            prompt_limit=arguments.prompt_limit,
            include_strict=not arguments.skip_strict,
        )
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                indent=2 if arguments.pretty else None,
                separators=None if arguments.pretty else (",", ":"),
            )
        )
        return 0 if result.get("status") == "valid" else 1

    if arguments.chat_extraction_smoke and command != "live":
        argument_parser.error(
            "--chat-extraction-smoke requires the live command"
        )

    workspace_parent = (
        Path(arguments.workspace_parent).expanduser().resolve()
        if arguments.workspace_parent
        else None
    )
    if workspace_parent is not None and _is_within(
        workspace_parent,
        project_root,
    ):
        argument_parser.error(
            "--workspace-parent must be outside --project-root"
        )
    output = (
        Path(arguments.output).expanduser().resolve()
        if arguments.output
        else _default_output(command).resolve()
    )
    try:
        output.relative_to(project_root)
    except ValueError:
        pass
    else:
        parser().error("--output must be outside --project-root")
    report = run_v15_live_e2e(
        project_root=project_root,
        prompts_path=arguments.prompts,
        transport=command,
        workspace_parent=workspace_parent,
        prompt_limit=arguments.prompt_limit,
        warmup=arguments.warmup,
        include_strict=not arguments.skip_strict,
        include_chat_extraction_smoke=arguments.chat_extraction_smoke,
        keep_workspace=arguments.keep_workspace,
    )
    artifact = write_redacted_report(
        report,
        output,
        overwrite=arguments.overwrite,
        pretty=arguments.pretty,
    )
    print(
        json.dumps(
            {
                "passed": bool(report.get("passed")),
                "transport": report.get("transport"),
                "prompt_count": report.get("prompt_count"),
                "measured_round_count": report.get(
                    "measured_round_count"
                ),
                "strict_chain": (
                    report.get("strict_chain") or {}
                ).get("status"),
                "chat_extraction_smoke": (
                    report.get("chat_extraction_smoke") or {}
                ).get("status"),
                "formal_project_unchanged": (
                    report.get("formal_project_tree") or {}
                ).get("unchanged"),
                "output": artifact["output"],
                "sha256": artifact["sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
