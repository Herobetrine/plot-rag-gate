from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from longform.benchmarking import (  # noqa: E402
    load_annotation_manifest,
    run_annotation_benchmark,
    run_power_annotation_benchmark,
    validate_annotation_manifest,
    validate_power_annotation_manifest,
)


DEFAULT_MANIFEST = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "longform_annotations.v1.jsonl"
)


def _is_power_manifest(path: str | Path) -> bool:
    records = load_annotation_manifest(path)
    first = records[0] if records else {}
    return str(first.get("suite") or "") == "plot-rag-power" or {
        "profile",
        "case_kind",
        "stop_envelope",
    }.issubset(first)


def validate_manifest(path: str | Path) -> dict[str, object]:
    if _is_power_manifest(path):
        return validate_power_annotation_manifest(path)
    return validate_annotation_manifest(path)


def run_manifest(
    path: str | Path,
    *,
    limit: int = 1,
) -> dict[str, object]:
    if _is_power_manifest(path):
        return run_power_annotation_benchmark(path)
    return run_annotation_benchmark(path, limit=limit)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate or run the reproducible assistant-text continuity "
            "proposal benchmark."
        )
    )
    parser.add_argument(
        "command",
        choices=("validate", "run"),
        nargs="?",
        default="run",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--top-k",
        type=int,
        default=1,
        help=(
            "Compatibility field retained in JSON output; proposal-gate "
            "evaluation does not rank retrieval candidates."
        ),
    )
    args = parser.parse_args()
    if args.command == "validate":
        result = validate_manifest(args.manifest)
    else:
        result = run_manifest(args.manifest, limit=args.top_k)
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
