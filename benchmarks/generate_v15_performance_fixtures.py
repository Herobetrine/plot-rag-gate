from __future__ import annotations

import argparse
import sys
from pathlib import Path


BENCHMARKS = Path(__file__).resolve().parent
if str(BENCHMARKS) not in sys.path:
    sys.path.insert(0, str(BENCHMARKS))

from v15_performance import (  # noqa: E402
    DEFAULT_FIXTURE,
    default_fixture_manifest,
    validate_fixture_manifest,
    write_json,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the synthetic v1.5 performance fixture."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_FIXTURE)
    args = parser.parse_args(argv)
    manifest = default_fixture_manifest()
    validate_fixture_manifest(manifest)
    write_json(args.output, manifest, overwrite=True)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
