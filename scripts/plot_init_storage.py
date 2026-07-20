"""Explicit inspection and migration CLI for initialization payload storage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from plot_init.errors import PlotInitError
from plot_init.storage import DEFAULT_MAX_PAYLOAD_BYTES, InitStorage


CANONICAL_DATABASE_RELATIVE_PATH = Path(".plot-rag") / "init.sqlite3"
LEGACY_DATABASE_RELATIVE_PATH = Path(".plot-rag-init") / "init.sqlite3"


def _add_database_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database",
        type=Path,
        help=(
            "initialization SQLite path; when omitted, prefer "
            "<workspace-root>/.plot-rag/init.sqlite3, then an existing "
            "legacy <workspace-root>/.plot-rag-init/init.sqlite3"
        ),
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path.cwd(),
        help="workspace used when --database is omitted",
    )
    parser.add_argument(
        "--max-payload-bytes",
        type=int,
        default=DEFAULT_MAX_PAYLOAD_BYTES,
        help="maximum uncompressed bytes accepted for one JSON payload",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plot_init_storage",
        description=(
            "Inspect or explicitly migrate initialization state payloads to "
            "schema-v2 content-addressed blob storage."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inspect_parser = commands.add_parser(
        "inspect",
        help="read-only schema, payload, deduplication, and integrity report",
    )
    _add_database_arguments(inspect_parser)

    migrate_parser = commands.add_parser(
        "migrate",
        help="online-backup then migrate legacy inline payloads",
    )
    _add_database_arguments(migrate_parser)
    migrate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="return the migration plan with zero writes",
    )
    migrate_parser.add_argument(
        "--backup",
        nargs="?",
        const="auto",
        default="auto",
        metavar="PATH",
        help=(
            "create the mandatory online backup at PATH; passing the flag "
            "without PATH selects an automatic backups/ path"
        ),
    )
    migrate_parser.add_argument(
        "--compact",
        action="store_true",
        help="run VACUUM after the migration transaction commits",
    )
    migrate_parser.add_argument(
        "--cleanup-orphans",
        action="store_true",
        help="delete unreferenced payload blobs inside the migration transaction",
    )
    return parser


def _default_database_path(workspace_root: Path) -> Path:
    canonical = workspace_root / CANONICAL_DATABASE_RELATIVE_PATH
    legacy = workspace_root / LEGACY_DATABASE_RELATIVE_PATH
    if canonical.is_file():
        return canonical
    if legacy.is_file():
        return legacy
    return canonical


def _storage_from_args(args: argparse.Namespace) -> InitStorage:
    workspace_root = Path(args.workspace_root).expanduser().resolve(strict=False)
    database_path = (
        Path(args.database).expanduser().resolve(strict=False)
        if args.database is not None
        else _default_database_path(workspace_root)
    )
    return InitStorage(
        database_path,
        max_payload_bytes=int(args.max_payload_bytes),
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    storage = _storage_from_args(args)
    if args.command == "inspect":
        return storage.migration_plan()
    return storage.migrate_payload_storage(
        dry_run=bool(args.dry_run),
        backup_path=None if args.backup == "auto" else Path(args.backup),
        compact=bool(args.compact),
        cleanup_orphans=bool(args.cleanup_orphans),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except PlotInitError as exc:
        print(
            json.dumps(
                exc.as_dict(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
