from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

try:  # ``scripts.longform`` package import.
    from ..sqlite_guard import (
        SQLiteComponentSchemaError,
        execute_sqlite_script_in_transaction,
        validate_sqlite_component_schema,
    )
except ImportError:  # Top-level ``longform`` with ``scripts`` on sys.path.
    from sqlite_guard import (
        SQLiteComponentSchemaError,
        execute_sqlite_script_in_transaction,
        validate_sqlite_component_schema,
    )


class _ClosingConnection(sqlite3.Connection):
    """Close SQLite handles when leaving a ``with`` block on Windows."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


AUTHORITY_INDEX_SCHEMA_VERSION = 1
AUTHORITY_INDEX_TABLES = frozenset(
    {
        "authority_index_meta",
        "authority_files",
        "authority_chunks",
        "authority_vectors",
        "rerank_candidate_cache",
        "authority_chunks_fts",
        "authority_chunks_fts_data",
        "authority_chunks_fts_idx",
        "authority_chunks_fts_content",
        "authority_chunks_fts_docsize",
        "authority_chunks_fts_config",
    }
)
_VALID_ROLES = {
    "canon",
    "setting",
    "outline",
    "draft",
    "reference",
    "note",
    "unknown",
}
_VALID_SCOPE_POLICIES = {
    "infer_and_review",
    "current",
    "current_only",
    "planned",
    "planned_only",
    "historical",
    "historical_only",
    "timeless",
    "timeless_only",
    "timeless_candidate",
    "preserve_unknown",
}
_VALID_INGEST_POLICIES = {"include", "review", "exclude"}
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]+")
_HARD_EXCLUDED_PARTS = frozenset({".git", ".plot-rag", ".plot-rag-init"})
_QUERY_EMBEDDING_PREPROCESSING_VERSION = "authority-query-normalize/v1"
_DEFAULT_QUERY_EMBEDDING_CACHE_SIZE = 2048
_DEFAULT_EMBEDDING_BATCH_SIZE = 32
_DEFAULT_EMBEDDING_BATCH_MAX_CHARS = 24_000
_DEFAULT_EMBEDDING_SINGLE_MAX_CONCURRENCY = 1
_DEFAULT_RERANK_MAX_CONCURRENCY = 4
_AUTHORITY_SCORING_VERSION = "authority-hybrid-score/v3"
_RERANK_NORMALIZATION_VERSION = "authority-rerank-rank-fusion/v3"
_RERANK_RESULT_CACHE_SIZE = 4096
_SHARED_FLIGHT_LOCK = threading.RLock()
_SHARED_QUERY_EMBEDDING_CACHE: OrderedDict[str, list[float]] = OrderedDict()
_SHARED_QUERY_EMBEDDING_FLIGHTS: dict[
    str,
    Future[tuple[list[float] | None, str]],
] = {}
_SHARED_SEARCH_FLIGHTS: dict[
    str,
    Future[list[dict[str, Any]]],
] = {}
_SHARED_RERANK_RESULT_CACHE: OrderedDict[
    str,
    tuple[Any | None, tuple[tuple[int, float], ...]],
] = OrderedDict()
_SHARED_RERANK_RESULT_FLIGHTS: dict[
    str,
    Future[tuple[tuple[int, float], ...]],
] = {}


class AuthorityIndexError(RuntimeError):
    """Raised when the derived authority index is invalid or inaccessible."""


@dataclass(frozen=True)
class _SearchSpec:
    """Normalized immutable input for one authority query."""

    query: str
    normalized_query: str
    limit: int
    roles: tuple[str, ...]
    scope_policies: tuple[str, ...]
    ingest_policies: tuple[str, ...]
    use_candidate_cache: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _tokens(value: str) -> list[str]:
    result: list[str] = []
    for match in _TOKEN_RE.finditer(value.casefold()):
        token = match.group(0)
        if token and "\u3400" <= token[0] <= "\u9fff":
            if len(token) == 1:
                result.append(token)
            else:
                result.extend(token[index : index + 2] for index in range(len(token) - 1))
        else:
            result.append(token)
    return result


def _search_text(value: str) -> str:
    return " ".join(_tokens(value))


def _lexical_score(query: str, text: str) -> float:
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0.0
    document_tokens = _tokens(text)
    if not document_tokens:
        return 0.0
    query_set = set(query_tokens)
    document_set = set(document_tokens)
    overlap = len(query_set & document_set)
    coverage = overlap / max(1, len(query_set))
    precision = overlap / max(1, len(document_set))
    exact_bonus = 0.25 if _normalize_text(query) in _normalize_text(text) else 0.0
    return coverage * 0.78 + precision * 0.22 + exact_bonus


def _coerce_vector(values: Sequence[float], *, label: str) -> list[float]:
    if isinstance(values, (str, bytes, bytearray)):
        raise AuthorityIndexError(f"{label} must be a numeric vector")
    vector: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AuthorityIndexError(
                f"{label} contains a non-numeric component"
            )
        number = float(value)
        if not math.isfinite(number):
            raise AuthorityIndexError(
                f"{label} contains a non-finite component"
            )
        vector.append(number)
    if not vector or len(vector) > 65_536:
        raise AuthorityIndexError(f"{label} has invalid dimensions")
    return vector


def _vector_norm(values: Sequence[float]) -> float | None:
    """Return the scalar-compatible Euclidean norm for one vector."""

    if not values:
        return None
    try:
        norm = math.sqrt(sum(value * value for value in values))
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(norm) or norm <= 0.0:
        return None
    return norm


def _cosine_many(
    left: Sequence[float],
    rights: Sequence[Sequence[float] | None],
) -> list[float | None]:
    """Score a candidate batch while computing the query norm exactly once.

    Candidate failures are isolated to their own output slot.  The arithmetic
    order for every valid candidate matches the former scalar implementation:
    dot product, Euclidean norms, division, finite check, then clamping.
    """

    if not left:
        return [None for _right in rights]
    left_norm = _vector_norm(left)
    if left_norm is None:
        return [None for _right in rights]
    scores: list[float | None] = []
    for right in rights:
        if right is None or not right or len(left) != len(right):
            scores.append(None)
            continue
        try:
            dot = sum(a * b for a, b in zip(left, right))
        except (OverflowError, TypeError, ValueError):
            scores.append(None)
            continue
        right_norm = _vector_norm(right)
        if right_norm is None:
            scores.append(None)
            continue
        try:
            score = dot / (left_norm * right_norm)
        except (OverflowError, ZeroDivisionError):
            scores.append(None)
            continue
        if not math.isfinite(score):
            scores.append(None)
            continue
        scores.append(max(-1.0, min(1.0, score)))
    return scores


def _cosine(left: Sequence[float], right: Sequence[float]) -> float | None:
    """Scalar compatibility wrapper around the batched implementation."""

    return _cosine_many(left, [right])[0]


@dataclass(frozen=True)
class AuthoritySource:
    """A normalized source rule used by the independent authority index."""

    glob: str
    role: str = "unknown"
    priority: int = 0
    scope_policy: str = "infer_and_review"
    ingest_policy: str = "include"

    def __post_init__(self) -> None:
        if not self.glob or Path(self.glob).is_absolute():
            raise ValueError("authority source glob must be a non-empty relative pattern")
        if self.role not in _VALID_ROLES:
            raise ValueError(f"unsupported authority source role: {self.role}")
        if self.scope_policy not in _VALID_SCOPE_POLICIES:
            raise ValueError(f"unsupported scope policy: {self.scope_policy}")
        if self.ingest_policy not in _VALID_INGEST_POLICIES:
            raise ValueError(f"unsupported ingest policy: {self.ingest_policy}")
        if not -1000 <= int(self.priority) <= 1000:
            raise ValueError("authority source priority must be between -1000 and 1000")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AuthoritySource":
        return cls(
            glob=str(value.get("glob", "")),
            role=str(value.get("role", "unknown")),
            priority=int(value.get("priority", 0)),
            scope_policy=str(value.get("scope_policy", "infer_and_review")),
            ingest_policy=str(value.get("ingest_policy", "include")),
        )

    @property
    def policy_sha256(self) -> str:
        return _sha256_bytes(_stable_json(asdict(self)).encode("utf-8"))


class AuthorityIndex:
    """Persistent FTS5/BM25 authority index with deterministic lexical fallback.

    Every refresh reads source bytes and computes SHA-256.  Parsing, chunking,
    and embedding are skipped only when the content hash and normalized source
    policy hash are unchanged; mtime and size are observational metadata only.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        max_chunk_chars: int = 1600,
        embedding_provider: Callable[[str], Sequence[float]] | None = None,
        embedding_batch_provider: (
            Callable[[Sequence[str]], Sequence[Any]] | None
        ) = None,
        embedding_model: str = "local-or-remote",
        rerank_provider: (
            Callable[
                [str, Sequence[str], int],
                Sequence[tuple[int, float]],
            ]
            | None
        ) = None,
        rerank_model: str = "disabled",
        force_lexical_fallback: bool = False,
        embedding_batch_size: int = _DEFAULT_EMBEDDING_BATCH_SIZE,
        embedding_batch_max_chars: int = _DEFAULT_EMBEDDING_BATCH_MAX_CHARS,
        embedding_single_max_concurrency: int = (
            _DEFAULT_EMBEDDING_SINGLE_MAX_CONCURRENCY
        ),
        rerank_max_concurrency: int = _DEFAULT_RERANK_MAX_CONCURRENCY,
        query_embedding_cache_size: int = _DEFAULT_QUERY_EMBEDDING_CACHE_SIZE,
        singleflight_enabled: bool = True,
    ) -> None:
        self.database_path = Path(database_path)
        self.max_chunk_chars = max(128, int(max_chunk_chars))
        self.embedding_provider = embedding_provider
        discovered_batch_provider = None
        if embedding_provider is not None:
            for attribute in ("embed_many", "batch_embed", "embedding_batch"):
                candidate = getattr(embedding_provider, attribute, None)
                if callable(candidate):
                    discovered_batch_provider = candidate
                    break
        self.embedding_batch_provider = (
            embedding_batch_provider or discovered_batch_provider
        )
        self.embedding_model = embedding_model
        self.rerank_provider = rerank_provider
        self.rerank_model = rerank_model
        self.force_lexical_fallback = force_lexical_fallback
        self.embedding_batch_size = max(1, int(embedding_batch_size))
        self.embedding_batch_max_chars = max(
            1,
            int(embedding_batch_max_chars),
        )
        self.embedding_single_max_concurrency = max(
            1,
            int(embedding_single_max_concurrency),
        )
        self.rerank_max_concurrency = max(1, int(rerank_max_concurrency))
        self.query_embedding_cache_size = max(
            0,
            int(query_embedding_cache_size),
        )
        self.singleflight_enabled = bool(singleflight_enabled)
        # Flights and exact query embeddings are process-scoped so concurrent
        # Prepare calls that construct separate AuthorityIndex wrappers still
        # collapse identical remote work.
        self._query_embedding_cache = _SHARED_QUERY_EMBEDDING_CACHE
        self._query_embedding_flights = _SHARED_QUERY_EMBEDDING_FLIGHTS
        self._search_flights = _SHARED_SEARCH_FLIGHTS
        self._rerank_result_cache = _SHARED_RERANK_RESULT_CACHE
        self._rerank_result_flights = _SHARED_RERANK_RESULT_FLIGHTS
        self._flight_lock = _SHARED_FLIGHT_LOCK
        self._diagnostics_local = threading.local()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            factory=_ClosingConnection,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except BaseException:
            with suppress(sqlite3.Error):
                connection.close()
            raise

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                validate_sqlite_component_schema(
                    connection,
                    component="authority index",
                    meta_table="authority_index_meta",
                    version_key="authority_index_schema_version",
                    supported_version=AUTHORITY_INDEX_SCHEMA_VERSION,
                    owned_tables=AUTHORITY_INDEX_TABLES,
                    allowed_tables=AUTHORITY_INDEX_TABLES,
                )
            except SQLiteComponentSchemaError as exc:
                if exc.code == "SQLITE_COMPONENT_SCHEMA_UNSUPPORTED":
                    raise AuthorityIndexError(
                        "authority index schema version does not match "
                        "this engine"
                    ) from exc
                raise
            execute_sqlite_script_in_transaction(
                connection,
                """
                CREATE TABLE IF NOT EXISTS authority_index_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS authority_files (
                    path TEXT PRIMARY KEY,
                    content_sha256 TEXT NOT NULL,
                    policy_sha256 TEXT NOT NULL,
                    byte_count INTEGER NOT NULL,
                    observed_mtime_ns INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    scope_policy TEXT NOT NULL,
                    ingest_policy TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    indexed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS authority_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    path TEXT NOT NULL REFERENCES authority_files(path) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    role TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    scope_policy TEXT NOT NULL,
                    ingest_policy TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS authority_chunks_path
                    ON authority_chunks(path, ordinal);
                CREATE INDEX IF NOT EXISTS authority_chunks_role
                    ON authority_chunks(role, ingest_policy, priority);
                CREATE TABLE IF NOT EXISTS authority_vectors (
                    content_sha256 TEXT NOT NULL,
                    model TEXT NOT NULL,
                    vector_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (content_sha256, model)
                );
                CREATE TABLE IF NOT EXISTS rerank_candidate_cache (
                    cache_key TEXT PRIMARY KEY,
                    index_digest TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """,
            )
            current = connection.execute(
                """
                SELECT value FROM authority_index_meta
                WHERE key = 'authority_index_schema_version'
                """
            ).fetchone()
            if current is not None and int(current["value"]) != AUTHORITY_INDEX_SCHEMA_VERSION:
                raise AuthorityIndexError(
                    "authority index schema version does not match this engine"
                )
            connection.execute(
                """
                INSERT INTO authority_index_meta(key, value)
                VALUES ('authority_index_schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(AUTHORITY_INDEX_SCHEMA_VERSION),),
            )
            previous_fts = connection.execute(
                """
                SELECT value FROM authority_index_meta
                WHERE key = 'fts5_available'
                """
            ).fetchone()
            fts5_available = False
            if not self.force_lexical_fallback:
                try:
                    connection.execute(
                        """
                        CREATE VIRTUAL TABLE IF NOT EXISTS authority_chunks_fts
                        USING fts5(chunk_id UNINDEXED, search_text, tokenize='unicode61')
                        """
                    )
                    fts5_available = True
                except sqlite3.OperationalError:
                    fts5_available = False
            connection.execute(
                """
                INSERT INTO authority_index_meta(key, value)
                VALUES ('fts5_available', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("1" if fts5_available else "0",),
            )
            if (
                previous_fts is not None
                and previous_fts["value"] != ("1" if fts5_available else "0")
            ):
                connection.execute("DELETE FROM rerank_candidate_cache")
            connection.execute(
                """
                INSERT OR IGNORE INTO authority_index_meta(key, value)
                VALUES ('index_digest', ?)
                """,
                (_sha256_bytes(b"[]"),),
            )

    def schema_info(self) -> dict[str, Any]:
        with self._connect() as connection:
            values = dict(
                connection.execute(
                    "SELECT key, value FROM authority_index_meta"
                ).fetchall()
            )
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM authority_files) AS files,
                    (SELECT COUNT(*) FROM authority_chunks) AS chunks,
                    (SELECT COUNT(*) FROM authority_vectors) AS vectors
                """
            ).fetchone()
        return {
            "authority_index_schema_version": int(
                values["authority_index_schema_version"]
            ),
            "fts5_available": values.get("fts5_available") == "1",
            "index_digest": values.get("index_digest", ""),
            "file_count": int(counts["files"]),
            "chunk_count": int(counts["chunks"]),
            "vector_count": int(counts["vectors"]),
            "embedding_enabled": self.embedding_provider is not None,
            "embedding_model": self.embedding_model,
            "rerank_enabled": self.rerank_provider is not None,
            "rerank_model": self.rerank_model,
        }

    def _ensure_embedding(
        self,
        connection: sqlite3.Connection,
        *,
        content_sha256: str,
        text: str,
        stats: dict[str, Any],
    ) -> None:
        if self.embedding_provider is None:
            return
        cached = connection.execute(
            """
            SELECT 1 FROM authority_vectors
            WHERE content_sha256 = ? AND model = ?
            """,
            (content_sha256, self.embedding_model),
        ).fetchone()
        if cached is not None:
            return
        stats["embedding_attempts"] += 1
        try:
            vector = _coerce_vector(
                self.embedding_provider(text),
                label="embedding response",
            )
        except Exception:
            # Authority text and BM25 remain usable when the remote provider
            # times out, rate-limits, or returns malformed output.
            stats["embedding_failures"] += 1
            return
        connection.execute(
            """
            INSERT INTO authority_vectors(
                content_sha256, model, vector_json, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                content_sha256,
                self.embedding_model,
                _stable_json(vector),
                _utc_now(),
            ),
        )
        stats["embedding_calls"] += 1

    @staticmethod
    def _resolve_sources(
        root: Path,
        sources: Iterable[AuthoritySource | Mapping[str, Any]],
    ) -> dict[str, AuthoritySource]:
        selected: dict[str, AuthoritySource] = {}
        excluded: set[str] = set()
        root_resolved = root.resolve()
        normalized_sources = [
            source
            if isinstance(source, AuthoritySource)
            else AuthoritySource.from_mapping(source)
            for source in sources
        ]
        for source in normalized_sources:
            for candidate in sorted(root.glob(source.glob)):
                try:
                    lexical_relative = candidate.relative_to(root).as_posix()
                except ValueError:
                    continue
                lexical_parts = {
                    part.casefold()
                    for part in Path(lexical_relative).parts
                }
                if lexical_parts & _HARD_EXCLUDED_PARTS:
                    continue
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError:
                    continue
                if not resolved.is_file():
                    continue
                try:
                    resolved.relative_to(root_resolved)
                except ValueError:
                    # Junctions and symlinks are never allowed to expand the
                    # authority boundary beyond the selected project.
                    continue
                relative = lexical_relative
                if source.ingest_policy == "exclude":
                    excluded.add(relative)
                    selected.pop(relative, None)
                    continue
                if relative in excluded:
                    continue
                previous = selected.get(relative)
                if previous is None or (
                    source.priority,
                    source.role,
                    source.glob,
                ) > (
                    previous.priority,
                    previous.role,
                    previous.glob,
                ):
                    selected[relative] = source
        return selected

    def refresh(
        self,
        project_root: str | Path,
        sources: Iterable[AuthoritySource | Mapping[str, Any]],
        *,
        accepted_hashes: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        root = Path(project_root).resolve()
        if not root.is_dir():
            raise AuthorityIndexError(f"project root is not a directory: {root}")
        selected = self._resolve_sources(root, sources)
        manifest_hashes = (
            {
                str(path).replace("\\", "/"): str(content_hash)
                for path, content_hash in accepted_hashes.items()
                if str(path).strip() and str(content_hash).strip()
            }
            if accepted_hashes is not None
            else None
        )
        if manifest_hashes is not None:
            selected = {
                relative: source
                for relative, source in selected.items()
                if relative in manifest_hashes
            }
        stats: dict[str, Any] = {
            "files_hashed": 0,
            "bytes_hashed": 0,
            "files_parsed": 0,
            "files_unchanged": 0,
            "source_policies_updated": 0,
            "files_removed": 0,
            "chunks_written": 0,
            "embedding_attempts": 0,
            "embedding_calls": 0,
            "embedding_failures": 0,
            "manifest_gated": manifest_hashes is not None,
            "manifest_allowed_files": (
                len(manifest_hashes) if manifest_hashes is not None else None
            ),
            "manifest_hash_mismatches": 0,
        }
        with self._connect() as connection:
            existing_rows = {
                row["path"]: row
                for row in connection.execute("SELECT * FROM authority_files")
            }
            fts5_available = (
                connection.execute(
                    """
                    SELECT value FROM authority_index_meta
                    WHERE key = 'fts5_available'
                    """
                ).fetchone()["value"]
                == "1"
            )
            effective_selected_paths: set[str] = set()
            for relative, source in sorted(selected.items()):
                path = root / Path(relative)
                payload = path.read_bytes()
                stats["files_hashed"] += 1
                stats["bytes_hashed"] += len(payload)
                content_sha256 = _sha256_bytes(payload)
                expected_manifest_hash = (
                    manifest_hashes.get(relative)
                    if manifest_hashes is not None
                    else None
                )
                if (
                    expected_manifest_hash is not None
                    and content_sha256 != expected_manifest_hash
                ):
                    stats["manifest_hash_mismatches"] += 1
                    continue
                effective_selected_paths.add(relative)
                policy_sha256 = source.policy_sha256
                old = existing_rows.get(relative)
                if old is not None and old["content_sha256"] == content_sha256:
                    stats["files_unchanged"] += 1
                    stat = path.stat()
                    connection.execute(
                        """
                        UPDATE authority_files
                        SET policy_sha256 = ?, byte_count = ?,
                            observed_mtime_ns = ?, role = ?, priority = ?,
                            scope_policy = ?, ingest_policy = ?
                        WHERE path = ?
                        """,
                        (
                            policy_sha256,
                            len(payload),
                            stat.st_mtime_ns,
                            source.role,
                            source.priority,
                            source.scope_policy,
                            source.ingest_policy,
                            relative,
                        ),
                    )
                    if old["policy_sha256"] != policy_sha256:
                        connection.execute(
                            """
                            UPDATE authority_chunks
                            SET role = ?, priority = ?, scope_policy = ?,
                                ingest_policy = ?
                            WHERE path = ?
                            """,
                            (
                                source.role,
                                source.priority,
                                source.scope_policy,
                                source.ingest_policy,
                                relative,
                            ),
                        )
                        stats["source_policies_updated"] += 1
                    if self.embedding_provider is not None:
                        for chunk in connection.execute(
                            """
                            SELECT content_sha256, text
                            FROM authority_chunks
                            WHERE path = ?
                            ORDER BY ordinal
                            """,
                            (relative,),
                        ):
                            self._ensure_embedding(
                                connection,
                                content_sha256=str(
                                    chunk["content_sha256"]
                                ),
                                text=str(chunk["text"]),
                                stats=stats,
                            )
                    continue
                stats["files_parsed"] += 1
                old_chunk_ids = [
                    row["chunk_id"]
                    for row in connection.execute(
                        "SELECT chunk_id FROM authority_chunks WHERE path = ?",
                        (relative,),
                    )
                ]
                if fts5_available:
                    for chunk_id in old_chunk_ids:
                        connection.execute(
                            "DELETE FROM authority_chunks_fts WHERE chunk_id = ?",
                            (chunk_id,),
                        )
                connection.execute(
                    "DELETE FROM authority_chunks WHERE path = ?", (relative,)
                )
                text = payload.decode("utf-8", errors="replace")
                chunks = self._chunk_markdown(text)
                stat = path.stat()
                connection.execute(
                    """
                    INSERT INTO authority_files(
                        path, content_sha256, policy_sha256, byte_count,
                        observed_mtime_ns, role, priority, scope_policy,
                        ingest_policy, chunk_count, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        content_sha256 = excluded.content_sha256,
                        policy_sha256 = excluded.policy_sha256,
                        byte_count = excluded.byte_count,
                        observed_mtime_ns = excluded.observed_mtime_ns,
                        role = excluded.role,
                        priority = excluded.priority,
                        scope_policy = excluded.scope_policy,
                        ingest_policy = excluded.ingest_policy,
                        chunk_count = excluded.chunk_count,
                        indexed_at = excluded.indexed_at
                    """,
                    (
                        relative,
                        content_sha256,
                        policy_sha256,
                        len(payload),
                        stat.st_mtime_ns,
                        source.role,
                        source.priority,
                        source.scope_policy,
                        source.ingest_policy,
                        len(chunks),
                        _utc_now(),
                    ),
                )
                for ordinal, (start_line, end_line, chunk_text) in enumerate(chunks):
                    chunk_hash = _sha256_bytes(chunk_text.encode("utf-8"))
                    chunk_id = _sha256_bytes(
                        f"{relative}\0{ordinal}\0{chunk_hash}".encode("utf-8")
                    )
                    search_text = _search_text(chunk_text)
                    connection.execute(
                        """
                        INSERT INTO authority_chunks(
                            chunk_id, path, ordinal, start_line, end_line, text,
                            search_text, content_sha256, role, priority,
                            scope_policy, ingest_policy
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chunk_id,
                            relative,
                            ordinal,
                            start_line,
                            end_line,
                            chunk_text,
                            search_text,
                            chunk_hash,
                            source.role,
                            source.priority,
                            source.scope_policy,
                            source.ingest_policy,
                        ),
                    )
                    if fts5_available:
                        connection.execute(
                            """
                            INSERT INTO authority_chunks_fts(chunk_id, search_text)
                            VALUES (?, ?)
                            """,
                            (chunk_id, search_text),
                        )
                    self._ensure_embedding(
                        connection,
                        content_sha256=chunk_hash,
                        text=chunk_text,
                        stats=stats,
                    )
                    stats["chunks_written"] += 1
            selected_paths = effective_selected_paths
            removed = sorted(set(existing_rows) - selected_paths)
            for relative in removed:
                old_chunk_ids = [
                    row["chunk_id"]
                    for row in connection.execute(
                        "SELECT chunk_id FROM authority_chunks WHERE path = ?",
                        (relative,),
                    )
                ]
                if fts5_available:
                    for chunk_id in old_chunk_ids:
                        connection.execute(
                            "DELETE FROM authority_chunks_fts WHERE chunk_id = ?",
                            (chunk_id,),
                        )
                connection.execute(
                    "DELETE FROM authority_files WHERE path = ?", (relative,)
                )
                stats["files_removed"] += 1
            digest_rows = connection.execute(
                """
                SELECT path, content_sha256, policy_sha256
                FROM authority_files ORDER BY path
                """
            ).fetchall()
            index_digest = _sha256_bytes(
                _stable_json([dict(row) for row in digest_rows]).encode("utf-8")
            )
            previous_digest = connection.execute(
                """
                SELECT value FROM authority_index_meta WHERE key = 'index_digest'
                """
            ).fetchone()["value"]
            connection.execute(
                """
                UPDATE authority_index_meta SET value = ?
                WHERE key = 'index_digest'
                """,
                (index_digest,),
            )
            if previous_digest != index_digest:
                connection.execute("DELETE FROM rerank_candidate_cache")
            elif stats["embedding_calls"]:
                # A lexical-only cache created before vectors became
                # available must not survive under the same source digest.
                connection.execute("DELETE FROM rerank_candidate_cache")
            stats["index_digest"] = index_digest
            stats["fts5_available"] = fts5_available
        return stats

    def _chunk_markdown(self, text: str) -> list[tuple[int, int, str]]:
        lines = text.splitlines()
        if not lines:
            return []
        chunks: list[tuple[int, int, str]] = []
        buffer: list[str] = []
        start_line = 1
        current_chars = 0

        def flush(end_line: int) -> None:
            nonlocal buffer, start_line, current_chars
            value = "\n".join(buffer).strip()
            if value:
                chunks.append((start_line, end_line, value))
            buffer = []
            current_chars = 0

        for index, line in enumerate(lines, start=1):
            if len(line) > self.max_chunk_chars:
                if buffer:
                    flush(index - 1)
                offset = 0
                while offset < len(line):
                    segment = line[offset : offset + self.max_chunk_chars]
                    chunks.append((index, index, segment))
                    offset += self.max_chunk_chars
                start_line = index + 1
                continue
            projected = current_chars + len(line) + (1 if buffer else 0)
            boundary = not line.strip() and buffer
            if buffer and (projected > self.max_chunk_chars or boundary):
                flush(index - 1 if boundary else index - 1)
                start_line = index + 1 if boundary else index
                if boundary:
                    continue
            if not buffer:
                start_line = index
            buffer.append(line)
            current_chars += len(line) + (1 if len(buffer) > 1 else 0)
        if buffer:
            flush(len(lines))
        return chunks

    @staticmethod
    def _fts_query(query: str) -> str:
        terms = list(dict.fromkeys(_tokens(query)))
        return " OR ".join(
            '"' + term.replace('"', '""') + '"' for term in terms[:32] if term
        )

    def _search_legacy(
        self,
        query: str,
        *,
        limit: int = 10,
        roles: Iterable[str] | None = None,
        scope_policies: Iterable[str] | None = None,
        ingest_policies: Iterable[str] = ("include", "review"),
        use_candidate_cache: bool = True,
        _query_vector_override: (
            tuple[Sequence[float] | None, str] | None
        ) = None,
        _defer_rerank: bool = False,
        _return_full_ranked: bool = False,
    ) -> list[dict[str, Any]]:
        normalized_query = _normalize_text(query)
        if not normalized_query or limit <= 0:
            return []
        role_values = sorted(set(roles or ()))
        scope_values = sorted(set(scope_policies or ()))
        ingest_values = sorted(set(ingest_policies))
        with self._connect() as connection:
            meta = dict(
                connection.execute(
                    "SELECT key, value FROM authority_index_meta"
                ).fetchall()
            )
            cache_payload = {
                "index_digest": meta["index_digest"],
                "query": normalized_query,
                "limit": int(limit),
                "roles": role_values,
                "scopes": scope_values,
                "ingest": ingest_values,
                "embedding": {
                    "enabled": self.embedding_provider is not None,
                    "model": self.embedding_model,
                },
                "rerank": {
                    "enabled": self.rerank_provider is not None,
                    "model": self.rerank_model,
                },
                "force_lexical_fallback": self.force_lexical_fallback,
            }
            cache_key = _sha256_bytes(
                _stable_json(cache_payload).encode("utf-8")
            )
            if use_candidate_cache:
                cached = connection.execute(
                    """
                    SELECT result_json FROM rerank_candidate_cache
                    WHERE cache_key = ? AND index_digest = ?
                    """,
                    (cache_key, meta["index_digest"]),
                ).fetchone()
                if cached is not None:
                    results = json.loads(cached["result_json"])
                    for item in results:
                        item["candidate_cache_hit"] = True
                    return results

            filters: list[str] = []
            parameters: list[Any] = []
            if role_values:
                filters.append(
                    "c.role IN (" + ",".join("?" for _ in role_values) + ")"
                )
                parameters.extend(role_values)
            if scope_values:
                filters.append(
                    "c.scope_policy IN ("
                    + ",".join("?" for _ in scope_values)
                    + ")"
                )
                parameters.extend(scope_values)
            if ingest_values:
                filters.append(
                    "c.ingest_policy IN ("
                    + ",".join("?" for _ in ingest_values)
                    + ")"
                )
                parameters.extend(ingest_values)
            where = " AND ".join(filters)
            if where:
                where = " AND " + where

            candidate_rows: dict[str, dict[str, Any]] = {}
            bm25_ranks: dict[str, float | None] = {}
            fts_query = self._fts_query(normalized_query)
            fts_candidates_found = False
            if meta.get("fts5_available") == "1" and fts_query:
                try:
                    rows = connection.execute(
                        f"""
                        SELECT c.*, bm25(authority_chunks_fts) AS rank
                        FROM authority_chunks_fts
                        JOIN authority_chunks c
                          ON c.chunk_id = authority_chunks_fts.chunk_id
                        WHERE authority_chunks_fts MATCH ? {where}
                        ORDER BY rank ASC, c.priority DESC, c.path, c.ordinal
                        LIMIT ?
                        """,
                        [fts_query, *parameters, max(limit * 8, 32)],
                    ).fetchall()
                    for row in rows:
                        chunk_id = str(row["chunk_id"])
                        candidate_rows[chunk_id] = dict(row)
                        bm25_ranks[chunk_id] = float(row["rank"])
                    fts_candidates_found = bool(rows)
                except sqlite3.OperationalError:
                    candidate_rows = {}
                    bm25_ranks = {}

            retrieval_mode = (
                "fts5_bm25"
                if fts_candidates_found
                else "lexical_fallback"
            )
            if not candidate_rows:
                rows = connection.execute(
                    f"""
                    SELECT c.* FROM authority_chunks c
                    WHERE 1 = 1 {where}
                    ORDER BY c.priority DESC, c.path, c.ordinal
                    """,
                    parameters,
                ).fetchall()
                for row in rows:
                    chunk_id = str(row["chunk_id"])
                    candidate_rows[chunk_id] = dict(row)
                    bm25_ranks[chunk_id] = None

            query_vector: list[float] | None = None
            embedding_status = (
                "disabled"
                if self.embedding_provider is None
                else "not_called"
            )
            if _query_vector_override is not None:
                raw_vector, embedding_status = _query_vector_override
                query_vector = (
                    None
                    if raw_vector is None
                    else _coerce_vector(
                        raw_vector,
                        label="query embedding override",
                    )
                )
            elif self.embedding_provider is not None:
                query_vector, embedding_status = (
                    self._exact_single_embedding(normalized_query)
                )

            vector_scores: dict[str, float] = {}
            if query_vector is not None:
                vector_rows = connection.execute(
                    f"""
                    SELECT c.*, v.vector_json AS stored_vector_json
                    FROM authority_chunks c
                    JOIN authority_vectors v
                      ON v.content_sha256=c.content_sha256
                     AND v.model=?
                    WHERE 1 = 1 {where}
                    ORDER BY c.priority DESC, c.path, c.ordinal
                    """,
                    [self.embedding_model, *parameters],
                ).fetchall()
                scored_rows: list[sqlite3.Row] = []
                stored_vectors: list[list[float]] = []
                for row in vector_rows:
                    try:
                        stored = _coerce_vector(
                            json.loads(str(row["stored_vector_json"])),
                            label="stored authority embedding",
                        )
                    except (
                        AuthorityIndexError,
                        TypeError,
                        ValueError,
                        json.JSONDecodeError,
                    ):
                        continue
                    scored_rows.append(row)
                    stored_vectors.append(stored)
                for row, score in zip(
                    scored_rows,
                    _cosine_many(query_vector, stored_vectors),
                ):
                    if score is None:
                        continue
                    chunk_id = str(row["chunk_id"])
                    vector_scores[chunk_id] = score
                    candidate_rows.setdefault(chunk_id, dict(row))
                    bm25_ranks.setdefault(chunk_id, None)

            ranked: list[dict[str, Any]] = []
            for chunk_id, row in candidate_rows.items():
                bm25_rank = bm25_ranks.get(chunk_id)
                lexical = _lexical_score(normalized_query, row["text"])
                vector_score = vector_scores.get(chunk_id)
                if lexical <= 0 and vector_score is None:
                    continue
                priority_bonus = max(-0.1, min(0.1, int(row["priority"]) / 10000))
                bm25_bonus = (
                    0.0 if bm25_rank is None else 1.0 / (100.0 + abs(bm25_rank))
                )
                lexical_score = lexical + priority_bonus + bm25_bonus
                if vector_score is None:
                    score = lexical_score
                    item_retrieval_mode = retrieval_mode
                    semantic_score = None
                else:
                    semantic_score = (vector_score + 1.0) / 2.0
                    score = (
                        0.42 * max(0.0, lexical_score)
                        + 0.58 * semantic_score
                    )
                    if lexical <= 0:
                        item_retrieval_mode = "vector"
                    elif bm25_rank is not None:
                        item_retrieval_mode = "hybrid_vector_bm25"
                    else:
                        item_retrieval_mode = "hybrid_vector_lexical"
                ranked.append(
                    {
                        "chunk_id": chunk_id,
                        "path": row["path"],
                        "ordinal": int(row["ordinal"]),
                        "start_line": int(row["start_line"]),
                        "end_line": int(row["end_line"]),
                        "text": row["text"],
                        "content_sha256": row["content_sha256"],
                        "role": row["role"],
                        "priority": int(row["priority"]),
                        "scope_policy": row["scope_policy"],
                        "ingest_policy": row["ingest_policy"],
                        "score": round(score, 8),
                        "base_score": round(score, 8),
                        "lexical_score": round(lexical, 8),
                        "vector_score": (
                            None
                            if vector_score is None
                            else round(vector_score, 8)
                        ),
                        "semantic_score": (
                            None
                            if semantic_score is None
                            else round(semantic_score, 8)
                        ),
                        "bm25": bm25_rank,
                        "retrieval_mode": item_retrieval_mode,
                        "embedding_status": embedding_status,
                        "embedding_model": self.embedding_model,
                        "rerank_status": (
                            "disabled"
                            if self.rerank_provider is None
                            else "not_called"
                        ),
                        "rerank_model": self.rerank_model,
                        "rerank_rank": None,
                        "rerank_score": None,
                        "candidate_cache_hit": False,
                    }
                )
            ranked.sort(
                key=lambda item: (
                    -float(item["score"]),
                    -int(item["priority"]),
                    item["path"],
                    int(item["ordinal"]),
                )
            )

            candidate_pool = ranked[: max(limit * 8, 32)]
            if (
                not _defer_rerank
                and self.rerank_provider is not None
                and candidate_pool
            ):
                try:
                    reranked_raw = self._exact_rerank(
                        normalized_query,
                        [item["text"] for item in candidate_pool],
                        len(candidate_pool),
                    )
                    reranked: list[tuple[int, float]] = []
                    seen: set[int] = set()
                    for pair in reranked_raw:
                        if (
                            not isinstance(pair, Sequence)
                            or isinstance(pair, (str, bytes, bytearray))
                            or len(pair) != 2
                        ):
                            raise AuthorityIndexError(
                                "rerank provider returned an invalid result"
                            )
                        index, raw_score = pair
                        if (
                            isinstance(index, bool)
                            or not isinstance(index, int)
                            or not 0 <= index < len(candidate_pool)
                            or index in seen
                        ):
                            raise AuthorityIndexError(
                                "rerank provider returned an invalid index"
                            )
                        if (
                            isinstance(raw_score, bool)
                            or not isinstance(raw_score, (int, float))
                            or not math.isfinite(float(raw_score))
                        ):
                            raise AuthorityIndexError(
                                "rerank provider returned an invalid score"
                            )
                        seen.add(index)
                        reranked.append((index, float(raw_score)))
                    if not reranked:
                        raise AuthorityIndexError(
                            "rerank provider returned no candidates"
                        )
                    reranked.sort(key=lambda item: (-item[1], item[0]))
                    ordered: list[dict[str, Any]] = []
                    total = max(1, len(reranked))
                    for rerank_rank, (index, raw_score) in enumerate(reranked):
                        item = candidate_pool[index]
                        rank_score = 1.0 - rerank_rank / total
                        item["rerank_status"] = "ok"
                        item["rerank_rank"] = rerank_rank
                        item["rerank_score"] = round(raw_score, 8)
                        item["score"] = round(
                            0.2 * float(item["base_score"])
                            + 0.8 * rank_score,
                            8,
                        )
                        item["retrieval_mode"] = (
                            "reranked_" + str(item["retrieval_mode"])
                        )
                        ordered.append(item)
                    for index, item in enumerate(candidate_pool):
                        if index not in seen:
                            item["rerank_status"] = "partial"
                            ordered.append(item)
                    ranked = ordered + ranked[len(candidate_pool) :]
                except Exception:
                    for item in ranked:
                        item["rerank_status"] = "failed"
            elif not _defer_rerank and self.rerank_provider is not None:
                for item in ranked:
                    item["rerank_status"] = "skipped"

            results = ranked if _return_full_ranked else ranked[:limit]
            cacheable = (
                not _defer_rerank
                and
                embedding_status != "failed"
                and all(
                    item.get("rerank_status")
                    not in {"failed", "partial"}
                    for item in ranked
                )
            )
            if use_candidate_cache and cacheable:
                connection.execute(
                    """
                    INSERT INTO rerank_candidate_cache(
                        cache_key, index_digest, result_json, created_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        index_digest = excluded.index_digest,
                        result_json = excluded.result_json,
                        created_at = excluded.created_at
                    """,
                    (
                        cache_key,
                        meta["index_digest"],
                        _stable_json(results),
                        _utc_now(),
                    ),
                )
            return results

    @staticmethod
    def _normalize_filter_values(
        value: Iterable[str] | str | None,
        default: Iterable[str] = (),
    ) -> tuple[str, ...]:
        selected = default if value is None else value
        if isinstance(selected, str):
            selected = (selected,)
        return tuple(sorted({str(item) for item in selected}))

    def _normalize_search_spec(
        self,
        request: str | Mapping[str, Any],
        *,
        limit: int,
        roles: Iterable[str] | None,
        scope_policies: Iterable[str] | None,
        ingest_policies: Iterable[str],
        use_candidate_cache: bool,
    ) -> _SearchSpec:
        if isinstance(request, Mapping):
            query = str(request.get("query") or "")
            request_limit = int(request.get("limit", limit))
            request_roles = request.get("roles", roles)
            request_scopes = request.get(
                "scope_policies",
                scope_policies,
            )
            request_ingest = request.get(
                "ingest_policies",
                ingest_policies,
            )
            request_cache = bool(
                request.get(
                    "use_candidate_cache",
                    use_candidate_cache,
                )
            )
        else:
            query = str(request)
            request_limit = int(limit)
            request_roles = roles
            request_scopes = scope_policies
            request_ingest = ingest_policies
            request_cache = bool(use_candidate_cache)
        return _SearchSpec(
            query=query,
            normalized_query=_normalize_text(query),
            limit=request_limit,
            roles=self._normalize_filter_values(request_roles),
            scope_policies=self._normalize_filter_values(request_scopes),
            ingest_policies=self._normalize_filter_values(
                request_ingest,
                ("include", "review"),
            ),
            use_candidate_cache=request_cache,
        )

    def _candidate_cache_key(
        self,
        spec: _SearchSpec,
        index_digest: str,
    ) -> str:
        payload = {
            "index_digest": index_digest,
            "query": spec.normalized_query,
            "limit": int(spec.limit),
            "roles": list(spec.roles),
            "scopes": list(spec.scope_policies),
            "ingest": list(spec.ingest_policies),
            "embedding": {
                "enabled": self.embedding_provider is not None,
                "model": self.embedding_model,
                "provider": self._provider_cache_identity(
                    self.embedding_provider
                ),
            },
            "rerank": {
                "enabled": self.rerank_provider is not None,
                "model": self.rerank_model,
                "provider": self._provider_cache_identity(
                    self.rerank_provider
                ),
            },
            "force_lexical_fallback": self.force_lexical_fallback,
            "scoring_version": _AUTHORITY_SCORING_VERSION,
            "rerank_normalization_version": (
                _RERANK_NORMALIZATION_VERSION
            ),
        }
        return _sha256_bytes(_stable_json(payload).encode("utf-8"))

    def _read_candidate_cache(
        self,
        spec: _SearchSpec,
    ) -> tuple[str, str, list[dict[str, Any]] | None]:
        with self._connect() as connection:
            meta = dict(
                connection.execute(
                    "SELECT key, value FROM authority_index_meta"
                ).fetchall()
            )
            index_digest = str(meta["index_digest"])
            cache_key = self._candidate_cache_key(spec, index_digest)
            if not spec.use_candidate_cache:
                return index_digest, cache_key, None
            cached = connection.execute(
                """
                SELECT result_json FROM rerank_candidate_cache
                WHERE cache_key = ? AND index_digest = ?
                """,
                (cache_key, index_digest),
            ).fetchone()
        if cached is None:
            return index_digest, cache_key, None
        value = json.loads(str(cached["result_json"]))
        if not isinstance(value, list):
            return index_digest, cache_key, None
        results = [
            dict(item)
            for item in value
            if isinstance(item, Mapping)
        ]
        for item in results:
            item["candidate_cache_hit"] = True
        return index_digest, cache_key, results

    def _write_candidate_cache(
        self,
        *,
        spec: _SearchSpec,
        cache_key: str,
        expected_index_digest: str,
        results: Sequence[Mapping[str, Any]],
    ) -> bool:
        if not spec.use_candidate_cache:
            return False
        with self._connect() as connection:
            current = connection.execute(
                """
                SELECT value FROM authority_index_meta
                WHERE key = 'index_digest'
                """
            ).fetchone()
            if (
                current is None
                or str(current["value"]) != expected_index_digest
            ):
                return False
            connection.execute(
                """
                INSERT INTO rerank_candidate_cache(
                    cache_key, index_digest, result_json, created_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    index_digest = excluded.index_digest,
                    result_json = excluded.result_json,
                    created_at = excluded.created_at
                """,
                (
                    cache_key,
                    expected_index_digest,
                    _stable_json([dict(item) for item in results]),
                    _utc_now(),
                ),
            )
        return True

    @staticmethod
    def _provider_cache_identity(provider: Any) -> str:
        if provider is None:
            return "disabled"
        explicit_identity = getattr(provider, "cache_identity", None)
        if explicit_identity is None:
            return (
                f"{getattr(provider, '__module__', '')}:"
                f"{getattr(provider, '__qualname__', '')}:"
                f"{id(provider)}"
            )
        return str(explicit_identity)

    def _embedding_cache_key(self, query: str) -> str:
        provider_identity = self._provider_cache_identity(
            self.embedding_provider
        )
        return _sha256_bytes(
            _stable_json(
                {
                    "provider": provider_identity,
                    "model": self.embedding_model,
                    "input": query,
                    "preprocessing": (
                        _QUERY_EMBEDDING_PREPROCESSING_VERSION
                    ),
                }
            ).encode("utf-8")
        )

    def _exact_single_embedding(
        self,
        query: str,
    ) -> tuple[list[float] | None, str]:
        """Resolve one query embedding with the shared exact cache.

        The legacy search path is intentionally sequential, but it must still
        reuse the same singleton vector as Prepare v2.  This prevents a
        provider's small per-request floating-point variation from changing
        the candidate pool between the two paths.
        """

        provider = self.embedding_provider
        if provider is None:
            return None, "disabled"
        cache_key = self._embedding_cache_key(query)
        if self.query_embedding_cache_size > 0:
            with self._flight_lock:
                cached = self._query_embedding_cache.get(cache_key)
                if cached is not None:
                    self._query_embedding_cache.move_to_end(cache_key)
                    return list(cached), "ok"
        try:
            vector = _coerce_vector(
                provider(query),
                label="query embedding",
            )
        except Exception:
            # A provider failure never suppresses BM25 or lexical candidates
            # and never confirms semantic absence.
            return None, "failed"
        if self.query_embedding_cache_size > 0:
            with self._flight_lock:
                self._query_embedding_cache[cache_key] = list(vector)
                self._query_embedding_cache.move_to_end(cache_key)
                while (
                    len(self._query_embedding_cache)
                    > self.query_embedding_cache_size
                ):
                    self._query_embedding_cache.popitem(last=False)
        return vector, "ok"

    @staticmethod
    def _indexed_batch_embedding_item(
        item: Any,
    ) -> tuple[int, Sequence[float]] | None:
        if isinstance(item, Mapping):
            index = item.get("index")
            vector = item.get("embedding")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not isinstance(vector, Sequence)
                or isinstance(vector, (str, bytes, bytearray))
            ):
                raise AuthorityIndexError(
                    "batch embedding response item is invalid"
                )
            return index, vector
        if (
            isinstance(item, Sequence)
            and not isinstance(item, (str, bytes, bytearray))
            and len(item) == 2
            and isinstance(item[0], int)
            and not isinstance(item[0], bool)
            and isinstance(item[1], Sequence)
            and not isinstance(item[1], (str, bytes, bytearray))
        ):
            return int(item[0]), item[1]
        return None

    def _coerce_batch_embeddings(
        self,
        raw: Any,
        expected_count: int,
    ) -> list[list[float]]:
        if (
            isinstance(raw, tuple)
            and len(raw) == 2
            and isinstance(raw[1], Mapping)
        ):
            raw = raw[0]
        if (
            not isinstance(raw, Sequence)
            or isinstance(raw, (str, bytes, bytearray))
        ):
            raise AuthorityIndexError(
                "batch embedding response must be a sequence"
            )
        items = list(raw)
        if len(items) != expected_count:
            raise AuthorityIndexError(
                "batch embedding response length does not match input"
            )
        indexed: list[tuple[int, Sequence[float]] | None] = [
            self._indexed_batch_embedding_item(item)
            for item in items
        ]
        if any(value is not None for value in indexed):
            if not all(value is not None for value in indexed):
                raise AuthorityIndexError(
                    "batch embedding response mixes indexed and ordered items"
                )
            ordered: list[Sequence[float] | None] = [None] * expected_count
            for value in indexed:
                assert value is not None
                index, vector = value
                if (
                    not 0 <= index < expected_count
                    or ordered[index] is not None
                ):
                    raise AuthorityIndexError(
                        "batch embedding response index is invalid"
                    )
                ordered[index] = vector
            if any(vector is None for vector in ordered):
                raise AuthorityIndexError(
                    "batch embedding response has missing indexes"
                )
            vectors = [
                _coerce_vector(
                    vector,
                    label=f"batch embedding response {index}",
                )
                for index, vector in enumerate(ordered)
                if vector is not None
            ]
        else:
            vectors = [
                _coerce_vector(
                    item,
                    label=f"batch embedding response {index}",
                )
                for index, item in enumerate(items)
            ]
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) > 1:
            raise AuthorityIndexError(
                "batch embedding response dimensions do not match"
            )
        return vectors

    def _embedding_batches(
        self,
        values: Sequence[tuple[str, str, Future[Any]]],
    ) -> list[list[tuple[str, str, Future[Any]]]]:
        batches: list[list[tuple[str, str, Future[Any]]]] = []
        current: list[tuple[str, str, Future[Any]]] = []
        current_chars = 0
        for value in values:
            query_chars = len(value[1])
            if current and (
                len(current) >= self.embedding_batch_size
                or current_chars + query_chars
                > self.embedding_batch_max_chars
            ):
                batches.append(current)
                current = []
                current_chars = 0
            current.append(value)
            current_chars += query_chars
            if len(current) >= self.embedding_batch_size:
                batches.append(current)
                current = []
                current_chars = 0
        if current:
            batches.append(current)
        return batches

    def _complete_embedding_flight(
        self,
        cache_key: str,
        future: Future[tuple[list[float] | None, str]],
        vector: list[float] | None,
        status: str,
    ) -> None:
        with self._flight_lock:
            if (
                status == "ok"
                and vector is not None
                and self.query_embedding_cache_size > 0
            ):
                self._query_embedding_cache[cache_key] = list(vector)
                self._query_embedding_cache.move_to_end(cache_key)
                while (
                    len(self._query_embedding_cache)
                    > self.query_embedding_cache_size
                ):
                    self._query_embedding_cache.popitem(last=False)
            if not future.done():
                future.set_result(
                    (
                        None if vector is None else list(vector),
                        status,
                    )
                )
            if (
                self.singleflight_enabled
                and self._query_embedding_flights.get(cache_key) is future
            ):
                self._query_embedding_flights.pop(cache_key, None)

    def _fail_embedding_flight(
        self,
        cache_key: str,
        future: Future[tuple[list[float] | None, str]],
    ) -> None:
        """Fail and remove one owned query-embedding flight."""

        self._complete_embedding_flight(
            cache_key,
            future,
            None,
            "failed",
        )

    def _query_embeddings(
        self,
        queries: Sequence[str],
        metrics: dict[str, Any],
    ) -> list[tuple[list[float] | None, str]]:
        if self.embedding_provider is None:
            return [(None, "disabled") for _ in queries]
        resolved: list[tuple[list[float] | None, str] | None] = [
            None
        ] * len(queries)
        leaders: list[
            tuple[str, str, Future[tuple[list[float] | None, str]]]
        ] = []
        waiters: list[
            tuple[int, Future[tuple[list[float] | None, str]]]
        ] = []
        try:
            for index, query in enumerate(queries):
                cache_key = self._embedding_cache_key(query)
                with self._flight_lock:
                    cached = (
                        self._query_embedding_cache.get(cache_key)
                        if self.query_embedding_cache_size > 0
                        else None
                    )
                    if cached is not None:
                        self._query_embedding_cache.move_to_end(cache_key)
                        resolved[index] = (list(cached), "ok")
                        metrics["embedding_cache_hits"] += 1
                        continue
                    if not self.singleflight_enabled:
                        future = Future()
                        leaders.append((cache_key, query, future))
                        waiters.append((index, future))
                    else:
                        future = self._query_embedding_flights.get(cache_key)
                        if future is None:
                            future = Future()
                            self._query_embedding_flights[cache_key] = future
                            leaders.append((cache_key, query, future))
                        else:
                            metrics["embedding_singleflight_waits"] += 1
                        waiters.append((index, future))
        except BaseException:
            for cache_key, _query, future in leaders:
                self._fail_embedding_flight(cache_key, future)
            raise

        try:
            for batch in self._embedding_batches(leaders):
                batch_vectors: list[list[float]] | None = None
                if self.embedding_batch_provider is not None:
                    metrics["embedding_batch_calls"] += 1
                    batch_started = time.perf_counter()
                    try:
                        batch_vectors = self._coerce_batch_embeddings(
                            self.embedding_batch_provider(
                                [query for _, query, _ in batch]
                            ),
                            len(batch),
                        )
                    except Exception:
                        metrics["embedding_batch_failures"] += 1
                        batch_vectors = None
                    finally:
                        metrics["embedding_batch_ms"] += round(
                            (time.perf_counter() - batch_started) * 1000.0,
                            3,
                        )
                if batch_vectors is not None:
                    for (cache_key, _query, future), vector in zip(
                        batch,
                        batch_vectors,
                    ):
                        self._complete_embedding_flight(
                            cache_key,
                            future,
                            vector,
                            "ok",
                        )
                    continue
                if self.embedding_batch_provider is not None:
                    metrics["embedding_single_fallbacks"] += len(batch)
                metrics["embedding_single_calls"] += len(batch)
                single_wall_started = time.perf_counter()

                def embed_single(
                    value: tuple[str, str, Future[Any]],
                ) -> tuple[
                    str,
                    Future[Any],
                    list[float] | None,
                    str,
                    float,
                    BaseException | None,
                ]:
                    cache_key, query, future = value
                    single_started = time.perf_counter()
                    try:
                        vector = _coerce_vector(
                            self.embedding_provider(query),
                            label="query embedding",
                        )
                        return (
                            cache_key,
                            future,
                            vector,
                            "ok",
                            (time.perf_counter() - single_started) * 1000.0,
                            None,
                        )
                    except BaseException as exc:
                        return (
                            cache_key,
                            future,
                            None,
                            "failed",
                            (time.perf_counter() - single_started) * 1000.0,
                            exc,
                        )

                workers = min(
                    self.embedding_single_max_concurrency,
                    max(1, len(batch)),
                )
                if workers == 1:
                    completed_singles = [
                        embed_single(value) for value in batch
                    ]
                else:
                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        completed_singles = list(
                            executor.map(embed_single, batch)
                        )

                fatal_error: BaseException | None = None
                for (
                    cache_key,
                    future,
                    vector,
                    status,
                    elapsed_ms,
                    error,
                ) in completed_singles:
                    metrics["embedding_single_ms"] += round(elapsed_ms, 3)
                    self._complete_embedding_flight(
                        cache_key,
                        future,
                        vector,
                        status,
                    )
                    if (
                        error is not None
                        and not isinstance(error, Exception)
                        and fatal_error is None
                    ):
                        fatal_error = error
                metrics["embedding_single_wall_ms"] += round(
                    (time.perf_counter() - single_wall_started) * 1000.0,
                    3,
                )
                if fatal_error is not None:
                    raise fatal_error

            for index, future in waiters:
                vector, status = future.result()
                resolved[index] = (
                    None if vector is None else list(vector),
                    status,
                )
        except BaseException as exc:
            for cache_key, _query, future in leaders:
                self._fail_embedding_flight(cache_key, future)
            raise
        return [
            value if value is not None else (None, "failed")
            for value in resolved
        ]

    def _rerank_result_cache_scope(
        self,
        query: str,
        documents: Sequence[str],
        top_n: int,
    ) -> tuple[str, Any | None]:
        """Build the process-cache key and owner for an exact rerank request.

        The provider identity is intentionally part of the key.  A caller can
        opt into sharing across independently constructed provider wrappers by
        exposing a stable ``cache_identity`` attribute.  Without one, the
        completed cache entry retains the provider object itself and accepts a
        hit only for that exact object.  Keeping the object alive prevents a
        later provider from inheriting an old result if CPython reuses its
        numeric ``id``.  Documents remain ordered and byte-for-byte represented
        in the payload so that a reordered or edited candidate pool never
        reuses another request's scores.
        """

        provider = self.rerank_provider
        explicit_identity = getattr(provider, "cache_identity", None)
        if explicit_identity is None:
            provider_identity = self._provider_cache_identity(provider)
            provider_scope = "object"
            cache_owner = provider
        else:
            provider_identity = str(explicit_identity)
            provider_scope = "explicit"
            cache_owner = None
        payload = {
            "provider": provider_identity,
            "provider_scope": provider_scope,
            "model": self.rerank_model,
            "query": str(query),
            "documents": [str(document) for document in documents],
            "top_n": int(top_n),
            "normalization_version": _RERANK_NORMALIZATION_VERSION,
        }
        return (
            _sha256_bytes(_stable_json(payload).encode("utf-8")),
            cache_owner,
        )

    def _rerank_result_cache_key(
        self,
        query: str,
        documents: Sequence[str],
        top_n: int,
    ) -> str:
        """Return the stable key portion of an exact rerank cache scope."""

        cache_key, _cache_owner = self._rerank_result_cache_scope(
            query,
            documents,
            top_n,
        )
        return cache_key

    @staticmethod
    def _validate_rerank_pairs(
        raw_result: Iterable[Any],
        document_count: int,
        expected_count: int | None = None,
    ) -> tuple[tuple[int, float], ...]:
        """Validate and freeze a provider response before it can be cached."""

        try:
            pairs = list(raw_result)
        except TypeError as exc:
            raise AuthorityIndexError(
                "rerank provider returned an invalid result"
            ) from exc
        reranked: list[tuple[int, float]] = []
        seen: set[int] = set()
        for pair in pairs:
            if (
                not isinstance(pair, Sequence)
                or isinstance(pair, (str, bytes, bytearray))
                or len(pair) != 2
            ):
                raise AuthorityIndexError(
                    "rerank provider returned an invalid result"
                )
            index, raw_score = pair
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < document_count
                or index in seen
            ):
                raise AuthorityIndexError(
                    "rerank provider returned an invalid index"
                )
            if (
                isinstance(raw_score, bool)
                or not isinstance(raw_score, (int, float))
                or not math.isfinite(float(raw_score))
            ):
                raise AuthorityIndexError(
                    "rerank provider returned an invalid score"
                )
            seen.add(index)
            reranked.append((index, float(raw_score)))
        if not reranked:
            raise AuthorityIndexError(
                "rerank provider returned no candidates"
            )
        if (
            expected_count is not None
            and int(expected_count) >= document_count
            and len(reranked) != document_count
        ):
            raise AuthorityIndexError(
                "rerank provider returned a partial result"
            )
        return tuple(reranked)

    def _complete_rerank_flight(
        self,
        cache_key: str,
        future: Future[tuple[tuple[int, float], ...]],
        *,
        result: tuple[tuple[int, float], ...] | None = None,
        error: BaseException | None = None,
    ) -> None:
        with self._flight_lock:
            if not future.done():
                if error is not None:
                    future.set_exception(error)
                else:
                    future.set_result(tuple(result or ()))
            if (
                self.singleflight_enabled
                and self._rerank_result_flights.get(cache_key) is future
            ):
                self._rerank_result_flights.pop(cache_key, None)

    def _exact_rerank(
        self,
        query: str,
        documents: Sequence[str],
        top_n: int,
        *,
        stats: dict[str, int] | None = None,
    ) -> list[tuple[int, float]]:
        """Call the reranker once per exact request within this process.

        Cache entries contain only validated, immutable ``(index, score)``
        pairs.  Provider failures and malformed responses complete the
        singleflight with an error and are deliberately never inserted into
        the cache, allowing a later request to retry.  ``singleflight_enabled``
        controls only concurrent request coalescing; completed exact results
        remain reusable across legacy and v2 index wrappers.
        """

        provider = self.rerank_provider
        if provider is None:
            raise AuthorityIndexError("rerank provider is disabled")
        normalized_documents = [str(document) for document in documents]
        cache_key, cache_owner = self._rerank_result_cache_scope(
            query,
            normalized_documents,
            top_n,
        )
        waiter: Future[tuple[tuple[int, float], ...]] | None = None
        leader = True
        with self._flight_lock:
            cached_entry = self._rerank_result_cache.get(cache_key)
            cached: tuple[tuple[int, float], ...] | None = None
            if cached_entry is not None:
                cached_owner, cached_result = cached_entry
                if (
                    (cache_owner is None and cached_owner is None)
                    or cached_owner is cache_owner
                ):
                    cached = cached_result
                else:
                    self._rerank_result_cache.pop(cache_key, None)
            if cached is not None:
                self._rerank_result_cache.move_to_end(cache_key)
                if stats is not None:
                    stats["cache_hits"] = stats.get("cache_hits", 0) + 1
                return [
                    (index, score)
                    for index, score in cached
                ]
            if self.singleflight_enabled:
                waiter = self._rerank_result_flights.get(cache_key)
                if waiter is None:
                    waiter = Future()
                    self._rerank_result_flights[cache_key] = waiter
                else:
                    leader = False
                    if stats is not None:
                        stats["singleflight_waits"] = (
                            stats.get("singleflight_waits", 0) + 1
                        )
            if leader and stats is not None:
                stats["cache_misses"] = stats.get("cache_misses", 0) + 1

        if not leader:
            assert waiter is not None
            return [tuple(pair) for pair in waiter.result()]

        try:
            validated = self._validate_rerank_pairs(
                provider(
                    str(query),
                    normalized_documents,
                    int(top_n),
                ),
                len(normalized_documents),
                expected_count=int(top_n),
            )
        except BaseException as exc:
            if self.singleflight_enabled and waiter is not None:
                self._complete_rerank_flight(
                    cache_key,
                    waiter,
                    error=exc,
                )
            raise

        with self._flight_lock:
            self._rerank_result_cache[cache_key] = (
                cache_owner,
                validated,
            )
            self._rerank_result_cache.move_to_end(cache_key)
            while len(self._rerank_result_cache) > _RERANK_RESULT_CACHE_SIZE:
                self._rerank_result_cache.popitem(last=False)
        if self.singleflight_enabled and waiter is not None:
            self._complete_rerank_flight(
                cache_key,
                waiter,
                result=validated,
            )
        return [
            (index, score)
            for index, score in validated
        ]

    def _apply_rerank(
        self,
        spec: _SearchSpec,
        ranked: list[dict[str, Any]],
        *,
        _rerank_stats: dict[str, int] | None = None,
    ) -> list[dict[str, Any]]:
        candidate_pool = ranked[: max(spec.limit * 8, 32)]
        if self.rerank_provider is not None and candidate_pool:
            try:
                reranked_raw = self._exact_rerank(
                    spec.normalized_query,
                    [item["text"] for item in candidate_pool],
                    len(candidate_pool),
                    stats=_rerank_stats,
                )
                reranked: list[tuple[int, float]] = []
                seen: set[int] = set()
                for pair in reranked_raw:
                    if (
                        not isinstance(pair, Sequence)
                        or isinstance(pair, (str, bytes, bytearray))
                        or len(pair) != 2
                    ):
                        raise AuthorityIndexError(
                            "rerank provider returned an invalid result"
                        )
                    index, raw_score = pair
                    if (
                        isinstance(index, bool)
                        or not isinstance(index, int)
                        or not 0 <= index < len(candidate_pool)
                        or index in seen
                    ):
                        raise AuthorityIndexError(
                            "rerank provider returned an invalid index"
                        )
                    if (
                        isinstance(raw_score, bool)
                        or not isinstance(raw_score, (int, float))
                        or not math.isfinite(float(raw_score))
                    ):
                        raise AuthorityIndexError(
                            "rerank provider returned an invalid score"
                        )
                    seen.add(index)
                    reranked.append((index, float(raw_score)))
                if not reranked:
                    raise AuthorityIndexError(
                        "rerank provider returned no candidates"
                    )
                reranked.sort(key=lambda item: (-item[1], item[0]))
                ordered: list[dict[str, Any]] = []
                total = max(1, len(reranked))
                for rerank_rank, (index, raw_score) in enumerate(reranked):
                    item = candidate_pool[index]
                    rank_score = 1.0 - rerank_rank / total
                    item["rerank_status"] = "ok"
                    item["rerank_rank"] = rerank_rank
                    item["rerank_score"] = round(raw_score, 8)
                    item["score"] = round(
                        0.2 * float(item["base_score"])
                        + 0.8 * rank_score,
                        8,
                    )
                    item["retrieval_mode"] = (
                        "reranked_" + str(item["retrieval_mode"])
                    )
                    ordered.append(item)
                for index, item in enumerate(candidate_pool):
                    if index not in seen:
                        item["rerank_status"] = "partial"
                        ordered.append(item)
                completed = ordered + ranked[len(candidate_pool) :]
                # Preserve the legacy path's provider order exactly.  The
                # fused score is retained as metadata, but re-sorting by it
                # here can change the top-k boundary relative to
                # ``_search_legacy`` even when both paths consume the same
                # validated rerank response.
                return completed
            except Exception:
                for item in ranked:
                    item["rerank_status"] = "failed"
                return ranked
        if self.rerank_provider is not None:
            for item in ranked:
                item["rerank_status"] = "skipped"
        return ranked

    @staticmethod
    def _result_health(
        results: Sequence[Mapping[str, Any]],
        embedding_status: str,
        *,
        source: str,
    ) -> dict[str, Any]:
        rerank_statuses = sorted(
            {
                str(item.get("rerank_status"))
                for item in results
                if item.get("rerank_status") not in {None, ""}
            }
        )
        degraded = (
            embedding_status == "failed"
            or any(
                status in {"failed", "partial"}
                for status in rerank_statuses
            )
        )
        return {
            "status": "degraded" if degraded else source,
            "embedding_status": embedding_status,
            "rerank_statuses": rerank_statuses,
            "result_count": len(results),
            # A degraded provider, unavailable index, or one empty result set
            # never establishes semantic absence.
            "miss_confirmed": False,
        }

    def _complete_search_flight(
        self,
        cache_key: str,
        future: Future[list[dict[str, Any]]],
        *,
        result: list[dict[str, Any]] | None = None,
        error: BaseException | None = None,
    ) -> None:
        with self._flight_lock:
            if not future.done():
                if error is not None:
                    future.set_exception(error)
                else:
                    future.set_result(deepcopy(result or []))
            if (
                self.singleflight_enabled
                and self._search_flights.get(cache_key) is future
            ):
                self._search_flights.pop(cache_key, None)

    def search_many(
        self,
        queries: Sequence[str | Mapping[str, Any]],
        *,
        limit: int = 10,
        roles: Iterable[str] | None = None,
        scope_policies: Iterable[str] | None = None,
        ingest_policies: Iterable[str] = ("include", "review"),
        use_candidate_cache: bool = True,
        rerank_max_concurrency: int | None = None,
    ) -> list[list[dict[str, Any]]]:
        """Search multiple independent needs with batched query embeddings.

        Every request keeps its own filters, candidate pool, and rerank call.
        Remote reranks run concurrently within ``rerank_max_concurrency`` and
        results are joined in original request order.  Invalid batch embedding
        responses fall back to stable per-query calls; a failed query retains
        its BM25/FTS5/lexical candidates and is never cached as healthy.
        """

        search_started = time.perf_counter()
        specs = [
            self._normalize_search_spec(
                request,
                limit=limit,
                roles=roles,
                scope_policies=scope_policies,
                ingest_policies=ingest_policies,
                use_candidate_cache=use_candidate_cache,
            )
            for request in queries
        ]
        results: list[list[dict[str, Any]] | None] = [None] * len(specs)
        diagnostics: list[dict[str, Any] | None] = [None] * len(specs)
        metrics: dict[str, Any] = {
            "query_count": len(specs),
            "candidate_cache_hits": 0,
            "search_singleflight_waits": 0,
            "embedding_cache_hits": 0,
            "embedding_singleflight_waits": 0,
            "embedding_batch_calls": 0,
            "embedding_batch_failures": 0,
            "embedding_batch_ms": 0.0,
            "embedding_single_fallbacks": 0,
            "embedding_single_calls": 0,
            "embedding_single_ms": 0.0,
            "embedding_single_wall_ms": 0.0,
            "embedding_single_max_concurrency": (
                self.embedding_single_max_concurrency
            ),
            "rerank_wall_ms": 0.0,
            "rerank_sum_ms": 0.0,
            "rerank_cache_hits": 0,
            "rerank_singleflight_waits": 0,
            "rerank_cache_misses": 0,
            "rerank_max_concurrency": max(
                1,
                int(
                    rerank_max_concurrency
                    if rerank_max_concurrency is not None
                    else self.rerank_max_concurrency
                ),
            ),
        }
        leaders: list[
            tuple[
                int,
                _SearchSpec,
                str,
                str,
                Future[list[dict[str, Any]]],
            ]
        ] = []
        followers: list[
            tuple[int, Future[list[dict[str, Any]]]]
        ] = []

        try:
            for index, spec in enumerate(specs):
                if not spec.normalized_query or spec.limit <= 0:
                    results[index] = []
                    diagnostics[index] = self._result_health(
                        [],
                        (
                            "disabled"
                            if self.embedding_provider is None
                            else "not_called"
                        ),
                        source="empty_input",
                    )
                    continue
                (
                    index_digest,
                    cache_key,
                    cached,
                ) = self._read_candidate_cache(spec)
                if cached is not None:
                    results[index] = cached
                    metrics["candidate_cache_hits"] += 1
                    embedding_status = (
                        str(cached[0].get("embedding_status") or "not_called")
                        if cached
                        else (
                            "disabled"
                            if self.embedding_provider is None
                            else "ok"
                        )
                    )
                    diagnostics[index] = self._result_health(
                        cached,
                        embedding_status,
                        source="candidate_cache_hit",
                    )
                    continue
                if not self.singleflight_enabled:
                    future = Future()
                    leaders.append(
                        (
                            index,
                            spec,
                            cache_key,
                            index_digest,
                            future,
                        )
                    )
                    continue
                with self._flight_lock:
                    future = self._search_flights.get(cache_key)
                    if future is None:
                        future = Future()
                        self._search_flights[cache_key] = future
                        leaders.append(
                            (
                                index,
                                spec,
                                cache_key,
                                index_digest,
                                future,
                            )
                        )
                    else:
                        followers.append((index, future))
                        metrics["search_singleflight_waits"] += 1
        except BaseException as exc:
            for _, _spec, cache_key, _digest, future in leaders:
                self._complete_search_flight(
                    cache_key,
                    future,
                    error=exc,
                )
            raise

        try:
            embedding_results = self._query_embeddings(
                [spec.normalized_query for _, spec, *_ in leaders],
                metrics,
            )
        except BaseException as exc:
            for _, _spec, cache_key, _digest, future in leaders:
                self._complete_search_flight(
                    cache_key,
                    future,
                    error=exc,
                )
            raise
        ranked_by_index: dict[int, list[dict[str, Any]]] = {}
        leader_metadata: dict[
            int,
            tuple[
                _SearchSpec,
                str,
                str,
                Future[list[dict[str, Any]]],
                str,
            ],
        ] = {}
        unexpected_errors: list[BaseException] = []
        for leader, embedding_result in zip(leaders, embedding_results):
            (
                index,
                spec,
                cache_key,
                index_digest,
                future,
            ) = leader
            vector, embedding_status = embedding_result
            leader_metadata[index] = (
                spec,
                cache_key,
                index_digest,
                future,
                embedding_status,
            )
            try:
                ranked_by_index[index] = self._search_legacy(
                    spec.normalized_query,
                    limit=spec.limit,
                    roles=spec.roles,
                    scope_policies=spec.scope_policies,
                    ingest_policies=spec.ingest_policies,
                    use_candidate_cache=False,
                    _query_vector_override=(vector, embedding_status),
                    _defer_rerank=True,
                    _return_full_ranked=True,
                )
            except BaseException as exc:
                unexpected_errors.append(exc)
                self._complete_search_flight(
                    cache_key,
                    future,
                    error=exc,
                )

        rerank_workers = metrics["rerank_max_concurrency"]
        rerank_futures: dict[
            Future[tuple[list[dict[str, Any]], float, dict[str, int]]],
            int,
        ] = {}
        rerank_wall_started = time.perf_counter()

        def timed_rerank(
            spec: _SearchSpec,
            ranked: list[dict[str, Any]],
        ) -> tuple[list[dict[str, Any]], float, dict[str, int]]:
            started = time.perf_counter()
            rerank_stats: dict[str, int] = {}
            completed = self._apply_rerank(
                spec,
                ranked,
                _rerank_stats=rerank_stats,
            )
            return (
                completed,
                round((time.perf_counter() - started) * 1000.0, 3),
                rerank_stats,
            )

        try:
            with ThreadPoolExecutor(max_workers=rerank_workers) as executor:
                for index, ranked in ranked_by_index.items():
                    spec = leader_metadata[index][0]
                    rerank_futures[
                        executor.submit(
                            timed_rerank,
                            spec,
                            ranked,
                        )
                    ] = index
                for future in as_completed(rerank_futures):
                    index = rerank_futures[future]
                    (
                        spec,
                        cache_key,
                        index_digest,
                        search_future,
                        embedding_status,
                    ) = leader_metadata[index]
                    try:
                        (
                            completed,
                            rerank_elapsed,
                            rerank_stats,
                        ) = future.result()
                        metrics["rerank_sum_ms"] += rerank_elapsed
                        metrics["rerank_cache_hits"] += int(
                            rerank_stats.get("cache_hits", 0)
                        )
                        metrics["rerank_singleflight_waits"] += int(
                            rerank_stats.get("singleflight_waits", 0)
                        )
                        metrics["rerank_cache_misses"] += int(
                            rerank_stats.get("cache_misses", 0)
                        )
                        selected = completed[: spec.limit]
                        cacheable = (
                            embedding_status != "failed"
                            and all(
                                item.get("rerank_status")
                                not in {"failed", "partial"}
                                for item in completed
                            )
                        )
                        if cacheable:
                            self._write_candidate_cache(
                                spec=spec,
                                cache_key=cache_key,
                                expected_index_digest=index_digest,
                                results=selected,
                            )
                        results[index] = selected
                        diagnostics[index] = self._result_health(
                            selected,
                            embedding_status,
                            source="ok",
                        )
                        self._complete_search_flight(
                            cache_key,
                            search_future,
                            result=selected,
                        )
                    except BaseException as exc:
                        unexpected_errors.append(exc)
                        self._complete_search_flight(
                            cache_key,
                            search_future,
                            error=exc,
                        )
        except BaseException as exc:
            for _, _spec, cache_key, _digest, future in leaders:
                self._complete_search_flight(
                    cache_key,
                    future,
                    error=exc,
                )
            raise
        metrics["rerank_wall_ms"] = round(
            (time.perf_counter() - rerank_wall_started) * 1000.0,
            3,
        )

        for index, future in followers:
            try:
                value = deepcopy(future.result())
                results[index] = value
                embedding_status = (
                    str(value[0].get("embedding_status") or "not_called")
                    if value
                    else (
                        "disabled"
                        if self.embedding_provider is None
                        else "ok"
                    )
                )
                diagnostics[index] = self._result_health(
                    value,
                    embedding_status,
                    source="singleflight",
                )
            except BaseException as exc:
                unexpected_errors.append(exc)

        metrics["queries"] = [
            value
            if value is not None
            else {
                "status": "error",
                "embedding_status": "unknown",
                "rerank_statuses": [],
                "result_count": 0,
                "miss_confirmed": False,
            }
            for value in diagnostics
        ]
        metrics["cache_hit_count"] = (
            int(metrics["candidate_cache_hits"])
            + int(metrics["embedding_cache_hits"])
            + int(metrics["search_singleflight_waits"])
            + int(metrics["embedding_singleflight_waits"])
        )
        metrics["cache_miss_count"] = max(
            0,
            len(specs) - int(metrics["candidate_cache_hits"]),
        )
        metrics["authority_search_ms"] = round(
            (time.perf_counter() - search_started) * 1000.0,
            3,
        )
        self._diagnostics_local.last = deepcopy(metrics)
        if unexpected_errors:
            raise unexpected_errors[0]
        return [
            value if value is not None else []
            for value in results
        ]

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        roles: Iterable[str] | None = None,
        scope_policies: Iterable[str] | None = None,
        ingest_policies: Iterable[str] = ("include", "review"),
        use_candidate_cache: bool = True,
    ) -> list[dict[str, Any]]:
        """Compatibility wrapper for one request through ``search_many``."""

        return self.search_many(
            [query],
            limit=limit,
            roles=roles,
            scope_policies=scope_policies,
            ingest_policies=ingest_policies,
            use_candidate_cache=use_candidate_cache,
        )[0]

    def last_search_diagnostics(self) -> dict[str, Any]:
        """Return per-thread diagnostics for the most recent search call."""

        return deepcopy(
            getattr(self._diagnostics_local, "last", {})
        )

    def prune_derived_cache(self, *, keep_candidate_queries: int = 5000) -> dict[str, int]:
        """Bound only rebuildable vectors and candidate-cache rows.

        Authority files and chunks are never removed by retention policy; they
        change only during a source refresh.  Orphan vectors and old query
        candidates are derived data and can be regenerated deterministically.
        """

        keep = max(0, int(keep_candidate_queries))
        with self._connect() as connection:
            orphan_vectors = connection.execute(
                """
                DELETE FROM authority_vectors
                WHERE content_sha256 NOT IN (
                    SELECT DISTINCT content_sha256 FROM authority_chunks
                )
                """
            ).rowcount
            cache_rows = connection.execute(
                """
                SELECT cache_key FROM rerank_candidate_cache
                ORDER BY created_at DESC, cache_key DESC
                """
            ).fetchall()
            stale_keys = [row["cache_key"] for row in cache_rows[keep:]]
            if stale_keys:
                connection.executemany(
                    "DELETE FROM rerank_candidate_cache WHERE cache_key = ?",
                    ((key,) for key in stale_keys),
                )
        return {
            "orphan_vectors_removed": max(0, int(orphan_vectors)),
            "candidate_queries_removed": len(stale_keys),
        }
