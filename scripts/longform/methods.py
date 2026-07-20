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
    from ..sqlite_guard import execute_sqlite_script_in_transaction
except ImportError:  # Top-level ``longform`` with ``scripts`` on sys.path.
    from sqlite_guard import execute_sqlite_script_in_transaction

from .memory import (
    CRAFT_MEMORY_SCHEMA_VERSION,
    validate_longform_shared_schema,
)


class _ClosingConnection(sqlite3.Connection):
    """Close SQLite handles when leaving a ``with`` block on Windows."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


METHOD_PACK_SCHEMA_VERSION = 1
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]")


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
    return {match.group(0).casefold() for match in _TOKEN_RE.finditer(value)}


def _matches_filter(values: Iterable[str], requested: str | None) -> bool:
    normalized = {str(value).casefold() for value in values}
    if not requested:
        return True
    return "*" in normalized or "all" in normalized or requested.casefold() in normalized


class WebnovelMethodPack:
    """Versioned, source-traced craft cards filtered by writing context."""

    def __init__(self, pack_path: str | Path | None = None) -> None:
        if pack_path is None:
            pack_path = (
                Path(__file__).resolve().parents[2]
                / "knowledge"
                / "webnovel_methods.json"
            )
        self.pack_path = Path(pack_path)
        payload = json.loads(self.pack_path.read_text(encoding="utf-8"))
        schema_version = payload.get("schema_version")
        if (
            type(schema_version) is not int
            or schema_version != METHOD_PACK_SCHEMA_VERSION
        ):
            raise ValueError("webnovel method pack schema version mismatch")
        cards = payload.get("cards")
        if not isinstance(cards, list) or not cards:
            raise ValueError("webnovel method pack must contain cards")
        self.payload = payload
        self.cards: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in cards:
            card = dict(raw)
            method_id = str(card.get("id") or "")
            if not method_id or method_id in seen:
                raise ValueError("webnovel method card ids must be unique and non-empty")
            seen.add(method_id)
            source = card.get("source")
            if not isinstance(source, Mapping):
                raise ValueError(f"method card {method_id} lacks source metadata")
            source_payload = {
                "ref": source.get("ref"),
                "kind": source.get("kind"),
                "basis": source.get("basis"),
            }
            expected_hash = _sha256(_stable_json(source_payload))
            if source.get("sha256") != expected_hash:
                raise ValueError(f"method card {method_id} source hash mismatch")
            boundaries = card.get("misuse_boundaries")
            if not isinstance(boundaries, list) or not boundaries:
                raise ValueError(f"method card {method_id} lacks misuse boundaries")
            self.cards.append(card)

    def retrieve(
        self,
        query: str,
        *,
        genre: str | None = None,
        artifact_stage: str | None = None,
        task: str | None = None,
        continuity_risks: Iterable[str] | None = None,
        limit: int = 4,
    ) -> list[dict[str, Any]]:
        query_tokens = _tokens(query)
        requested_risks = {str(value).casefold() for value in continuity_risks or ()}
        ranked: list[tuple[float, dict[str, Any]]] = []
        for card in self.cards:
            if not _matches_filter(card.get("genres", []), genre):
                continue
            if not _matches_filter(card.get("artifact_stages", []), artifact_stage):
                continue
            if not _matches_filter(card.get("tasks", []), task):
                continue
            card_risks = {
                str(value).casefold() for value in card.get("continuity_risks", [])
            }
            if requested_risks and not (
                requested_risks & card_risks
                or "all" in card_risks
                or "*" in card_risks
            ):
                continue
            searchable = " ".join(
                [
                    str(card.get("title", "")),
                    str(card.get("summary", "")),
                    " ".join(str(value) for value in card.get("query_terms", [])),
                ]
            )
            lexical = (
                len(query_tokens & _tokens(searchable)) / max(1, len(query_tokens))
            )
            task_bonus = 0.35 if task and task in card.get("tasks", []) else 0.0
            stage_bonus = (
                0.2
                if artifact_stage and artifact_stage in card.get("artifact_stages", [])
                else 0.0
            )
            risk_bonus = (
                0.25
                if requested_risks
                and requested_risks
                & {str(value).casefold() for value in card.get("continuity_risks", [])}
                else 0.0
            )
            ranked.append((lexical + task_bonus + stage_bonus + risk_bonus, card))
        ranked.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        return [
            {**card, "retrieval_score": round(score, 8)}
            for score, card in ranked[: max(0, int(limit))]
        ]

    @staticmethod
    def render_guidance(
        cards: Iterable[Mapping[str, Any]],
        *,
        expose_internal_checks: bool = False,
    ) -> str:
        """Render model guidance; public-facing mode never exposes checklists."""

        blocks: list[str] = []
        for card in cards:
            lines = [f"{card['title']}：{card['summary']}"]
            if expose_internal_checks:
                checks = card.get("internal_checks", [])
                if checks:
                    lines.append("内部校验：" + "；".join(str(item) for item in checks))
                boundaries = card.get("misuse_boundaries", [])
                if boundaries:
                    lines.append("误用边界：" + "；".join(str(item) for item in boundaries))
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)


class ProjectPatternStore:
    """Physically separate craft memory learned from accepted final work only."""

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
                    CREATE TABLE IF NOT EXISTS craft_memory_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS craft_patterns (
                        pattern_id TEXT PRIMARY KEY,
                        source_commit_id TEXT NOT NULL,
                        source_sha256 TEXT NOT NULL,
                        genre TEXT NOT NULL,
                        task TEXT NOT NULL,
                        artifact_stage TEXT NOT NULL,
                        pattern_text TEXT NOT NULL,
                        signals_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(source_commit_id, source_sha256)
                    );
                    CREATE INDEX IF NOT EXISTS craft_patterns_lookup
                        ON craft_patterns(genre, task, artifact_stage);
                    """,
                )
                connection.execute(
                    """
                    INSERT INTO craft_memory_meta(key, value)
                    VALUES ('schema_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(CRAFT_MEMORY_SCHEMA_VERSION),),
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
            cursor = connection.execute("DELETE FROM craft_patterns")
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
                cursor = connection.execute("DELETE FROM craft_patterns")
            else:
                placeholders = ",".join("?" for _ in active)
                cursor = connection.execute(
                    f"""
                    DELETE FROM craft_patterns
                    WHERE source_commit_id NOT IN ({placeholders})
                    """,
                    active,
                )
            return int(cursor.rowcount)

    def learn(self, commit: Mapping[str, Any]) -> dict[str, Any]:
        status = str(commit.get("canon_status") or "")
        stage = str(commit.get("artifact_stage") or "")
        if status != "accepted":
            return {"learned": False, "reason": "canon_status_not_accepted"}
        if stage not in {"final", "published"}:
            return {"learned": False, "reason": "artifact_stage_not_final"}
        commit_id = str(commit.get("commit_id") or "")
        if not commit_id:
            raise ValueError("accepted final/published commit requires commit_id")
        pattern_text = str(
            commit.get("success_pattern")
            or commit.get("summary")
            or commit.get("text")
            or ""
        ).strip()
        if not pattern_text:
            return {"learned": False, "reason": "empty_pattern"}
        signals = commit.get("craft_signals") or {}
        source_payload = {
            "commit_id": commit_id,
            "artifact_stage": stage,
            "text": pattern_text,
            "signals": signals,
        }
        source_sha256 = _sha256(_stable_json(source_payload))
        pattern_id = _sha256(f"{commit_id}\0{source_sha256}")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO craft_patterns(
                    pattern_id, source_commit_id, source_sha256, genre, task,
                    artifact_stage, pattern_text, signals_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pattern_id,
                    commit_id,
                    source_sha256,
                    str(commit.get("genre") or "all"),
                    str(commit.get("task") or "prose"),
                    stage,
                    " ".join(pattern_text.split()),
                    _stable_json(signals),
                    _utc_now(),
                ),
            )
        return {
            "learned": cursor.rowcount > 0,
            "pattern_id": pattern_id,
            "source_sha256": source_sha256,
        }

    def query(
        self,
        query: str,
        *,
        genre: str | None = None,
        task: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if genre:
            clauses.append("(genre = ? OR genre = 'all')")
            parameters.append(genre)
        if task:
            clauses.append("task = ?")
            parameters.append(task)
        where = " AND ".join(clauses) or "1 = 1"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM craft_patterns
                WHERE {where}
                ORDER BY created_at DESC, pattern_id
                """,
                parameters,
            ).fetchall()
        query_tokens = _tokens(query)
        results: list[dict[str, Any]] = []
        for row in rows:
            score = len(query_tokens & _tokens(row["pattern_text"])) / max(
                1, len(query_tokens)
            )
            results.append(
                {
                    "pattern_id": row["pattern_id"],
                    "source_commit_id": row["source_commit_id"],
                    "source_sha256": row["source_sha256"],
                    "genre": row["genre"],
                    "task": row["task"],
                    "artifact_stage": row["artifact_stage"],
                    "pattern_text": row["pattern_text"],
                    "signals": json.loads(row["signals_json"]),
                    "score": round(score, 8),
                }
            )
        results.sort(key=lambda item: (-item["score"], item["pattern_id"]))
        return results[: max(0, int(limit))]

    def count(self) -> int:
        with self._connect() as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM craft_patterns").fetchone()[0]
            )
