from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

try:  # ``scripts.longform`` package import.
    from ..sqlite_guard import (
        execute_sqlite_script_in_transaction,
        validate_sqlite_component_schema,
    )
except ImportError:  # Top-level ``longform`` with ``scripts`` on sys.path.
    from sqlite_guard import (
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


MEMORY_SCHEMA_VERSION = 1
SUMMARY_SCHEMA_VERSION = 1
CRAFT_MEMORY_SCHEMA_VERSION = 1
MEMORY_TABLES = frozenset({"longform_memory_meta", "memory_entries"})
SUMMARY_TABLES = frozenset(
    {"longform_summary_meta", "accepted_summaries"}
)
CRAFT_MEMORY_TABLES = frozenset({"craft_memory_meta", "craft_patterns"})
LONGFORM_SHARED_TABLES = frozenset(
    {*MEMORY_TABLES, *SUMMARY_TABLES, *CRAFT_MEMORY_TABLES}
)
MEMORY_LAYERS = ("working", "episodic", "semantic")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]")
_SENTENCE_RE = re.compile(r"(?<=[。！？!?])\s+|\n+")


def validate_longform_shared_schema(
    connection: sqlite3.Connection,
) -> set[str]:
    """Validate every component already present in the shared long-form DB."""

    tables: set[str] = set()
    component_contracts = (
        (
            "long-form layered memory",
            "longform_memory_meta",
            MEMORY_SCHEMA_VERSION,
            MEMORY_TABLES,
        ),
        (
            "long-form accepted summaries",
            "longform_summary_meta",
            SUMMARY_SCHEMA_VERSION,
            SUMMARY_TABLES,
        ),
        (
            "long-form craft memory",
            "craft_memory_meta",
            CRAFT_MEMORY_SCHEMA_VERSION,
            CRAFT_MEMORY_TABLES,
        ),
    )
    for component, meta_table, version, owned_tables in component_contracts:
        tables = validate_sqlite_component_schema(
            connection,
            component=component,
            meta_table=meta_table,
            version_key="schema_version",
            supported_version=version,
            owned_tables=owned_tables,
            allowed_tables=LONGFORM_SHARED_TABLES,
        )
    return tables


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: str | bytes) -> str:
    payload = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _tokens(value: str) -> set[str]:
    return {match.group(0).casefold() for match in _WORD_RE.finditer(value)}


def _lexical_score(query: str, text: str) -> float:
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0.0
    overlap = len(query_tokens & _tokens(text))
    return overlap / len(query_tokens)


def _compact_text(text: str, max_chars: int = 520) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    sentences = [
        " ".join(sentence.split())
        for sentence in _SENTENCE_RE.split(text)
        if sentence.strip()
    ]
    if not sentences:
        return normalized[: max_chars - 1] + "…"
    selected: list[str] = []
    used = 0
    for sentence in (sentences[:2] + sentences[-1:]):
        if sentence in selected:
            continue
        projected = used + len(sentence) + (1 if selected else 0)
        if projected > max_chars:
            break
        selected.append(sentence)
        used = projected
    result = " ".join(selected) or normalized[: max_chars - 1]
    return result if len(result) <= max_chars else result[: max_chars - 1] + "…"


class LayeredMemoryStore:
    """Accepted-only working, episodic, and semantic continuity memory."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                validate_longform_shared_schema(connection)
                execute_sqlite_script_in_transaction(
                    connection,
                    """
                    CREATE TABLE IF NOT EXISTS longform_memory_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS memory_entries (
                        memory_id TEXT PRIMARY KEY,
                        layer TEXT NOT NULL,
                        category TEXT NOT NULL,
                        content TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        source_commit_id TEXT NOT NULL,
                        branch_id TEXT NOT NULL,
                        chapter_no INTEGER,
                        arc_id TEXT,
                        volume_id TEXT,
                        scope TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(
                            layer, category, content_sha256, source_commit_id
                        )
                    );
                    CREATE INDEX IF NOT EXISTS memory_entries_lookup
                        ON memory_entries(layer, category, chapter_no);
                    """,
                )
                connection.execute(
                    """
                    INSERT INTO longform_memory_meta(key, value)
                    VALUES ('schema_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(MEMORY_SCHEMA_VERSION),),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            factory=_ClosingConnection,
        )
        try:
            connection.row_factory = sqlite3.Row
            return connection
        except BaseException:
            with suppress(sqlite3.Error):
                connection.close()
            raise

    def add(
        self,
        *,
        layer: str,
        category: str,
        content: str,
        source_commit_id: str,
        canon_status: str,
        branch_id: str = "main",
        chapter_no: int | None = None,
        arc_id: str | None = None,
        volume_id: str | None = None,
        scope: str = "current",
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        if canon_status != "accepted":
            return False
        if layer not in MEMORY_LAYERS:
            raise ValueError(f"unsupported memory layer: {layer}")
        normalized = " ".join(str(content).split())
        if not normalized:
            return False
        content_sha256 = _sha256(normalized)
        memory_id = _sha256(
            "\0".join((layer, category, content_sha256, source_commit_id))
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO memory_entries(
                    memory_id, layer, category, content, content_sha256,
                    source_commit_id, branch_id, chapter_no, arc_id, volume_id,
                    scope, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    layer,
                    category,
                    normalized,
                    content_sha256,
                    source_commit_id,
                    branch_id,
                    chapter_no,
                    arc_id,
                    volume_id,
                    scope,
                    _stable_json(dict(metadata or {})),
                    _utc_now(),
                ),
            )
            return cursor.rowcount > 0

    @staticmethod
    def _iter_strings(value: Any) -> Iterable[str]:
        if isinstance(value, str):
            if value.strip():
                yield value
            return
        if isinstance(value, Mapping):
            text = value.get("text") or value.get("content") or value.get("summary")
            if text:
                yield str(text)
            elif value:
                yield _stable_json(dict(value))
            return
        if isinstance(value, Iterable):
            for item in value:
                yield from LayeredMemoryStore._iter_strings(item)

    @staticmethod
    def _iter_memory_records(
        value: Any,
        *,
        default_scope: str,
    ) -> Iterable[dict[str, Any]]:
        if isinstance(value, str):
            if value.strip():
                yield {
                    "content": value,
                    "scope": default_scope,
                    "metadata": {},
                }
            return
        if isinstance(value, Mapping):
            content = (
                value.get("content")
                or value.get("text")
                or value.get("summary")
            )
            if not content and value:
                content = _stable_json(dict(value))
            if not content:
                return
            metadata = dict(value.get("metadata") or {})
            for key in (
                "knowledge_plane",
                "scope",
                "source_event_id",
                "fact_type",
                "semantic_key",
                "entity_id",
                "subject_entity_id",
                "target_entity_id",
                "field",
            ):
                if value.get(key) is not None:
                    metadata.setdefault(key, value.get(key))
            yield {
                "content": str(content),
                "scope": str(value.get("scope") or default_scope),
                "metadata": metadata,
            }
            return
        if isinstance(value, Iterable):
            for item in value:
                yield from LayeredMemoryStore._iter_memory_records(
                    item,
                    default_scope=default_scope,
                )

    @staticmethod
    def _power_memory_category(
        default_category: str,
        metadata: Mapping[str, Any],
    ) -> str:
        fact_type = str(metadata.get("fact_type") or "")
        if fact_type.startswith("ability"):
            return "ability"
        return {
            "progression": "progression",
            "resource": "resource",
            "power_binding": "power_binding",
            "power_spec": "power_definition",
        }.get(fact_type, default_category)

    @staticmethod
    def _power_memory_identity(
        record: Mapping[str, Any],
        *,
        source_field: str,
    ) -> tuple[str, ...]:
        metadata = dict(record.get("metadata") or {})
        source_event_id = str(metadata.get("source_event_id") or "")
        semantic_key = str(metadata.get("semantic_key") or "")
        if source_event_id or semantic_key:
            return ("structured", source_event_id, semantic_key)
        return (
            "legacy",
            source_field,
            _sha256(str(record.get("content") or "")),
        )

    def project_accepted_commit(
        self,
        commit: Mapping[str, Any],
        *,
        layers: Iterable[str] | None = None,
    ) -> dict[str, int]:
        """Derive the three memory layers without learning from provisional work."""

        if commit.get("canon_status") != "accepted":
            return {"working": 0, "episodic": 0, "semantic": 0}
        selected_layers = set(layers or MEMORY_LAYERS)
        unsupported = selected_layers - set(MEMORY_LAYERS)
        if unsupported:
            raise ValueError(
                "unsupported memory layer(s): " + ", ".join(sorted(unsupported))
            )
        commit_id = str(commit.get("commit_id") or "")
        if not commit_id:
            raise ValueError("accepted commit requires commit_id")
        common = {
            "source_commit_id": commit_id,
            "canon_status": "accepted",
            "branch_id": str(commit.get("branch_id") or "main"),
            "chapter_no": (
                int(commit["chapter_no"]) if commit.get("chapter_no") is not None else None
            ),
            "arc_id": (
                str(commit["arc_id"]) if commit.get("arc_id") is not None else None
            ),
            "volume_id": (
                str(commit["volume_id"]) if commit.get("volume_id") is not None else None
            ),
        }
        counts = {"working": 0, "episodic": 0, "semantic": 0}
        if "working" in selected_layers:
            for category, field in (
                ("current_state", "current_state"),
                ("open_loop", "open_loops"),
            ):
                for text in self._iter_strings(commit.get(field, [])):
                    counts["working"] += int(
                        self.add(
                            layer="working",
                            category=category,
                            content=text,
                            scope="current",
                            metadata={"projection": "accepted_commit"},
                            **common,
                        )
                    )
            seen_power_facts: set[tuple[str, ...]] = set()
            for default_category, field in (
                ("power_state", "power_state"),
                ("progression", "power_progression"),
                ("ability", "power_abilities"),
                ("resource", "power_resources"),
                ("power_binding", "power_bindings"),
                ("power_state", "power_debts"),
            ):
                for record in self._iter_memory_records(
                    commit.get(field, []),
                    default_scope="current",
                ):
                    identity = self._power_memory_identity(
                        record,
                        source_field=field,
                    )
                    if identity in seen_power_facts:
                        continue
                    seen_power_facts.add(identity)
                    metadata = {
                        "projection": "accepted_power_fact",
                        **dict(record.get("metadata") or {}),
                    }
                    category = self._power_memory_category(
                        default_category,
                        metadata,
                    )
                    counts["working"] += int(
                        self.add(
                            layer="working",
                            category=category,
                            content=str(record["content"]),
                            scope=str(record.get("scope") or "current"),
                            metadata=metadata,
                            **common,
                        )
                    )
        summary = str(commit.get("summary") or "").strip()
        if not summary:
            summary = _compact_text(str(commit.get("text") or ""))
        if "episodic" in selected_layers:
            if summary:
                counts["episodic"] += int(
                    self.add(
                        layer="episodic",
                        category="chapter_summary",
                        content=summary,
                        scope="historical",
                        metadata={"projection": "accepted_commit"},
                        **common,
                    )
                )
            for text in self._iter_strings(commit.get("events", [])):
                counts["episodic"] += int(
                    self.add(
                        layer="episodic",
                        category="event",
                        content=text,
                        scope="historical",
                        metadata={"projection": "accepted_commit"},
                        **common,
                    )
                )
        if "semantic" in selected_layers:
            for text in self._iter_strings(commit.get("semantic_facts", [])):
                counts["semantic"] += int(
                    self.add(
                        layer="semantic",
                        category="world_rule",
                        content=text,
                        scope="timeless",
                        metadata={"projection": "accepted_commit"},
                        **common,
                    )
                )
            seen_power_definitions: set[tuple[str, ...]] = set()
            for record in self._iter_memory_records(
                commit.get("power_definitions", []),
                default_scope="timeless",
            ):
                identity = self._power_memory_identity(
                    record,
                    source_field="power_definitions",
                )
                if identity in seen_power_definitions:
                    continue
                seen_power_definitions.add(identity)
                metadata = {
                    "projection": "accepted_power_spec",
                    **dict(record.get("metadata") or {}),
                }
                counts["semantic"] += int(
                    self.add(
                        layer="semantic",
                        category="power_definition",
                        content=str(record["content"]),
                        scope=str(record.get("scope") or "timeless"),
                        metadata=metadata,
                        **common,
                    )
                )
        return counts

    def clear(self, *, layers: Iterable[str] | None = None) -> int:
        selected_layers = sorted(set(layers or ()))
        unsupported = set(selected_layers) - set(MEMORY_LAYERS)
        if unsupported:
            raise ValueError(
                "unsupported memory layer(s): " + ", ".join(sorted(unsupported))
            )
        with self._connect() as connection:
            if not selected_layers:
                cursor = connection.execute("DELETE FROM memory_entries")
            else:
                placeholders = ",".join("?" for _ in selected_layers)
                cursor = connection.execute(
                    f"DELETE FROM memory_entries WHERE layer IN ({placeholders})",
                    selected_layers,
                )
            return int(cursor.rowcount)

    def prune_to_source_commits(
        self,
        source_commit_ids: Iterable[str],
    ) -> int:
        active = sorted(
            {
                str(commit_id)
                for commit_id in source_commit_ids
                if str(commit_id).strip()
            }
        )
        with self._connect() as connection:
            if not active:
                cursor = connection.execute("DELETE FROM memory_entries")
            else:
                placeholders = ",".join("?" for _ in active)
                cursor = connection.execute(
                    f"""
                    DELETE FROM memory_entries
                    WHERE source_commit_id NOT IN ({placeholders})
                    """,
                    active,
                )
            return int(cursor.rowcount)

    def query(
        self,
        query: str,
        *,
        layers: Iterable[str] | None = None,
        categories: Iterable[str] | None = None,
        branch_id: str | None = "main",
        chapter_no: int | None = None,
        arc_id: str | None = None,
        volume_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        layer_values = sorted(set(layers or MEMORY_LAYERS))
        category_values = sorted(set(categories or ()))
        clauses: list[str] = []
        parameters: list[Any] = []
        if layer_values:
            clauses.append(
                "layer IN (" + ",".join("?" for _ in layer_values) + ")"
            )
            parameters.extend(layer_values)
        if category_values:
            clauses.append(
                "category IN (" + ",".join("?" for _ in category_values) + ")"
            )
            parameters.extend(category_values)
        if branch_id is not None:
            clauses.append("branch_id = ?")
            parameters.append(branch_id)
        if chapter_no is not None:
            clauses.append("(chapter_no IS NULL OR chapter_no <= ?)")
            parameters.append(int(chapter_no))
        where = " AND ".join(clauses) or "1 = 1"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM memory_entries
                WHERE {where}
                ORDER BY COALESCE(chapter_no, -1) DESC, created_at DESC, memory_id
                """,
                parameters,
            ).fetchall()
        ranked: list[dict[str, Any]] = []
        for row in rows:
            lexical = _lexical_score(query, row["content"])
            context_score = 0.0
            if chapter_no is not None and row["chapter_no"] is not None:
                distance = max(0, int(chapter_no) - int(row["chapter_no"]))
                context_score += 0.24 / (1.0 + distance)
            if arc_id is not None and str(row["arc_id"] or "") == str(arc_id):
                context_score += 0.2
            if volume_id is not None and str(row["volume_id"] or "") == str(volume_id):
                context_score += 0.14
            mandatory_bonus = (
                0.5
                if row["category"]
                in {
                    "current_state",
                    "open_loop",
                    "power_state",
                    "progression",
                    "ability",
                    "resource",
                    "power_binding",
                }
                else 0.0
            )
            layer_bonus = {
                "working": 0.3,
                "episodic": 0.15,
                "semantic": 0.1,
            }[row["layer"]]
            ranked.append(
                {
                    "memory_id": row["memory_id"],
                    "layer": row["layer"],
                    "category": row["category"],
                    "content": row["content"],
                    "source_commit_id": row["source_commit_id"],
                    "branch_id": row["branch_id"],
                    "chapter_no": row["chapter_no"],
                    "arc_id": row["arc_id"],
                    "volume_id": row["volume_id"],
                    "scope": row["scope"],
                    "metadata": json.loads(row["metadata_json"]),
                    "lexical_score": round(lexical, 8),
                    "context_score": round(context_score, 8),
                    "score": round(
                        lexical + context_score + mandatory_bonus + layer_bonus,
                        8,
                    ),
                }
            )
        ranked.sort(
            key=lambda item: (
                -float(item["score"]),
                -(int(item["chapter_no"]) if item["chapter_no"] is not None else -1),
                item["memory_id"],
            )
        )
        return ranked[: max(0, int(limit))]

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT layer, COUNT(*) AS count
                FROM memory_entries GROUP BY layer
                """
            ).fetchall()
        result = {layer: 0 for layer in MEMORY_LAYERS}
        result.update({row["layer"]: int(row["count"]) for row in rows})
        return result


class AcceptedSummaryStore:
    """Deterministic chapter, event-arc, and volume summaries."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                validate_longform_shared_schema(connection)
                execute_sqlite_script_in_transaction(
                    connection,
                    """
                    CREATE TABLE IF NOT EXISTS longform_summary_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS accepted_summaries (
                        summary_id TEXT PRIMARY KEY,
                        level TEXT NOT NULL,
                        subject_id TEXT NOT NULL,
                        text TEXT NOT NULL,
                        input_sha256 TEXT NOT NULL,
                        output_sha256 TEXT NOT NULL,
                        source_commits_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(level, subject_id)
                    );
                    """,
                )
                connection.execute(
                    """
                    INSERT INTO longform_summary_meta(key, value)
                    VALUES ('schema_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(SUMMARY_SCHEMA_VERSION),),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            factory=_ClosingConnection,
        )
        try:
            connection.row_factory = sqlite3.Row
            return connection
        except BaseException:
            with suppress(sqlite3.Error):
                connection.close()
            raise

    def clear(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM accepted_summaries")
            return int(cursor.rowcount)

    def prune_to_source_commits(
        self,
        source_commit_ids: Iterable[str],
    ) -> dict[str, int]:
        active = {
            str(commit_id)
            for commit_id in source_commit_ids
            if str(commit_id).strip()
        }
        with self._connect() as connection:
            chapter_rows = connection.execute(
                """
                SELECT summary_id, source_commits_json
                FROM accepted_summaries
                WHERE level='chapter'
                """
            ).fetchall()
            stale_ids: list[str] = []
            for row in chapter_rows:
                sources = {
                    str(commit_id)
                    for commit_id in json.loads(row["source_commits_json"])
                }
                if not sources or not sources.issubset(active):
                    stale_ids.append(str(row["summary_id"]))
            if stale_ids:
                connection.executemany(
                    "DELETE FROM accepted_summaries WHERE summary_id=?",
                    ((summary_id,) for summary_id in stale_ids),
                )
                connection.execute(
                    "DELETE FROM accepted_summaries WHERE level IN ('arc', 'volume')"
                )
            remaining_subjects = [
                str(row["subject_id"])
                for row in connection.execute(
                    """
                    SELECT subject_id
                    FROM accepted_summaries
                    WHERE level='chapter'
                    ORDER BY subject_id
                    """
                ).fetchall()
            ]
        rebuilt_arcs = 0
        rebuilt_volumes = 0
        if stale_ids:
            arc_subjects = sorted(
                {
                    "/".join(subject.split("/")[:3])
                    for subject in remaining_subjects
                    if len(subject.split("/")) >= 4
                }
            )
            volume_subjects = sorted(
                {
                    "/".join(subject.split("/")[:2])
                    for subject in remaining_subjects
                    if len(subject.split("/")) >= 4
                }
            )
            for subject in arc_subjects:
                rebuilt_arcs += int(
                    self._aggregate(level="arc", subject_id=subject)
                    is not None
                )
            for subject in volume_subjects:
                rebuilt_volumes += int(
                    self._aggregate(level="volume", subject_id=subject)
                    is not None
                )
        return {
            "chapters_removed": len(stale_ids),
            "arcs_rebuilt": rebuilt_arcs,
            "volumes_rebuilt": rebuilt_volumes,
        }

    def _upsert(
        self,
        *,
        level: str,
        subject_id: str,
        text: str,
        input_payload: Any,
        source_commits: list[str],
    ) -> dict[str, Any]:
        compacted = _compact_text(text, 900 if level == "chapter" else 1400)
        input_sha256 = _sha256(_stable_json(input_payload))
        output_sha256 = _sha256(compacted)
        summary_id = _sha256(f"{level}\0{subject_id}")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO accepted_summaries(
                    summary_id, level, subject_id, text, input_sha256,
                    output_sha256, source_commits_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(level, subject_id) DO UPDATE SET
                    text = excluded.text,
                    input_sha256 = excluded.input_sha256,
                    output_sha256 = excluded.output_sha256,
                    source_commits_json = excluded.source_commits_json,
                    updated_at = excluded.updated_at
                """,
                (
                    summary_id,
                    level,
                    subject_id,
                    compacted,
                    input_sha256,
                    output_sha256,
                    _stable_json(source_commits),
                    _utc_now(),
                ),
            )
        return {
            "summary_id": summary_id,
            "level": level,
            "subject_id": subject_id,
            "text": compacted,
            "input_sha256": input_sha256,
            "output_sha256": output_sha256,
            "source_commits": source_commits,
        }

    def _aggregate(self, *, level: str, subject_id: str) -> dict[str, Any] | None:
        # Summary DB does not keep canonical commits.  Aggregation is rebuilt
        # from accepted chapter rows whose subject id encodes the grouping in
        # the stable form: volume/arc/chapter.
        prefix = subject_id + "/"
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM accepted_summaries
                WHERE level = 'chapter' AND subject_id LIKE ?
                ORDER BY subject_id
                """,
                (prefix + "%",),
            ).fetchall()
        if not rows:
            return None
        texts = [row["text"] for row in rows]
        commits: list[str] = []
        for row in rows:
            commits.extend(json.loads(row["source_commits_json"]))
        return self._upsert(
            level=level,
            subject_id=subject_id,
            text=" ".join(texts),
            input_payload=[row["output_sha256"] for row in rows],
            source_commits=list(dict.fromkeys(commits)),
        )

    def project_commit(self, commit: Mapping[str, Any]) -> dict[str, Any]:
        if commit.get("canon_status") != "accepted":
            return {"projected": False, "reason": "commit is not accepted"}
        commit_id = str(commit.get("commit_id") or "")
        if not commit_id:
            raise ValueError("accepted commit requires commit_id")
        chapter_no = int(commit.get("chapter_no") or 0)
        branch_id = str(commit.get("branch_id") or "main")
        volume_id = str(commit.get("volume_id") or "volume-unknown")
        arc_id = str(commit.get("arc_id") or "arc-unknown")
        chapter_key = f"{branch_id}/{volume_id}/{arc_id}/{chapter_no:06d}"
        base_text = str(commit.get("summary") or commit.get("text") or "")
        event_text = " ".join(
            LayeredMemoryStore._iter_strings(commit.get("events", []))
        )
        chapter = self._upsert(
            level="chapter",
            subject_id=chapter_key,
            text=" ".join(part for part in (base_text, event_text) if part),
            input_payload={
                "commit_id": commit_id,
                "text": base_text,
                "events": list(
                    LayeredMemoryStore._iter_strings(commit.get("events", []))
                ),
            },
            source_commits=[commit_id],
        )
        arc = self._aggregate(
            level="arc",
            subject_id=f"{branch_id}/{volume_id}/{arc_id}",
        )
        volume = self._aggregate(
            level="volume",
            subject_id=f"{branch_id}/{volume_id}",
        )
        return {
            "projected": True,
            "chapter": chapter,
            "arc": arc,
            "volume": volume,
        }

    @staticmethod
    def _subject_coordinates(level: str, subject_id: str) -> dict[str, Any]:
        parts = str(subject_id).split("/")
        coordinates: dict[str, Any] = {
            "branch_id": parts[0] if parts else None,
            "volume_id": parts[1] if len(parts) >= 2 else None,
            "arc_id": None,
            "chapter_no": None,
        }
        if level in {"arc", "chapter"} and len(parts) >= 3:
            coordinates["arc_id"] = parts[2]
        if level == "chapter" and len(parts) >= 4:
            try:
                coordinates["chapter_no"] = int(parts[3])
            except ValueError:
                coordinates["chapter_no"] = None
        return coordinates

    def query(
        self,
        query: str,
        *,
        levels: Iterable[str] | None = None,
        branch_id: str | None = "main",
        chapter_no: int | None = None,
        arc_id: str | None = None,
        volume_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        level_values = sorted(set(str(value) for value in (levels or ())))
        clauses: list[str] = []
        parameters: list[Any] = []
        if level_values:
            clauses.append(
                "level IN (" + ",".join("?" for _ in level_values) + ")"
            )
            parameters.extend(level_values)
        where = " AND ".join(clauses) or "1 = 1"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM accepted_summaries
                WHERE {where}
                ORDER BY level, subject_id
                """,
                parameters,
            ).fetchall()

        ranked: list[dict[str, Any]] = []
        for row in rows:
            level = str(row["level"])
            coordinates = self._subject_coordinates(level, str(row["subject_id"]))
            if branch_id is not None and coordinates["branch_id"] != str(branch_id):
                continue
            if (
                chapter_no is not None
                and coordinates["chapter_no"] is not None
                and int(coordinates["chapter_no"]) > int(chapter_no)
            ):
                continue
            lexical = _lexical_score(query, str(row["text"]))
            context_score = 0.0
            if volume_id is not None and coordinates["volume_id"] == str(volume_id):
                context_score += 0.22
            if arc_id is not None and coordinates["arc_id"] == str(arc_id):
                context_score += 0.3
            if chapter_no is not None and coordinates["chapter_no"] is not None:
                distance = max(0, int(chapter_no) - int(coordinates["chapter_no"]))
                context_score += 0.34 / (1.0 + distance)
            if lexical <= 0.0 and context_score <= 0.0:
                continue
            level_bonus = {"chapter": 0.08, "arc": 0.06, "volume": 0.04}.get(
                level,
                0.0,
            )
            ranked.append(
                {
                    "summary_id": row["summary_id"],
                    "level": level,
                    "subject_id": row["subject_id"],
                    "text": row["text"],
                    "content": row["text"],
                    "input_sha256": row["input_sha256"],
                    "output_sha256": row["output_sha256"],
                    "source_commits": json.loads(row["source_commits_json"]),
                    "scope": "accepted",
                    **coordinates,
                    "lexical_score": round(lexical, 8),
                    "context_score": round(context_score, 8),
                    "score": round(lexical + context_score + level_bonus, 8),
                }
            )
        ranked.sort(
            key=lambda item: (
                -float(item["score"]),
                {"volume": 0, "arc": 1, "chapter": 2}.get(
                    str(item["level"]),
                    3,
                ),
                str(item["subject_id"]),
            )
        )
        return ranked[: max(0, int(limit))]

    def list(self, level: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if level:
                rows = connection.execute(
                    """
                    SELECT * FROM accepted_summaries
                    WHERE level = ? ORDER BY subject_id
                    """,
                    (level,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM accepted_summaries ORDER BY level, subject_id"
                ).fetchall()
        return [
            {
                "summary_id": row["summary_id"],
                "level": row["level"],
                "subject_id": row["subject_id"],
                "text": row["text"],
                "input_sha256": row["input_sha256"],
                "output_sha256": row["output_sha256"],
                "source_commits": json.loads(row["source_commits_json"]),
            }
            for row in rows
        ]
