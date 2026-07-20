"""Canonical JSON, hashes, paths, and timestamps."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


VOLATILE_HASH_KEYS = frozenset(
    {
        "bundle_hash",
        "package_hash",
        "proposal_id",
        "session_id",
        "session_revision",
        "created_at",
        "updated_at",
        "mtime_ns",
        "observed_at",
        "head_revision",
        "active_revision",
        "journal_sequence",
    }
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def stable_id(prefix: str, *parts: Any, length: int = 24) -> str:
    digest = sha256_text("\x1f".join(canonical_json(part) for part in parts))
    return f"{prefix}-{digest[:length]}"


def normalize_real_path(path: Path | str) -> str:
    resolved = Path(path).expanduser().resolve(strict=False)
    return os.path.normcase(str(resolved)).replace("\\", "/")


def path_is_within(path: Path, root: Path) -> bool:
    normalized_path = os.path.normcase(str(path.resolve(strict=False)))
    normalized_root = os.path.normcase(str(root.resolve(strict=False)))
    try:
        return os.path.commonpath([normalized_path, normalized_root]) == normalized_root
    except ValueError:
        return False


def canonical_hash_payload(
    value: Any,
    *,
    extra_volatile_keys: Iterable[str] = (),
    strip_default_volatile: bool = False,
) -> Any:
    """Return a deterministic JSON-safe hashing payload.

    Generic callers must hash the complete value.  Earlier revisions removed
    names such as ``session_id``, ``created_at`` and ``active_revision``
    recursively for every caller, which made semantically different requests,
    cache prompts and source manifests collide merely because a nested domain
    object happened to use one of those field names.

    Initialization bundle hashes are the narrow exception: they intentionally
    exclude runtime/session metadata so the same reviewed content has the same
    package identity after restart.  Those callers opt in explicitly with
    ``strip_default_volatile=True``.
    """

    volatile = frozenset(extra_volatile_keys)
    if strip_default_volatile:
        volatile |= VOLATILE_HASH_KEYS

    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                str(key): clean(child)
                for key, child in sorted(item.items(), key=lambda pair: str(pair[0]))
                if str(key) not in volatile
            }
        if isinstance(item, list):
            return [clean(child) for child in item]
        if isinstance(item, tuple):
            return [clean(child) for child in item]
        return copy.deepcopy(item)

    return clean(value)


def canonical_hash(
    value: Any,
    *,
    extra_volatile_keys: Iterable[str] = (),
    strip_default_volatile: bool = False,
) -> str:
    payload = canonical_hash_payload(
        value,
        extra_volatile_keys=extra_volatile_keys,
        strip_default_volatile=strip_default_volatile,
    )
    return sha256_text(canonical_json(payload))


def json_pointer(parts: Iterable[str]) -> str:
    escaped = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(escaped)
