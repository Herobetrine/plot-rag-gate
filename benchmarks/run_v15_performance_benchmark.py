from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType


BENCHMARKS = Path(__file__).resolve().parent
PLUGIN_ROOT = BENCHMARKS.parent


def _load_performance_runtime() -> ModuleType:
    runtime_path = PLUGIN_ROOT / "scripts" / "performance_runtime.py"
    module_name = (
        "_plot_rag_gate_performance_runtime_cli_"
        + hashlib.sha256(str(PLUGIN_ROOT).encode("utf-8")).hexdigest()[:16]
    )
    existing = sys.modules.get(module_name)
    if isinstance(existing, ModuleType):
        existing_path = Path(str(getattr(existing, "__file__", "")))
        if (
            existing_path.resolve(strict=False)
            != runtime_path.resolve(strict=False)
        ):
            raise RuntimeError(
                f"isolated performance runtime collision: {module_name}"
            )
        return existing
    spec = importlib.util.spec_from_file_location(module_name, runtime_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("performance runtime is not loadable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if sys.modules.get(module_name) is module:
            sys.modules.pop(module_name, None)
        raise
    return module


_PERFORMANCE_RUNTIME = _load_performance_runtime()
_BENCHMARK = _PERFORMANCE_RUNTIME._load_benchmark_module()
DEFAULT_ARTIFACT_ROOT = _BENCHMARK.DEFAULT_ARTIFACT_ROOT
DEFAULT_FIXTURE = _BENCHMARK.DEFAULT_FIXTURE
DEFAULT_ITERATIONS = _BENCHMARK.DEFAULT_ITERATIONS
DEFAULT_WARMUP_ITERATIONS = _BENCHMARK.DEFAULT_WARMUP_ITERATIONS
BenchmarkFixtureError = _BENCHMARK.BenchmarkFixtureError
build_redacted_result = _BENCHMARK.build_redacted_result
create_run_artifact_directory = _BENCHMARK.create_run_artifact_directory
run_v15_performance_benchmark = _BENCHMARK.run_v15_performance_benchmark
validate_fixture_manifest = _BENCHMARK.validate_fixture_manifest
write_json = _BENCHMARK.write_json


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        raise BenchmarkFixtureError(
            "fixture manifest is invalid JSON"
        ) from error
    if not isinstance(value, dict):
        raise BenchmarkFixtureError(
            "fixture manifest root must be an object"
        )
    return value


def _normalized_output_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _preflight_output_paths(
    output: Path,
    manifest_output: Path,
    *,
    overwrite: bool,
) -> None:
    destinations = (output, manifest_output)
    if len({_normalized_output_key(path) for path in destinations}) != 2:
        raise ValueError(
            "--output and --redacted-manifest-output must be different paths"
        )
    if overwrite:
        return
    existing = [path for path in destinations if path.exists()]
    if existing:
        rendered = ", ".join(str(path) for path in existing)
        raise FileExistsError(
            "refusing to replace existing benchmark artifact(s): "
            f"{rendered}; pass --overwrite to replace both outputs"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the deterministic offline plot-rag-gate v1.5 retrieval "
            "performance benchmark."
        )
    )
    parser.add_argument(
        "command",
        choices=("validate", "run"),
        nargs="?",
        default="run",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument(
        "--workspace-parent",
        type=Path,
        help=(
            "Optional parent for the temporary benchmark workspace. "
            "The workspace is removed after the run."
        ),
    )
    parser.add_argument(
        "--rerank-delay-ms",
        type=int,
        help=(
            "Override the deterministic offline rerank delay. "
            "Use 0 for a fast contract-only run."
        ),
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=DEFAULT_ITERATIONS,
        help="Measured iterations per scenario.",
    )
    parser.add_argument(
        "--warmup-iterations",
        type=int,
        default=DEFAULT_WARMUP_ITERATIONS,
        help="Discarded warmup iterations per scenario.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--redacted-manifest-output", type=Path)
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT,
        help=(
            "Parent for timestamped run directories when explicit output "
            "paths are omitted."
        ),
    )
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Replace both output artifacts when they already exist. "
            "Without this flag the runner preflights both paths and fails "
            "before writing either file."
        ),
    )
    args = parser.parse_args(argv)
    manifest = _load_manifest(args.manifest)

    if args.command == "validate":
        display_result = validate_fixture_manifest(manifest)
    else:
        raw_result = run_v15_performance_benchmark(
            manifest,
            workspace_parent=args.workspace_parent,
            rerank_delay_ms=args.rerank_delay_ms,
            iterations=args.iterations,
            warmup_iterations=args.warmup_iterations,
        )
        display_result = build_redacted_result(raw_result)
        output = args.output
        manifest_output = args.redacted_manifest_output
        if output is None or manifest_output is None:
            run_directory, run_id, started_at = create_run_artifact_directory(
                args.artifact_root,
                run_id=str(raw_result["provenance"]["run_id"]),
                started_at_utc=str(
                    raw_result["provenance"]["started_at_utc"]
                ),
            )
            output = output or run_directory / "result.redacted.json"
            manifest_output = (
                manifest_output
                or run_directory / "run-manifest.redacted.json"
            )
            display_result["artifact_run"] = {
                "run_id": run_id,
                "started_at_utc": started_at,
                "result_filename": output.name,
                "manifest_filename": manifest_output.name,
            }
        _preflight_output_paths(
            output,
            manifest_output,
            overwrite=args.overwrite,
        )
        write_json(output, display_result, overwrite=args.overwrite)
        write_json(
            manifest_output,
            display_result["redacted_manifest"],
            overwrite=args.overwrite,
        )
    print(
        json.dumps(
            display_result,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            allow_nan=False,
        )
    )
    return (
        0
        if display_result.get("status") in {"valid", "passed"}
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
