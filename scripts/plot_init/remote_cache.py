"""Secret-free response cache for optional initialization model calls.

The initialization engine is currently deterministic and local-first.  This
adapter is deliberately independent from any HTTP client so a later
SiliconFlow classifier or extractor can wrap its JSON request with
``resolve`` without gaining storage or canonical-write authority.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from collections import OrderedDict
from concurrent.futures import Future
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

try:  # ``scripts.plot_init`` package import.
    from ..sqlite_guard import (
        SQLiteComponentSchemaError,
        execute_sqlite_script_in_transaction,
        sqlite_user_tables,
        validate_sqlite_component_schema,
    )
except ImportError:  # Top-level ``plot_init`` with ``scripts`` on sys.path.
    from sqlite_guard import (
        SQLiteComponentSchemaError,
        execute_sqlite_script_in_transaction,
        sqlite_user_tables,
        validate_sqlite_component_schema,
    )

from .canonical import canonical_hash, canonical_json, sha256_text, utc_now
from .constants import DATABASE_SCHEMA_VERSION
from .errors import PlotInitError


REMOTE_CACHE_PROTOCOL = "plot-rag-init-remote-cache/v2"
REMOTE_CACHE_IDENTITY_PROTOCOL = "plot-rag-init-remote-cache-key/v2"
DEFAULT_MAX_ENTRIES = 512
DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
REMOTE_CACHE_TABLE = "initialization_remote_response_cache"
REMOTE_CACHE_SHARED_TABLES = frozenset(
    {
        "initialization_meta",
        "initialization_payload_blobs",
        "initialization_sessions",
        "initialization_revisions",
        "initialization_journal",
        "initialization_checkpoints",
        "initialization_idempotency",
        "initialization_proposals",
        "initialization_session_proposals",
        "initialization_source_versions",
        "initialization_session_sources",
        REMOTE_CACHE_TABLE,
    }
)
INIT_STORAGE_TABLES = REMOTE_CACHE_SHARED_TABLES - {REMOTE_CACHE_TABLE}
REMOTE_CACHE_COLUMNS = frozenset(
    {
        "cache_key",
        "model",
        "prompt_hash",
        "schema_hash",
        "source_hash",
        "response_json",
        "response_hash",
        "created_at",
        "accessed_at",
        "hit_count",
    }
)

REMOTE_CACHE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS initialization_remote_response_cache(
    cache_key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    schema_hash TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    response_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    accessed_at TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS initialization_remote_cache_accessed
ON initialization_remote_response_cache(accessed_at DESC, cache_key);

CREATE INDEX IF NOT EXISTS initialization_remote_cache_source
ON initialization_remote_response_cache(source_hash, model);
"""

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|"
    r"credential|client[_-]?secret|password|passwd|secret|token)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(
        r"(?i)(?P<key>\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"token|secret|password|passwd)\b[\"']?\s*[:=]\s*)"
        r"[\"']?[^\s,;}\"']{6,}[\"']?"
    ),
)

_FLIGHT_GUARD = threading.Lock()
_INFLIGHT_RESOLVES: dict[str, Future[tuple[str, Any]]] = {}


def _positive_integer_bound(
    value: Any,
    *,
    code: str,
    message: str,
) -> int:
    if type(value) is not int or value < 1:
        raise PlotInitError(code, message)
    return value


def _hash_input(value: Any) -> str:
    if isinstance(value, str):
        return sha256_text(value)
    return canonical_hash(value)


def _normalize_source_hash(value: str) -> str:
    source_hash = str(value or "").strip()
    if not source_hash:
        raise PlotInitError(
            "REMOTE_CACHE_SOURCE_HASH_REQUIRED",
            "remote response cache requires a source hash",
        )
    if _SHA256_RE.fullmatch(source_hash):
        return source_hash.casefold()
    return sha256_text(source_hash)


def normalize_remote_base_url(value: str) -> str:
    """Return a stable, credential-free OpenAI-compatible API base URL."""

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urllib.parse.urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise PlotInitError(
            "REMOTE_CACHE_BASE_URL_INVALID",
            "remote response cache requires a valid base URL",
        ) from exc
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise PlotInitError(
            "REMOTE_CACHE_BASE_URL_INVALID",
            "remote response cache requires a credential-free HTTP(S) base URL",
        )
    rendered_host = f"[{host}]" if ":" in host else host
    include_port = port is not None and not (
        (scheme == "https" and port == 443)
        or (scheme == "http" and port == 80)
    )
    netloc = f"{rendered_host}:{port}" if include_port else rendered_host
    path = parsed.path.rstrip("/")
    endpoint_suffix = "/chat/completions"
    if path.casefold().endswith(endpoint_suffix):
        path = path[: -len(endpoint_suffix)].rstrip("/")
    return urllib.parse.urlunsplit((scheme, netloc, path, "", ""))


def _redact_string(value: str) -> str:
    result = value
    for index, pattern in enumerate(_SECRET_VALUE_PATTERNS):
        if index == len(_SECRET_VALUE_PATTERNS) - 1:
            result = pattern.sub(
                lambda match: f"{match.group('key')}[REDACTED]",
                result,
            )
        else:
            result = pattern.sub("[REDACTED]", result)
    return result


def sanitize_remote_cache_value(value: Any) -> Any:
    """Return a JSON-safe deep copy with credentials removed.

    Request headers and credentials should never be passed as response data,
    but this fail-closed scrubber also handles model echo and accidental
    wrapper metadata before the payload crosses the persistence boundary.
    """

    def clean(item: Any) -> Any:
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, child in item.items():
                text_key = str(key)
                if _SENSITIVE_KEY_RE.search(text_key):
                    continue
                result[text_key] = clean(child)
            return result
        if isinstance(item, list):
            return [clean(child) for child in item]
        if isinstance(item, tuple):
            return [clean(child) for child in item]
        if isinstance(item, str):
            return _redact_string(item)
        if item is None or isinstance(item, (bool, int, float)):
            return copy.deepcopy(item)
        raise PlotInitError(
            "REMOTE_CACHE_VALUE_NOT_JSON",
            f"remote cache response contains unsupported value: {type(item).__name__}",
        )

    sanitized = clean(value)
    try:
        canonical_json(sanitized)
    except (TypeError, ValueError) as exc:
        raise PlotInitError(
            "REMOTE_CACHE_VALUE_NOT_JSON",
            "remote cache response must be finite JSON",
        ) from exc
    return sanitized


def _validated_cached_response(
    response: Any,
    response_hash: Any,
) -> tuple[bool, Any]:
    """Validate a persisted response before it can become a cache hit."""

    expected_hash = str(response_hash or "").strip().casefold()
    if not _SHA256_RE.fullmatch(expected_hash):
        return False, None
    try:
        sanitized = sanitize_remote_cache_value(response)
        actual_hash = canonical_hash(sanitized)
    except (PlotInitError, TypeError, ValueError):
        return False, None
    if actual_hash != expected_hash:
        return False, None
    return True, sanitized


@dataclass(frozen=True)
class RemoteCacheIdentity:
    """Versioned identity binding provider, prompt contract, and generation."""

    identity_protocol: str
    cache_key: str
    provider: str
    base_url: str
    model: str
    prompt_hash: str
    system_prompt_hash: str
    schema_hash: str
    source_hash: str
    generation_parameters_hash: str

    @classmethod
    def build(
        cls,
        *,
        provider: str = "unspecified",
        base_url: str = "",
        model: str,
        prompt: Any,
        system_prompt: Any = "",
        schema: Any,
        source_hash: str,
        generation_parameters: Mapping[str, Any] | None = None,
    ) -> "RemoteCacheIdentity":
        normalized_provider = str(provider or "").strip().casefold()
        if not normalized_provider:
            raise PlotInitError(
                "REMOTE_CACHE_PROVIDER_REQUIRED",
                "remote response cache requires a provider name",
            )
        normalized_base_url = normalize_remote_base_url(base_url)
        normalized_model = str(model or "").strip()
        if not normalized_model:
            raise PlotInitError(
                "REMOTE_CACHE_MODEL_REQUIRED",
                "remote response cache requires a model name",
            )
        prompt_hash = _hash_input(prompt)
        system_prompt_hash = _hash_input(system_prompt)
        schema_hash = _hash_input(schema)
        normalized_source_hash = _normalize_source_hash(source_hash)
        generation_payload = dict(generation_parameters or {})
        try:
            generation_parameters_json = canonical_json(generation_payload)
        except (TypeError, ValueError) as exc:
            raise PlotInitError(
                "REMOTE_CACHE_GENERATION_PARAMETERS_INVALID",
                "remote response cache generation parameters must be finite JSON",
            ) from exc
        generation_parameters_hash = sha256_text(generation_parameters_json)
        key_material = {
            "identity_protocol": REMOTE_CACHE_IDENTITY_PROTOCOL,
            "provider": normalized_provider,
            "base_url": normalized_base_url,
            "model": normalized_model,
            "prompt_hash": prompt_hash,
            "system_prompt_hash": system_prompt_hash,
            "schema_hash": schema_hash,
            "source_hash": normalized_source_hash,
            "generation_parameters": generation_payload,
        }
        return cls(
            identity_protocol=REMOTE_CACHE_IDENTITY_PROTOCOL,
            cache_key=sha256_text(canonical_json(key_material)),
            provider=normalized_provider,
            base_url=normalized_base_url,
            model=normalized_model,
            prompt_hash=prompt_hash,
            system_prompt_hash=system_prompt_hash,
            schema_hash=schema_hash,
            source_hash=normalized_source_hash,
            generation_parameters_hash=generation_parameters_hash,
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "identity_protocol": self.identity_protocol,
            "cache_key": self.cache_key,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "system_prompt_hash": self.system_prompt_hash,
            "schema_hash": self.schema_hash,
            "source_hash": self.source_hash,
            "generation_parameters_hash": self.generation_parameters_hash,
        }


class RemoteResponseCache:
    """Common adapter used by CLASSIFY/EXTRACT session stages."""

    storage_mode = "abstract"
    persistent = False

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
        session_id: str = "",
        stage: str = "",
    ) -> None:
        self.max_entries = _positive_integer_bound(
            max_entries,
            code="INVALID_REMOTE_CACHE_BOUND",
            message="remote cache max_entries must be a positive integer",
        )
        self.max_age_seconds = _positive_integer_bound(
            max_age_seconds,
            code="INVALID_REMOTE_CACHE_TTL",
            message="remote cache max_age_seconds must be a positive integer",
        )
        self.session_id = str(session_id or "")
        self.stage = str(stage or "")
        self._local_hits = 0
        self._local_misses = 0

    def bind(self, *, session_id: str, stage: str) -> "RemoteResponseCache":
        raise NotImplementedError

    def get(self, identity: RemoteCacheIdentity) -> Any | None:
        raise NotImplementedError

    def put(self, identity: RemoteCacheIdentity, response: Any) -> Any:
        raise NotImplementedError

    def invalidate(
        self,
        *,
        cache_key: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        schema_hash: str | None = None,
        source_hash: str | None = None,
    ) -> int:
        raise NotImplementedError

    def prune(
        self,
        *,
        max_entries: int | None = None,
        max_age_seconds: int | None = None,
    ) -> int:
        raise NotImplementedError

    def _flight_scope(self) -> str:
        return f"{type(self).__name__}:{id(self)}"

    def resolve(
        self,
        *,
        provider: str = "unspecified",
        base_url: str = "",
        model: str,
        prompt: Any,
        system_prompt: Any = "",
        schema: Any,
        source_hash: str,
        generation_parameters: Mapping[str, Any] | None = None,
        loader: Callable[[], Any],
    ) -> dict[str, Any]:
        """Return a cached response or execute one process-local miss flight.

        Concurrent callers with the same storage scope and cache identity join
        the same future.  A successful leader stores one response; a failed
        leader shares the same exception with current waiters and leaves no
        cache entry, so a later independent call can retry.
        """

        identity = RemoteCacheIdentity.build(
            provider=provider,
            base_url=base_url,
            model=model,
            prompt=prompt,
            system_prompt=system_prompt,
            schema=schema,
            source_hash=source_hash,
            generation_parameters=generation_parameters,
        )
        cached = self.get(identity)
        if cached is not None:
            self._local_hits += 1
            return {
                "protocol": REMOTE_CACHE_PROTOCOL,
                "cache_hit": True,
                **identity.as_dict(),
                "response": cached,
            }

        flight_key = f"{self._flight_scope()}:{identity.cache_key}"
        with _FLIGHT_GUARD:
            future = _INFLIGHT_RESOLVES.get(flight_key)
            leader = future is None
            if future is None:
                future = Future()
                _INFLIGHT_RESOLVES[flight_key] = future

        if not leader:
            _outcome, response = future.result()
            self._local_hits += 1
            return {
                "protocol": REMOTE_CACHE_PROTOCOL,
                "cache_hit": True,
                **identity.as_dict(),
                "response": copy.deepcopy(response),
            }

        try:
            # Another cache instance or process may have populated the entry
            # between the optimistic read and flight leadership.
            cached = self.get(identity)
            if cached is not None:
                self._local_hits += 1
                future.set_result(("cache", cached))
                return {
                    "protocol": REMOTE_CACHE_PROTOCOL,
                    "cache_hit": True,
                    **identity.as_dict(),
                    "response": cached,
                }
            self._local_misses += 1
            response = self.put(identity, loader())
            future.set_result(("loaded", response))
            return {
                "protocol": REMOTE_CACHE_PROTOCOL,
                "cache_hit": False,
                **identity.as_dict(),
                "response": response,
            }
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with _FLIGHT_GUARD:
                if _INFLIGHT_RESOLVES.get(flight_key) is future:
                    _INFLIGHT_RESOLVES.pop(flight_key, None)

    def describe(self) -> dict[str, Any]:
        return {
            "protocol": REMOTE_CACHE_PROTOCOL,
            "storage_mode": self.storage_mode,
            "persistent": self.persistent,
            "session_id": self.session_id,
            "stage": self.stage,
            "key_fields": [
                "model",
                "prompt_hash",
                "schema_hash",
                "source_hash",
            ],
            "identity_protocol": REMOTE_CACHE_IDENTITY_PROTOCOL,
            "identity_key_fields": [
                "identity_protocol",
                "provider",
                "base_url",
                "model",
                "prompt_hash",
                "system_prompt_hash",
                "schema_hash",
                "source_hash",
                "generation_parameters_hash",
            ],
            "legacy_cache_policy": (
                "v1 cache keys are unreachable under the v2 identity and "
                "expire through normal TTL or entry-limit pruning"
            ),
            "max_entries": self.max_entries,
            "max_age_seconds": self.max_age_seconds,
            "local_hits": self._local_hits,
            "local_misses": self._local_misses,
        }


class MemoryRemoteResponseCache(RemoteResponseCache):
    """Process-local cache used by one-shot ``init dry-run``."""

    storage_mode = "memory"
    persistent = False

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._entries: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._entry_lock = threading.RLock()

    def bind(self, *, session_id: str, stage: str) -> "MemoryRemoteResponseCache":
        bound = MemoryRemoteResponseCache(
            max_entries=self.max_entries,
            max_age_seconds=self.max_age_seconds,
            session_id=session_id,
            stage=stage,
        )
        bound._entries = self._entries
        bound._entry_lock = self._entry_lock
        return bound

    def _flight_scope(self) -> str:
        return f"memory:{id(self._entries)}"

    def _expired(self, entry: Mapping[str, Any], now: float) -> bool:
        return now - float(entry["created_epoch"]) > self.max_age_seconds

    def get(self, identity: RemoteCacheIdentity) -> Any | None:
        with self._entry_lock:
            entry = self._entries.get(identity.cache_key)
            if entry is None:
                return None
            now = time.time()
            if self._expired(entry, now):
                self._entries.pop(identity.cache_key, None)
                return None
            valid, response = _validated_cached_response(
                entry.get("response"),
                entry.get("response_hash"),
            )
            if not valid:
                self._entries.pop(identity.cache_key, None)
                return None
            entry["accessed_epoch"] = now
            entry["hit_count"] = int(entry["hit_count"]) + 1
            self._entries.move_to_end(identity.cache_key)
            return copy.deepcopy(response)

    def put(self, identity: RemoteCacheIdentity, response: Any) -> Any:
        sanitized = sanitize_remote_cache_value(response)
        now = time.time()
        with self._entry_lock:
            self._entries[identity.cache_key] = {
                **identity.as_dict(),
                "response": copy.deepcopy(sanitized),
                "response_hash": canonical_hash(sanitized),
                "created_epoch": now,
                "accessed_epoch": now,
                "hit_count": 0,
            }
            self._entries.move_to_end(identity.cache_key)
            self.prune()
        return copy.deepcopy(sanitized)

    def invalidate(
        self,
        *,
        cache_key: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        schema_hash: str | None = None,
        source_hash: str | None = None,
    ) -> int:
        selectors = {
            "cache_key": cache_key,
            "model": model,
            "prompt_hash": prompt_hash,
            "schema_hash": schema_hash,
            "source_hash": (
                _normalize_source_hash(source_hash) if source_hash else None
            ),
        }
        with self._entry_lock:
            removed = 0
            for key, entry in list(self._entries.items()):
                if all(
                    expected is None or str(entry.get(field)) == str(expected)
                    for field, expected in selectors.items()
                ):
                    del self._entries[key]
                    removed += 1
            return removed

    def prune(
        self,
        *,
        max_entries: int | None = None,
        max_age_seconds: int | None = None,
    ) -> int:
        entry_limit = (
            self.max_entries
            if max_entries is None
            else _positive_integer_bound(
                max_entries,
                code="INVALID_REMOTE_CACHE_BOUND",
                message="remote cache cleanup bounds must be positive integers",
            )
        )
        age_limit = (
            self.max_age_seconds
            if max_age_seconds is None
            else _positive_integer_bound(
                max_age_seconds,
                code="INVALID_REMOTE_CACHE_BOUND",
                message="remote cache cleanup bounds must be positive integers",
            )
        )
        with self._entry_lock:
            now = time.time()
            removed = 0
            for key, entry in list(self._entries.items()):
                if now - float(entry["created_epoch"]) > age_limit:
                    del self._entries[key]
                    removed += 1
            while len(self._entries) > entry_limit:
                self._entries.popitem(last=False)
                removed += 1
            return removed


class SQLiteRemoteResponseCache(RemoteResponseCache):
    """Persistent cache sharing the initialization session database."""

    storage_mode = "sqlite"
    persistent = True

    def __init__(self, database_path: Path | str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.database_path = Path(database_path).expanduser().resolve(strict=False)

    def bind(self, *, session_id: str, stage: str) -> "SQLiteRemoteResponseCache":
        return SQLiteRemoteResponseCache(
            self.database_path,
            max_entries=self.max_entries,
            max_age_seconds=self.max_age_seconds,
            session_id=session_id,
            stage=stage,
        )

    def _flight_scope(self) -> str:
        return f"sqlite:{os.path.normcase(str(self.database_path))}"

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(
            sqlite3.connect(self.database_path, timeout=30.0)
        ) as connection:
            connection.execute("BEGIN IMMEDIATE")
            tables = sqlite_user_tables(connection)
            unexpected = sorted(tables - REMOTE_CACHE_SHARED_TABLES)
            if unexpected:
                raise PlotInitError(
                    "REMOTE_CACHE_DATABASE_UNOWNED",
                    "remote cache database contains foreign user tables",
                    tables=unexpected,
                )
            try:
                validate_sqlite_component_schema(
                    connection,
                    component="initialization storage",
                    meta_table="initialization_meta",
                    version_key="schema_version",
                    supported_version=DATABASE_SCHEMA_VERSION,
                    compatible_versions=range(1, DATABASE_SCHEMA_VERSION + 1),
                    owned_tables=INIT_STORAGE_TABLES,
                    allowed_tables=REMOTE_CACHE_SHARED_TABLES,
                )
            except SQLiteComponentSchemaError as exc:
                raise PlotInitError(
                    "REMOTE_CACHE_SCHEMA_INVALID",
                    "remote cache cannot share an incompatible "
                    "initialization database",
                    reason=str(exc),
                ) from exc
            cache_only_database = not tables or tables == {REMOTE_CACHE_TABLE}
            if REMOTE_CACHE_TABLE in tables:
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        f"PRAGMA table_info({REMOTE_CACHE_TABLE})"
                    )
                }
                if columns != REMOTE_CACHE_COLUMNS:
                    raise PlotInitError(
                        "REMOTE_CACHE_SCHEMA_INVALID",
                        "remote cache table columns do not match the "
                        "supported schema",
                        missing_columns=sorted(
                            REMOTE_CACHE_COLUMNS - columns
                        ),
                        unexpected_columns=sorted(
                            columns - REMOTE_CACHE_COLUMNS
                        ),
                    )
            try:
                if cache_only_database:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS initialization_meta(
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO initialization_meta(key, value)
                        VALUES('schema_version', ?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value
                        """,
                        (str(DATABASE_SCHEMA_VERSION),),
                    )
                execute_sqlite_script_in_transaction(
                    connection,
                    REMOTE_CACHE_SCHEMA_SQL,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _cutoff(max_age_seconds: int) -> str:
        return (
            datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        ).isoformat(timespec="microseconds")

    def get(self, identity: RemoteCacheIdentity) -> Any | None:
        if not self.database_path.is_file():
            return None
        self._initialize()
        with closing(
            sqlite3.connect(self.database_path, timeout=30.0)
        ) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT response_json, response_hash, created_at
                FROM initialization_remote_response_cache
                WHERE cache_key=? AND model=? AND prompt_hash=?
                  AND schema_hash=? AND source_hash=?
                """,
                (
                    identity.cache_key,
                    identity.model,
                    identity.prompt_hash,
                    identity.schema_hash,
                    identity.source_hash,
                ),
            ).fetchone()
            if row is None:
                return None
            response_json = row["response_json"]
            response_hash = row["response_hash"]
            created_at = str(row["created_at"])
            if created_at < self._cutoff(self.max_age_seconds):
                connection.execute(
                    """
                    DELETE FROM initialization_remote_response_cache
                    WHERE cache_key=? AND response_json=? AND response_hash=?
                      AND created_at=?
                    """,
                    (
                        identity.cache_key,
                        response_json,
                        response_hash,
                        created_at,
                    ),
                )
                connection.commit()
                return None
            try:
                decoded = json.loads(response_json)
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                valid, response = False, None
            else:
                valid, response = _validated_cached_response(
                    decoded,
                    response_hash,
                )
            if not valid:
                connection.execute(
                    """
                    DELETE FROM initialization_remote_response_cache
                    WHERE cache_key=? AND response_json=? AND response_hash=?
                      AND created_at=?
                    """,
                    (
                        identity.cache_key,
                        response_json,
                        response_hash,
                        created_at,
                    ),
                )
                connection.commit()
                return None
            updated = connection.execute(
                """
                UPDATE initialization_remote_response_cache
                SET accessed_at=?, hit_count=hit_count+1
                WHERE cache_key=? AND response_json=? AND response_hash=?
                  AND created_at=?
                """,
                (
                    utc_now(),
                    identity.cache_key,
                    response_json,
                    response_hash,
                    created_at,
                ),
            )
            connection.commit()
            if updated.rowcount != 1:
                return None
            return copy.deepcopy(response)

    def put(self, identity: RemoteCacheIdentity, response: Any) -> Any:
        sanitized = sanitize_remote_cache_value(response)
        self._initialize()
        now = utc_now()
        with closing(
            sqlite3.connect(self.database_path, timeout=30.0)
        ) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO initialization_remote_response_cache(
                    cache_key, model, prompt_hash, schema_hash, source_hash,
                    response_json, response_hash, created_at, accessed_at,
                    hit_count
                ) VALUES(?,?,?,?,?,?,?,?,?,0)
                ON CONFLICT(cache_key) DO UPDATE SET
                    model=excluded.model,
                    prompt_hash=excluded.prompt_hash,
                    schema_hash=excluded.schema_hash,
                    source_hash=excluded.source_hash,
                    response_json=excluded.response_json,
                    response_hash=excluded.response_hash,
                    created_at=excluded.created_at,
                    accessed_at=excluded.accessed_at,
                    hit_count=0
                """,
                (
                    identity.cache_key,
                    identity.model,
                    identity.prompt_hash,
                    identity.schema_hash,
                    identity.source_hash,
                    canonical_json(sanitized),
                    canonical_hash(sanitized),
                    now,
                    now,
                ),
            )
            connection.commit()
        self.prune()
        return copy.deepcopy(sanitized)

    def invalidate(
        self,
        *,
        cache_key: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        schema_hash: str | None = None,
        source_hash: str | None = None,
    ) -> int:
        if not self.database_path.is_file():
            return 0
        self._initialize()
        clauses: list[str] = []
        parameters: list[str] = []
        for field, value in (
            ("cache_key", cache_key),
            ("model", model),
            ("prompt_hash", prompt_hash),
            ("schema_hash", schema_hash),
            (
                "source_hash",
                _normalize_source_hash(source_hash) if source_hash else None,
            ),
        ):
            if value is not None:
                clauses.append(f"{field}=?")
                parameters.append(str(value))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(
            sqlite3.connect(self.database_path, timeout=30.0)
        ) as connection:
            removed = connection.execute(
                f"DELETE FROM initialization_remote_response_cache{where}",
                tuple(parameters),
            ).rowcount
            connection.commit()
            return int(removed)

    def prune(
        self,
        *,
        max_entries: int | None = None,
        max_age_seconds: int | None = None,
    ) -> int:
        entry_limit = (
            self.max_entries
            if max_entries is None
            else _positive_integer_bound(
                max_entries,
                code="INVALID_REMOTE_CACHE_BOUND",
                message="remote cache cleanup bounds must be positive integers",
            )
        )
        age_limit = (
            self.max_age_seconds
            if max_age_seconds is None
            else _positive_integer_bound(
                max_age_seconds,
                code="INVALID_REMOTE_CACHE_BOUND",
                message="remote cache cleanup bounds must be positive integers",
            )
        )
        if not self.database_path.is_file():
            return 0
        self._initialize()
        removed = 0
        with closing(
            sqlite3.connect(self.database_path, timeout=30.0)
        ) as connection:
            removed += int(
                connection.execute(
                    """
                    DELETE FROM initialization_remote_response_cache
                    WHERE created_at < ?
                    """,
                    (self._cutoff(age_limit),),
                ).rowcount
            )
            stale = connection.execute(
                """
                SELECT cache_key
                FROM initialization_remote_response_cache
                ORDER BY accessed_at DESC, created_at DESC, cache_key DESC
                LIMIT -1 OFFSET ?
                """,
                (entry_limit,),
            ).fetchall()
            for (cache_key,) in stale:
                removed += int(
                    connection.execute(
                        "DELETE FROM initialization_remote_response_cache "
                        "WHERE cache_key=?",
                        (str(cache_key),),
                    ).rowcount
                )
            connection.commit()
        return removed
